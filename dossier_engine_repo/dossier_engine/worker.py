"""
Task worker.

Polls for due system:task entities and executes them.
Runs as a separate process: python -m dossier_engine.worker

Task types:
  - recorded (type 2): call function, completeTask with result
  - scheduled_activity (type 3): execute_activity in same dossier, completeTask
  - cross_dossier_activity (type 4): call function for target, execute_activity
    in target dossier, completeTask in source dossier

All operations within a single DB transaction. If anything fails, everything rolls back.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select

from .app import load_config_and_registry, SYSTEM_USER
from .db import init_db, get_session_factory
from .db.models import EntityRow, Repository
from .engine import ActivityContext, Caller, execute_activity
from .engine.refs import EntityRef
from .sentry_integration import (
    init_sentry,
    capture_task_retry,
    capture_task_dead_letter,
    capture_worker_loop_crash,
)

logger = logging.getLogger("dossier.worker")


def _parse_scheduled_for(value: str | None) -> datetime | None:
    """Parse a `scheduled_for` value into an aware datetime.

    The engine writes `scheduled_for` as an ISO 8601 string. Depending
    on who produced it, the string can look like `2026-05-01T00:00:00Z`
    (Python-ish with trailing Z), `2026-05-01T00:00:00+00:00` (also
    Python-ish, datetime.isoformat with UTC tz), or `2026-05-01T00:00:00`
    (naive, which we treat as UTC). Comparing these as strings is
    wrong — `"Z" > "+"` lexically, so a "+00:00"-formatted now can
    compare greater than a "Z"-formatted scheduled_for even when
    they're the same instant.

    Returns None for None or for strings that don't parse.
    """
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _build_scheduled_task_query(for_update: bool = False):
    """Shared SQLAlchemy query builder for due-task selection.

    Filters at the SQL layer to: `type = 'system:task'`, latest version
    per logical entity_id (via a `MAX(created_at)` subquery), and
    `status = 'scheduled'` (via JSONB field extraction that translates
    to `content ->> 'status' = 'scheduled'` on Postgres).

    The `scheduled_for` and `next_attempt_at` fields live in JSONB but
    are compared in Python after hydration because ISO 8601 lexical
    comparison is incorrect (`"Z"` > `"+"` is wrong but string-true).
    `_parse_scheduled_for` handles the parsing.

    When `for_update=True`, adds `FOR UPDATE OF entities SKIP LOCKED`
    so the worker's poll transaction locks the candidate rows and
    concurrent workers skip over them. The `OF entities` clause is
    required because Postgres rejects `FOR UPDATE` on a query whose
    set includes an aggregated subquery — `OF` tells Postgres to lock
    only the outer `entities` table, leaving the subquery's aggregate
    rows alone.
    """
    latest_per_entity = (
        select(
            EntityRow.entity_id.label("eid"),
            func.max(EntityRow.created_at).label("latest_at"),
        )
        .where(EntityRow.type == "system:task")
        .group_by(EntityRow.entity_id)
        .subquery()
    )
    stmt = (
        select(EntityRow)
        .join(
            latest_per_entity,
            (EntityRow.entity_id == latest_per_entity.c.eid)
            & (EntityRow.created_at == latest_per_entity.c.latest_at),
        )
        .where(EntityRow.type == "system:task")
        .where(EntityRow.content["status"].as_string() == "scheduled")
    )
    if for_update:
        stmt = stmt.with_for_update(skip_locked=True, of=EntityRow)
    return stmt


def _is_task_due(task: EntityRow, now: datetime) -> tuple[bool, datetime]:
    """Return (is_due, sort_key) for a task row.

    A task is due when:
    * It has no `scheduled_for` (treated as immediately due), AND
    * It has no `next_attempt_at` (first-attempt, no retry delay), OR
    * Both `scheduled_for <= now` and `next_attempt_at <= now` when
      either is present.

    `sort_key` is used to order the due set so the oldest overdue
    task drains first. Priority (earliest first):
    1. `next_attempt_at` if set (retry delay has priority — we want to
       drain retries as soon as they're ready so they don't pile up).
    2. `scheduled_for` if set.
    3. `datetime.min` otherwise (unscheduled = treat as ancient).
    """
    if not task.content:
        return False, datetime.min.replace(tzinfo=timezone.utc)
    scheduled_for = _parse_scheduled_for(task.content.get("scheduled_for"))
    next_attempt_at = _parse_scheduled_for(task.content.get("next_attempt_at"))

    if scheduled_for is not None and scheduled_for > now:
        return False, scheduled_for
    if next_attempt_at is not None and next_attempt_at > now:
        return False, next_attempt_at

    sort_key = (
        next_attempt_at
        or scheduled_for
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return True, sort_key


async def find_due_tasks(session) -> list[EntityRow]:
    """Find all scheduled task entities that are due — read-only,
    non-locking. Used by `--once` drain mode and by observability
    tooling that wants to inspect the backlog without interfering
    with running workers.

    Returns a list sorted by "most overdue first" — see
    `_is_task_due` for the sort-key rule.
    """
    now = datetime.now(timezone.utc)
    stmt = await _build_scheduled_task_query(for_update=False)
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())

    due: list[tuple[datetime, EntityRow]] = []
    for task in candidates:
        is_due, sort_key = _is_task_due(task, now)
        if is_due:
            due.append((sort_key, task))

    due.sort(key=lambda pair: pair[0])
    return [task for _, task in due]


async def _claim_one_due_task(session) -> EntityRow | None:
    """Select and lock one due task row inside the caller's
    transaction.

    Strategy: `SELECT ... FOR UPDATE OF entities SKIP LOCKED LIMIT 5`
    to pull a small batch of candidate rows from the SQL layer, then
    Python-filter through `_is_task_due` and return the first
    actually-due row. Rows that don't pass the Python filter stay
    locked until the transaction commits or rolls back, but the
    bounded `LIMIT 5` caps the over-lock blast radius to 5 rows per
    worker per cycle. Acceptable for a system with many more due
    tasks than concurrent workers.

    Returns None if the query returned nothing or if no candidate
    passes the `scheduled_for` / `next_attempt_at` time filters.
    `None` signals the poll loop "nothing claimable this cycle" — it
    may mean the backlog is empty or it may mean everything that
    was SKIP-LOCKED skippable was genuinely locked; either way the
    loop moves on to the next poll interval.
    """
    now = datetime.now(timezone.utc)
    stmt = (await _build_scheduled_task_query(for_update=True)).limit(5)
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())

    for task in candidates:
        is_due, _ = _is_task_due(task, now)
        if is_due:
            return task
    return None


def _compute_next_attempt_at(
    attempt_count: int,
    base_delay_seconds: int,
    now: datetime,
) -> datetime:
    """Compute the next retry time for a task that just failed its
    `attempt_count`'th attempt.

    Uses exponential backoff with ±10% jitter:
        delay = base * 2**(attempt_count - 1) * (1 + random(-0.1, 0.1))

    `attempt_count` is the count AFTER the failure — so a task that
    just failed its first attempt passes `attempt_count=1` and gets
    a delay of ~base, a second failure (attempt_count=2) gets ~2×base,
    a third gets ~4×base, and so on. The jitter prevents the thundering
    herd effect where many tasks that failed at the same time all
    retry at the same moment.
    """
    exponent = max(0, attempt_count - 1)
    base_delay = base_delay_seconds * (2 ** exponent)
    jitter = random.uniform(-0.1, 0.1)
    delay = base_delay * (1 + jitter)
    return now + timedelta(seconds=delay)


async def _record_failure(
    repo: Repository,
    plugin,
    dossier_id: UUID,
    task: EntityRow,
    error: Exception,
) -> None:
    """Record a task execution failure by writing a new task version
    through the engine's `systemAction` pathway.

    Increments `attempt_count`. If the new count has reached the
    task's `max_attempts` budget, the new task version is written
    with `status = "dead_letter"` — the task is terminal and won't
    be picked up by the poll loop again. Otherwise, the new version
    stays in `status = "scheduled"` but gains a `next_attempt_at`
    field set by `_compute_next_attempt_at`, so the poll loop skips
    it until the retry delay elapses.

    Error telemetry goes to the Python logging system via
    `logger.exception(...)`, which captures the full traceback and
    sends it through whatever handlers are installed — in production
    that's typically Sentry via `sentry_sdk`'s logging integration.
    The task content itself carries only operational state
    (`attempt_count`, `last_attempt_at`, `next_attempt_at`); the
    full error history for a task is reconstructed from the
    telemetry backend keyed by `task_id`.

    The new task version is written via `complete_task` (which itself
    goes through `execute_activity` — see sub-step 5), so the
    failure write path inherits all the engine's invariants and the
    retry is visible in the PROV graph as a regular `systemAction`.
    """
    now = datetime.now(timezone.utc)
    current_count = task.content.get("attempt_count")
    attempt_count = (current_count if current_count is not None else 0) + 1
    max_attempts_val = task.content.get("max_attempts")
    max_attempts = max_attempts_val if max_attempts_val is not None else 3
    base_delay_val = task.content.get("base_delay_seconds")
    base_delay = base_delay_val if base_delay_val is not None else 60

    # Context carried into log records so Sentry (or whatever backend)
    # can index events by task/dossier/attempt.
    log_extra = {
        "task_id": str(task.id),
        "task_entity_id": str(task.entity_id),
        "dossier_id": str(dossier_id),
        "function": task.content.get("function"),
        "kind": task.content.get("kind"),
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
    }

    extra_content = {
        "attempt_count": attempt_count,
        "last_attempt_at": now.isoformat(),
    }

    if attempt_count >= max_attempts:
        # ERROR-level with exc_info=True so the stack trace rides
        # along to whatever log backend is configured. Sentry's
        # logging integration promotes ERROR+exc_info events to
        # full Sentry events with structured tags from `extra`.
        logger.error(
            "Task %s: attempt %d/%d failed, moving to dead_letter",
            task.id, attempt_count, max_attempts,
            exc_info=error, extra=log_extra,
        )
        # Explicit Sentry event with per-task fingerprint: each
        # dead-lettered task is its own issue (operator needs to
        # investigate/fix/requeue individually).
        capture_task_dead_letter(
            exc=error,
            task_id=task.id,
            task_entity_id=task.entity_id,
            dossier_id=dossier_id,
            function=task.content.get("function"),
            attempt_count=attempt_count,
            max_attempts=max_attempts,
        )
        await complete_task(
            repo, plugin, dossier_id, task,
            status="dead_letter",
            extra_content=extra_content,
        )
    else:
        next_attempt_at = _compute_next_attempt_at(
            attempt_count, base_delay, now,
        )
        extra_content["next_attempt_at"] = next_attempt_at.isoformat()
        # WARNING-level for transient retries — Sentry typically
        # drops warnings by default so the noise floor stays
        # reasonable during flaky-infrastructure events. ERROR comes
        # only when we actually give up (dead_letter branch above).
        logger.warning(
            "Task %s: attempt %d/%d failed, retry at %s",
            task.id, attempt_count, max_attempts, next_attempt_at.isoformat(),
            exc_info=error, extra=log_extra,
        )
        # Explicit Sentry event with per-function fingerprint: all
        # retries of the same task function collapse into ONE issue
        # (event count reflects retry rate — signal, not noise).
        capture_task_retry(
            exc=error,
            task_id=task.id,
            task_entity_id=task.entity_id,
            dossier_id=dossier_id,
            function=task.content.get("function"),
            attempt_count=attempt_count,
            max_attempts=max_attempts,
        )
        await complete_task(
            repo, plugin, dossier_id, task,
            status="scheduled",  # back to scheduled for retry
            extra_content=extra_content,
        )


async def complete_task(
    repo: Repository,
    plugin,
    dossier_id: UUID,
    task: EntityRow,
    status: str = "completed",
    result_uri: str | None = None,
    informed_by: str | None = None,
    extra_content: dict | None = None,
):
    """Record task completion by running a `systemAction` activity
    through the engine's full pipeline.

    Previously this function hand-wrote the activity row, association,
    task entity revision, and note entity directly via `Repository`
    calls, and then manually recomputed `cached_status` and
    `eligible_activities` on the dossier row. That was a second write
    path in parallel with the engine, which meant the post-activity
    hook didn't fire, the schema-versioning and disjoint-invariant
    checks were skipped, and any new engine invariant that gets added
    in the future would silently not apply to task completions.

    Now every write goes through `execute_activity` with the built-in
    `SYSTEM_ACTION_DEF`. The engine's pipeline runs normally: the task
    revision is validated against the TaskEntity Pydantic model, the
    note is validated against SystemNote, derivation chains are
    checked, the post-activity hook runs, and the finalization phase
    updates the cached status and eligible activities automatically.
    The worker no longer has a special-case write path — it's just
    another `execute_activity` caller.

    `extra_content` is a dict of additional fields to merge into the
    new task version's content. The retry policy uses this to carry
    `attempt_count`, `next_attempt_at`, and `last_attempt_at`
    through the completion path. Error telemetry does NOT flow
    through here — it goes to `logger.exception` and out to the
    configured logging backend (typically Sentry).
    """
    # Build the new task content with the status transition (and
    # optional result URI, plus any extra fields from the caller).
    # This is just a Python dict mutation on a copy of the existing
    # content; the engine will validate the dict against TaskEntity
    # when resolve_generated runs.
    new_content = dict(task.content)
    new_content["status"] = status
    if result_uri:
        new_content["result"] = result_uri
    if extra_content:
        new_content.update(extra_content)

    # Generate a fresh version UUID for the new task entity version.
    # The logical entity_id stays the same — we're creating a
    # revision, not a new logical task.
    new_task_version_id = uuid4()
    prev_task_ref = str(EntityRef(
        type="system:task", entity_id=task.entity_id, version_id=task.id,
    ))
    new_task_ref = str(EntityRef(
        type="system:task", entity_id=task.entity_id, version_id=new_task_version_id,
    ))

    # Build the explanatory note. It's a new logical entity, not a
    # revision of anything, so both the entity_id and version_id are
    # fresh UUIDs and there's no derivedFrom link.
    fn_name = task.content.get("function", "") if task.content else ""
    note_text = f"Task {status}: {fn_name}" if fn_name else f"Task {status}"
    new_note_entity_id = uuid4()
    new_note_version_id = uuid4()
    note_ref = str(EntityRef(
        type="system:note",
        entity_id=new_note_entity_id,
        version_id=new_note_version_id,
    ))

    systemaction_def = plugin.find_activity_def("systemAction")
    if not systemaction_def:
        raise RuntimeError(
            "systemAction activity definition not found in plugin — "
            "the engine should have registered it at startup"
        )

    await execute_activity(
        plugin=plugin,
        activity_def=systemaction_def,
        repo=repo,
        dossier_id=dossier_id,
        activity_id=uuid4(),
        user=SYSTEM_USER,
        role="systeem",
        used_items=[],
        generated_items=[
            {
                "entity": new_task_ref,
                "content": new_content,
                "derivedFrom": prev_task_ref,
            },
            {
                "entity": note_ref,
                "content": {"text": note_text},
            },
        ],
        informed_by=informed_by,
        caller=Caller.SYSTEM,
    )


async def process_task(task: EntityRow, registry, config):
    """Legacy entry point — opens its own session and calls the
    session-aware inner function. Kept for callers and tests that
    want to process a task without owning the transaction themselves.
    The production worker loop uses `_execute_claimed_task` directly
    so the claim-lock-execute dance all happens in one transaction.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        async with session.begin():
            await _execute_claimed_task(session, task, registry)


