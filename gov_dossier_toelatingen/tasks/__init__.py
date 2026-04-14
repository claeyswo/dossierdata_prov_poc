"""
Task handlers for toelatingen.

These are type 2 (recorded) task functions. The worker picks them up,
executes the function, and creates a completeTask activity.

In production these would send emails, call external systems, etc.
For POC they log the action.
"""

from __future__ import annotations

import logging

from gov_dossier_engine.engine import ActivityContext

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
    """Move uploaded bijlagen from temp to permanent dossier location in the File Service."""
    aanvraag = context.get_typed("oe:aanvraag")
    if not aanvraag or not aanvraag.bijlagen:
        logger.info("[TASK] move_bijlagen: no bijlagen to move")
        return

    dossier_id = str(context.dossier_id)

    # In production, read file_service URL from config
    file_service_url = "http://localhost:8001"

    import aiohttp
    async with aiohttp.ClientSession() as session:
        for bijlage in aanvraag.bijlagen:
            try:
                async with session.post(
                    f"{file_service_url}/internal/move",
                    params={"file_id": bijlage.file_id, "dossier_id": dossier_id},
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        logger.info(f"[TASK] Moved bijlage {bijlage.file_id} → {dossier_id}/bijlagen/")
                    else:
                        error = await resp.text()
                        logger.warning(f"[TASK] Failed to move bijlage {bijlage.file_id}: {error}")
            except Exception as e:
                logger.error(f"[TASK] Error moving bijlage {bijlage.file_id}: {e}")


TASK_HANDLERS = {
    "send_ontvangstbevestiging": send_ontvangstbevestiging,
    "log_beslissing_genomen": log_beslissing_genomen,
    "log_organisatie_aangeduid": log_organisatie_aangeduid,
    "send_behandelaar_notificatie": send_behandelaar_notificatie,
    "move_bijlagen_to_permanent": move_bijlagen_to_permanent,
}
