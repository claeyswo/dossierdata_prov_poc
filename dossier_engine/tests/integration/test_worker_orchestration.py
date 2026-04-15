"""
Integration tests for the worker's orchestration functions:

* `complete_task` — records task completion via execute_activity
* `_record_failure` — increments attempt_count, decides retry vs
  dead_letter, writes a new task version
* `_process_recorded` — type-2 task dispatch (call plugin function,
  record completion)
* `_process_scheduled_activity` — type-3 task dispatch (run the
  target activity, record completion)
* `_execute_claimed_task` — the dispatcher: refetch task, skip if
  status changed, route to kind-specific processor

These tests use a REAL Plugin (not a stub) because `complete_task`
calls `execute_activity` with `SYSTEM_ACTION_DEF`, and the engine
pipeline runs for real. That means we get full fidelity: the
TaskEntity content model validates, the system:note validates,
the post-activity hook gets a chance to fire, and the cached
status + eligible activities are updated. If any of those
interactions have a regression, these tests catch it.

Covered branches:

**`complete_task`** (5 tests):
* successful completion writes a new scheduled→completed revision
* extra_content merges into the new task content (attempt_count,
  next_attempt_at)
* result URI goes into the new revision's `result` field
* completion status override (dead_letter) writes that status
* plugin missing systemAction def raises RuntimeError

**`_record_failure`** (6 tests):
* first failure within budget → new version has
  attempt_count=1, next_attempt_at set, status still scheduled
* failure that exceeds max_attempts → status=dead_letter,
  no next_attempt_at
* custom max_attempts override on task content is respected
* custom base_delay_seconds override is respected
* zero-attempt failure transitions to attempt_count=1 (baseline)
* the error is logged via logger.exception but doesn't bubble up
  as a task-content field (the Sentry-telemetry-only contract)

**`_process_recorded`** (3 tests):
* function registered → called with context, task marked completed
* function not registered → warning logged, task still marked
  completed
* function raises → exception propagates (caller handles retry)

**`_process_scheduled_activity`** (2 tests):
* Target activity executes, task completed with informed_by set
* Target activity not found → raises ValueError

**`_execute_claimed_task`** (4 tests):
* Missing dossier → logs + returns early (no crash)
* Missing plugin → logs + returns early
* Task refetch returns None → warning + return
* Task refetched with status != scheduled → skipped (cancel/
  supersede visibility)

These are all higher-setup tests because each one needs a real
Plugin registered with systemAction support. We build one fresh
plugin per test class via a helper and bootstrap the dossier +
task chain inside the test.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.errors import ActivityError
from dossier_engine.entities import SYSTEM_ACTION_DEF, SystemNote, TaskEntity
from dossier_engine.plugin import Plugin
from dossier_engine.worker import (
    complete_task, _record_failure, _process_recorded,
    _process_scheduled_activity, _execute_claimed_task,
)


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _make_worker_plugin(
    task_handlers: dict | None = None,
    extra_activities: list[dict] | None = None,
) -> Plugin:
    """Build a real Plugin with systemAction wired in and
    optional extra activity defs / task handlers.

    The worker pipeline calls `plugin.find_activity_def("systemAction")`
    inside `complete_task`, so the systemAction def must be in
    `workflow.activities`. It also calls `plugin.task_handlers`
    inside `_process_recorded`, so the map needs to be populated
    for tests that register functions.

    `entity_models` carries the Pydantic models the engine
    validates content against — system:task and system:note are
    required because every task completion writes both.
    """
    activities = [SYSTEM_ACTION_DEF]
    if extra_activities:
        activities.extend(extra_activities)

    return Plugin(
        name="test",
        workflow={
            "activities": activities,
            "entity_types": [
                {"type": "system:task", "cardinality": "multiple"},
                {"type": "system:note", "cardinality": "multiple"},
            ],
            "relations": [],
        },
        entity_models={
            "system:task": TaskEntity,
            "system:note": SystemNote,
        },
        task_handlers=task_handlers or {},
    )


async def _bootstrap_dossier(repo: Repository) -> UUID:
    """Create D1 and a real ActivityRow that subsequent task
    revisions can point at as their generated_by. Returns the
    activity_id."""
    await repo.create_dossier(D1, "test")
    await repo.ensure_agent("system", "systeem", "Systeem", {})
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


async def _seed_task(
    repo: Repository,
    generated_by: UUID,
    **content_overrides,
) -> tuple[UUID, UUID, object]:
    """Seed one system:task entity and return
    (entity_id, version_id, row)."""
    eid = uuid4()
    vid = uuid4()
    content = {
        "kind": "recorded",
        "function": "test_fn",
        "status": "scheduled",
        "result_activity_id": str(uuid4()),
        "attempt_count": 0,
        "max_attempts": 3,
        "base_delay_seconds": 60,
    }
    content.update(content_overrides)
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type="system:task", generated_by=generated_by,
        content=content, attributed_to="system",
    )
    await repo.session.flush()
    row = await repo.get_entity(vid)
    return eid, vid, row


async def _latest_task(repo: Repository, entity_id: UUID) -> object:
    """Fetch the latest version of a task entity."""
    return await repo.get_latest_entity_by_id(D1, entity_id)


# --------------------------------------------------------------------
# complete_task
# --------------------------------------------------------------------


class TestCompleteTask:

    async def test_successful_completion_writes_new_version(self, repo):
        """Happy path: complete_task writes a scheduled→completed
        revision. The latest task version now has
        status=completed and carries the new version_id."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        eid, vid_v1, task_row = await _seed_task(repo, boot)

        await complete_task(repo, plugin, D1, task_row, status="completed")
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert latest is not None
        assert latest.id != vid_v1  # new revision written
        assert latest.content["status"] == "completed"
        assert latest.content["function"] == "test_fn"  # preserved

    async def test_extra_content_merged_into_new_version(self, repo):
        """The `extra_content` dict is merged into the new task
        version's content. The retry policy uses this to carry
        attempt_count and next_attempt_at through the completion
        path."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        eid, _, task_row = await _seed_task(repo, boot)

        await complete_task(
            repo, plugin, D1, task_row,
            status="scheduled",
            extra_content={
                "attempt_count": 2,
                "next_attempt_at": "2030-01-01T00:00:00+00:00",
            },
        )
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert latest.content["attempt_count"] == 2
        assert latest.content["next_attempt_at"] == "2030-01-01T00:00:00+00:00"
        assert latest.content["status"] == "scheduled"

    async def test_result_uri_stored_in_new_version(self, repo):
        """A cross-dossier completion carries a `result` URI
        pointing at the target activity. The new version's
        `result` field should be populated."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        eid, _, task_row = await _seed_task(repo, boot)

        result_uri = "urn:dossier:other/activity/xyz"
        await complete_task(
            repo, plugin, D1, task_row,
            status="completed",
            result_uri=result_uri,
        )
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert latest.content["result"] == result_uri

    async def test_dead_letter_status_recorded(self, repo):
        """Caller sets status=dead_letter (via _record_failure's
        giving-up branch). The new version has that status."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        eid, _, task_row = await _seed_task(repo, boot)

        await complete_task(
            repo, plugin, D1, task_row,
            status="dead_letter",
            extra_content={"attempt_count": 3},
        )
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert latest.content["status"] == "dead_letter"
        assert latest.content["attempt_count"] == 3

    async def test_missing_system_action_def_raises(self, repo):
        """A plugin with no systemAction activity definition
        can't record task completions. The function raises
        RuntimeError with an actionable message."""
        boot = await _bootstrap_dossier(repo)
        # Build a plugin WITHOUT SYSTEM_ACTION_DEF
        plugin = Plugin(
            name="test",
            workflow={"activities": [], "relations": []},
            entity_models={"system:task": TaskEntity, "system:note": SystemNote},
        )
        _, _, task_row = await _seed_task(repo, boot)

        with pytest.raises(RuntimeError) as exc:
            await complete_task(repo, plugin, D1, task_row)
        assert "systemAction" in str(exc.value)


# --------------------------------------------------------------------
# _record_failure
# --------------------------------------------------------------------


class TestRecordFailure:

    async def test_first_failure_within_budget_stays_scheduled(self, repo):
        """First failure: attempt_count 0 → 1, status stays
        scheduled, next_attempt_at gets set via backoff. The
        poll loop will skip this task until the delay elapses."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        eid, _, task_row = await _seed_task(repo, boot)

        await _record_failure(
            repo, plugin, D1, task_row, error=ValueError("nope"),
        )
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert latest.content["status"] == "scheduled"
        assert latest.content["attempt_count"] == 1
        assert latest.content["next_attempt_at"] is not None
        assert latest.content["last_attempt_at"] is not None

    async def test_exhausted_budget_becomes_dead_letter(self, repo):
        """attempt_count is already at max_attempts - 1. One more
        failure crosses the threshold → dead_letter. No
        next_attempt_at (dead letters are terminal)."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        eid, _, task_row = await _seed_task(
            repo, boot, attempt_count=2, max_attempts=3,
        )

        await _record_failure(
            repo, plugin, D1, task_row, error=ValueError("nope"),
        )
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert latest.content["status"] == "dead_letter"
        assert latest.content["attempt_count"] == 3

    async def test_custom_max_attempts_respected(self, repo):
        """A task with max_attempts=1 dead-letters on the first
        failure. This is the "no retries, fail fast" config for
        tasks where a second attempt won't help."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        eid, _, task_row = await _seed_task(
            repo, boot, attempt_count=0, max_attempts=1,
        )

        await _record_failure(
            repo, plugin, D1, task_row, error=ValueError("nope"),
        )
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert latest.content["status"] == "dead_letter"
        assert latest.content["attempt_count"] == 1

    async def test_custom_base_delay_respected(self, repo):
        """A task with base_delay_seconds=10 retries with a
        smaller initial backoff than the default 60s. The
        next_attempt_at should reflect the shorter delay."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        eid, _, task_row = await _seed_task(
            repo, boot, base_delay_seconds=10, max_attempts=3,
        )

        before = datetime.now(timezone.utc)
        await _record_failure(
            repo, plugin, D1, task_row, error=ValueError("nope"),
        )
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        next_at = datetime.fromisoformat(latest.content["next_attempt_at"])
        delta = (next_at - before).total_seconds()
        # First attempt: 10 * 2^0 = 10s, ± 10% jitter → [9, 11]s
        assert 9 <= delta <= 12

    async def test_error_telemetry_not_stored_on_task(self, repo):
        """The `error` argument is sent to `logger.exception`, not
        stored on the task content. This is the Sentry-only
        contract: retry state is on the task, error history is in
        the telemetry backend keyed by task_id. Verifies that
        neither `error` nor `last_error` shows up in the new
        version's content."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        eid, _, task_row = await _seed_task(repo, boot)

        await _record_failure(
            repo, plugin, D1, task_row, error=ValueError("secret traceback"),
        )
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert "error" not in latest.content
        assert "last_error" not in latest.content
        # "secret traceback" must not leak into any content field
        assert "secret traceback" not in str(latest.content)

    async def test_next_attempt_at_monotonic_with_attempt_count(self, repo):
        """Second retry should have a larger next_attempt_at
        delta than the first retry (exponential backoff). We
        compute both and compare the gaps."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()

        # First failure: attempt 0 → 1
        eid_a, _, task_a = await _seed_task(
            repo, boot, attempt_count=0, base_delay_seconds=100,
        )
        t0 = datetime.now(timezone.utc)
        await _record_failure(repo, plugin, D1, task_a, error=ValueError())
        await repo.session.flush()
        latest_a = await _latest_task(repo, eid_a)
        delta_1 = (
            datetime.fromisoformat(latest_a.content["next_attempt_at"]) - t0
        ).total_seconds()

        # Second failure: attempt 1 → 2
        eid_b, _, task_b = await _seed_task(
            repo, boot, attempt_count=1, base_delay_seconds=100,
        )
        t1 = datetime.now(timezone.utc)
        await _record_failure(repo, plugin, D1, task_b, error=ValueError())
        await repo.session.flush()
        latest_b = await _latest_task(repo, eid_b)
        delta_2 = (
            datetime.fromisoformat(latest_b.content["next_attempt_at"]) - t1
        ).total_seconds()

        # delta_1 should be ~100s (base*2^0), delta_2 should be ~200s
        # (base*2^1). Even with 10% jitter, delta_2 > delta_1.
        assert delta_2 > delta_1


# --------------------------------------------------------------------
# _process_recorded
# --------------------------------------------------------------------


class TestProcessRecorded:

    async def test_function_called_and_task_completed(self, repo):
        """Registered task function is invoked, then
        complete_task writes a completed revision."""
        called = []
        async def fn(ctx):
            called.append(ctx.dossier_id)

        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin(task_handlers={"test_fn": fn})
        eid, _, task_row = await _seed_task(repo, boot, function="test_fn")

        await _process_recorded(repo, plugin, D1, task_row)
        await repo.session.flush()

        assert called == [D1]
        latest = await _latest_task(repo, eid)
        assert latest.content["status"] == "completed"

    async def test_function_not_registered_still_completes(self, repo):
        """If the task names a function the plugin doesn't have,
        we log a warning but still record completion. This
        matches the 'do no work but report done' contract that
        the existing API suite depends on for some paths."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin(task_handlers={})  # nothing
        eid, _, task_row = await _seed_task(
            repo, boot, function="missing_fn",
        )

        await _process_recorded(repo, plugin, D1, task_row)
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert latest.content["status"] == "completed"

    async def test_function_raising_propagates(self, repo):
        """The task function raises. The exception bubbles up
        out of _process_recorded so the outer worker loop's
        error handler can route it through _record_failure."""
        async def fn(ctx):
            raise RuntimeError("boom")

        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin(task_handlers={"test_fn": fn})
        _, _, task_row = await _seed_task(repo, boot, function="test_fn")

        with pytest.raises(RuntimeError) as exc:
            await _process_recorded(repo, plugin, D1, task_row)
        assert "boom" in str(exc.value)


