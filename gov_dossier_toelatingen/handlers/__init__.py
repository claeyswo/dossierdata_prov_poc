"""
Handler functions for toelatingen system activities.

Each handler receives an ActivityContext and optional client content,
and returns a HandlerResult with the computed entity content and optional status.

Handlers use context.get_typed("oe:type") to get Pydantic model instances
instead of accessing raw dicts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from gov_dossier_engine.engine import ActivityContext, HandlerResult
from gov_dossier_toelatingen.entities import (
    Aanvraag, Beslissing, Handtekening, VerantwoordelijkeOrganisatie,
)


async def set_dossier_access(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Determines who can see this dossier based on the current state.
    Creates/updates the dossier_access entity.
    """
    access_entries = []

    # Aanvrager can always see their own dossier
    aanvraag: Aanvraag | None = context.get_typed("oe:aanvraag")
    if aanvraag:
        if aanvraag.aanvrager.kbo:
            access_entries.append({
                "role": f"kbo-toevoeger:{aanvraag.aanvrager.kbo}",
                "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external", "external"],
                "activity_view": "own",
            })
        if aanvraag.aanvrager.rrn:
            access_entries.append({
                "role": aanvraag.aanvrager.rrn,
                "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external", "external"],
                "activity_view": "own",
            })

    # Verantwoordelijke organisatie gets full access
    verantw: VerantwoordelijkeOrganisatie | None = await context.get_latest_typed("oe:verantwoordelijke_organisatie")
    if verantw:
        access_entries.append({
            "role": f"gemeente-toevoeger:{verantw.uri}",
            "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external",
                      "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                      "oe:system_fields", "system:task"],
            "activity_view": "all",
        })

    # Behandelaar gets access — oe:behandelaar is cardinality=multiple, so
    # we iterate all behandelaar entities currently on the dossier and grant
    # each one its own access entry. Previously this singleton-looked-up
    # the "latest" one which was incorrect for multi-cardinality types.
    # Dedupe by URI so repeated handler invocations that each create a new
    # behandelaar entity (phase 3 will formalize revisions) don't cause
    # duplicate access entries.
    behandelaars = await context.get_entities_latest("oe:behandelaar")
    seen_uris: set[str] = set()
    for behandelaar_row in behandelaars:
        uri = (behandelaar_row.content or {}).get("uri")
        if not uri or uri in seen_uris:
            continue
        seen_uris.add(uri)
        access_entries.append({
            "role": f"behandelaar:{uri}",
            "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external",
                      "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                      "oe:system_fields", "system:task"],
            "activity_view": "all",
        })

    # Back-compat: also emit a generic "behandelaar" role so access rules
    # that match by bare role-name (not by behandelaar URI) keep working.
    # Remove once all downstream consumers match by URI.
    if behandelaars:
        access_entries.append({
            "role": "behandelaar",
            "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external",
                      "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                      "oe:system_fields", "system:task"],
            "activity_view": "all",
        })

    # Beheerder gets everything
    access_entries.append({
        "role": "beheerder",
        "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external",
                  "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                  "oe:system_fields", "oe:dossier_access", "system:task"],
        "activity_view": "all",
    })

    return HandlerResult(
        content={"access": access_entries},
        status=None,
    )