async def _execute_claimed_task(session, task: EntityRow, registry) -> None:
    """Execute a task within an already-open transaction.

    The caller is responsible for the `async with session.begin()`
    block. This lets the worker loop hold the row-level lock acquired
    by `_claim_one_due_task` through the entire execution — if the
    caller opened a new transaction per task, the lock would be
    released between claim and execute and two workers could race.

    Responsibilities inside this function:
    * Resolve the dossier and plugin.
    * Re-fetch the task for latest version. The re-fetch is how we
      observe cancellations: the pipeline's `cancel_matching_tasks`
      runs synchronously as part of every activity that could cancel
      a task, so if the task was cancelled between when the poll
      selected it and when we got the row lock, the latest version's
      status will be `cancelled` (not `scheduled`) and we return
      early. No separate cancel check is needed — the status guard
      below handles it uniformly with other "status already changed"
      cases.
    * Dispatch on `kind` to the appropriate `_process_*` handler.

    Raises on execution failure. The caller's error handler in the
    worker loop catches the exception and routes it through
    `_record_failure`, which decides retry-vs-dead-letter and writes
    the new task version via `complete_task → execute_activity`.
    """
    repo = Repository(session)
    dossier_id = task.dossier_id

    dossier = await repo.get_dossier(dossier_id)
    if not dossier:
        logger.error(f"Task {task.id}: dossier {dossier_id} not found")
        return

    plugin = registry.get(dossier.workflow)
    if not plugin:
        logger.error(
            f"Task {task.id}: plugin not found for "
            f"workflow {dossier.workflow}"
        )
        return

    current_task = await _refetch_task(repo, dossier_id, task.entity_id)
    if current_task is None:
        logger.warning(f"Task {task.id}: not found in re-fetch")
        return
    if current_task.content.get("status") != "scheduled":
        logger.info(
            f"Task {task.id}: already "
            f"{current_task.content.get('status')}, skipping"
        )
        return

    kind = current_task.content.get("kind")
    logger.info(
        f"Task {task.id}: processing kind={kind} "
        f"function={current_task.content.get('function')}"
    )

    if kind == "recorded":
        await _process_recorded(repo, plugin, dossier_id, current_task)
    elif kind == "scheduled_activity":
        await _process_scheduled_activity(
            repo, plugin, dossier_id, current_task,
        )
    elif kind == "cross_dossier_activity":
        await _process_cross_dossier(
            repo, plugin, registry, dossier_id, current_task,
        )
    else:
        logger.warning(f"Task {task.id}: unknown kind '{kind}'")