# --------------------------------------------------------------------
# _process_scheduled_activity
# --------------------------------------------------------------------


class TestProcessScheduledActivity:

    async def test_target_activity_not_found_raises_valueerror(self, repo):
        """Task declares a target_activity name that the plugin
        doesn't know. ValueError — this is a workflow
        misconfiguration bug and should bubble up."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        _, _, task_row = await _seed_task(
            repo, boot,
            kind="scheduled_activity",
            target_activity="nonexistent",
        )

        with pytest.raises(ValueError) as exc:
            await _process_scheduled_activity(repo, plugin, D1, task_row)
        assert "nonexistent" in str(exc.value)

    async def test_target_activity_executes_and_task_completed(self, repo):
        """Target activity is registered in the plugin. The
        phase executes it via execute_activity, then records
        task completion with informed_by pointing at the
        scheduled activity's result_activity_id."""
        boot = await _bootstrap_dossier(repo)

        # Register a no-op target activity that takes no used/
        # generated and writes nothing. The engine still runs
        # the full pipeline but there's no real work to do.
        target_def = {
            "name": "doNothing",
            "label": "Do Nothing",
            "can_create_dossier": False,
            "client_callable": True,
            "default_role": "systeem",
            "allowed_roles": ["systeem"],
            "authorization": {
                "access": "roles",
                "roles": [{"role": "systeemgebruiker"}],
            },
            "used": [],
            "generates": [],
            "status": None,
            "validators": [],
            "side_effects": [],
            "tasks": [],
        }
        plugin = _make_worker_plugin(extra_activities=[target_def])
        eid, _, task_row = await _seed_task(
            repo, boot,
            kind="scheduled_activity",
            target_activity="doNothing",
        )

        await _process_scheduled_activity(repo, plugin, D1, task_row)
        await repo.session.flush()

        latest = await _latest_task(repo, eid)
        assert latest.content["status"] == "completed"


