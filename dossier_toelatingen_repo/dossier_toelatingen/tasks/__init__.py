"""
Task handlers for toelatingen.

These are type 2 (recorded) task functions. The worker picks them up,
executes the function, and creates a completeTask activity.

In production these would send emails, call external systems, etc.
For POC they log the action.
"""

from __future__ import annotations

import logging

from dossier_engine.audit import emit_dossier_audit
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

    # Per-file failures accumulate here. After the loop, any non-empty
    # list becomes a RuntimeError so the worker's recorded-task retry
    # machinery kicks in — a transient file-service outage should
    # recover on retry without human intervention, because
    # /internal/move is idempotent (successfully-moved files are no-ops
    # on subsequent calls).
    #
    # Before Bug 30's fix this list didn't exist: a per-file exception
    # was caught with a bare `except Exception` and logged at ERROR
    # without exc_info, the loop continued, and the task was marked
    # completed even when half its work had failed. Downloads against
    # the aanvraag would 404 forever, invisibly to operators.
    failures: list[tuple[str, str]] = []

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
                        logger.info(
                            f"[TASK] Moved bijlage {fid} → {dossier_id}/bijlagen/"
                        )
                    elif resp.status == 403:
                        # Dossier-binding mismatch: the file service
                        # rejected a cross-dossier graft attempt. Three
                        # possible causes — client bug (frontend reused a
                        # file_id from a different dossier), stale token,
                        # actual attack. All three deserve SIEM attention:
                        # the file service blocked the data leak, but a
                        # human (or Wazuh rule) should see that the
                        # attempt happened.
                        #
                        # Emit dossier.denied attributed to the agent of
                        # the triggering activity (via context.triggering_user,
                        # plumbed through ActivityContext per the
                        # two-field attribution model — worker runs AS
                        # the system, but this denial is ABOUT the
                        # person whose activity referenced the problematic
                        # file_id). Then count as a failure so the task
                        # fails loudly — persistent 403 means a corrupted
                        # aanvraag that needs operator attention, not a
                        # tolerated steady state.
                        error = await resp.text()
                        emit_dossier_audit(
                            action="dossier.denied",
                            user=context.triggering_user,
                            dossier_id=context.dossier_id,
                            outcome="denied",
                            reason=(
                                "bijlage move rejected: file uploaded "
                                "for different dossier"
                            ),
                            file_id=fid,
                        )
                        logger.warning(
                            "[TASK] Refused to move bijlage %s → %s: "
                            "dossier binding mismatch. %s",
                            fid, dossier_id, error,
                        )
                        failures.append((fid, f"HTTP 403: {error[:200]}"))
                    else:
                        error = await resp.text()
                        logger.error(
                            "[TASK] Failed to move bijlage %s (HTTP %s): %s",
                            fid, resp.status, error,
                        )
                        failures.append((fid, f"HTTP {resp.status}: {error[:200]}"))
            except Exception as e:
                # Bug 30 / M2 pattern: log with exc_info=True so the
                # Sentry LoggingIntegration (Round 13) surfaces the
                # traceback. Also record the failure for the raise
                # below — don't swallow.
                logger.error(
                    "[TASK] Error moving bijlage %s", fid, exc_info=True,
                )
                failures.append((fid, f"{type(e).__name__}: {e}"))

    if failures:
        # Raise so the worker's recorded-task retry machinery fires.
        # File service /internal/move is idempotent, so files already
        # moved successfully in this attempt are no-ops on retry.
        summary = ", ".join(f"{fid}({reason})" for fid, reason in failures)
        raise RuntimeError(
            f"move_bijlagen_to_permanent: {len(failures)} of {len(to_move)} "
            f"bijlage move(s) failed: {summary}"
        )


# Obs 95 / Round 28: the ``TASK_HANDLERS`` dict has been removed.
# Workflow YAML now references task functions by dotted path
# (``function: "dossier_toelatingen.tasks.send_ontvangstbevestiging"``)
# and the engine resolves them at plugin load via
# ``build_callable_registries_from_workflow``.
