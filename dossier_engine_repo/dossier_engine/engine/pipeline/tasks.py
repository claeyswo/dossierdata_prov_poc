"""
Task scheduling and cancellation.

After persistence and side effects, the engine processes the activity's
task list — both YAML-declared (`activity_def.tasks`) and
handler-appended (`HandlerResult.tasks`). Tasks fall into four kinds:

* **fire_and_forget** — execute a `task_handler` function inline, no
  record. Errors are swallowed (the name is literal: fire-and-forget).
* **recorded** — execute a `task_handler` function and record a
  `system:task` entity capturing the result. (Note: the recorded
  variant is currently scheduled but the actual execution is left to
  the worker — this engine phase only writes the task entity.)
* **scheduled_activity** — schedule a future activity to run via the
  worker. The task entity carries `target_activity` and
  `scheduled_for`, plus `cancel_if_activities` controlling when it
  should be cancelled.
* **cross_dossier_activity** — same as scheduled_activity but the
  worker is expected to dispatch it against a different dossier.

Three pieces of cross-cutting machinery apply to recorded /
scheduled / cross-dossier tasks:

1. **Anchors.** A task may declare an `anchor_type` in YAML (or the
   handler may supply an explicit `anchor_entity_id`). The anchor
   entity_id scopes the task to a specific logical entity — only
   activities that touch that entity will trigger cancellation
   (step 16) or supersession (in `process_tasks`). Auto-resolution
   walks `resolve_from_trigger` over the activity's used+generated
   to find a matching entity; if no anchor candidate exists and one
   was declared, we raise 500 — the handler must provide it.

2. **Supersession.** Unless `allow_multiple: true`, scheduling a new
   task with the same `target_activity` and same `anchor_entity_id`
   as an existing scheduled task supersedes the old one — its
   content is rewritten with `status: superseded` so it won't be
   picked up by the worker.

3. **Cancellation** (step 16). After the new tasks are written, walk
   every existing `system:task` entity in the dossier and check
   whether the canceling activity (the one we just ran) is in its
   `cancel_if_activities` list. If so, AND the canceling activity
   actually advanced the task's anchored entity (generated a new
   version of it), AND the task was scheduled before this activity
   started, mark it cancelled.

The "must have generated a new version" clause is critical: it means
that merely *consulting* an entity (putting it in `used`) is not
enough to cancel a scheduled task. State must actually advance.
"""

from __future__ import annotations

import logging
from datetime import timezone
from uuid import UUID, uuid4

from ..context import ActivityContext, HandlerResult
from ..errors import ActivityError
from ..lookups import resolve_from_trigger
from ..state import ActivityState
from ...db.models import EntityRow
from ...entities import TaskEntity

_log = logging.getLogger("dossier.engine.tasks")


async def process_tasks(state: ActivityState) -> None:
    """Schedule every task the activity declared (YAML + handler-appended).

    Walks `state.activity_def["tasks"]` and `state.handler_result.tasks`
    in order. For each task:

    * **fire_and_forget**: invoke the registered task_handler function
      inline, swallowing any exception.
    * **other kinds**: resolve the anchor (handler override → engine
      auto-fill), supersede any existing scheduled task with the same
      target+anchor (unless `allow_multiple`), then write a new
      `system:task` entity carrying the full task descriptor.

    Reads:  state.activity_def, state.handler_result, state.plugin,
            state.repo, state.dossier_id, state.activity_id,
            state.resolved_entities
    Writes: nothing on `state` directly; persists `system:task`
            entities to the database.
    Raises: 500 if a task declared `anchor_type` but the activity
            didn't touch any entity of that type and the handler
            didn't supply an explicit `anchor_entity_id`.
    """
    all_task_defs = list(state.activity_def.get("tasks", []))
    if isinstance(state.handler_result, HandlerResult):
        all_task_defs.extend(state.handler_result.tasks)

    for task_def in all_task_defs:
        task_kind = task_def.get("kind", "recorded")

        if task_kind == "fire_and_forget":
            await _fire_and_forget(state, task_def)
        else:
            await _schedule_recorded_task(state, task_def, task_kind)


