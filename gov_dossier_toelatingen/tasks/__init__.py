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


TASK_HANDLERS = {
    "send_ontvangstbevestiging": send_ontvangstbevestiging,
    "log_beslissing_genomen": log_beslissing_genomen,
    "log_organisatie_aangeduid": log_organisatie_aangeduid,
    "send_behandelaar_notificatie": send_behandelaar_notificatie,
}
