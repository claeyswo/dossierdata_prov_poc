"""
Handler functions for toelatingen system activities.

Each handler receives an ActivityContext and optional client content,
and returns a HandlerResult with the computed entity content and optional status.

Handlers use context.get_typed("oe:type") to get Pydantic model instances
instead of accessing raw dicts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from dossier_engine.engine import ActivityContext, HandlerResult
from dossier_toelatingen.entities import (
    Aanvraag, Beslissing, Handtekening, VerantwoordelijkeOrganisatie,
)


# ---------------------------------------------------------------------------
# Access-view constants
# ---------------------------------------------------------------------------
# The set of entity types each role can see. Before this lived inline at
# six separate ``access_entries.append(...)`` call sites — any new entity
# type had to be added in six places, and a miss would silently hide the
# type from a role. Extracting the shared lists gives one source of truth.
#
# When adding a new entity type to the platform, update here:
#   * everyone sees ``external`` + their own document types → _AANVRAGER_VIEW
#   * staff roles see the full platform surface            → _BEHANDELAAR_VIEW
#   * beheerder additionally sees access entities themselves → _BEHEERDER_VIEW
#
# These are plain list literals rather than tuples — the access check
# membership-tests them, and the content field of an ``oe:dossier_access``
# entity is JSON-serialized for storage, which keeps lists and rejects
# tuples.

_AANVRAGER_VIEW = [
    "oe:aanvraag",
    "oe:beslissing",
    "oe:handtekening",
    "external",
]

_BEHANDELAAR_VIEW = [
    "oe:aanvraag",
    "oe:beslissing",
    "oe:handtekening",
    "external",
    "oe:verantwoordelijke_organisatie",
    "oe:behandelaar",
    "oe:system_fields",
    "system:task",
]

_BEHEERDER_VIEW = _BEHANDELAAR_VIEW + ["oe:dossier_access"]


# ---------------------------------------------------------------------------
# Role-name minting helpers
# ---------------------------------------------------------------------------
# Role strings were hardcoded f-strings at every production site. Extracting
# them here gives a single place to rename the prefix if the identity model
# changes, and a single place to grep for "where do kbo roles come from."
# Consumers match against these strings in ``workflow.yaml`` (role: "...")
# and in ``routes/access.py``; the set of prefixes here is the full
# vocabulary of dossier-level role names.

def _kbo_role(kbo: str) -> str:
    """Role for a natural person acting on behalf of an enterprise."""
    return f"kbo-toevoeger:{kbo}"


def _rrn_role(rrn: str) -> str:
    """Role for a natural person acting on their own behalf.

    Currently the rrn itself is the role string (no prefix). Kept behind a
    helper anyway so that if we need to add one — e.g. to namespace against
    other citizen-identity schemes — it's a single-line change here, not a
    hunt through the workflow yaml for every bare rrn."""
    return rrn


def _gemeente_role(uri: str) -> str:
    """Role for a staff member of the responsible municipality."""
    return f"gemeente-toevoeger:{uri}"


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
                "role": _kbo_role(aanvraag.aanvrager.kbo),
                "view": _AANVRAGER_VIEW,
                "activity_view": "own",
            })
        if aanvraag.aanvrager.rrn:
            access_entries.append({
                "role": _rrn_role(aanvraag.aanvrager.rrn),
                "view": _AANVRAGER_VIEW,
                "activity_view": "own",
            })

    # Verantwoordelijke organisatie gets full access
    verantw: VerantwoordelijkeOrganisatie | None = await context.get_singleton_typed("oe:verantwoordelijke_organisatie")
    if verantw:
        access_entries.append({
            "role": _gemeente_role(verantw.uri),
            "view": _BEHANDELAAR_VIEW,
            "activity_view": "all",
        })

    # Behandelaar access is granted on two axes:
    #
    # 1. **Per-behandelaar URI** — each ``oe:behandelaar`` entity's URI is
    #    itself a role. A user whose ``user.roles`` contains that URI can
    #    see the dossier. This supports identity-scoped access: a specific
    #    behandelaar can be granted access without giving the whole staff
    #    pool the ``behandelaar`` role.
    # 2. **Bare ``"behandelaar"``** — the global staff role. Users with the
    #    generic ``behandelaar`` entry in their roles see every dossier
    #    that has at least one behandelaar assigned.
    #
    # These are independent populations of users and the two kinds of
    # access rules coexist. ``oe:behandelaar`` is cardinality=multiple, so
    # we iterate all currently-attached behandelaars and emit one entry
    # per URI. Dedup by URI because repeated handler invocations (e.g.
    # the same behandelaar re-attached via a later activity) shouldn't
    # produce multiple identical access entries.
    behandelaars = await context.get_entities_latest("oe:behandelaar")
    seen_uris: set[str] = set()
    for behandelaar_row in behandelaars:
        uri = (behandelaar_row.content or {}).get("uri")
        if not uri or uri in seen_uris:
            continue
        seen_uris.add(uri)
        access_entries.append({
            "role": uri,
            "view": _BEHANDELAAR_VIEW,
            "activity_view": "all",
        })

    if behandelaars:
        access_entries.append({
            "role": "behandelaar",
            "view": _BEHANDELAAR_VIEW,
            "activity_view": "all",
        })

    # Beheerder gets everything, including the access entity itself — only
    # beheerders can see who has access to what.
    access_entries.append({
        "role": "beheerder",
        "view": _BEHEERDER_VIEW,
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
        org_uri = "https://id.erfgoed.net/organisaties/brugge"
    else:
        org_uri = "https://id.erfgoed.net/organisaties/oe"

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
            "aanmaker": f"https://id.erfgoed.net/agenten/{aanmaker}",
        },
        status=None,
    )


async def handle_beslissing(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    System activity triggered after tekenBeslissing.
    Determines the final status based on the handtekening and beslissing.
    If onvolledig, schedules a trekAanvraagIn task with a 30-day deadline.

    NOTE: This handler is kept for backward compatibility only. The
    tekenBeslissing and neemBeslissing activities now use the split-
    style YAML declarations (``status_resolver:`` + ``task_builders:``)
    which route to ``resolve_beslissing_status`` and
    ``schedule_trekAanvraag_if_onvolledig`` below. This legacy handler
    reproduces the combined behaviour for any caller still using it.
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
            task = await _build_trekAanvraag_task(context)
            return HandlerResult(
                status="aanvraag_onvolledig",
                tasks=[task] if task else [],
            )
        else:
            return HandlerResult(status="toelating_geweigerd")

    return HandlerResult(status="beslissing_ondertekend")


# ---------- Split-style hooks for tekenBeslissing / neemBeslissing ----------
#
# These three functions together replace the monolithic handle_beslissing.
# The YAML now declares:
#
#   handler: null (no content to generate for these activities)
#   status_resolver: "resolve_beslissing_status"
#   task_builders: ["schedule_trekAanvraag_if_onvolledig"]
#
# Each function has a single, documented responsibility. The status
# resolver reads used entities and returns a status string. The task
# builder decides whether to schedule a trekAanvraagIn task and, if
# so, computes the deadline from plugin constants. Both are
# independently testable.


async def resolve_beslissing_status(context: ActivityContext) -> str | None:
    """Decide the dossier status after a tekenBeslissing or
    neemBeslissing activity.

    Reads the latest handtekening and beslissing and maps them to a
    status string. Returns None only in theoretical cases where
    neither entity exists — the engine leaves the status unchanged
    when None is returned.
    """
    handtekening: Handtekening | None = context.get_typed("oe:handtekening")
    beslissing: Beslissing | None = context.get_typed("oe:beslissing")

    if not handtekening:
        return "beslissing_te_tekenen"
    if not handtekening.getekend:
        return "klaar_voor_behandeling"

    if beslissing:
        if beslissing.beslissing == "goedgekeurd":
            return "toelating_verleend"
        if beslissing.beslissing == "onvolledig":
            return "aanvraag_onvolledig"
        return "toelating_geweigerd"

    return "beslissing_ondertekend"


async def schedule_trekAanvraag_if_onvolledig(
    context: ActivityContext,
) -> list[dict]:
    """When a beslissing is ``onvolledig``, schedule a
    trekAanvraagIn task cancellable by vervolledigAanvraag.

    Returns an empty list in every other case. Separating "do I
    schedule?" into its own function makes the scheduling condition
    grep-able and the task shape testable in isolation.
    """
    beslissing: Beslissing | None = context.get_typed("oe:beslissing")
    if not beslissing or beslissing.beslissing != "onvolledig":
        return []

    task = await _build_trekAanvraag_task(context)
    return [task] if task else []


async def _build_trekAanvraag_task(context: ActivityContext) -> dict | None:
    """Build the trekAanvraagIn task dict.

    Reads the deadline days from plugin constants and returns a task
    descriptor for the engine's scheduler. Supersession on the engine
    side matches by `target_activity` within the dossier, so scheduling
    this task a second time simply revises the first — there can only
    be one scheduled trekAanvraagIn per dossier at a time.
    """
    from datetime import datetime, timezone, timedelta

    deadline_days = context.constants.aanvraag_deadline_days
    deadline = (
        datetime.now(timezone.utc) + timedelta(days=deadline_days)
    ).isoformat()

    return {
        "kind": "scheduled_activity",
        "target_activity": "trekAanvraagIn",
        "scheduled_for": deadline,
        "cancel_if_activities": ["vervolledigAanvraag"],
        "allow_multiple": False,
    }


async def duid_behandelaar_aan(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Assigns a behandelaar based on the verantwoordelijke organisatie.
    """
    verantw: VerantwoordelijkeOrganisatie | None = context.get_typed("oe:verantwoordelijke_organisatie")

    if verantw and verantw.uri == "https://id.erfgoed.net/organisaties/oe":
        behandelaar_uri = f"{verantw.uri}/behandelaar/benjamma"
    elif verantw:
        behandelaar_uri = verantw.uri
    else:
        behandelaar_uri = "https://id.erfgoed.net/organisaties/onbekend"

    return HandlerResult(
        content={"uri": behandelaar_uri},
        status="klaar_voor_behandeling",
    )




# Obs 95 / Round 28: registry dicts (``HANDLERS``, ``STATUS_RESOLVERS``,
# ``TASK_BUILDERS``, ``SIDE_EFFECT_CONDITIONS``) previously lived here
# and were passed to ``Plugin(...)`` by ``create_plugin()``. They've
# been removed — the engine now builds these registries directly from
# ``workflow.yaml`` via ``build_callable_registries_from_workflow``,
# resolving dotted paths at plugin load time. The functions themselves
# remain module-level (importable as
# ``dossier_toelatingen.handlers.set_dossier_access`` etc.), which is
# what the new YAML references. See Round 28 writeup for rationale.