async def _refetch_task(
    repo: Repository,
    dossier_id: UUID,
    task_entity_id: UUID,
) -> EntityRow | None:
    """Pull the latest version of one logical task entity inside the
    current transaction.

    Returns None if the task doesn't exist or has no content.

    History note: an earlier implementation used
    `get_entities_by_type(dossier_id, "system:task")` and then looped
    through the results in Python looking for a matching entity_id.
    That version was buggy in two ways — it fetched every task row
    in the dossier just to find one, and
    `get_entities_by_type` orders by `created_at ASC` and the loop
    returned the first match, so for any task with multiple versions
    it returned the OLDEST version instead of the latest. The bug
    was invisible for a long time because the completion path was
    only reached by single-version tasks in the test suite, and the
    retry path in `_record_failure` doesn't go through `_refetch_task`
    at all — it gets the claimed (latest) task from the outer loop
    directly. The bug only surfaced when the requeue feature created
    a multi-version task that then hit the success path.
    """
    task = await repo.get_latest_entity_by_id(dossier_id, task_entity_id)
    if task is None or not task.content:
        return None
    return task


async def _process_recorded(
    repo: Repository,
    plugin,
    dossier_id: UUID,
    task: EntityRow,
) -> None:
    """Type 2 — recorded task: call a plugin function and record
    completion. The function may do anything (side effects, external
    calls, reading entities through the ActivityContext) but its
    return value is ignored — completion is recorded as a status
    transition on the task entity, not as a separate result row.
    """
    fn_name = task.content.get("function")
    fn = plugin.task_handlers.get(fn_name) if fn_name else None
    if fn:
        all_latest = await repo.get_all_latest_entities(dossier_id)
        resolved = {e.type: e for e in all_latest}
        ctx = ActivityContext(
            repo, dossier_id, resolved, plugin.entity_models, plugin=plugin,
            triggering_activity_id=task.generated_by,
        )
        await fn(ctx)
    else:
        logger.warning(f"Task {task.id}: function '{fn_name}' not found")

    await complete_task(repo, plugin, dossier_id, task, status="completed")
    logger.info(f"Task {task.id}: recorded task '{fn_name}' completed")


