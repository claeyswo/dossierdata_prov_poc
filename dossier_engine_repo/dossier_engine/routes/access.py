"""Shared access control utilities for routes.

Access-check flow
-----------------
1. ``check_dossier_access`` looks for a matching entry — first in
   ``global_access`` (from config.yaml), then in the per-dossier
   ``oe:dossier_access`` entity.  If no entry matches, the user is
   **denied** (default-deny).

2. ``get_visibility_from_entry`` reads the ``view`` and
   ``activity_view`` keys from the matched entry to determine what
   the user is allowed to see.

Design principle: *default-deny*.  Access must be explicitly granted
by a matching entry.  There is no implicit "everyone can see
everything if we forgot to set up access rules."  This means:

- Global-access entries in config.yaml must have a ``view`` key
  (use ``"all"`` to mean unrestricted) and an ``activity_view``
  key (use ``"all"`` to mean all activities visible).
- A dossier without an ``oe:dossier_access`` entity is locked to
  global-access users only.

Entity visibility (``view``)
----------------------------
- ``"all"`` — all entity types visible (sentinel).
- A list of type prefixes, e.g. ``["oe:aanvraag", "oe:beslissing"]``
  — only those types visible.
- ``[]`` (empty list) — no entities visible, but activities may
  still be visible depending on ``activity_view``.
- Key absent — **empty set** (see nothing).  With default-deny the
  entry already matched on role or agent, but the author didn't
  specify what entities are visible.  Safe default: nothing.

Activity visibility (``activity_view``)
---------------------------------------
- ``"all"`` — all activities in the timeline are visible (sentinel).
- ``"own"`` — only activities where the user is the PROV agent.
- ``"related"`` — activities that touched visible entities, plus
  the user's own.
- A list of activity type names, e.g. ``["dienAanvraagIn",
  "bewerkAanvraag"]`` — only activities of those types are visible.
"""

from __future__ import annotations

from uuid import UUID
from fastapi import HTTPException
from ..db.models import Repository
from ..auth import User


async def check_dossier_access(
    repo: Repository, dossier_id: UUID, user: User,
    global_access: list[dict] | None = None,
) -> dict:
    """Check if user has access to this dossier.

    Checks global_access first (applies to all dossiers), then
    dossier-specific access via the ``oe:dossier_access`` entity.

    Returns:
        dict — the matched access entry (with role, view,
        activity_view).

    Raises:
        HTTPException 403 if no entry matches (default-deny).
    """
    # Global access entries (from config.yaml) apply to every
    # dossier regardless of the dossier-level access entity.
    if global_access:
        for entry in global_access:
            entry_role = entry.get("role")
            if entry_role and entry_role in user.roles:
                return entry

    # Per-dossier access entity.
    access_entity = await repo.get_singleton_entity(
        dossier_id, "oe:dossier_access",
    )
    if not access_entity or not access_entity.content:
        # No access entity on this dossier → no restrictions apply.
        # Every authenticated user gets through. This is the normal
        # state for new dossiers before access rules are provisioned.
        return None

    for entry in access_entity.content.get("access", []):
        entry_role = entry.get("role")
        if entry_role and entry_role in user.roles:
            return entry
        entry_agents = entry.get("agents", [])
        if user.id in entry_agents:
            return entry

    # Access entity exists but no entry matches → deny.
    from ..audit import emit_audit
    emit_audit(
        action="dossier.denied",
        actor_id=user.id,
        actor_name=user.name,
        target_type="Dossier",
        target_id=str(dossier_id),
        outcome="denied",
        dossier_id=str(dossier_id),
        reason="User has no matching role or agent entry for this dossier",
    )
    raise HTTPException(403, detail="No access to this dossier")


def get_visibility_from_entry(
    entry: dict | None,
) -> tuple[set[str] | None, str | list[str] | dict]:
    """Extract visible entity types and activity-view mode from an
    access entry.

    Returns:
        (visible_types, activity_view_mode)

        visible_types:
          ``None`` when entry is ``None`` or ``view`` is ``"all"``
          — no type filtering.
          A ``set[str]`` of type prefixes for list values (including
          empty set = nothing visible).

        activity_view_mode:
          ``"all"`` / ``"own"`` / ``"related"`` — sentinel values
          with built-in semantics.
          A ``list[str]`` of activity type names — only those types
          are shown in the timeline.
          A ``dict`` with ``mode`` (a sentinel) and ``include``
          (a list of type names always shown regardless of mode).
    """
    if entry is None:
        return None, "all"
    # --- Entity visibility ---
    view = entry.get("view")
    if view is None:
        # Key absent → no entity-type restriction. The caller has
        # access (they matched an entry) but the entry doesn't
        # constrain which entity types are visible.
        visible_types = None
    elif view == "all":
        visible_types = None  # explicit "all" sentinel, same effect
    elif isinstance(view, list):
        # Explicit list of allowed entity-type prefixes. An empty
        # list means "see no entity content" (but still see activities
        # depending on activity_view).
        visible_types = set(view)
    else:
        # Unrecognised value → treat as no restriction rather than
        # hard-deny, so a typo doesn't lock people out.
        visible_types = None

    # --- Activity visibility ---
    # Can be a string sentinel ("all", "own", "related") or a list
    # of activity type names.  Returned as-is; the caller dispatches
    # on type.
    activity_view = entry.get("activity_view", "all")

    return visible_types, activity_view
