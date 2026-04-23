"""
Integration tests for `process_generated` (the public phase) in
`engine.pipeline.generated`.

The inner helpers `_validate_derivation`, `_resolve_schema_version`,
and `_validate_content` are covered elsewhere. This file covers
the top-level phase logic: iteration, external-URI routing,
content-present checks, disallowed-type rejection, and
pending-entity registration into `state.resolved_entities`.

Branches covered:

* `empty_generated_items_noop` — baseline, walks nothing.
* `external_uri_routed_to_externals` — an `https://` ref in
  `generated` goes to `state.generated_externals` and does NOT
  get content-validated.
* `missing_content_rejected` — a local ref without a `content`
  dict raises 422 "must have content".
* `disallowed_type_rejected` — activity declares
  `generates: [oe:beslissing]`, item is `oe:aanvraag` → 422.
* `allowed_types_empty_means_unrestricted` — locks in that
  missing or empty `generates` declaration is "no type filter",
  not "reject everything". This is the current behavior and it's
  the only sensible default for a catch-all activity.
* `happy_path_populates_state` — fresh entity, content passes
  validation, `state.generated` gets one normalized dict,
  `state.resolved_entities[type]` becomes a `_PendingEntity`.
* `pending_entity_carries_expected_fields` — the `_PendingEntity`
  stand-in has content, entity_id, id (version), attributed_to,
  schema_version. Handlers see it via `context.get_typed` before
  persistence.
* `multiple_items_processed_in_order` — two items, both land.

One thing we discovered while reading the code: the 422 "Invalid
entity reference for generated item" branch on line 74 is
unreachable. `is_external_uri` returns True for anything that
doesn't match the canonical pattern, so non-matching refs are
routed to the external branch on line 66 before the
`parse_entity_ref` check ever runs. There is no local-ref shape
that parses to None. This test file doesn't try to cover that
branch; instead, `test_bare_string_ref_treated_as_external`
documents the actual behavior so a future refactor that tightens
the external-routing (e.g. requiring `http://` or `https://` prefix)
remembers to also remove the dead 422 branch.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.context import _PendingEntity
from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.pipeline.generated import process_generated
from dossier_engine.engine.state import ActivityState, Caller


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _user() -> User:
    return User(
        id="u1", type="systeem", name="Test",
        roles=[], properties={},
    )


class _NoValidationPlugin:
    """Plugin stub whose `resolve_schema` always returns None, so
    `_validate_content` short-circuits and accepts any content dict.
    We use this for the top-level phase tests because we're not
    testing content validation here — that's
    `test_generated_helpers.py`'s job."""
    def resolve_schema(self, entity_type: str, schema_version: str | None):
        return None


async def _bootstrap_dossier(repo: Repository) -> UUID:
    await repo.create_dossier(D1, "toelatingen")
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="systemAction",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


def _state(
    repo: Repository,
    *,
    generated_items: list[dict],
    generates_filter: list[str] | None = None,
    entities_declaration: dict | None = None,
) -> ActivityState:
    """Minimal ActivityState for `process_generated`. Fills in the
    fields the phase reads. `generates_filter` populates
    `activity_def.generates`, `entities_declaration` populates
    `activity_def.entities` for the schema-version branch."""
    activity_def: dict = {"name": "testActivity"}
    if generates_filter is not None:
        activity_def["generates"] = generates_filter
    if entities_declaration is not None:
        activity_def["entities"] = entities_declaration

    return ActivityState(
        plugin=_NoValidationPlugin(),
        activity_def=activity_def,
        repo=repo,
        dossier_id=D1,
        activity_id=uuid4(),
        user=_user(),
        role="",
        used_items=[],
        generated_items=generated_items,
        relation_items=[],
        caller=Caller.CLIENT,
    )