async def _process_scheduled_activity(
    repo: Repository,
    plugin,
    dossier_id: UUID,
    task: EntityRow,
) -> None:
    """Type 3 — scheduled activity: execute an activity in the same
    dossier at the scheduled time, then record completion.

    The target activity runs with the task's anchor (if any) passed
    through so the engine's auto-resolve can locate the anchored
    entity even when the informing activity's scope didn't cover
    every needed type.
    """
    target_activity_type = task.content.get("target_activity")
    result_activity_id = UUID(task.content["result_activity_id"])

    act_def = plugin.find_activity_def(target_activity_type)
    if not act_def:
        raise ValueError(
            f"Activity definition not found: {target_activity_type}"
        )

    task_anchor_id_str = task.content.get("anchor_entity_id")
    task_anchor_type = task.content.get("anchor_type")
    task_anchor_id = UUID(task_anchor_id_str) if task_anchor_id_str else None

    await execute_activity(
        plugin=plugin,
        activity_def=act_def,
        repo=repo,
        dossier_id=dossier_id,
        activity_id=result_activity_id,
        user=SYSTEM_USER,
        role="systeem",
        used_items=[],
        generated_items=[],
        informed_by=str(task.generated_by) if task.generated_by else None,
        caller=Caller.SYSTEM,
        anchor_entity_id=task_anchor_id,
        anchor_type=task_anchor_type,
    )
    await repo.session.flush()

    await complete_task(
        repo, plugin, dossier_id, task,
        status="completed",
        informed_by=str(result_activity_id),
    )
    logger.info(
        f"Task {task.id}: scheduled activity "
        f"{target_activity_type} executed"
    )


