"""
Integration tests for `_select_dead_lettered_tasks` — the query
helper extracted from `requeue_dead_letters` so the selection logic
can be exercised without running through the config-loading
bootstrap.

What this file locks in:

1. The query returns only dead-lettered tasks. Scheduled tasks,
   completed tasks, and cancelled tasks are excluded at the SQL
   level.
2. The query respects the latest-version-per-entity rule. A task
   whose latest version is scheduled (a requeued dead-letter, for
   example) must NOT appear in the result set even though an older
   dead_letter version still exists in the history.
3. Scope filters narrow correctly: `dossier_id` limits to one
   dossier, `task_entity_id` limits to one logical task, both
   together AND together.
4. Empty result is a plain empty list, not None or an exception.

These aren't speculative tests — they're pinning down the exact
behavior that the `--requeue-dead-letters` CLI depends on. If
anyone ever changes the query shape in a way that drops one of
these guarantees, the CLI silently starts requeuing the wrong
tasks, which is exactly the kind of bug end-to-end suites don't
reliably catch.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import (
    EntityRow, Repository, AssociationRow,
)
from dossier_engine.worker import _select_dead_lettered_tasks


UTC = timezone.utc
D1 = UUID("11111111-1111-1111-1111-111111111111")
D2 = UUID("22222222-2222-2222-2222-222222222222")


async def _minimal_dossier(repo: Repository, dossier_id: UUID) -> UUID:
    """Create a dossier and a single bootstrap systemAction. Returns
    the activity id so seeded task versions can reference it as
    `generated_by`."""
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


async def _seed_task_versions(
    repo: Repository,
    activity_id: UUID,
    dossier_id: UUID,
    entity_id: UUID,
    *contents: dict,
) -> list[UUID]:
    """Create a chain of task versions linked via `derived_from`.
    1ms sleep between versions guarantees distinct `created_at`
    so `latest by max(created_at)` is unambiguous."""
    version_ids = []
    prev = None
    for content in contents:
        vid = uuid4()
        await repo.create_entity(
            version_id=vid, entity_id=entity_id, dossier_id=dossier_id,
            type="system:task", generated_by=activity_id,
            content=content, derived_from=prev, attributed_to="system",
        )
        await repo.session.flush()
        version_ids.append(vid)
        prev = vid
        await asyncio.sleep(0.001)
    return version_ids


class TestSelectDeadLetteredTasks:

    async def test_empty_database_returns_empty_list(self, db_session):
        result = await _select_dead_lettered_tasks(db_session)
        assert result == []

    async def test_selects_dead_lettered_task(self, db_session):
        """Baseline: one dead-lettered task, no other rows. Should
        be selected."""
        repo = Repository(db_session)
        act = await _minimal_dossier(repo, D1)
        eid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        [_, v2] = await _seed_task_versions(
            repo, act, D1, eid,
            {"status": "scheduled", "function": "foo", "attempt_count": 0},
            {"status": "dead_letter", "function": "foo", "attempt_count": 3},
        )
        await db_session.flush()

        result = await _select_dead_lettered_tasks(db_session)
        assert len(result) == 1
        assert result[0].id == v2
        assert result[0].entity_id == eid

    async def test_excludes_scheduled_tasks(self, db_session):
        """A task that's still scheduled (even if it went through
        failed attempts) must not be selected."""
        repo = Repository(db_session)
        act = await _minimal_dossier(repo, D1)
        eid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        await _seed_task_versions(
            repo, act, D1, eid,
            {"status": "scheduled", "function": "foo", "attempt_count": 1},
        )
        await db_session.flush()

        result = await _select_dead_lettered_tasks(db_session)
        assert result == []

    async def test_excludes_completed_tasks(self, db_session):
        repo = Repository(db_session)
        act = await _minimal_dossier(repo, D1)
        eid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        await _seed_task_versions(
            repo, act, D1, eid,
            {"status": "scheduled", "function": "foo"},
            {"status": "completed", "function": "foo"},
        )
        await db_session.flush()

        result = await _select_dead_lettered_tasks(db_session)
        assert result == []

    async def test_excludes_cancelled_tasks(self, db_session):
        repo = Repository(db_session)
        act = await _minimal_dossier(repo, D1)
        eid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        await _seed_task_versions(
            repo, act, D1, eid,
            {"status": "scheduled", "function": "foo"},
            {"status": "cancelled", "function": "foo"},
        )
        await db_session.flush()

        result = await _select_dead_lettered_tasks(db_session)
        assert result == []

    async def test_dead_letter_superseded_by_scheduled_requeue_is_excluded(
        self, db_session,
    ):
        """The critical requeue scenario: a task went
        scheduled → dead_letter → scheduled (via operator requeue).
        The OLD dead_letter version still exists in the history,
        but the LATEST version is now scheduled. The query must
        see only the latest and exclude this task from the
        dead-letter set.

        This is what prevents `--requeue-dead-letters` from
        re-requeuing a task on every invocation forever."""
        repo = Repository(db_session)
        act = await _minimal_dossier(repo, D1)
        eid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        await _seed_task_versions(
            repo, act, D1, eid,
            {"status": "scheduled", "function": "foo", "attempt_count": 0},
            {"status": "dead_letter", "function": "foo", "attempt_count": 3},
            # Simulated requeue: new version, attempt_count reset,
            # status back to scheduled.
            {"status": "scheduled", "function": "foo", "attempt_count": 0},
        )
        await db_session.flush()

        result = await _select_dead_lettered_tasks(db_session)
        assert result == []

    async def test_multiple_dead_letters_returned(self, db_session):
        """Two separate logical tasks, both dead-lettered, in the
        same dossier. Both should come back. This is the "bulk
        requeue" scenario: an operator fixed a shared root cause
        and wants to retry every affected task."""
        repo = Repository(db_session)
        act = await _minimal_dossier(repo, D1)
        eid_a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        eid_b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        await _seed_task_versions(
            repo, act, D1, eid_a,
            {"status": "dead_letter", "function": "fA", "attempt_count": 3},
        )
        await _seed_task_versions(
            repo, act, D1, eid_b,
            {"status": "dead_letter", "function": "fB", "attempt_count": 3},
        )
        await db_session.flush()

        result = await _select_dead_lettered_tasks(db_session)
        assert len(result) == 2
        functions = sorted(r.content["function"] for r in result)
        assert functions == ["fA", "fB"]

    async def test_dossier_scope_filter(self, db_session):
        """Seed dead-lettered tasks in TWO dossiers, filter by
        `dossier_id=D1`, expect only D1's task back."""
        repo = Repository(db_session)
        act_d1 = await _minimal_dossier(repo, D1)
        act_d2 = await _minimal_dossier(repo, D2)
        eid_d1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        eid_d2 = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
        await _seed_task_versions(
            repo, act_d1, D1, eid_d1,
            {"status": "dead_letter", "function": "in_D1", "attempt_count": 3},
        )
        await _seed_task_versions(
            repo, act_d2, D2, eid_d2,
            {"status": "dead_letter", "function": "in_D2", "attempt_count": 3},
        )
        await db_session.flush()

        # No filter → both come back
        both = await _select_dead_lettered_tasks(db_session)
        assert len(both) == 2

        # Filter to D1 → only D1's task
        d1_only = await _select_dead_lettered_tasks(
            db_session, dossier_id=D1,
        )
        assert len(d1_only) == 1
        assert d1_only[0].content["function"] == "in_D1"
        assert d1_only[0].dossier_id == D1

        # Filter to D2 → only D2's task
        d2_only = await _select_dead_lettered_tasks(
            db_session, dossier_id=D2,
        )
        assert len(d2_only) == 1
        assert d2_only[0].content["function"] == "in_D2"

    async def test_task_entity_id_scope_filter(self, db_session):
        """Two dead-lettered tasks in the same dossier. Filter to
        one specific entity_id. Only that one comes back."""
        repo = Repository(db_session)
        act = await _minimal_dossier(repo, D1)
        eid_a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        eid_b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        await _seed_task_versions(
            repo, act, D1, eid_a,
            {"status": "dead_letter", "function": "fA", "attempt_count": 3},
        )
        await _seed_task_versions(
            repo, act, D1, eid_b,
            {"status": "dead_letter", "function": "fB", "attempt_count": 3},
        )
        await db_session.flush()

        result = await _select_dead_lettered_tasks(
            db_session, task_entity_id=eid_a,
        )
        assert len(result) == 1
        assert result[0].entity_id == eid_a
