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

3. ``check_audit_access`` is a separate, stricter check for the
   endpoints that expose the full, unfiltered provenance record —
   PROV-JSON export, column-layout visualization, archive PDF.
   These views don't honor per-user activity/entity filtering and
   show everything, including system activities and tasks. They're
   intended for auditors, compliance, and long-term preservation.
   Default-deny: only roles listed in ``global_audit_access``
   (config.yaml) or in the dossier's ``audit_access`` list pass.

Design principle: *default-deny*.  Access must be explicitly granted
by a matching entry.  There is no implicit "everyone can see
everything if we forgot to set up access rules."  This means:

- Global-access entries in config.yaml must have a ``view`` key
  (use ``"all"`` to mean unrestricted) and an ``activity_view``
  key (use ``"all"`` to mean all activities visible).
- A dossier without an ``oe:dossier_access`` entity is locked to
  global-access users only.
- ``check_audit_access`` is separate — a user granted
  ``check_dossier_access`` does NOT automatically get audit-level
  views; that requires explicit listing in ``global_audit_access``
  or the dossier's ``audit_access``.

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

import logging
from uuid import UUID
from fastapi import HTTPException
from ..audit import emit_dossier_audit
from ..db.models import Repository
from ..auth import User

_log = logging.getLogger("dossier.engine.access")


async def check_dossier_access(
    repo: Repository, dossier_id: UUID, user: User,
    global_access: list[dict] | None = None,
) -> dict:
    """Check if user has access to this dossier.

    Checks global_access first (applies to all dossiers), then
    dossier-specific access via the ``oe:dossier_access`` entity.

    Default-deny: an un-provisioned dossier (no ``oe:dossier_access``
    entity, or one with empty content) raises 403 rather than
    falling through to permit. See the module docstring for the
    design principle; the atomic-provisioning guarantee that makes
    this safe lives in ``workflow.yaml``'s side-effect chain.

    Returns:
        dict — the matched access entry (with role, view,
        activity_view).

    Raises:
        HTTPException 403 if no entry matches, or if the dossier has
        no access entity configured (default-deny).
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
        # No access entity (or empty content) → default-deny, per the
        # module-level design principle. In this platform every dossier
        # gets an ``oe:dossier_access`` entity committed atomically with
        # its creating activity (``dienAanvraagIn`` chains into
        # ``setDossierAccess`` as a transactional side effect; see
        # ``workflow.yaml`` and ``engine/pipeline/side_effects.py``), so
        # an un-provisioned dossier is an anomaly — migration half-apply,
        # manual DB edit, plugin mis-wire. Safe default is reject, not
        # permit; global_access holders already bypassed above.
        emit_dossier_audit(
            action="dossier.denied",
            user=user,
            dossier_id=dossier_id,
            outcome="denied",
            reason="Dossier has no access entity configured",
        )
        raise HTTPException(403, detail="No access to this dossier")

    for entry in access_entity.content.get("access", []):
        entry_role = entry.get("role")
        if entry_role and entry_role in user.roles:
            return entry
        entry_agents = entry.get("agents", [])
        if user.id in entry_agents:
            return entry

    # Access entity exists but no entry matches → deny.
    emit_dossier_audit(
        action="dossier.denied",
        user=user,
        dossier_id=dossier_id,
        outcome="denied",
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
          ``None`` when entry is ``None`` or ``view`` is the explicit
          ``"all"`` sentinel — no type filtering, user sees all types.
          A ``set[str]`` of type prefixes for list values (including
          empty set = nothing visible).
          Empty ``set()`` when ``view:`` is missing or has an
          unrecognised value (Bug 79, Round 27.5) — default-deny.
          See the module docstring's design principle.

        activity_view_mode:
          ``"all"`` / ``"own"`` / ``"related"`` — sentinel values
          with built-in semantics.
          A ``list[str]`` of activity type names — only those types
          are shown in the timeline.
          A ``dict`` with ``mode`` (a sentinel) and ``include``
          (a list of type names always shown regardless of mode).

    Default-deny (Bug 79, Round 27.5): a matched entry with no
    ``view:`` key, or a ``view:`` value the code doesn't recognise,
    now returns ``set()`` (empty = deny) rather than ``None`` (no
    restriction). Previous behaviour was fail-open; flipped to
    fail-closed to match the module's stated default-deny design
    principle. Callers who want broad access declare ``view: "all"``
    explicitly.
    """
    if entry is None:
        return None, "all"
    # --- Entity visibility ---
    view = entry.get("view")
    if view is None:
        # Bug 79 (Round 27.5): the module-level `default-deny` design
        # principle (and the module docstring at the top of this file)
        # says access must be explicitly granted. An access entry that
        # matched the user but omitted `view:` was previously treated
        # as "no restriction" — fail-open. Flipped to default-deny:
        # a missing `view:` key now returns the empty set (nothing
        # visible), matching what the module docstring always claimed.
        # Callers who want broad access declare `view: "all"` explicitly.
        #
        # Logged (not audit-emitted) because this is a config-health
        # finding, not a dossier-user action: neither `user` nor
        # `dossier_id` is in scope here, and the event is "your access
        # config is broken," not "user X was denied access to dossier Y."
        # Operators searching the logs for this message find the
        # offending entry in `offending_entry=...`.
        _log.warning(
            "Access entry matched but lacks a `view:` key; "
            "default-deny applied. Fix by adding `view: \"all\"` "
            "or `view: [...]` to the entry. Offending entry: %r",
            entry,
        )
        visible_types = set()
    elif view == "all":
        visible_types = None  # explicit "all" sentinel — unrestricted
    elif isinstance(view, list):
        # Explicit list of allowed entity-type prefixes. An empty
        # list means "see no entity content" (but still see activities
        # depending on activity_view).
        visible_types = set(view)
    else:
        # Bug 79 (Round 27.5): an unrecognised value (neither `"all"`,
        # a list, nor absent) was previously treated as "no restriction"
        # with the rationale "so a typo doesn't lock people out."
        # That rationale is backwards for security-adjacent code —
        # a typo SHOULD lock people out, because that's when the author
        # notices and fixes. Fail-open on unrecognised value silently
        # grants more access than intended. Flipped to default-deny.
        # Logged (not audit-emitted) as a config-health finding; see
        # the corresponding note on the missing-view branch above.
        _log.warning(
            "Access entry has invalid `view:` value %r "
            "(expected \"all\" or list of type strings); "
            "default-deny applied. Offending entry: %r",
            view, entry,
        )
        visible_types = set()

    # --- Activity visibility ---
    # Can be a string sentinel ("all", "own", "related") or a list
    # of activity type names.  Returned as-is; the caller dispatches
    # on type.
    activity_view = entry.get("activity_view", "all")

    return visible_types, activity_view