async def _process_cross_dossier(
    repo: Repository,
    plugin,
    registry,
    dossier_id: UUID,
    task: EntityRow,
) -> None:
    """Type 4 — cross-dossier activity: call a plugin function to
    determine the target dossier, execute the target activity there,
    then record completion in the source dossier.

    PROV links the source and target via URIs: the target activity's
    `used` block carries a `urn:dossier:{source_id}` reference, and
    its `informed_by` points at the source activity URI. The source
    dossier's completeTask in turn points at the target activity URI
    so the graph closes both ways.
    """
    fn_name = task.content.get("function")
    fn = plugin.task_handlers.get(fn_name) if fn_name else None
    if not fn:
        raise ValueError(f"Task function not found: {fn_name}")

    ctx = ActivityContext(
        repo, dossier_id, {}, plugin.entity_models, plugin=plugin,
    )
    task_result = await fn(ctx)

    target_dossier_id = UUID(task_result.target_dossier_id)
    target_activity_type = task.content.get("target_activity")
    result_activity_id = UUID(task.content["result_activity_id"])

    target_dossier = await repo.get_dossier(target_dossier_id)
    target_plugin = registry.get(target_dossier.workflow) if target_dossier else plugin

    target_act_def = target_plugin.find_activity_def(target_activity_type)
    if not target_act_def:
        raise ValueError(f"Target activity not found: {target_activity_type}")

    source_uri = f"urn:dossier:{dossier_id}"
    from .prov_iris import activity_full_iri
    informed_by_uri = (
        activity_full_iri(dossier_id, task.generated_by)
        if task.generated_by else None
    )

    generated_items: list[dict] = []
    if hasattr(task_result, "content") and task_result.content:
        generates = target_act_def.get("generates", [])
        if generates:
            generated_items = [{
                "entity": str(EntityRef(
                    type=generates[0],
                    entity_id=uuid4(),
                    version_id=uuid4(),
                )),
                "content": task_result.content,
            }]

    await execute_activity(
        plugin=target_plugin,
        activity_def=target_act_def,
        repo=repo,
        dossier_id=target_dossier_id,
        activity_id=result_activity_id,
        user=SYSTEM_USER,
        role="systeem",
        used_items=[{"entity": source_uri}],
        generated_items=generated_items,
        informed_by=informed_by_uri,
        caller=Caller.SYSTEM,
    )
    await repo.session.flush()

    result_uri = activity_full_iri(target_dossier_id, result_activity_id)
    await complete_task(
        repo, plugin, dossier_id, task,
        status="completed",
        result_uri=result_uri,
        informed_by=result_uri,
    )
    logger.info(
        f"Task {task.id}: cross-dossier activity "
        f"{target_activity_type} in {target_dossier_id}"
    )


