"""
``check_exceptions`` pipeline phase.

Runs between ``authorize`` / ``resolve_role`` and ``check_workflow_rules``.

Exception grants (``system:exception``) let an administrator authorize
legal-audit-grade bypass of the structural workflow-rules layer
(``requirements`` / ``forbidden`` / ``not_before`` / ``not_after``).
Never overrides authorization — ``client_callable: false`` activities
remain system-only, role-gated access stays role-gated.

**This phase is bypass-or-nothing.** If workflow rules already pass,
the phase is a no-op and no exception is consumed. Only when the
rules *would* fail AND an active matching exception exists does the
phase take effect. That keeps granted exceptions stable across
attempts until there's actually a blocking rule to override.

When the phase finds a matching exception:

* Sets ``state.exempted_by_exception`` to the exception's version_id.
  Downstream phases branch on this: ``check_workflow_rules`` skips
  its raise, ``execute_side_effects`` injects a ``consumeException``
  follow-up to revise the exception with ``status: consumed``.
* Appends the exception to ``state.used_refs`` /
  ``state.resolved_entities`` / ``state.used_rows_by_ref``. That
  makes the PROV graph correct (the activity genuinely used the
  exception — it couldn't have run without it) and unlocks
  ``consumeException``'s side-effect auto-resolve: its
  ``used: [system:exception]`` slot fills from the trigger's used list
  via ``resolve_from_prefetched``.

Exception matching (at-most-one-per-activity invariant means ≤1
logical entity survives the filter):

* Latest version's ``content.activity`` equals ``state.activity_def``'s
  name (both qualified at this point — plugin load normalizes).
* ``content.status == "active"``.
* ``content.granted_until`` unset OR in the future relative to
  ``state.now``. Expired exceptions are treated as not-a-match; the
  caller falls through to the normal workflow-rule error and the
  expired exception sits in the dossier as audit history.

Malformed ``granted_until`` content raises — plugin authors should
never see it in production (the validator rejects bad submissions)
but a stray value shouldn't silently ignore the deadline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from ..state import ActivityState, UsedRef
from .authorization import validate_workflow_rules


async def find_active_exception_for_activity(
    repo,
    dossier_id: UUID,
    activity_name: str,
    now: datetime | None = None,
):
    """Return the active ``system:exception`` row matching ``activity_name``,
    or None.

    Shared between the ``check_exceptions`` pipeline phase (executes the
    bypass at activity-time) and ``compute_eligible_activities`` (must
    surface exception-eligibility in GET /dossiers/{id} responses so
    frontends know which activities are runnable thanks to a granted
    exception). Without this helper, the eligible-activities computation
    would only see the rule-failure side and would hide exempted
    activities from the user — making the exception functionally
    invisible to the UI.

    Single source of truth for "is there a matching active exception?"
    keeps the two callers from drifting on the activity-name comparison,
    status filter, or deadline semantics.

    Filter rules:

    * Latest version's ``content.activity`` equals ``activity_name``.
      Both are qualified at this point (plugin load normalizes activity
      names to qualified form, and the validator stores them qualified).
    * ``content.status == "active"``. Consumed and cancelled exceptions
      stay in the dossier's PROV history but never authorize bypass.
    * ``content.granted_until`` unset OR strictly in the future.
      Boundary is exclusive (``now >= deadline`` rejects), matching
      ``forbidden.not_after`` semantics.

    Malformed ``granted_until`` values are treated as not-a-match. The
    plugin validator should reject bad submissions, but if one slips
    through, this helper short-circuits to "no bypass" rather than
    silently relying on a broken deadline. The stored exception still
    shows up in audit history.

    Per-activity uniqueness (the ``valideer_exception`` invariant) means
    at most one entity survives the filter — but we walk the full
    latest-versions list because the filter has three parts and the
    one candidate can be made ineligible by any of them.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    exceptions = await repo.get_entities_by_type_latest(
        dossier_id, "system:exception",
    )

    for ex in exceptions:
        content = ex.content or {}
        if content.get("activity") != activity_name:
            continue
        if content.get("status") != "active":
            continue

        granted_until = content.get("granted_until")
        if granted_until:
            try:
                deadline = _parse_iso(granted_until)
            except ValueError:
                continue
            if now >= deadline:
                continue

        return ex

    return None


async def check_exceptions(state: ActivityState) -> None:
    """Look for an active ``system:exception`` that bypasses the activity's
    workflow rules. See module docstring for semantics.

    Reads:  state.activity_def, state.repo, state.dossier_id,
            state.plugin, state.now
    Writes (on bypass): state.exempted_by_exception,
                        state.used_refs, state.resolved_entities,
                        state.used_rows_by_ref
    Raises: nothing on its own — mismatched / missing exceptions
            cause ``check_workflow_rules`` to raise 409 as it would
            without the exception mechanism.
    """
    # Skip on the very first activity of a brand-new dossier —
    # structurally identical to the check_workflow_rules skip, and
    # necessary for the same reason (no history to evaluate against).
    # Without this, the exception-lookup query would run pointlessly
    # on every dossier-creation activity.
    is_bootstrap = state.activity_def.get("can_create_dossier")
    if is_bootstrap and not await state.repo.get_activities_for_dossier(state.dossier_id):
        return

    # Peek at the workflow rules. If they pass, no exception needed —
    # granted exceptions stay on ice for whenever they're first
    # actually blocking. Duplicating the validate_workflow_rules call
    # here costs one extra DB round trip per activity when exceptions
    # aren't in play, which is the typical case; acceptable.
    valid, _ = await validate_workflow_rules(
        state.activity_def, state.repo, state.dossier_id,
        plugin=state.plugin, now=state.now,
    )
    if valid:
        return

    # Rules would fail. Look for an active, non-expired exception
    # matching this activity.
    match = await find_active_exception_for_activity(
        state.repo, state.dossier_id, state.activity_def["name"],
        now=state.now,
    )
    if match is None:
        return  # No bypass available — check_workflow_rules will 409.

    # Inject the exception into the activity's used set. The engine's
    # persistence phase will write the ``wasInformedBy`` / ``used``
    # edges from here, which is exactly the PROV fact we want:
    # this activity used the exception.
    #
    # Engine permits used refs for types the activity doesn't
    # declare in its YAML ``used:`` block (the declaration is only
    # consulted for auto-resolve, not for accepting client- or
    # engine-supplied refs), so we don't need every exception-
    # eligible activity to pre-declare ``system:exception`` in its YAML.
    #
    # Ref format: ``system:exception/<entity_id>@<version_id>`` — the
    # shorthand form from ``engine.refs``. We build it here rather
    # than via ``EntityRef.__str__`` to avoid importing the class
    # just for a two-field format string.
    entity_ref = f"system:exception/{match.entity_id}@{match.id}"
    state.used_refs.append(UsedRef(
        entity=entity_ref,
        version_id=match.id,
        type="system:exception",
    ))
    state.resolved_entities["system:exception"] = match
    state.used_rows_by_ref[entity_ref] = match

    state.exempted_by_exception = match.id


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 datetime into an aware UTC datetime.

    Mirrors ``engine.scheduling._parse_iso`` but kept local to avoid
    a module-level import cycle (scheduling imports from engine,
    engine phases import from scheduling is fine but symmetry
    matters here — this phase is on the authorization path). The
    implementation is trivially a few lines; extraction isn't worth
    the import dance.
    """
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
