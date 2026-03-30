"""
Handler functions for toelatingen system activities.

Each handler receives an ActivityContext and optional client content,
and returns a HandlerResult with the computed entity content and optional status.
"""

from __future__ import annotations

from datetime import datetime, timezone

from gov_dossier_engine.engine import ActivityContext, HandlerResult


async def set_dossier_access(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Determines who can see this dossier based on the current state.
    Creates/updates the dossier_access entity.
    """
    access_entries = []

    # Aanvrager can always see their own dossier
    aanvraag = context.get_used_entity("oe:aanvraag")
    if aanvraag and aanvraag.content:
        aanvrager = aanvraag.content.get("aanvrager", {})
        # Add the aanvrager by their identifier
        if aanvrager.get("kbo"):
            access_entries.append({
                "role": f"kbo-toevoeger:{aanvrager['kbo']}",
                "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening"],
                "activity_view": "own",
            })
        if aanvrager.get("rrn"):
            access_entries.append({
                "role": aanvrager["rrn"],
                "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening"],
                "activity_view": "own",
            })

    # Verantwoordelijke organisatie gets full access
    verantw = await context.get_latest_entity("oe:verantwoordelijke_organisatie")
    if verantw and verantw.content:
        org_uri = verantw.content.get("uri", "")
        access_entries.append({
            "role": f"gemeente-toevoeger:{org_uri}",
            "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening",
                      "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                      "oe:system_fields"],
            "activity_view": "all",
        })

    # Behandelaar gets access
    behandelaar = await context.get_latest_entity("oe:behandelaar")
    if behandelaar:
        access_entries.append({
            "role": "behandelaar",
            "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening",
                      "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                      "oe:system_fields"],
            "activity_view": "all",
        })

    # Beheerder gets everything
    access_entries.append({
        "role": "beheerder",
        "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening",
                  "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                  "oe:system_fields", "oe:dossier_access"],
        "activity_view": "all",
    })

    return HandlerResult(
        content={"access": access_entries},
        status=None,
    )


async def set_verantwoordelijke_organisatie(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Determines the responsible organization based on the aanvraag.
    In a real system, this would look up the organisation registry.
    For POC: derives from gemeente field.
    """
    aanvraag = context.get_used_entity("oe:aanvraag")
    if not aanvraag or not aanvraag.content:
        return HandlerResult(content={"uri": "https://organisatie.onbekend"}, status=None)

    gemeente = aanvraag.content.get("gemeente", "onbekend")

    # POC: simple mapping. In production: lookup in organisation registry.
    if gemeente == "Brugge":
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
    aanvraag = context.get_used_entity("oe:aanvraag")
    aanmaker = aanvraag.attributed_to if aanvraag else "unknown"

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
    """
    handtekening = context.get_used_entity("oe:handtekening")
    beslissing = context.get_used_entity("oe:beslissing")

    if not handtekening or not handtekening.content:
        return HandlerResult(content=None, status="beslissing_te_tekenen")

    getekend = handtekening.content.get("getekend", False)

    if not getekend:
        # Signature rejected — go back to proposal
        return HandlerResult(content=None, status="klaar_voor_behandeling")

    if beslissing and beslissing.content:
        uitkomst = beslissing.content.get("beslissing", "afgekeurd")
        if uitkomst == "goedgekeurd":
            return HandlerResult(content=None, status="toelating_verleend")
        elif uitkomst == "onvolledig":
            return HandlerResult(content=None, status="aanvraag_onvolledig")
        else:
            return HandlerResult(content=None, status="toelating_geweigerd")

    return HandlerResult(content=None, status="beslissing_ondertekend")


async def duid_behandelaar_aan(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Assigns a behandelaar based on the verantwoordelijke organisatie.
    In production: lookup from org registry or assignment rules.
    For POC: derives from verantwoordelijke organisatie URI.
    """
    verantw = context.get_used_entity("oe:verantwoordelijke_organisatie")
    org_uri = ""
    if verantw and verantw.content:
        org_uri = verantw.content.get("uri", "")

    if org_uri == "https://data.vlaanderen.be/id/organisatie/oe":
        behandelaar_uri = f"{org_uri}/behandelaar/benjamma"
    else:
        behandelaar_uri = org_uri

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
