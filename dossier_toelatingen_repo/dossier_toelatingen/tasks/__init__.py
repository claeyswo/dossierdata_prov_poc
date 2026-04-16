"""
Task handlers for toelatingen.

These are type 2 (recorded) task functions. The worker picks them up,
executes the function, and creates a completeTask activity.

In production these would send emails, call external systems, etc.
For POC they log the action.
"""

from __future__ import annotations

import logging

from dossier_engine.engine import ActivityContext

logger = logging.getLogger("toelatingen.tasks")


async def send_ontvangstbevestiging(context: ActivityContext):
    """Send a confirmation receipt to the aanvrager."""
    aanvraag = context.get_typed("oe:aanvraag")
    if aanvraag:
        logger.info(f"[TASK] Ontvangstbevestiging voor aanvraag '{aanvraag.onderwerp}' "
                     f"aan aanvrager in gemeente {aanvraag.gemeente}")
    else:
        logger.info("[TASK] Ontvangstbevestiging (geen aanvraag gevonden)")


async def log_beslissing_genomen(context: ActivityContext):
    """Log that a decision was taken."""
    beslissing = context.get_typed("oe:beslissing")
    if beslissing:
        logger.info(f"[TASK] Beslissing genomen: {beslissing.beslissing} op {beslissing.datum}")
    else:
        logger.info("[TASK] Beslissing genomen (geen beslissing entiteit)")


async def log_organisatie_aangeduid(context: ActivityContext):
    """Log that a responsible organization was assigned."""
    org = context.get_typed("oe:verantwoordelijke_organisatie")
    if org:
        logger.info(f"[TASK] Verantwoordelijke organisatie aangeduid: {org.uri}")
    else:
        logger.info("[TASK] Verantwoordelijke organisatie aangeduid (geen entiteit)")


async def send_behandelaar_notificatie(context: ActivityContext):
    """Send a notification to the assigned behandelaar."""
    behandelaar = context.get_typed("oe:behandelaar")
    if behandelaar:
        logger.info(f"[TASK] Notificatie naar behandelaar: {behandelaar.uri}")
    else:
        logger.info("[TASK] Notificatie naar behandelaar (geen entiteit)")


async def move_bijlagen_to_permanent(context: ActivityContext):
    """Move newly-added bijlagen from temp to permanent in the File Service.

    Operates on the aanvraag version produced by the activity that
    scheduled this task (NOT the current latest). This matters because
    a subsequent bewerkAanvraag may already have superseded the version
    that introduced these bijlagen, and the current latest may have a
    different (or empty) bijlagen array. The files that need moving
    are the ones the SCHEDULING activity introduced.

    Dedup: bijlagen inherited from the parent version were moved when
    their introducing revision ran this task. Only file IDs that are
    NEW in this specific version are moved. For the very first version
    of an aanvraag (no parent), all bijlagen are moved.

    Diffing at the worker side avoids pointless roundtrips to the file
    service (the file service's move endpoint is idempotent, so it's
    also a correctness-neutral optimisation — but it makes the task
    logs readable, and it means a phantom file_id introduced by a
    later revision fails loudly instead of hiding behind an
    `already_permanent: true` from a successful earlier move of the
    same file_id).
    """
    if context.triggering_activity_id is None:
        logger.warning("[TASK] move_bijlagen: no triggering_activity_id, cannot locate correct aanvraag version")
        return

    # Find the aanvraag version that the scheduling activity generated.
    generated = await context.repo.get_entities_generated_by_activity(
        context.triggering_activity_id
    )
    aanvraag_row = next((e for e in generated if e.type == "oe:aanvraag"), None)
    if aanvraag_row is None:
        logger.info("[TASK] move_bijlagen: scheduling activity generated no aanvraag version")
        return

    bijlagen = (aanvraag_row.content or {}).get("bijlagen") or []
    if not bijlagen:
        logger.info("[TASK] move_bijlagen: aanvraag version has no bijlagen")
        return

    current_file_ids = {
        b["file_id"] for b in bijlagen if isinstance(b, dict) and "file_id" in b
    }

    # Diff against parent version. If no parent (first revision), every bijlage is new.
    prior_file_ids: set[str] = set()
    if aanvraag_row.derived_from is not None:
        parent = await context.repo.get_entity(aanvraag_row.derived_from)
        if parent is not None and parent.content:
            prior_file_ids = {
                b["file_id"]
                for b in parent.content.get("bijlagen", [])
                if isinstance(b, dict) and "file_id" in b
            }

    to_move = current_file_ids - prior_file_ids
    if not to_move:
        logger.info(
            "[TASK] move_bijlagen: all %d bijlage(n) inherited from parent version, nothing to move",
            len(current_file_ids),
        )
        return

    logger.info(
        "[TASK] move_bijlagen: moving %d new bijlage(n) (of %d total in this version)",
        len(to_move), len(current_file_ids),
    )

    dossier_id = str(context.dossier_id)

    import os
    file_service_url = os.environ.get("FILE_SERVICE_URL", "http://localhost:8001")

    import aiohttp
    async with aiohttp.ClientSession() as session:
        for bijlage in bijlagen:
            if not isinstance(bijlage, dict):
                continue
            fid = bijlage.get("file_id")
            if fid not in to_move:
                continue
            try:
                async with session.post(
                    f"{file_service_url}/internal/move",
                    params={"file_id": fid, "dossier_id": dossier_id},
                ) as resp:
                    if resp.status == 200:
                        logger.info(f"[TASK] Moved bijlage {fid} → {dossier_id}/bijlagen/")
                    else:
                        error = await resp.text()
                        logger.warning(f"[TASK] Failed to move bijlage {fid}: {error}")
            except Exception as e:
                logger.error(f"[TASK] Error moving bijlage {fid}: {e}")


TASK_HANDLERS = {
    "send_ontvangstbevestiging": send_ontvangstbevestiging,
    "log_beslissing_genomen": log_beslissing_genomen,
    "log_organisatie_aangeduid": log_organisatie_aangeduid,
    "send_behandelaar_notificatie": send_behandelaar_notificatie,
    "move_bijlagen_to_permanent": move_bijlagen_to_permanent,
}