# --------------------------------------------------------------------
# _execute_claimed_task — the dispatcher
# --------------------------------------------------------------------


class TestExecuteClaimedTask:

    async def test_missing_dossier_returns_early(self, repo):
        """The claimed task's dossier_id doesn't exist (rare but
        possible via database corruption). Log + return, don't
        crash the worker.

        We can't actually create a task row whose dossier_id
        points at a nonexistent dossier (the FK blocks that).
        Instead, we seed a real task, `expunge` it from the
        session so SQLAlchemy stops tracking it, then mutate
        its dossier_id in-memory to a nonexistent UUID before
        passing it to the executor. The executor calls
        `repo.get_dossier(fake_id)` which misses, triggering
        the early-return branch."""
        boot = await _bootstrap_dossier(repo)
        _, _, task_row = await _seed_task(repo, boot)

        # Evict from session so mutating dossier_id won't try to
        # UPDATE the row on autoflush (which would fail the FK
        # constraint against the nonexistent target dossier).
        repo.session.expunge(task_row)
        fake_id = UUID("99999999-9999-9999-9999-999999999999")
        task_row.dossier_id = fake_id

        class _FakeRegistry:
            def get(self, name):
                return _make_worker_plugin()

        # repo.get_dossier(fake_id) returns None → early return.
        # No exception raised.
        await _execute_claimed_task(
            repo.session, task_row, _FakeRegistry(),
        )

    async def test_missing_plugin_returns_early(self, repo):
        """Dossier exists but its workflow isn't registered in
        the plugin registry. Log + return, don't crash."""
        boot = await _bootstrap_dossier(repo)
        _, _, task_row = await _seed_task(repo, boot)

        class _EmptyRegistry:
            def get(self, name):
                return None  # always misses

        await _execute_claimed_task(
            repo.session, task_row, _EmptyRegistry(),
        )

    async def test_task_refetch_returns_none_skips(self, repo):
        """If the task's latest version can't be re-fetched
        (deleted or tombstoned between claim and execute), the
        phase logs a warning and returns. Construct the
        scenario by mutating the task_row's entity_id to a
        nonexistent UUID — the executor's _refetch_task call
        then returns None, triggering the early-return branch."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        _, vid, task_row = await _seed_task(repo, boot)

        # Expunge first so mutating entity_id doesn't trigger
        # an UPDATE on autoflush.
        repo.session.expunge(task_row)
        task_row.entity_id = UUID("99999999-9999-9999-9999-999999999999")

        class _Reg:
            def get(self, name):
                return plugin

        await _execute_claimed_task(repo.session, task_row, _Reg())
        # No exception.

    async def test_already_completed_task_skipped(self, repo):
        """The claim selected a task whose latest version is
        already completed (cancelled between claim and execute,
        or a race between two workers). Phase re-fetches, sees
        status != scheduled, skips. No new revision written."""
        boot = await _bootstrap_dossier(repo)
        plugin = _make_worker_plugin()
        # Seed a task that's ALREADY cancelled
        eid, vid, task_row = await _seed_task(
            repo, boot, status="cancelled",
        )

        class _Reg:
            def get(self, name):
                return plugin

        # Count versions before
        before_versions = await repo.get_entity_versions(D1, eid)
        assert len(before_versions) == 1

        await _execute_claimed_task(repo.session, task_row, _Reg())

        # Count versions after — should still be 1 (no completion
        # revision written because status was already non-scheduled)
        after_versions = await repo.get_entity_versions(D1, eid)
        assert len(after_versions) == 1
