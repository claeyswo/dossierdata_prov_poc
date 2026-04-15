"""
Integration tests for `cancel_matching_tasks` in
`engine.pipeline.tasks`.

This is the phase that runs after every activity and cancels any
scheduled `system:task` whose `cancel_if_activities` list includes
the activity we just ran. The subtlety is **anchor scope**: an
anchored task is only cancelled when this activity actually
advanced the anchored entity (wrote a new version of it). Global
tasks (no anchor) are cancelled whenever the canceling activity
type fires, no additional scoping.

This exact invariant is why the worker's `check_cancelled` was
deleted earlier in the session — it existed as a shadow of the
pipeline's logic AND had a bug (ignored anchors entirely). The
pipeline's version handles anchors correctly, but nothing currently
tests that. These tests lock it in.

Branches:

* `no_cancel_if_list_noop` — task has no `cancel_if_activities`
  at all. The canceling activity name doesn't match anything.
  Task remains scheduled.
* `activity_not_in_cancel_list_noop` — task has a list but the
  canceling activity isn't in it. No cancel.
* `global_task_matching_activity_cancelled` — task has no anchor,
  canceling activity is in the list. Task cancelled unconditionally.
* `anchored_task_activity_advanced_anchor_cancelled` — task has
  anchor A, canceling activity's `generated` includes entity A.
  Task cancelled.
* `anchored_task_activity_did_not_advance_anchor_not_cancelled` —
  THE regression gate for the anchor-scope bug. Task has anchor A,
  canceling activity is in the cancel list BUT doesn't generate
  a new version of A. Task must remain scheduled.
* `task_already_completed_ignored` — a task whose latest version
  is `completed` must be ignored — no attempt to write a cancel
  revision.
* `task_created_after_activity_start_not_cancelled` — a task
  whose `created_at` is at-or-after `state.now` (the activity's
  started_at) must be ignored, otherwise an activity that
  schedules and then cancels in the same run would loop.

The phase writes through `repo.create_entity` to persist cancel
revisions, so after each test that expects a cancel we query the
latest task version and assert status is 'cancelled'.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import (
    EntityRow, Repository, AssociationRow,
)
from dossier_engine.engine.pipeline.tasks import cancel_matching_tasks
from dossier_engine.engine.state import ActivityState, Caller


UTC = timezone.utc
D1 = UUID("11111111-1111-1111-1111-111111111111")
ANCHOR_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ANCHOR_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


async def _bootstrap_dossier(repo: Repository) -> UUID:
    await repo.create_dossier(D1, "toelatingen")
    act_id = uuid4()
    now = datetime.now(UTC)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="systemAction",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    return act_id


async def _seed_task(
    repo: Repository,
    activity_id: UUID,
    *,
    status: str = "scheduled",
    cancel_if_activities: list[str] | None = None,
    anchor_entity_id: UUID | None = None,
    anchor_type: str | None = None,
) -> tuple[UUID, UUID]:
    """Seed one scheduled task and return (entity_id, version_id).
    1ms sleep after the insert guarantees the task's `created_at`
    is distinctly before `datetime.now()` when the test then calls
    `cancel_matching_tasks` with a fresh `state.now` — otherwise
    the 'task created >= now' self-protection branch might fire
    and swallow the test signal."""
    eid = uuid4()
    vid = uuid4()
    content = {
        "kind": "scheduled_activity",
        "target_activity": "someTargetActivity",
        "status": status,
        "cancel_if_activities": cancel_if_activities or [],
    }
    if anchor_entity_id is not None:
        content["anchor_entity_id"] = str(anchor_entity_id)
    if anchor_type is not None:
        content["anchor_type"] = anchor_type
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type="system:task", generated_by=activity_id,
        content=content, attributed_to="system",
    )
    await repo.session.flush()
    await asyncio.sleep(0.002)
    return eid, vid


async def _latest_task_status(
    repo: Repository, task_entity_id: UUID,
) -> str | None:
    row = await repo.get_latest_entity_by_id(D1, task_entity_id)
    if row is None or not row.content:
        return None
    return row.content.get("status")


async def _state_and_persist_activity(
    repo: Repository,
    *,
    activity_name: str,
    generated: list[dict] | None = None,
) -> ActivityState:
    """Build the minimum ActivityState `cancel_matching_tasks`
    reads, AND persist a real ActivityRow for state.activity_id
    so the phase's cancel writes can satisfy the `generated_by`
    foreign key.

    The phase reads repo, dossier_id, activity_def['name'],
    activity_id, generated (list of dicts with entity_id), and
    `now` (the activity's started_at). When it cancels a task, it
    writes a new task version with `generated_by = state.activity_id`,
    which must reference a real activity row. In production, the
    orchestrator's `create_activity_row` phase runs before this
    one and creates that row; our tests bypass the orchestrator so
    we have to create it ourselves.

    `state.now` is set to now()-plus-one-second so seeded tasks
    (whose created_at is strictly before this moment) pass the
    'task_created < state.now' check. A microsecond-level delta
    would be flaky under test-side timing jitter.
    """
    activity_id = uuid4()
    now = datetime.now(UTC)
    await repo.create_activity(
        activity_id=activity_id, dossier_id=D1, type=activity_name,
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=activity_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return ActivityState(
        plugin=None,
        activity_def={"name": activity_name},
        repo=repo,
        dossier_id=D1,
        activity_id=activity_id,
        user=None,
        role="",
        used_items=[],
        generated_items=[],
        relation_items=[],
        caller=Caller.CLIENT,
        generated=generated or [],
        now=now + timedelta(seconds=1),
    )


def _state(
    repo: Repository,
    *,
    activity_name: str,
    generated: list[dict] | None = None,
) -> ActivityState:
    """Non-persisting variant used by tests that expect the phase
    to short-circuit before writing (no-op branches). A bogus
    activity_id is fine here because no cancel revision will be
    written."""
    return ActivityState(
        plugin=None,
        activity_def={"name": activity_name},
        repo=repo,
        dossier_id=D1,
        activity_id=uuid4(),
        user=None,
        role="",
        used_items=[],
        generated_items=[],
        relation_items=[],
        caller=Caller.CLIENT,
        generated=generated or [],
        now=datetime.now(UTC) + timedelta(seconds=1),
    )


class TestCancelMatchingTasks:

    async def test_no_cancel_if_list_noop(self, repo):
        """Task with empty `cancel_if_activities`. Nothing can
        match. Task stays scheduled."""
        act = await _bootstrap_dossier(repo)
        task_eid, _ = await _seed_task(
            repo, act, cancel_if_activities=[],
        )
        state = _state(repo, activity_name="someActivity")

        await cancel_matching_tasks(state)

        assert await _latest_task_status(repo, task_eid) == "scheduled"

    async def test_activity_not_in_cancel_list_noop(self, repo):
        """Task has a cancel list but the activity we just ran
        isn't in it. No cancel."""
        act = await _bootstrap_dossier(repo)
        task_eid, _ = await _seed_task(
            repo, act, cancel_if_activities=["otherActivity"],
        )
        state = _state(repo, activity_name="someActivity")

        await cancel_matching_tasks(state)

        assert await _latest_task_status(repo, task_eid) == "scheduled"

    async def test_global_task_matching_activity_cancelled(self, repo):
        """Task has no anchor. Canceling activity is in the list.
        Task cancelled unconditionally — this is the 'global
        scope' branch."""
        act = await _bootstrap_dossier(repo)
        task_eid, _ = await _seed_task(
            repo, act, cancel_if_activities=["vervolledigAanvraag"],
        )
        state = await _state_and_persist_activity(
            repo, activity_name="vervolledigAanvraag",
        )

        await cancel_matching_tasks(state)
        await repo.session.flush()

        assert await _latest_task_status(repo, task_eid) == "cancelled"

    async def test_anchored_task_activity_advanced_anchor_cancelled(self, repo):
        """Task is anchored to entity A. The canceling activity's
        `state.generated` includes an item with `entity_id = A`,
        meaning the activity wrote a new version of A. This is
        the "state actually advanced on the anchored entity"
        branch — cancel fires."""
        act = await _bootstrap_dossier(repo)
        task_eid, _ = await _seed_task(
            repo, act,
            cancel_if_activities=["vervolledigAanvraag"],
            anchor_entity_id=ANCHOR_A,
            anchor_type="oe:aanvraag",
        )
        # State's `generated` has entity A in it — simulating
        # that the canceling activity revised A.
        state = await _state_and_persist_activity(
            repo,
            activity_name="vervolledigAanvraag",
            generated=[{"entity_id": ANCHOR_A}],
        )

        await cancel_matching_tasks(state)
        await repo.session.flush()

        assert await _latest_task_status(repo, task_eid) == "cancelled"

    async def test_anchored_task_activity_did_not_advance_anchor_not_cancelled(
        self, repo,
    ):
        """THE regression gate for the anchor-scope bug. Task is
        anchored to entity A. Canceling activity is in the cancel
        list BUT its `state.generated` does NOT include A — it
        touched something else, maybe entity B. The task must NOT
        be cancelled, because the state of A didn't actually
        advance.

        This is the invariant the deleted worker `check_cancelled`
        violated. The pipeline's version handles it correctly and
        this test locks that correctness in place so a future
        refactor can't silently lose it."""
        act = await _bootstrap_dossier(repo)
        task_eid, _ = await _seed_task(
            repo, act,
            cancel_if_activities=["vervolledigAanvraag"],
            anchor_entity_id=ANCHOR_A,
            anchor_type="oe:aanvraag",
        )
        # State generated entity B, not A. Anchor doesn't match.
        state = _state(
            repo,
            activity_name="vervolledigAanvraag",
            generated=[{"entity_id": ANCHOR_B}],
        )

        await cancel_matching_tasks(state)
        await repo.session.flush()

        # Must still be scheduled.
        assert await _latest_task_status(repo, task_eid) == "scheduled"

    async def test_anchored_task_activity_generated_nothing_not_cancelled(
        self, repo,
    ):
        """Edge case: anchored task, canceling activity matches by
        name, but state.generated is empty (activity was read-only
        or side-effect-only). No entity was advanced, so the
        anchor-scope check correctly fails. Task stays scheduled."""
        act = await _bootstrap_dossier(repo)
        task_eid, _ = await _seed_task(
            repo, act,
            cancel_if_activities=["vervolledigAanvraag"],
            anchor_entity_id=ANCHOR_A,
            anchor_type="oe:aanvraag",
        )
        state = _state(
            repo,
            activity_name="vervolledigAanvraag",
            generated=[],  # nothing generated
        )

        await cancel_matching_tasks(state)
        await repo.session.flush()

        assert await _latest_task_status(repo, task_eid) == "scheduled"

    async def test_task_already_completed_ignored(self, repo):
        """A task whose latest version is already `completed`
        must not get a cancel revision written over it. The
        phase checks `status == 'scheduled'` before doing
        anything."""
        act = await _bootstrap_dossier(repo)
        task_eid, _ = await _seed_task(
            repo, act,
            status="completed",  # already done
            cancel_if_activities=["vervolledigAanvraag"],
        )
        state = _state(repo, activity_name="vervolledigAanvraag")

        await cancel_matching_tasks(state)
        await repo.session.flush()

        # Still completed — no cancel written.
        assert await _latest_task_status(repo, task_eid) == "completed"

    async def test_task_created_after_activity_start_not_cancelled(self, repo):
        """A task whose `created_at >= state.now` is ignored.
        This protects an activity from cancelling a task that the
        SAME activity just scheduled — without this check, an
        activity that both schedules a follow-up AND appears in
        its own `cancel_if_activities` list (unusual but possible)
        would cancel its own freshly-scheduled task in the same
        run."""
        act = await _bootstrap_dossier(repo)
        task_eid, _ = await _seed_task(
            repo, act,
            cancel_if_activities=["vervolledigAanvraag"],
        )
        # Set state.now to a time BEFORE the task was created.
        # This simulates "the task is newer than this activity
        # started" — the in-same-run case.
        state = _state(repo, activity_name="vervolledigAanvraag")
        state.now = datetime(2000, 1, 1, tzinfo=UTC)  # far in the past

        await cancel_matching_tasks(state)
        await repo.session.flush()

        # Task still scheduled — not cancelled by its own
        # scheduling activity.
        assert await _latest_task_status(repo, task_eid) == "scheduled"
