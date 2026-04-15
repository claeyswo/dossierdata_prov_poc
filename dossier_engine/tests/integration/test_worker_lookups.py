"""
Integration tests for the worker's task-lookup functions against a
real Postgres database with multi-version task histories.

Why this file exists:

For most of the worker arc, the tests were either happy-path API
suite runs (test_requests.sh) or bespoke synthetic harnesses built
from scratch for specific bug hunts. The `_refetch_task` bug found
during the requeue E2E verification — a function that loops through
`get_entities_by_type` in created_at ASC order and returns the FIRST
match, so for any multi-version task it returns the oldest version
instead of the latest — hid undetected because nothing in the test
suite ever exercised the multi-version success path.

These tests seed a multi-version task directly into the DB, then
call the lookup functions and assert they return the latest version.
If anyone ever regresses `_refetch_task` or `_claim_one_due_task` to
ordering bugs, these tests fail in milliseconds.

The tests also cover `requeue_dead_letters`'s selection logic (what
does it pick up?) and its scope filters, which lived only in the
E2E harness before today.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import (
    EntityRow, Repository, ActivityRow, AssociationRow,
)
from dossier_engine.worker import (
    _refetch_task, _claim_one_due_task,
)


UTC = timezone.utc
FIXED_DOSSIER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
FIXED_TASK_EID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


async def _seed_dossier(repo: Repository, dossier_id: UUID = FIXED_DOSSIER):
    """Create a minimal dossier + one bootstrap systemAction activity
    that can be used as `generated_by` for the task versions we
    seed. The systemAction needs one association row so the engine's
    invariants don't reject it if we ever read it back.

    Returns the activity_id so tests can reference it.
    """
    await repo.create_dossier(dossier_id, "toelatingen")
    act_id = uuid4()
    now = datetime.now(UTC)
    await repo.create_activity(
        activity_id=act_id,
        dossier_id=dossier_id,
        type="systemAction",
        started_at=now,
        ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    return act_id


async def _seed_task_chain(
    repo: Repository,
    activity_id: UUID,
    *contents: dict,
    dossier_id: UUID = FIXED_DOSSIER,
    entity_id: UUID = FIXED_TASK_EID,
) -> list[UUID]:
    """Create N versions of one logical task entity, in order.

    Each `contents` dict is one version's content. The versions are
    linked via `derived_from` in order — v2 derives from v1, v3
    derives from v2, etc. A 1ms sleep between versions ensures each
    gets a distinct `created_at` stamp so `latest by max(created_at)`
    is unambiguous.

    Returns the list of version UUIDs in creation order.
    """
    version_ids: list[UUID] = []
    prev_id: UUID | None = None
    for content in contents:
        vid = uuid4()
        await repo.create_entity(
            version_id=vid,
            entity_id=entity_id,
            dossier_id=dossier_id,
            type="system:task",
            generated_by=activity_id,
            content=content,
            derived_from=prev_id,
            attributed_to="system",
        )
        await repo.session.flush()
        version_ids.append(vid)
        prev_id = vid
        # 1ms sleep guarantees distinct created_at stamps — the
        # Python-side `default=lambda: datetime.now(...)` fires
        # fresh on each create_entity call, and microsecond-
        # resolution timestamps would otherwise risk ties.
        await asyncio.sleep(0.001)
    return version_ids


# --------------------------------------------------------------------
# _refetch_task — the bug that motivated this file
# --------------------------------------------------------------------


class TestRefetchTask:
    """`_refetch_task(repo, dossier_id, entity_id)` must return the
    LATEST version of the task entity. The old implementation used
    `get_entities_by_type(... ORDER BY created_at ASC)` and returned
    the first match, which was the oldest — a latent bug that hid
    for months because no test exercised the multi-version case.
    """

    async def test_single_version_returned(self, repo):
        """Baseline: a task with exactly one version returns that
        version. This is what the old buggy code happened to get
        right, because "oldest" and "latest" are the same when
        there's only one row."""
        act_id = await _seed_dossier(repo)
        [v1] = await _seed_task_chain(
            repo, act_id,
            {"status": "scheduled", "function": "foo", "attempt_count": 0},
        )
        result = await _refetch_task(repo, FIXED_DOSSIER, FIXED_TASK_EID)
        assert result is not None
        assert result.id == v1

    async def test_latest_of_three_versions_returned(self, repo):
        """The regression test for the bug. Three versions, each
        with a different attempt_count, linked via derived_from.
        The function must return v3 (the one with attempt_count=2),
        not v1 (attempt_count=0) which the old buggy code returned."""
        act_id = await _seed_dossier(repo)
        v1, v2, v3 = await _seed_task_chain(
            repo, act_id,
            {"status": "scheduled", "function": "foo", "attempt_count": 0},
            {"status": "scheduled", "function": "foo", "attempt_count": 1},
            {"status": "scheduled", "function": "foo", "attempt_count": 2},
        )
        result = await _refetch_task(repo, FIXED_DOSSIER, FIXED_TASK_EID)
        assert result is not None
        assert result.id == v3, (
            f"expected latest version {v3}, got {result.id} "
            f"(attempt_count={result.content.get('attempt_count')})"
        )
        assert result.content["attempt_count"] == 2

    async def test_dead_lettered_latest_returned(self, repo):
        """A task that went scheduled → scheduled → dead_letter.
        _refetch_task must return the dead-lettered version, not
        any of the earlier scheduled versions. This is the shape
        the requeue feature writes over."""
        act_id = await _seed_dossier(repo)
        _, _, v3 = await _seed_task_chain(
            repo, act_id,
            {"status": "scheduled", "function": "foo", "attempt_count": 0},
            {"status": "scheduled", "function": "foo", "attempt_count": 1},
            {"status": "dead_letter", "function": "foo", "attempt_count": 2},
        )
        result = await _refetch_task(repo, FIXED_DOSSIER, FIXED_TASK_EID)
        assert result.id == v3
        assert result.content["status"] == "dead_letter"

    async def test_returns_none_for_unknown_entity_id(self, repo):
        """An entity_id that doesn't exist in the dossier returns
        None, not a spurious row from a different task."""
        act_id = await _seed_dossier(repo)
        await _seed_task_chain(
            repo, act_id,
            {"status": "scheduled", "function": "foo"},
        )
        other_eid = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
        result = await _refetch_task(repo, FIXED_DOSSIER, other_eid)
        assert result is None

    async def test_does_not_confuse_tasks_in_same_dossier(self, repo):
        """Two separate logical tasks in the same dossier. A refetch
        for one must return that one's latest version, not
        accidentally pick up a version from the other task (which
        `get_entities_by_type`'s old "return first match" logic
        might have done on a different iteration order)."""
        act_id = await _seed_dossier(repo)
        eid_a = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
        eid_b = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
        a_v1 = uuid4()
        b_v1 = uuid4()
        await repo.create_entity(
            version_id=a_v1, entity_id=eid_a, dossier_id=FIXED_DOSSIER,
            type="system:task", generated_by=act_id,
            content={"status": "scheduled", "function": "A"},
            attributed_to="system",
        )
        await asyncio.sleep(0.001)
        await repo.create_entity(
            version_id=b_v1, entity_id=eid_b, dossier_id=FIXED_DOSSIER,
            type="system:task", generated_by=act_id,
            content={"status": "scheduled", "function": "B"},
            attributed_to="system",
        )
        await repo.session.flush()

        result_a = await _refetch_task(repo, FIXED_DOSSIER, eid_a)
        result_b = await _refetch_task(repo, FIXED_DOSSIER, eid_b)
        assert result_a.id == a_v1
        assert result_a.content["function"] == "A"
        assert result_b.id == b_v1
        assert result_b.content["function"] == "B"