async def check_audit_access(
    repo: Repository, dossier_id: UUID, user: User,
    global_audit_access: list[str] | None = None,
) -> None:
    """Check if user may access the full-provenance views for this
    dossier.

    "Audit-level" views are the ones that bypass per-user filtering
    and expose the complete provenance record: PROV-JSON export,
    columns graph visualization, archive PDF. They're intended for
    auditors, compliance officers, and long-term preservation — not
    for the day-to-day dossier timeline.

    Matches on role only (not on individual agent IDs — audit
    access is a role-based grant, not something you'd hand to a
    specific person ad-hoc).

    Sources (in order):

    1. ``global_audit_access`` — a list of role names from
       config.yaml. Granted to every dossier.
    2. ``audit_access`` list on the dossier's ``oe:dossier_access``
       entity. Per-dossier roles — useful when a workflow needs
       dossier-specific audit roles (e.g. ``oe:ondertekenaar`` for
       a signing authority).

    Raises:
        HTTPException 403 if no role match (default-deny). The
        403 is deliberately generic — don't leak whether the user
        has basic access or just lacks audit rights.
    """
    # Fast path: global list.
    if global_audit_access:
        if any(role in user.roles for role in global_audit_access):
            return

    # Per-dossier list on the access entity.
    access_entity = await repo.get_singleton_entity(
        dossier_id, "oe:dossier_access",
    )
    if access_entity and access_entity.content:
        audit_roles = access_entity.content.get("audit_access", [])
        if audit_roles and any(r in user.roles for r in audit_roles):
            return

    emit_dossier_audit(
        action="dossier.audit_denied",
        user=user,
        dossier_id=dossier_id,
        outcome="denied",
        reason="User has no role in global_audit_access or dossier audit_access",
    )
    raise HTTPException(
        403,
        detail=(
            "No audit-level access to this dossier. Audit views "
            "(PROV-JSON, columns graph, archive) require a role "
            "listed in global_audit_access or the dossier's "
            "audit_access list."
        ),
    )