async def worker_loop(config_path: str = "config.yaml", poll_interval: int = 10, once: bool = False):
    """Main worker loop.

    Two nested loops:

    * **Outer loop** — controls the poll cadence. Sleeps `poll_interval`
      seconds between drain passes, or until SIGTERM arrives. Exits
      on SIGTERM or after a single pass in `--once` mode.

    * **Inner drain loop** — repeatedly claims one task at a time
      via `_claim_one_due_task`, executes it inside the same
      transaction that holds the row lock, and commits. Breaks when
      `_claim_one_due_task` returns None (nothing claimable this
      cycle — either backlog drained or everything remaining is
      locked by other workers).

    The claim-lock-execute pattern gives us concurrency safety for
    multi-worker deployments: `SELECT ... FOR UPDATE OF entities
    SKIP LOCKED` means worker A's locked row is invisible to worker
    B's next claim attempt. When A commits (success) or rolls back
    (failure), the lock releases and B's subsequent claim either sees
    the new `completed` / `dead_letter` status (and skips the row)
    or sees `scheduled` with updated `next_attempt_at` (and respects
    the retry delay). No two workers ever execute the same task
    version concurrently.

    Signal handling: SIGTERM and SIGINT set an `asyncio.Event`. The
    outer loop's interruptible sleep (`asyncio.wait_for` on the
    event) returns immediately when the signal arrives. The inner
    drain loop also checks the event at the top of each iteration,
    so a SIGTERM mid-drain finishes the in-flight task cleanly (its
    transaction runs to completion) and then exits without starting
    the next one. We never interrupt a task mid-transaction —
    doing so would leak locks and potentially leave the dossier
    state in a half-written form.

    Failure handling: if `_execute_claimed_task` raises, the error
    is routed through `_record_failure` in a *fresh* transaction.
    The original transaction is rolled back (so the locked row's
    content state isn't touched), and the fresh transaction writes
    a new task version with the retry decision (retry with backoff,
    or dead_letter). The new task version goes through
    `complete_task → execute_activity` so it gets validated and the
    post-activity hook fires.
    """
    config, registry = load_config_and_registry(config_path)

    db_url = config.get("database", {}).get("url")
    if not db_url:
        raise RuntimeError(
            "database.url is required in config (Postgres connection string)"
        )
    await init_db(db_url)
    # Schema is managed by Alembic migrations (run via the API startup
    # or `alembic upgrade head`). The worker does not create or migrate
    # tables — it only needs the engine connection.

    # Initialize Sentry if SENTRY_DSN is set. No-op otherwise.
    # Placed after config load so deployments can override DSN via
    # config in the future if they want to, though env var wins for now.
    init_sentry()

    shutdown = asyncio.Event()

    def _on_signal(signum, _frame):
        logger.info(f"Worker received signal {signum}, shutting down gracefully")
        shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)

    logger.info(f"Worker started. Poll interval: {poll_interval}s. Once: {once}")

    session_factory = get_session_factory()

    try:
        await _worker_loop_body(session_factory, registry, shutdown, poll_interval, once)
    except (KeyboardInterrupt, SystemExit):
        # Not a crash — these are expected ways to stop the worker.
        raise
    except Exception as exc:
        # Top-level worker-loop crash. This is the "the worker itself
        # died" signal — distinct from the per-task retry/dead_letter
        # events handled inside the loop. Single fingerprint so all
        # such crashes group into one Sentry issue.
        logger.exception("Worker loop crashed")
        capture_worker_loop_crash(exc)
        raise
    finally:
        logger.info("Worker stopped")


async def _worker_loop_body(session_factory, registry, shutdown, poll_interval: int, once: bool):
    """Extracted body of the poll/drain loop. See `worker_loop` for
    the top-level orchestration (config load, DB init, Sentry init,
    signal wiring, top-level try/except)."""

    while not shutdown.is_set():
        # Inner drain loop — keep claiming and executing one task at
        # a time until _claim_one_due_task returns None (nothing
        # claimable this cycle). Each iteration is its own session
        # and its own transaction; the lock held by the SELECT FOR
        # UPDATE persists for the lifetime of that transaction and
        # is released on commit or rollback.
        processed_this_cycle = 0
        while not shutdown.is_set():
            task_for_failure_path: EntityRow | None = None
            failure: Exception | None = None

            async with session_factory() as session:
                async with session.begin():
                    task = await _claim_one_due_task(session)
                    if task is None:
                        break  # nothing claimable — leave the drain loop

                    try:
                        await _execute_claimed_task(session, task, registry)
                        processed_this_cycle += 1
                    except Exception as e:
                        # Capture the exception so we can handle it in a
                        # fresh transaction below. The `async with
                        # session.begin()` will roll back this transaction
                        # on the way out because we're re-raising — no,
                        # wait, we don't want to re-raise, we want the
                        # transaction to roll back cleanly and then handle
                        # failure separately. Do that by catching here
                        # and remembering the task + exception, then
                        # exiting the inner `begin()` block by falling
                        # through to the end of the `with`. That commits
                        # the (empty) transaction, which is fine because
                        # _claim_one_due_task only did a SELECT.
                        logger.error(
                            f"Task {task.id} execution failed: {e}",
                            exc_info=True,
                        )
                        task_for_failure_path = task
                        failure = e

            # If execution failed, record the failure in a fresh
            # transaction. The original session/transaction from the
            # claim is already closed — its SELECT-only work committed
            # cleanly — and the failure write path needs its own
            # transaction to land the new task version through
            # execute_activity.
            if task_for_failure_path is not None and failure is not None:
                try:
                    async with session_factory() as fail_session:
                        async with fail_session.begin():
                            fail_repo = Repository(fail_session)
                            fail_dossier = await fail_repo.get_dossier(
                                task_for_failure_path.dossier_id,
                            )
                            fail_plugin = (
                                registry.get(fail_dossier.workflow)
                                if fail_dossier else None
                            )
                            if fail_plugin:
                                await _record_failure(
                                    fail_repo,
                                    fail_plugin,
                                    task_for_failure_path.dossier_id,
                                    task_for_failure_path,
                                    failure,
                                )
                except Exception as e2:
                    logger.error(
                        f"Task {task_for_failure_path.id}: failed to record "
                        f"failure (will be retried by next poll): {e2}",
                        exc_info=True,
                    )
                processed_this_cycle += 1  # count as drained to avoid spinning

        if processed_this_cycle:
            logger.info(f"Drain cycle: processed {processed_this_cycle} tasks")

        if once:
            break

        # Interruptible sleep between poll cycles.
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass


async def _select_dead_lettered_tasks(
    session,
    dossier_id: UUID | None = None,
    task_entity_id: UUID | None = None,
) -> list[EntityRow]:
    """Select dead-lettered tasks (latest version per logical task
    where `status = 'dead_letter'`), optionally filtered by dossier
    and/or task entity id.

    Extracted from `requeue_dead_letters` so integration tests can
    exercise the selection logic against a real database without
    triggering the config-load / `init_db` bootstrap that the main
    entry point does. The two callers — `requeue_dead_letters` and
    the test suite — share the same query shape, so a regression in
    one is a regression in both.

    The query mirrors `_build_scheduled_task_query`: a `MAX(created_at)
    per entity_id` subquery identifies the latest version of each
    logical task, and the outer query joins against it and filters
    on the JSONB status field. The FOR UPDATE variant isn't used
    here because the requeue is a single administrative operation —
    no concurrent-worker locking is needed.
    """
    latest_per_entity = (
        select(
            EntityRow.entity_id.label("eid"),
            func.max(EntityRow.created_at).label("latest_at"),
        )
        .where(EntityRow.type == "system:task")
        .group_by(EntityRow.entity_id)
        .subquery()
    )
    stmt = (
        select(EntityRow)
        .join(
            latest_per_entity,
            (EntityRow.entity_id == latest_per_entity.c.eid)
            & (EntityRow.created_at == latest_per_entity.c.latest_at),
        )
        .where(EntityRow.type == "system:task")
        .where(EntityRow.content["status"].as_string() == "dead_letter")
    )
    if dossier_id is not None:
        stmt = stmt.where(EntityRow.dossier_id == dossier_id)
    if task_entity_id is not None:
        stmt = stmt.where(EntityRow.entity_id == task_entity_id)

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def requeue_dead_letters(
    config_path: str,
    dossier_id: UUID | None = None,
    task_entity_id: UUID | None = None,
) -> int:
    """Requeue dead-lettered tasks by writing fresh task revisions
    with reset retry state.

    Scope filters (mutually inclusive):
    * `dossier_id=None, task_entity_id=None` — every dead-lettered
      task across every dossier
    * `dossier_id=X` — every dead-lettered task in one dossier
    * `task_entity_id=Y` — one specific task by its logical entity id
    * `dossier_id=X, task_entity_id=Y` — both filters applied (the
      task must be in the given dossier AND match the entity id)

    Semantics. Each matching task gets a new version written via a
    `systemAction` activity per dossier. The new version has:

    * `status = "scheduled"`  — claimable again by the poll loop
    * `attempt_count = 0`     — fresh retry budget
    * `next_attempt_at = None` — no retry delay, immediately due
    * `last_attempt_at` preserved from the dead-lettered version so
      operators can still see when the task last tried
    * `scheduled_for` preserved from the original task — it's the
      historical record of when the task was first queued, not a
      retry-scheduling field

    The requeue goes through `execute_activity` (like every other
    task-content write now) so each dossier's requeue operation is
    auditable in its PROV graph as a `systemAction` with N task
    revisions plus one `system:note` explaining the bulk requeue.

    Returns the total number of tasks requeued across all dossiers.
    """
    config, registry = load_config_and_registry(config_path)
    db_url = config.get("database", {}).get("url")
    if not db_url:
        raise RuntimeError(
            "database.url is required in config (Postgres connection string)"
        )
    await init_db(db_url)
    # Schema is managed by Alembic migrations (run via the API startup
    # or `alembic upgrade head`). The worker does not create or migrate
    # tables — it only needs the engine connection.

    session_factory = get_session_factory()

    async with session_factory() as session:
        dead_letters = await _select_dead_lettered_tasks(
            session, dossier_id=dossier_id, task_entity_id=task_entity_id,
        )

    if not dead_letters:
        logger.info("requeue_dead_letters: no dead-lettered tasks match")
        return 0

    # Group by dossier so each dossier's requeue is a single
    # systemAction. Writing the requeue as one activity per dossier
    # matches the auditing story — an operator running a bulk requeue
    # against the whole database gets one PROV event per dossier
    # touched, listing every task that was requeued.
    by_dossier: dict[UUID, list[EntityRow]] = {}
    for task in dead_letters:
        by_dossier.setdefault(task.dossier_id, []).append(task)

    logger.info(
        "requeue_dead_letters: requeuing %d task(s) across %d dossier(s)",
        len(dead_letters), len(by_dossier),
    )

    total = 0
    for d_id, tasks in by_dossier.items():
        async with session_factory() as session:
            async with session.begin():
                repo = Repository(session)
                dossier = await repo.get_dossier(d_id)
                if not dossier:
                    logger.error(
                        "requeue_dead_letters: dossier %s not found, "
                        "skipping %d tasks", d_id, len(tasks),
                    )
                    continue
                plugin = registry.get(dossier.workflow)
                if not plugin:
                    logger.error(
                        "requeue_dead_letters: plugin not found for "
                        "workflow %s, skipping %d tasks",
                        dossier.workflow, len(tasks),
                    )
                    continue

                systemaction_def = plugin.find_activity_def("systemAction")
                if not systemaction_def:
                    raise RuntimeError(
                        "systemAction activity definition not found — "
                        "engine should register it at startup"
                    )

                generated_items: list[dict] = []
                task_refs_for_note: list[str] = []
                for task in tasks:
                    # Build a fresh-start revision: status back to
                    # scheduled, attempt_count reset, next_attempt_at
                    # cleared so the poll loop treats it as immediately
                    # due on the scheduled_for axis. last_attempt_at
                    # preserved for the "when did this last try"
                    # diagnostic query. scheduled_for preserved as
                    # historical record.
                    new_content = dict(task.content)
                    new_content["status"] = "scheduled"
                    new_content["attempt_count"] = 0
                    new_content["next_attempt_at"] = None
                    new_version_id = uuid4()
                    generated_items.append({
                        "entity": str(EntityRef(
                            type="system:task",
                            entity_id=task.entity_id,
                            version_id=new_version_id,
                        )),
                        "content": new_content,
                        "derivedFrom": str(EntityRef(
                            type="system:task",
                            entity_id=task.entity_id,
                            version_id=task.id,
                        )),
                    })
                    task_refs_for_note.append(str(task.entity_id))

                # One system:note per bulk requeue, describing the
                # scope and listing the task entity ids.
                note_entity_id = uuid4()
                note_version_id = uuid4()
                scope_desc = []
                if dossier_id is not None:
                    scope_desc.append(f"dossier={dossier_id}")
                if task_entity_id is not None:
                    scope_desc.append(f"task={task_entity_id}")
                scope_str = ", ".join(scope_desc) if scope_desc else "all dossiers"
                generated_items.append({
                    "entity": str(EntityRef(
                        type="system:note",
                        entity_id=note_entity_id,
                        version_id=note_version_id,
                    )),
                    "content": {
                        "text": (
                            f"Operator requeue of {len(tasks)} dead-lettered "
                            f"task(s) (scope: {scope_str}). Task entity ids: "
                            f"{task_refs_for_note}"
                        ),
                    },
                })

                await execute_activity(
                    plugin=plugin,
                    activity_def=systemaction_def,
                    repo=repo,
                    dossier_id=d_id,
                    activity_id=uuid4(),
                    user=SYSTEM_USER,
                    role="systeem",
                    used_items=[],
                    generated_items=generated_items,
                    caller=Caller.SYSTEM,
                )

        logger.info(
            "requeue_dead_letters: dossier %s — requeued %d task(s)",
            d_id, len(tasks),
        )
        total += len(tasks)

    logger.info("requeue_dead_letters: done, %d task(s) requeued total", total)
    return total