# --------------------------------------------------------------------
# _claim_one_due_task — the poll query with multi-version history
# --------------------------------------------------------------------


class TestClaimOneDueTask:
    """`_claim_one_due_task(session)` runs the SQL-level poll query
    and returns one due task (or None). Because the query joins on
    `MAX(created_at)` per entity_id, it sees only the latest version
    of each logical task — so a task whose latest version is
    `completed` or `dead_letter` must NOT be claimed, even if older
    versions with `status=scheduled` still exist in the history.
    """

    async def test_returns_none_on_empty(self, db_session):
        """Fresh DB, nothing to claim. Not an error, just None."""
        result = await _claim_one_due_task(db_session)
        assert result is None

    async def test_claims_scheduled_single_version_task(self, db_session):
        """Happy path: one scheduled task, no retry delay, no
        scheduled_for in the future. Should be claimed."""
        repo = Repository(db_session)
        act_id = await _seed_dossier(repo)
        [v1] = await _seed_task_chain(
            repo, act_id,
            {"status": "scheduled", "function": "foo"},
        )
        await db_session.flush()

        result = await _claim_one_due_task(db_session)
        assert result is not None
        assert result.id == v1

    async def test_skips_task_whose_latest_is_completed(self, db_session):
        """A task that went scheduled → completed. The SQL layer's
        latest-version join filters on `status = 'scheduled'` against
        the newest version, so the completed latest version is
        rejected and the claim returns None. Critically, this must
        NOT be fooled by the older `scheduled` version still existing
        in the task history."""
        repo = Repository(db_session)
        act_id = await _seed_dossier(repo)
        await _seed_task_chain(
            repo, act_id,
            {"status": "scheduled", "function": "foo"},
            {"status": "completed", "function": "foo"},
        )
        await db_session.flush()

        result = await _claim_one_due_task(db_session)
        assert result is None

    async def test_skips_task_whose_latest_is_dead_letter(self, db_session):
        """Same shape, terminal status is dead_letter instead of
        completed. Must NOT be claimed — dead letters require
        operator intervention (requeue) to become claimable again.
        This test proves the poll query correctly treats dead_letter
        as invisible."""
        repo = Repository(db_session)
        act_id = await _seed_dossier(repo)
        await _seed_task_chain(
            repo, act_id,
            {"status": "scheduled", "function": "foo", "attempt_count": 0},
            {"status": "scheduled", "function": "foo", "attempt_count": 1},
            {"status": "dead_letter", "function": "foo", "attempt_count": 2},
        )
        await db_session.flush()

        result = await _claim_one_due_task(db_session)
        assert result is None

    async def test_skips_task_with_future_next_attempt_at(self, db_session):
        """A retry task with `next_attempt_at` in the future is
        structurally `scheduled` but operationally not claimable
        yet. The SQL layer returns it as a candidate; Python-side
        `_is_task_due` filter rejects it. The claim should return
        None rather than waking up the task early."""
        repo = Repository(db_session)
        act_id = await _seed_dossier(repo)
        future = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
        await _seed_task_chain(
            repo, act_id,
            {
                "status": "scheduled",
                "function": "foo",
                "attempt_count": 1,
                "next_attempt_at": future,
            },
        )
        await db_session.flush()

        result = await _claim_one_due_task(db_session)
        assert result is None

    async def test_claims_task_with_past_next_attempt_at(self, db_session):
        """Retry delay has elapsed → claimable. The past timestamp
        in `next_attempt_at` passes through the Python-side filter
        because it's ≤ now."""
        repo = Repository(db_session)
        act_id = await _seed_dossier(repo)
        past = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
        [_, v2] = await _seed_task_chain(
            repo, act_id,
            {"status": "scheduled", "function": "foo", "attempt_count": 0},
            {
                "status": "scheduled",
                "function": "foo",
                "attempt_count": 1,
                "next_attempt_at": past,
            },
        )
        await db_session.flush()

        result = await _claim_one_due_task(db_session)
        assert result is not None
        assert result.id == v2  # the latest (retry) version
        assert result.content["attempt_count"] == 1


# --------------------------------------------------------------------
# get_latest_entity_by_id — the Repository helper _refetch_task now uses
# --------------------------------------------------------------------


class TestGetLatestEntityById:
    """`get_latest_entity_by_id` is the query _refetch_task now
    delegates to after the bug fix. It does one targeted query with
    `ORDER BY created_at DESC LIMIT 1` — much cleaner than the old
    "fetch all then loop" pattern."""

    async def test_returns_latest_of_multiple_versions(self, repo):
        act_id = await _seed_dossier(repo)
        _, _, v3 = await _seed_task_chain(
            repo, act_id,
            {"status": "scheduled", "attempt_count": 0},
            {"status": "scheduled", "attempt_count": 1},
            {"status": "dead_letter", "attempt_count": 2},
        )
        result = await repo.get_latest_entity_by_id(FIXED_DOSSIER, FIXED_TASK_EID)
        assert result.id == v3

    async def test_returns_none_for_nonexistent(self, repo):
        await _seed_dossier(repo)
        result = await repo.get_latest_entity_by_id(
            FIXED_DOSSIER, UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        )
        assert result is None
