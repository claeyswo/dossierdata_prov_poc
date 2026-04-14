"""
Task worker.

Polls for due system:task entities and executes them.
Runs as a separate process: python -m gov_dossier_engine.worker

Task types:
  - recorded (type 2): call function, completeTask with result
  - scheduled_activity (type 3): execute_activity in same dossier, completeTask
  - cross_dossier_activity (type 4): call function for target, execute_activity
    in target dossier, completeTask in source dossier

All operations within a single DB transaction. If anything fails, everything rolls back.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from .app import load_config_and_registry, SYSTEM_USER
from .db import init_db, create_tables, get_session_factory
from .db.models import EntityRow, Repository
from .engine import execute_activity, ActivityContext, HandlerResult, _find_activity_def
from .entities import COMPLETE_TASK_ACTIVITY_DEF

logger = logging.getLogger("dossier.worker")


async def find_due_tasks(session) -> list[tuple[EntityRow, str]]:
    """Find all scheduled task entities that are due.
    
    Returns list of (task_entity, dossier_id) tuples.
    Only returns the latest version of each logical task entity.
    """
    now = datetime.now(timezone.utc).isoformat()
    result = await session.execute(
        select(EntityRow)
        .where(EntityRow.type == "system:task")
        .order_by(EntityRow.created_at)
    )
    all_tasks = list(result.scalars().all())

    # Group by logical entity, keep latest version
    latest_by_entity: dict[UUID, EntityRow] = {}
    for task in all_tasks:
        existing = latest_by_entity.get(task.entity_id)
        if not existing or (task.created_at and existing.created_at and task.created_at > existing.created_at):
            latest_by_entity[task.entity_id] = task

    due = []
    for task in latest_by_entity.values():
        if not task.content:
            continue
        if task.content.get("status") != "scheduled":
            continue
        scheduled_for = task.content.get("scheduled_for")
        if scheduled_for and scheduled_for > now:
            continue  # not yet due
        due.append(task)

    return due


async def check_cancelled(repo: Repository, task: EntityRow) -> bool:
    """Check if any cancel_if_activities have occurred after this task was created."""
    cancel_list = task.content.get("cancel_if_activities", [])
    if not cancel_list:
        return False

    activities = await repo.get_activities_for_dossier(task.dossier_id)
    for act in activities:
        if act.type in cancel_list:
            if act.created_at and task.created_at and act.created_at > task.created_at:
                return True
    return False


async def complete_task(
    repo: Repository,
    plugin,
    dossier_id: UUID,
    task: EntityRow,
    status: str = "completed",
    result_uri: str | None = None,
    error: str | None = None,
    informed_by: str | None = None,
):
    """Create a systemAction activity that generates a new version of the task entity + a note."""
    new_content = dict(task.content)
    new_content["status"] = status
    if result_uri:
        new_content["result"] = result_uri
    if error:
        new_content["error"] = error

    activity_id = uuid4()
    now = datetime.now(timezone.utc)

    await repo.ensure_agent("system", "systeem", "Systeem", {})

    activity_row = await repo.create_activity(
        activity_id=activity_id,
        dossier_id=dossier_id,
        type="systemAction",
        started_at=now,
        ended_at=now,
        informed_by=informed_by,
    )

    await repo.create_association(
        association_id=uuid4(),
        activity_id=activity_id,
        agent_id="system",
        agent_name="Systeem",
        agent_type="systeem",
        role="systeem",
    )

    # Create new version of task entity
    await repo.create_entity(
        version_id=uuid4(),
        entity_id=task.entity_id,
        dossier_id=dossier_id,
        type="system:task",
        generated_by=activity_id,
        content=new_content,
        derived_from=task.id,
        attributed_to="system",
    )

    # Create a note explaining the action
    fn_name = task.content.get("function", "") if task.content else ""
    note_text = f"Task {status}: {fn_name}" if fn_name else f"Task {status}"
    await repo.create_entity(
        version_id=uuid4(),
        entity_id=uuid4(),
        dossier_id=dossier_id,
        type="system:note",
        generated_by=activity_id,
        content={"text": note_text},
        attributed_to="system",
    )

    # Update cached status and eligible activities on dossier row
    from .engine import derive_status, compute_eligible_activities
    import json as _json
    current_status = await derive_status(repo, dossier_id)
    eligible = await compute_eligible_activities(plugin, repo, dossier_id)
    dossier = await repo.get_dossier(dossier_id)
    if dossier:
        dossier.cached_status = current_status
        dossier.eligible_activities = _json.dumps(eligible)

    return activity_id


async def process_task(task: EntityRow, registry, config):
    """Process a single due task within one transaction."""
    session_factory = get_session_factory()
    dossier_id = task.dossier_id
    task_content = task.content
    kind = task_content.get("kind")

    # Find the plugin for this dossier
    async with session_factory() as session:
        async with session.begin():
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                logger.error(f"Task {task.id}: dossier {dossier_id} not found")
                return

            plugin = registry.get(dossier.workflow)
            if not plugin:
                logger.error(f"Task {task.id}: plugin not found for workflow {dossier.workflow}")
                return

            # Re-fetch the task to get latest version within this transaction
            latest_tasks = await repo.get_entities_by_type(dossier_id, "system:task")
            current_task = None
            for t in latest_tasks:
                if t.entity_id == task.entity_id:
                    current_task = t
            if not current_task or not current_task.content:
                logger.warning(f"Task {task.id}: not found in re-fetch")
                return
            if current_task.content.get("status") != "scheduled":
                logger.info(f"Task {task.id}: already {current_task.content.get('status')}, skipping")
                return

            logger.info(f"Task {task.id}: processing kind={kind} function={task_content.get('function')}")

            # Check if cancelled by a recent activity
            if await check_cancelled(repo, current_task):
                logger.info(f"Task {task.id}: cancelled by activity")
                await complete_task(repo, plugin, dossier_id, current_task, status="cancelled")
                return

            try:
                if kind == "recorded":
                    # Type 2: call function, store result
                    fn_name = task_content.get("function")
                    fn = plugin.task_handlers.get(fn_name) if fn_name else None
                    if fn:
                        # Resolve all latest entities for context
                        all_latest = await repo.get_all_latest_entities(dossier_id)
                        resolved = {e.type: e for e in all_latest}
                        ctx = ActivityContext(repo, dossier_id, resolved, plugin.entity_models, plugin=plugin)
                        await fn(ctx)
                    else:
                        logger.warning(f"Task {task.id}: function '{fn_name}' not found")
                    await complete_task(repo, plugin, dossier_id, current_task, status="completed")
                    logger.info(f"Task {task.id}: recorded task '{fn_name}' completed")

                elif kind == "scheduled_activity":
                    # Type 3: execute activity in same dossier, then completeTask
                    target_activity_type = task_content.get("target_activity")
                    result_activity_id = UUID(task_content["result_activity_id"])

                    act_def = _find_activity_def(plugin, target_activity_type)
                    if not act_def:
                        raise ValueError(f"Activity definition not found: {target_activity_type}")

                    # Extract anchor from task content so the engine's
                    # auto-resolve can fall back to it when the informing
                    # activity's scope doesn't cover all needed types.
                    task_anchor_id_str = task_content.get("anchor_entity_id")
                    task_anchor_type = task_content.get("anchor_type")
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
                        informed_by=str(current_task.generated_by) if current_task.generated_by else None,
                        caller="system",
                        anchor_entity_id=task_anchor_id,
                        anchor_type=task_anchor_type,
                    )
                    await repo.session.flush()

                    await complete_task(
                        repo, plugin, dossier_id, current_task,
                        status="completed",
                        informed_by=str(result_activity_id),
                    )
                    logger.info(f"Task {task.id}: scheduled activity {target_activity_type} executed")

                elif kind == "cross_dossier_activity":
                    # Type 4: call function for target, execute in target dossier, completeTask in source
                    fn_name = task_content.get("function")
                    fn = plugin.task_handlers.get(fn_name) if fn_name else None
                    if not fn:
                        raise ValueError(f"Task function not found: {fn_name}")

                    ctx = ActivityContext(repo, dossier_id, {}, plugin.entity_models, plugin=plugin)
                    task_result = await fn(ctx)

                    # task_result should have target_dossier_id and optionally content
                    target_dossier_id = UUID(task_result.target_dossier_id)
                    target_activity_type = task_content.get("target_activity")
                    result_activity_id = UUID(task_content["result_activity_id"])

                    # Find target dossier's plugin
                    target_dossier = await repo.get_dossier(target_dossier_id)
                    target_plugin = registry.get(target_dossier.workflow) if target_dossier else plugin

                    target_act_def = _find_activity_def(target_plugin, target_activity_type)
                    if not target_act_def:
                        raise ValueError(f"Target activity not found: {target_activity_type}")

                    # Build used block with reference to source dossier
                    source_uri = f"urn:dossier:{dossier_id}"
                    informed_by_uri = f"urn:dossier:{dossier_id}/activity/{current_task.generated_by}" if current_task.generated_by else None

                    # Build generated items from task_result if provided
                    generated_items = []
                    if hasattr(task_result, 'content') and task_result.content:
                        generates = target_act_def.get("generates", [])
                        if generates:
                            generated_items = [{
                                "entity": f"{generates[0]}/{uuid4()}@{uuid4()}",
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
                    caller="system",)
                    await repo.session.flush()

                    # completeTask in source dossier, informed by the activity in target dossier
                    result_uri = f"urn:dossier:{target_dossier_id}/activity/{result_activity_id}"
                    await complete_task(
                        repo, plugin, dossier_id, current_task,
                        status="completed",
                        result_uri=result_uri,
                        informed_by=result_uri,
                    )
                    logger.info(f"Task {task.id}: cross-dossier activity {target_activity_type} in {target_dossier_id}")

                else:
                    logger.warning(f"Task {task.id}: unknown kind '{kind}'")

            except Exception as e:
                logger.error(f"Task {task.id} failed: {e}", exc_info=True)
                raise


async def worker_loop(config_path: str = "config.yaml", poll_interval: int = 10, once: bool = False):
    """Main worker loop."""
    config, registry = load_config_and_registry(config_path)

    db_url = config.get("database", {}).get("url", "sqlite+aiosqlite:///./dossiers.db")
    await init_db(db_url)
    await create_tables()

    logger.info(f"Worker started. Poll interval: {poll_interval}s. Once: {once}")

    while True:
        session_factory = get_session_factory()

        # Find due tasks (read-only scan)
        async with session_factory() as session:
            due_tasks = await find_due_tasks(session)

        if due_tasks:
            logger.info(f"Found {len(due_tasks)} due tasks")

        for task in due_tasks:
            try:
                await process_task(task, registry, config)
            except Exception as e:
                logger.error(f"Task {task.id} processing failed: {e}")
                # Mark as failed in a clean transaction
                try:
                    async with session_factory() as session:
                        async with session.begin():
                            repo = Repository(session)
                            dossier = await repo.get_dossier(task.dossier_id)
                            plugin = registry.get(dossier.workflow) if dossier else None
                            if plugin:
                                await complete_task(
                                    repo, plugin, task.dossier_id, task,
                                    status="failed",
                                    error=str(e),
                                )
                except Exception as e2:
                    logger.error(f"Failed to mark task {task.id} as failed: {e2}")

        if once:
            break

        await asyncio.sleep(poll_interval)


def main():
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    once = "--once" in sys.argv
    config_path = "config.yaml"

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--config" and i + 1 < len(sys.argv):
            config_path = sys.argv[i + 1]

    poll_interval = 10
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--interval" and i + 1 < len(sys.argv):
            poll_interval = int(sys.argv[i + 1])

    asyncio.run(worker_loop(config_path=config_path, poll_interval=poll_interval, once=once))


if __name__ == "__main__":
    main()