def main():
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="python -m dossier_engine.worker",
        description=(
            "Dossier task worker. Polls for due system:task entities and "
            "executes them (recorded functions, scheduled activities, "
            "cross-dossier activities). Runs against the same database as "
            "the dossier API."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to the deployment's config.yaml. If omitted, resolves "
            "the path via the installed dossier_app package."
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Poll interval in seconds between scans for due tasks (default: 10).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain all currently-due tasks and exit instead of polling forever.",
    )
    parser.add_argument(
        "--requeue-dead-letters",
        action="store_true",
        help=(
            "Requeue dead-lettered tasks: write fresh revisions with "
            "status=scheduled, attempt_count=0, next_attempt_at cleared. "
            "Scope defaults to all dead-lettered tasks across all "
            "dossiers; narrow with --dossier and/or --task. The requeue "
            "runs once and exits (does not start a drain cycle). Use "
            "a separate `--once` invocation afterward if you want to "
            "immediately execute the requeued tasks."
        ),
    )
    parser.add_argument(
        "--dossier",
        default=None,
        help=(
            "Scope filter for --requeue-dead-letters: only requeue "
            "dead-lettered tasks belonging to the given dossier UUID."
        ),
    )
    parser.add_argument(
        "--task",
        default=None,
        help=(
            "Scope filter for --requeue-dead-letters: only requeue the "
            "task with the given logical entity UUID (system:task "
            "entity_id, not version_id)."
        ),
    )
    args = parser.parse_args()

    # Default config path via installed dossier_app package, same
    # pattern file_service uses. Lets the worker launch from any cwd.
    config_path = args.config
    if config_path is None:
        try:
            import dossier_app
            config_path = str(Path(dossier_app.__file__).parent / "config.yaml")
        except ImportError:
            config_path = "config.yaml"

    # --requeue-dead-letters is an admin one-shot action. It shares
    # the config/db bootstrap with the polling loop but runs a
    # different top-level coroutine and exits when done.
    if args.requeue_dead_letters:
        dossier_uuid = UUID(args.dossier) if args.dossier else None
        task_uuid = UUID(args.task) if args.task else None
        asyncio.run(requeue_dead_letters(
            config_path=config_path,
            dossier_id=dossier_uuid,
            task_entity_id=task_uuid,
        ))
        return

    if args.dossier or args.task:
        parser.error(
            "--dossier and --task are only valid with --requeue-dead-letters"
        )

    asyncio.run(worker_loop(
        config_path=config_path,
        poll_interval=args.interval,
        once=args.once,
    ))


if __name__ == "__main__":
    main()