async def set_verantwoordelijke_organisatie(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Determines the responsible organization based on the aanvraag.
    """
    aanvraag: Aanvraag | None = context.get_typed("oe:aanvraag")
    if not aanvraag:
        return HandlerResult(content={"uri": "https://organisatie.onbekend"}, status=None)

    # POC: simple mapping. In production: lookup in organisation registry.
    if aanvraag.gemeente == "Brugge":
        org_uri = "https://data.vlaanderen.be/id/organisatie/brugge"
    else:
        org_uri = "https://data.vlaanderen.be/id/organisatie/oe"

    return HandlerResult(
        content={"uri": org_uri},
        status=None,
    )


async def set_system_fields(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Sets system-computed fields: creation date, creator.
    """
    entity = context.get_used_entity("oe:aanvraag")
    aanmaker = entity.attributed_to if entity else "unknown"

    return HandlerResult(
        content={
            "datum": datetime.now(timezone.utc).isoformat(),
            "aanmaker": f"https://data.vlaanderen.be/id/agent/{aanmaker}",
        },
        status=None,
    )


async def handle_beslissing(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    System activity triggered after tekenBeslissing.
    Determines the final status based on the handtekening and beslissing.
    If onvolledig, schedules a trekAanvraagIn task with a 30-day deadline.
    """
    handtekening: Handtekening | None = context.get_typed("oe:handtekening")
    beslissing: Beslissing | None = context.get_typed("oe:beslissing")

    if not handtekening:
        return HandlerResult(status="beslissing_te_tekenen")

    if not handtekening.getekend:
        return HandlerResult(status="klaar_voor_behandeling")

    if beslissing:
        if beslissing.beslissing == "goedgekeurd":
            return HandlerResult(status="toelating_verleend")
        elif beslissing.beslissing == "onvolledig":
            # Schedule trekAanvraagIn in 30 days, cancelled if vervollediging happens.
            #
            # The task must be anchored to the aanvraag so cancellation only
            # fires when someone advances THIS specific aanvraag, not when any
            # aanvraag in the dossier is touched. Two cases:
            #
            # * The activity running this handler DID use the aanvraag
            #   directly (neemBeslissing path): read it from context.
            # * The activity did NOT use the aanvraag directly
            #   (tekenBeslissing path uses the beslissing, not the aanvraag):
            #   walk the activity graph from the beslissing to find it via
            #   find_related_entity.
            from datetime import datetime, timezone, timedelta
            from gov_dossier_engine.lineage import find_related_entity

            aanvraag_row = context.get_used_row("oe:aanvraag")
            if aanvraag_row is None:
                beslissing_row = context.get_used_row("oe:beslissing")
                if beslissing_row is not None:
                    aanvraag_row = await find_related_entity(
                        context.repo,
                        context.dossier_id,
                        beslissing_row,
                        "oe:aanvraag",
                    )

            anchor_entity_id = str(aanvraag_row.entity_id) if aanvraag_row else None

            deadline = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            task_dict = {
                "kind": "scheduled_activity",
                "target_activity": "trekAanvraagIn",
                "scheduled_for": deadline,
                "cancel_if_activities": ["vervolledigAanvraag"],
                "allow_multiple": False,
                "anchor_type": "oe:aanvraag",
            }
            if anchor_entity_id is not None:
                task_dict["anchor_entity_id"] = anchor_entity_id

            return HandlerResult(
                status="aanvraag_onvolledig",
                tasks=[task_dict],
            )
        else:
            return HandlerResult(status="toelating_geweigerd")

    return HandlerResult(status="beslissing_ondertekend")


async def duid_behandelaar_aan(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Assigns a behandelaar based on the verantwoordelijke organisatie.
    """
    verantw: VerantwoordelijkeOrganisatie | None = context.get_typed("oe:verantwoordelijke_organisatie")

    if verantw and verantw.uri == "https://data.vlaanderen.be/id/organisatie/oe":
        behandelaar_uri = f"{verantw.uri}/behandelaar/benjamma"
    elif verantw:
        behandelaar_uri = verantw.uri
    else:
        behandelaar_uri = "https://data.vlaanderen.be/id/organisatie/onbekend"

    return HandlerResult(
        content={"uri": behandelaar_uri},
        status="klaar_voor_behandeling",
    )


# Registry of all handlers
HANDLERS = {
    "set_dossier_access": set_dossier_access,
    "set_verantwoordelijke_organisatie": set_verantwoordelijke_organisatie,
    "set_system_fields": set_system_fields,
    "handle_beslissing": handle_beslissing,
    "duid_behandelaar_aan": duid_behandelaar_aan,
}