async def _fire_and_forget(state: ActivityState, task_def: dict) -> None:
    """Execute a fire-and-forget task handler inline.

    Errors are swallowed by design — fire_and_forget is for things
    like "send a notification" where a transient failure shouldn't
    bring down the entire activity.
    """
    fn_name = task_def.get("function")
    if not fn_name:
        return
    fn = state.plugin.task_handlers.get(fn_name)
    if fn is None:
        return

    ctx = ActivityContext(
        repo=state.repo,
        dossier_id=state.dossier_id,
        used_entities=state.resolved_entities,
        entity_models=state.plugin.entity_models,
        plugin=state.plugin,
    )
    try:
        await fn(ctx)
    except Exception:
        # Fire-and-forget: swallow by design (see docstring). Log with
        # traceback so "a notification never arrived" is investigable
        # rather than silent. Logged at WARNING, not ERROR, because the
        # activity itself did succeed — this handler's failure doesn't
        # change any invariant the caller cared about. Sentry's
        # LoggingIntegration picks WARNING up as a breadcrumb and (if
        # promoted) an event; either way it stops being invisible.
        _log.warning(
            f"fire_and_forget task '{fn_name}' raised (swallowed by design)",
            exc_info=True,
        )


async def _schedule_recorded_task(
    state: ActivityState, task_def: dict, task_kind: str,
) -> None:
    """Resolve anchor, handle supersession, persist the task entity."""
    anchor_entity_id = await _resolve_anchor(state, task_def)

    # Resolve scheduled_for: accepts "+20d"/"+2h"/"+45m"/"+3w" relative
    # offsets (resolved against state.now) or absolute ISO 8601. Raises
    # ValueError on a malformed value so YAML typos fail loudly at
    # activity execution time instead of silently scheduling for "now".
    from ..scheduling import resolve_scheduled_for
    try:
        resolved_scheduled_for = resolve_scheduled_for(
            task_def.get("scheduled_for"), state.now,
        )
    except ValueError as e:
        raise ActivityError(
            500,
            f"Bad task declaration in workflow YAML: {e}",
        ) from None

    task_content = TaskEntity(
        kind=task_kind,
        function=task_def.get("function"),
        target_activity=task_def.get("target_activity"),
        scheduled_for=resolved_scheduled_for,
        cancel_if_activities=task_def.get("cancel_if_activities", []),
        allow_multiple=task_def.get("allow_multiple", False),
        result_activity_id=str(uuid4()),
        status="scheduled",
        anchor_entity_id=str(anchor_entity_id) if anchor_entity_id else None,
        anchor_type=task_def.get("anchor_type"),
    )

    if not task_content.allow_multiple and task_content.target_activity:
        await _supersede_matching(state, task_content)

    await state.repo.create_entity(
        version_id=uuid4(),
        entity_id=uuid4(),
        dossier_id=state.dossier_id,
        type="system:task",
        generated_by=state.activity_id,
        content=task_content.model_dump(),
        attributed_to="system",
    )


async def _resolve_anchor(state: ActivityState, task_def: dict) -> UUID | None:
    """Resolve a task's anchor entity_id.

    Order:
    1. Handler override (`task_def["anchor_entity_id"]`)
    2. Engine auto-fill via `resolve_from_trigger` (looks at what
       this activity used or generated, finds the first row matching
       the declared anchor_type)
    3. None — only allowed when no anchor_type was declared

    If anchor_type was declared but neither the handler nor
    auto-resolution can produce an entity_id, raise 500. This is a
    workflow misconfiguration: the activity asked for a task scoped
    to an anchor it doesn't actually touch.
    """
    handler_anchor = task_def.get("anchor_entity_id")
    if handler_anchor is not None:
        return UUID(str(handler_anchor))

    anchor_type = task_def.get("anchor_type")
    if not anchor_type:
        return None

    anchor_row = await resolve_from_trigger(
        state.repo, state.activity_id, state.dossier_id, anchor_type,
    )
    if anchor_row is not None:
        return anchor_row.entity_id

    # Anchor required but unresolvable — fail loud.
    raise ActivityError(
        500,
        f"Cannot resolve anchor for task "
        f"{task_def.get('target_activity') or task_def.get('function')}: "
        f"activity '{state.activity_def['name']}' did not touch any "
        f"entity of type '{anchor_type}'. The handler must supply "
        f"anchor_entity_id explicitly.",
    )


