"""
Integration tests for `_validate_derivation` in
`engine.pipeline.generated`.

This is the check that fires when a generated entity's
`derivedFrom` doesn't match the actual current latest version of
its logical entity in the dossier. It's one of the engine's most
important invariants — without it, a client with a stale view of
the dossier could silently overwrite someone else's revision.

This check is also the one that caught the `_refetch_task` bug
during the requeue E2E verification earlier in the session. The
worker was completing tasks using a stale version as the
derivedFrom, and the engine correctly rejected every completion
with this 422. So we care a lot about locking this check down.

Five branches to cover:

* `fresh_entity_no_parent_ok` — no prior version, no `derivedFrom`.
  Creation, not revision. Must pass.
* `revision_pointing_at_latest_ok` — a prior version exists, the
  client's `derivedFrom` matches it. The common case. Must pass.
* `revision_pointing_at_stale_version_rejected` — the regression
  gate for the bug hunt. A v3 exists, client derives from v1,
  must raise 422 with `invalid_derivation_chain`.
* `missing_derivation_on_existing_entity_rejected` — a prior
  version exists but the client didn't declare `derivedFrom` at
  all. Must raise 422 with `missing_derivation_chain`.
* `cross_entity_derivation_rejected` — `derivedFrom` points at a
  version that exists but belongs to a DIFFERENT logical entity.
  Must raise 422 with `cross_entity_derivation`.

Plus two edge cases:

* `unknown_parent_version_rejected` — `derivedFrom` points at a
  version UUID that doesn't exist in the DB at all.
* `parent_in_different_dossier_rejected` — the version exists but
  belongs to another dossier. This is the sibling of the
  cross-dossier `used` check; same invariant (PROV closure).

The tests are "integration" because they use a real Repository
against the test DB, but they call `_validate_derivation` directly
with constructed inputs rather than driving the whole pipeline.
That keeps each test focused on the exact branch it exercises.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import EntityRow, Repository, AssociationRow
from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.pipeline.generated import _validate_derivation


UTC = timezone.utc
D1 = UUID("11111111-1111-1111-1111-111111111111")
D2 = UUID("22222222-2222-2222-2222-222222222222")
ENTITY_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ENTITY_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


async def _bootstrap_dossier(repo: Repository, dossier_id: UUID) -> UUID:
    """Create a minimal dossier + one bootstrap systemAction so
    seeded entities have something to point at as `generated_by`.
    Returns the activity_id."""
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


async def _seed_entity_chain(
    repo: Repository,
    activity_id: UUID,
    dossier_id: UUID,
    entity_id: UUID,
    entity_type: str,
    count: int,
) -> list[UUID]:
    """Seed `count` versions of one logical entity, linked via
    derivedFrom. Returns version UUIDs in creation order (oldest
    first). 1ms sleep between inserts guarantees distinct
    created_at stamps."""
    version_ids: list[UUID] = []
    prev = None
    for i in range(count):
        vid = uuid4()
        await repo.create_entity(
            version_id=vid, entity_id=entity_id, dossier_id=dossier_id,
            type=entity_type, generated_by=activity_id,
            content={"index": i},
            derived_from=prev, attributed_to="system",
        )
        await repo.session.flush()
        version_ids.append(vid)
        prev = vid
        await asyncio.sleep(0.001)
    return version_ids


def _state_for_derivation(repo: Repository, dossier_id: UUID):
    """Build the minimum `state` shape `_validate_derivation` reads.
    It only touches `state.repo`, `state.dossier_id`, and — via
    `_latest_version_payload` on the error path — nothing else.
    SimpleNamespace is enough; no need for the full ActivityState
    dataclass."""
    return SimpleNamespace(repo=repo, dossier_id=dossier_id)


class TestValidateDerivation:

    async def test_fresh_entity_no_parent_ok(self, repo):
        """Creating a brand-new logical entity. No `derivedFrom`,
        no prior version. Must not raise."""
        await _bootstrap_dossier(repo, D1)
        state = _state_for_derivation(repo, D1)
        # Pass latest_existing=None to simulate "no prior version"
        await _validate_derivation(
            state=state,
            entity_ref=f"oe:aanvraag/{ENTITY_A}@{uuid4()}",
            entity_type="oe:aanvraag",
            entity_logical_id=ENTITY_A,
            derived_from_ref=None,
            latest_existing=None,
        )

    async def test_revision_pointing_at_latest_ok(self, repo):
        """A v1 exists, client derives from it to produce v2.
        The normal happy-path revision. Must not raise."""
        act = await _bootstrap_dossier(repo, D1)
        [v1] = await _seed_entity_chain(
            repo, act, D1, ENTITY_A, "oe:aanvraag", 1,
        )
        v1_row = await repo.get_entity(v1)
        state = _state_for_derivation(repo, D1)

        await _validate_derivation(
            state=state,
            entity_ref=f"oe:aanvraag/{ENTITY_A}@{uuid4()}",
            entity_type="oe:aanvraag",
            entity_logical_id=ENTITY_A,
            derived_from_ref=f"oe:aanvraag/{ENTITY_A}@{v1}",
            latest_existing=v1_row,
        )

    async def test_revision_pointing_at_stale_version_rejected(self, repo):
        """THE regression gate for the bug hunt. v1, v2, v3 exist
        in sequence. Client builds a generated item with
        `derivedFrom = v1`. v3 is the actual latest. Must raise
        422 `invalid_derivation_chain`, and the error payload must
        carry both the declared parent and the actual latest so
        the client can rebase."""
        act = await _bootstrap_dossier(repo, D1)
        v1, v2, v3 = await _seed_entity_chain(
            repo, act, D1, ENTITY_A, "oe:aanvraag", 3,
        )
        v3_row = await repo.get_entity(v3)
        state = _state_for_derivation(repo, D1)

        with pytest.raises(ActivityError) as exc:
            await _validate_derivation(
                state=state,
                entity_ref=f"oe:aanvraag/{ENTITY_A}@{uuid4()}",
                entity_type="oe:aanvraag",
                entity_logical_id=ENTITY_A,
                derived_from_ref=f"oe:aanvraag/{ENTITY_A}@{v1}",
                latest_existing=v3_row,
            )
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "invalid_derivation_chain"
        assert exc.value.payload["declared_parent"] == str(v1)
        assert exc.value.payload["latest_parent"] == str(v3)

    async def test_missing_derivation_on_existing_entity_rejected(self, repo):
        """A prior version exists but the client didn't declare
        `derivedFrom` at all — they're trying to create a fresh
        entity with an ID that's already taken. Must raise 422
        `missing_derivation_chain` so the client knows to
        refresh and supply the parent link."""
        act = await _bootstrap_dossier(repo, D1)
        [v1] = await _seed_entity_chain(
            repo, act, D1, ENTITY_A, "oe:aanvraag", 1,
        )
        v1_row = await repo.get_entity(v1)
        state = _state_for_derivation(repo, D1)

        with pytest.raises(ActivityError) as exc:
            await _validate_derivation(
                state=state,
                entity_ref=f"oe:aanvraag/{ENTITY_A}@{uuid4()}",
                entity_type="oe:aanvraag",
                entity_logical_id=ENTITY_A,
                derived_from_ref=None,  # missing!
                latest_existing=v1_row,
            )
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "missing_derivation_chain"

    async def test_cross_entity_derivation_rejected(self, repo):
        """`derivedFrom` points at a version that exists and is in
        the correct dossier, but it belongs to a DIFFERENT logical
        entity. This is never legitimate — derivation chains are
        per-logical-entity. Must raise 422 `cross_entity_derivation`.
        """
        act = await _bootstrap_dossier(repo, D1)
        # Two separate logical entities. We'll point derivedFrom
        # at entity B's version while claiming to generate entity A.
        [b_v1] = await _seed_entity_chain(
            repo, act, D1, ENTITY_B, "oe:aanvraag", 1,
        )
        state = _state_for_derivation(repo, D1)

        with pytest.raises(ActivityError) as exc:
            await _validate_derivation(
                state=state,
                entity_ref=f"oe:aanvraag/{ENTITY_A}@{uuid4()}",
                entity_type="oe:aanvraag",
                entity_logical_id=ENTITY_A,
                derived_from_ref=f"oe:aanvraag/{ENTITY_B}@{b_v1}",
                latest_existing=None,  # A has no prior version
            )
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "cross_entity_derivation"

    async def test_unknown_parent_version_rejected(self, repo):
        """`derivedFrom` points at a version UUID that doesn't
        exist in the DB. Must raise 422 `unknown_parent` rather
        than a 500 or a silent pass."""
        await _bootstrap_dossier(repo, D1)
        state = _state_for_derivation(repo, D1)
        bogus_version = uuid4()

        with pytest.raises(ActivityError) as exc:
            await _validate_derivation(
                state=state,
                entity_ref=f"oe:aanvraag/{ENTITY_A}@{uuid4()}",
                entity_type="oe:aanvraag",
                entity_logical_id=ENTITY_A,
                derived_from_ref=f"oe:aanvraag/{ENTITY_A}@{bogus_version}",
                latest_existing=None,
            )
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "unknown_parent"

    async def test_parent_in_different_dossier_rejected(self, repo):
        """`derivedFrom` points at a version that exists but lives
        in another dossier. Cross-dossier derivation breaks PROV
        closure — same invariant as cross-dossier `used` refs. Must
        raise 422 `unknown_parent` (the check treats a different-
        dossier parent as "unknown" from the current dossier's
        point of view, which is the right framing)."""
        # Dossier D1 is our target. D2 has an entity we're going
        # to try to reference in D1's derivedFrom chain.
        act_d2 = await _bootstrap_dossier(repo, D2)
        [b_v1] = await _seed_entity_chain(
            repo, act_d2, D2, ENTITY_B, "oe:aanvraag", 1,
        )
        # D1 itself: create it but don't seed the entity we're
        # trying to revise.
        await _bootstrap_dossier(repo, D1)
        state = _state_for_derivation(repo, D1)

        with pytest.raises(ActivityError) as exc:
            await _validate_derivation(
                state=state,
                entity_ref=f"oe:aanvraag/{ENTITY_B}@{uuid4()}",
                entity_type="oe:aanvraag",
                entity_logical_id=ENTITY_B,
                derived_from_ref=f"oe:aanvraag/{ENTITY_B}@{b_v1}",
                latest_existing=None,
            )
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "unknown_parent"
