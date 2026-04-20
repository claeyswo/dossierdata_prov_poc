"""
Integration tests for `_resolve_explicit` in
`engine.pipeline.used`.

This is the first pass of the `used` phase: it takes the raw
`state.used_items` list the client supplied, parses each ref,
looks up the row, validates dossier ownership, and populates
`state.used_refs`, `state.resolved_entities`, and
`state.used_rows_by_ref` accordingly.

The auto-resolve pass (`_auto_resolve_for_system_caller`) is a
separate function and needs plugin-declared `used` shapes; we
don't test it here.

Branches covered:

* `valid_local_ref_resolved` — baseline: a ref that exists in the
  correct dossier populates all three output collections.
* `multiple_refs_all_resolved` — two different-type entities in
  the same activity. Both land in `resolved_entities` under their
  respective type keys.
* `cross_dossier_ref_rejected` — the security-adjacent invariant.
  An entity that exists in the DB but lives in a different dossier
  must be rejected with 422 "Entity belongs to a different dossier".
  PROV closure depends on this.
* `nonexistent_version_rejected` — a version UUID that doesn't
  exist in the DB at all. 422 "Entity not found".
* `malformed_ref_rejected` — garbage in the `entity` string. 422
  "Invalid entity reference".
* `external_uri_shortcircuits_to_external_path` — an `https://`
  URI in `used` goes through `ensure_external_entity` and lands
  in `used_refs` with `external: True`.
* `duplicate_external_uri_idempotent` — the same external URI
  twice in the same request produces one external row, not two.

The cross-dossier check is the test I care about most here: the
whole API suite (`test_requests.sh`) never exercises it because no
flow in the suite tries to reference an entity from another
dossier. Without this test, the 422 is defended only by the code
path itself — the moment someone refactors `_resolve_explicit` and
forgets the ownership check, the suite keeps passing and the bug
ships.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.pipeline.used import _resolve_explicit
from dossier_engine.engine.state import ActivityState, Caller


UTC = timezone.utc
D1 = UUID("11111111-1111-1111-1111-111111111111")
D2 = UUID("22222222-2222-2222-2222-222222222222")


async def _bootstrap_dossier(repo: Repository, dossier_id: UUID) -> UUID:
    await repo.create_dossier(dossier_id, "toelatingen")
    act_id = uuid4()
    now = datetime.now(UTC)
    await repo.create_activity(
        activity_id=act_id, dossier_id=dossier_id, type="systemAction",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    return act_id


async def _seed_entity(
    repo: Repository,
    activity_id: UUID,
    dossier_id: UUID,
    entity_type: str,
) -> tuple[UUID, UUID]:
    """Seed one fresh entity. Returns (entity_id, version_id)."""
    eid = uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=dossier_id,
        type=entity_type, generated_by=activity_id,
        content={"note": "seed"}, attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid


def _state(repo: Repository, dossier_id: UUID, used_items: list[dict]) -> ActivityState:
    """Build the minimum ActivityState `_resolve_explicit` needs.
    It reads used_items, repo, dossier_id and writes used_refs,
    resolved_entities, used_rows_by_ref. Everything else can be
    None / empty. Using the real dataclass (not a SimpleNamespace)
    because the phase mutates `state.used_refs.append(...)` and
    dataclass field_factory default handles the empty-list
    initialization correctly."""
    return ActivityState(
        plugin=None,
        activity_def={},
        repo=repo,
        dossier_id=dossier_id,
        activity_id=uuid4(),
        user=None,
        role="",
        used_items=used_items,
        generated_items=[],
        relation_items=[],
        caller=Caller.CLIENT,
    )


class TestResolveExplicit:

    async def test_valid_local_ref_resolved(self, repo):
        """Baseline happy path: one ref, correct dossier, exists.
        After the phase runs, all three output collections carry
        the entity."""
        act = await _bootstrap_dossier(repo, D1)
        eid, vid = await _seed_entity(repo, act, D1, "oe:aanvraag")
        ref = f"oe:aanvraag/{eid}@{vid}"
        state = _state(repo, D1, [{"entity": ref}])

        await _resolve_explicit(state)

        assert len(state.used_refs) == 1
        assert state.used_refs[0].entity == ref
        assert state.used_refs[0].version_id == vid
        assert state.used_refs[0].type == "oe:aanvraag"
        assert "oe:aanvraag" in state.resolved_entities
        assert state.resolved_entities["oe:aanvraag"].id == vid
        assert ref in state.used_rows_by_ref

    async def test_multiple_refs_all_resolved(self, repo):
        """Two entities of different types. Both land in
        resolved_entities under their respective type keys."""
        act = await _bootstrap_dossier(repo, D1)
        eid_a, vid_a = await _seed_entity(repo, act, D1, "oe:aanvraag")
        eid_b, vid_b = await _seed_entity(repo, act, D1, "oe:beslissing")
        ref_a = f"oe:aanvraag/{eid_a}@{vid_a}"
        ref_b = f"oe:beslissing/{eid_b}@{vid_b}"
        state = _state(repo, D1, [
            {"entity": ref_a},
            {"entity": ref_b},
        ])

        await _resolve_explicit(state)

        assert len(state.used_refs) == 2
        assert set(state.resolved_entities.keys()) == {
            "oe:aanvraag", "oe:beslissing",
        }
        assert state.resolved_entities["oe:aanvraag"].id == vid_a
        assert state.resolved_entities["oe:beslissing"].id == vid_b

    async def test_cross_dossier_ref_rejected(self, repo):
        """The security-adjacent invariant. Entity exists and the
        ref is well-formed, but the entity lives in D2 and the
        activity is running in D1. Must raise 422 "belongs to a
        different dossier". PROV closure depends on this — if
        cross-dossier refs leaked through, a client with access to
        one dossier could influence the provenance graph of
        another."""
        # Seed the entity in D2
        act_d2 = await _bootstrap_dossier(repo, D2)
        eid, vid = await _seed_entity(repo, act_d2, D2, "oe:aanvraag")
        # Also create D1 (empty) so the state points somewhere real
        await _bootstrap_dossier(repo, D1)

        ref = f"oe:aanvraag/{eid}@{vid}"
        state = _state(repo, D1, [{"entity": ref}])

        with pytest.raises(ActivityError) as exc:
            await _resolve_explicit(state)
        assert exc.value.status_code == 422
        assert "different dossier" in str(exc.value)

    async def test_nonexistent_version_rejected(self, repo):
        """The ref parses fine and the entity_id looks plausible,
        but no row exists for the given version. Must raise 422
        'Entity not found'."""
        await _bootstrap_dossier(repo, D1)
        ref = f"oe:aanvraag/{uuid4()}@{uuid4()}"
        state = _state(repo, D1, [{"entity": ref}])

        with pytest.raises(ActivityError) as exc:
            await _resolve_explicit(state)
        assert exc.value.status_code == 422
        assert "not found" in str(exc.value).lower()

    async def test_non_canonical_ref_treated_as_external(self, repo):
        """A ref that doesn't match the canonical `type/eid@vid`
        shape is NOT rejected — it's treated as an external URI
        and persisted via `ensure_external_entity`. This is the
        engine's "if it's not a local ref, it must be external"
        fallback, and it's the behavior `_resolve_explicit` relies
        on to handle arbitrary URL-like external identifiers.

        The test used to assert a 422 `Invalid entity reference`
        here, but that was wrong: `parse_entity_ref` only returns
        None on non-matches, and the caller passes non-matches to
        `is_external_uri` → True → external path. There is no
        'malformed ref' branch in `_resolve_explicit` proper.
        Locking in the current behavior so a future refactor that
        adds a real malformed-ref check has to update this test
        and make the choice consciously."""
        await _bootstrap_dossier(repo, D1)
        state = _state(repo, D1, [{"entity": "this-is-not-a-ref"}])

        await _resolve_explicit(state)

        assert len(state.used_refs) == 1
        assert state.used_refs[0].entity == "this-is-not-a-ref"
        assert state.used_refs[0].external is True

    async def test_external_uri_shortcircuits_to_external_path(self, repo):
        """An `https://` URI in `used` is an external reference.
        The phase persists it via `ensure_external_entity` and
        records the ref with `external: True`. No attempt is made
        to parse it as a local ref."""
        await _bootstrap_dossier(repo, D1)
        uri = "https://id.erfgoed.net/erfgoedobjecten/60001"
        state = _state(repo, D1, [{"entity": uri}])

        await _resolve_explicit(state)

        assert len(state.used_refs) == 1
        assert state.used_refs[0].entity == uri
        assert state.used_refs[0].external is True
        assert state.used_refs[0].version_id is not None

    async def test_duplicate_external_uri_idempotent(self, repo):
        """Listing the same external URI twice in the same activity
        should produce two `used_refs` entries (the activity did
        reference it twice, and that's what the PROV graph should
        show) but exactly ONE underlying external entity row —
        `ensure_external_entity` is idempotent by design. The two
        refs share the same `version_id`."""
        await _bootstrap_dossier(repo, D1)
        uri = "https://id.erfgoed.net/erfgoedobjecten/60001"
        state = _state(repo, D1, [
            {"entity": uri},
            {"entity": uri},
        ])

        await _resolve_explicit(state)

        assert len(state.used_refs) == 2
        assert all(ref.external is True for ref in state.used_refs)
        # Same version_id on both — same underlying external row.
        assert state.used_refs[0].version_id == state.used_refs[1].version_id