async def _supersede_matching(
    state: ActivityState, new_task: TaskEntity,
) -> None:
    """Mark any existing scheduled task with the same target+anchor as
    superseded.

    Two tasks supersede each other only if they share both
    `target_activity` and `anchor_entity_id` (None == None matches
    global-scope tasks). The supersession writes a new revision of the
    existing task entity with `status: superseded`.

    Uses a flat `get_entities_by_type` query and dedupes in Python
    instead of a SQL GROUP BY — faster for the small task lists we
    typically deal with.
    """
    rows = await state.repo.get_entities_by_type(state.dossier_id, "system:task")
    latest: dict[UUID, EntityRow] = {}
    for row in rows:
        existing = latest.get(row.entity_id)
        if existing is None or row.created_at > existing.created_at:
            latest[row.entity_id] = row

    for existing in latest.values():
        if not existing.content:
            continue
        if existing.content.get("status") != "scheduled":
            continue
        if existing.content.get("target_activity") != new_task.target_activity:
            continue
        if existing.content.get("anchor_entity_id") != new_task.anchor_entity_id:
            continue

        # Same target, same anchor → supersede.
        superseded_content = dict(existing.content)
        superseded_content["status"] = "superseded"
        await state.repo.create_entity(
            version_id=uuid4(),
            entity_id=existing.entity_id,
            dossier_id=state.dossier_id,
            type="system:task",
            generated_by=state.activity_id,
            content=superseded_content,
            derived_from=existing.id,
            attributed_to="system",
        )


async def cancel_matching_tasks(state: ActivityState) -> None:
    """Walk every existing scheduled task and cancel those whose
    `cancel_if_activities` includes the activity we just ran.

    Cancellation is anchor-scoped: an anchored task is cancelled only
    if the canceling activity actually generated a new version of the
    anchored entity (state must have advanced — merely consulting the
    entity via `used` is not enough). None-anchored tasks are global-
    scope and cancel whenever the target activity runs.

    Tasks created at-or-after this activity's start time are skipped
    — we don't cancel tasks the activity itself just scheduled.

    Reads:  state.repo, state.dossier_id, state.activity_def,
            state.activity_id, state.generated, state.now
    Writes: nothing on `state`; persists cancellation revisions of
            `system:task` entities.
    """
    rows = await state.repo.get_entities_by_type(state.dossier_id, "system:task")
    latest_by_eid: dict[UUID, EntityRow] = {}
    for row in rows:
        existing = latest_by_eid.get(row.entity_id)
        if existing is None or row.created_at > existing.created_at:
            latest_by_eid[row.entity_id] = row

    # The set of logical entity_ids this activity generated — used for
    # the anchor-scope check below.
    advanced_entity_ids: set[UUID] = {g["entity_id"] for g in state.generated}

    for task_entity in latest_by_eid.values():
        if not task_entity.content:
            continue
        if task_entity.content.get("status") != "scheduled":
            continue

        cancel_list = task_entity.content.get("cancel_if_activities", [])
        # Compare by local name so bare names from handler code
        # (``cancel_if_activities: ["vervolledigAanvraag"]``) match
        # the qualified name on the current activity definition
        # (``oe:vervolledigAanvraag``). Plugin authors can write
        # either form without caring about normalization.
        from ...activity_names import local_name
        current_local = local_name(state.activity_def["name"])
        cancel_locals = {local_name(n) for n in cancel_list}
        if current_local not in cancel_locals:
            continue

        # Anchor scope: anchored tasks only cancel when this activity
        # actually advanced the task's anchored entity.
        anchor_id_str = task_entity.content.get("anchor_entity_id")
        if anchor_id_str is not None:
            try:
                task_anchor_id = UUID(anchor_id_str)
            except (ValueError, TypeError):
                # Data corruption: the engine wrote this via
                # ``str(anchor_entity_id)`` (see ``_schedule_recorded_task``),
                # so a malformed value here means the row was tampered
                # with or the schema changed incompatibly. Log loudly
                # so it surfaces in Sentry; the skip is a safe default
                # (cancellation simply doesn't fire) but the cause
                # needs investigating.
                _log.error(
                    "Malformed anchor_entity_id %r on task %s — "
                    "skipping cancellation check",
                    anchor_id_str, task_entity.id,
                )
                continue  # malformed anchor — skip
            if task_anchor_id not in advanced_entity_ids:
                continue  # this activity didn't advance the anchor entity

        # Skip tasks created at-or-after this activity's start. Don't
        # cancel things this activity itself just scheduled.
        task_created = task_entity.created_at
        if task_created is None:
            continue
        if task_created.tzinfo is None:
            task_created = task_created.replace(tzinfo=timezone.utc)
        if task_created >= state.now:
            continue

        cancelled_content = dict(task_entity.content)
        cancelled_content["status"] = "cancelled"
        await state.repo.create_entity(
            version_id=uuid4(),
            entity_id=task_entity.entity_id,
            dossier_id=state.dossier_id,
            type="system:task",
            generated_by=state.activity_id,
            content=cancelled_content,
            derived_from=task_entity.id,
            attributed_to="system",
        )