class TestProcessGenerated:

    async def test_empty_generated_items_noop(self, repo):
        """No items to process. `state.generated` and
        `state.generated_externals` remain empty."""
        await _bootstrap_dossier(repo)
        state = _state(repo, generated_items=[])
        await process_generated(state)
        assert state.generated == []
        assert state.generated_externals == []

    async def test_external_uri_routed_to_externals(self, repo):
        """An https URI is routed to `generated_externals` and
        does NOT trigger the content-required check. A generated
        external is a "we referenced this URL in our provenance
        graph" signal, not a real entity with content."""
        await _bootstrap_dossier(repo)
        uri = "https://id.erfgoed.net/erfgoedobjecten/60001"
        state = _state(repo, generated_items=[
            {"entity": uri},  # note: no `content` field at all
        ])

        await process_generated(state)

        assert state.generated_externals == [uri]
        assert state.generated == []
        # No content was required, none was read.

    async def test_missing_content_on_local_ref_rejected(self, repo):
        """A canonical local ref with no `content` dict → 422.
        The content field is what distinguishes a real
        generated-entity declaration from a stub."""
        await _bootstrap_dossier(repo)
        ref = f"oe:aanvraag/{uuid4()}@{uuid4()}"
        state = _state(repo, generated_items=[
            {"entity": ref},  # no content
        ])

        with pytest.raises(ActivityError) as exc:
            await process_generated(state)
        assert exc.value.status_code == 422
        assert "must have content" in str(exc.value)

    async def test_disallowed_type_rejected(self, repo):
        """Activity declares `generates: [oe:beslissing]` but
        the item is `oe:aanvraag`. 422 — the type gate is one of
        the plugin's primary schema safeguards."""
        await _bootstrap_dossier(repo)
        ref = f"oe:aanvraag/{uuid4()}@{uuid4()}"
        state = _state(
            repo,
            generated_items=[{"entity": ref, "content": {"x": 1}}],
            generates_filter=["oe:beslissing"],
        )

        with pytest.raises(ActivityError) as exc:
            await process_generated(state)
        assert exc.value.status_code == 422
        assert "oe:aanvraag" in str(exc.value)

    async def test_empty_generates_filter_means_unrestricted(self, repo):
        """Activity declares no `generates` key at all — any type
        is allowed. This is the "catch-all activity" case.
        Important to lock in because a naive refactor might read
        'missing list' as 'empty list' and start rejecting
        everything."""
        await _bootstrap_dossier(repo)
        ref = f"oe:aanvraag/{uuid4()}@{uuid4()}"
        state = _state(
            repo,
            generated_items=[{"entity": ref, "content": {"x": 1}}],
            generates_filter=None,  # no filter
        )

        await process_generated(state)

        assert len(state.generated) == 1
        assert state.generated[0]["type"] == "oe:aanvraag"

    async def test_happy_path_populates_state(self, repo):
        """Fresh entity, content passes (no validation), item
        lands in `state.generated` as a normalized dict AND in
        `state.resolved_entities` as a `_PendingEntity`."""
        await _bootstrap_dossier(repo)
        entity_id = uuid4()
        version_id = uuid4()
        ref = f"oe:aanvraag/{entity_id}@{version_id}"
        state = _state(repo, generated_items=[
            {"entity": ref, "content": {"titel": "Test", "bedrag": 100.0}},
        ])

        await process_generated(state)

        # state.generated shape
        assert len(state.generated) == 1
        g = state.generated[0]
        assert g["version_id"] == version_id
        assert g["entity_id"] == entity_id
        assert g["type"] == "oe:aanvraag"
        assert g["content"] == {"titel": "Test", "bedrag": 100.0}
        assert g["derived_from"] is None  # fresh entity
        assert g["ref"] == ref

        # state.resolved_entities shape
        assert "oe:aanvraag" in state.resolved_entities
        pending = state.resolved_entities["oe:aanvraag"]
        assert isinstance(pending, _PendingEntity)
        # Bug 20 (Round 30): _PendingEntity must expose every column
        # EntityRow has, so handlers and the lineage walker can read
        # them uniformly whether the entity is pending or persisted.
        # Before this round, the following attributes were missing,
        # causing AttributeError whenever a pending entity reached
        # code expecting a full EntityRow — the concrete production
        # path was schedule_trekAanvraag_if_onvolledig →
        # find_related_entity(pending_beslissing, "oe:aanvraag"),
        # which reads ``.type`` at lineage.py:123 and ``.generated_by``
        # at lineage.py:126. The crash fires only when there's no
        # ``oe:aanvraag`` in the activity's ``used:`` block — a
        # structural edge case that workflow rules normally prevent
        # but the type system does not.
        assert pending.entity_id == entity_id
        assert pending.id == version_id
        assert pending.content == {"titel": "Test", "bedrag": 100.0}
        assert pending.attributed_to == "u1"  # from state.user.id
        # Newly required under Bug 20.
        assert pending.type == "oe:aanvraag"
        assert pending.dossier_id == D1
        assert pending.generated_by == state.activity_id
        assert pending.derived_from is None  # fresh entity, no parent version
        assert pending.tombstoned_by is None  # pending entities cannot be tombstoned
        # Previously-existing but kept as part of the contract pin.
        assert pending.schema_version is None  # _NoValidationPlugin returns None
        assert pending.created_at is None  # set at persist time, not here

    async def test_derived_from_version_extracted_into_normalized_dict(
        self, repo,
    ):
        """When the client supplies a `derivedFrom` ref, the
        normalized dict's `derived_from` key gets the version UUID
        extracted from the ref (the `@<uuid>` suffix). The
        persistence phase uses this to set the FK."""
        boot = await _bootstrap_dossier(repo)
        parent_eid = uuid4()
        parent_vid = uuid4()
        await repo.create_entity(
            version_id=parent_vid, entity_id=parent_eid, dossier_id=D1,
            type="oe:aanvraag", generated_by=boot,
            content={"titel": "v1"}, attributed_to="system",
        )
        await repo.session.flush()

        new_vid = uuid4()
        ref = f"oe:aanvraag/{parent_eid}@{new_vid}"
        derived_ref = f"oe:aanvraag/{parent_eid}@{parent_vid}"
        state = _state(repo, generated_items=[
            {
                "entity": ref,
                "content": {"titel": "v2"},
                "derivedFrom": derived_ref,
            },
        ])

        await process_generated(state)

        assert state.generated[0]["derived_from"] == parent_vid

    async def test_multiple_items_processed_in_order(self, repo):
        """Two separate logical entities in the same activity.
        Both processed, both land in `state.generated` in the
        order supplied."""
        await _bootstrap_dossier(repo)
        eid_a = uuid4()
        eid_b = uuid4()
        ref_a = f"oe:aanvraag/{eid_a}@{uuid4()}"
        ref_b = f"oe:beslissing/{eid_b}@{uuid4()}"
        state = _state(repo, generated_items=[
            {"entity": ref_a, "content": {"titel": "aanvraag"}},
            {"entity": ref_b, "content": {"uitkomst": "goedgekeurd"}},
        ])

        await process_generated(state)

        assert len(state.generated) == 2
        assert state.generated[0]["type"] == "oe:aanvraag"
        assert state.generated[1]["type"] == "oe:beslissing"
        # Both resolved_entities entries are populated.
        assert "oe:aanvraag" in state.resolved_entities
        assert "oe:beslissing" in state.resolved_entities

    async def test_bare_string_ref_treated_as_external(self, repo):
        """DOCUMENTED CURRENT BEHAVIOR: a bare string like
        `"not-a-ref"` that doesn't match the canonical entity
        pattern is treated as an external URI, not rejected as
        malformed. This is because `is_external_uri` checks "does
        this NOT match the canonical pattern" — anything that
        fails `parse_entity_ref` is externalized.

        As a result, the 422 'Invalid entity reference for
        generated item' branch in the source is unreachable — no
        input can get past `is_external_uri` AND fail
        `parse_entity_ref`.

        Locking this in so a future refactor that tightens the
        external-routing (e.g. requiring a `://` scheme) remembers
        to either (a) also remove the dead 422 branch or (b)
        update this test."""
        await _bootstrap_dossier(repo)
        state = _state(repo, generated_items=[
            {"entity": "this-is-not-a-canonical-ref"},  # no content!
        ])

        # No exception — the bare string is routed to externals,
        # and content isn't required for externals.
        await process_generated(state)

        assert state.generated_externals == ["this-is-not-a-canonical-ref"]
        assert state.generated == []
