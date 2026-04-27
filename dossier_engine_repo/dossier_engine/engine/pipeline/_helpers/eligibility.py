"""
Eligibility computation: which activities can the user run right now?

Two layers:

* `compute_eligible_activities` — returns entries for all activities
  in the workflow that pass the structural preconditions
  (`validate_workflow_rules`) OR have an active matching
  `system:exception` covering the gap. Each entry is a dict carrying
  the activity name and, when applicable, the version_id of the
  exception that authorizes the bypass. This is expensive (it
  evaluates every activity, plus an exception lookup for each one
  whose rules failed) and the result is cacheable on the dossier
  row, since it depends only on dossier state, not on the calling
  user.

* `filter_by_user_auth` — given the structurally-eligible entries and
  a user, returns just those the user is authorized to run, with
  resolved deadline metadata and the `exempted_by_exception` field
  passed through. Cheap per-request; keep separate from the
  structural pass so the expensive part can be cached.

* `derive_allowed_activities` — convenience that combines both. Use
  when no cache is available.
"""

from __future__ import annotations

from uuid import UUID

from ....auth import User
from ....db.models import Repository
from ....plugin import Plugin
from ..authorization import authorize_activity, validate_workflow_rules
from ..exceptions import find_active_exception_for_activity
from .status import derive_status


async def compute_eligible_activities(
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    known_status: str | None = None,
) -> list[dict]:
    """Return entries for every client-callable activity whose structural
    preconditions are met OR whose preconditions would fail but a granted
    exception covers the gap.

    Each entry is a dict with:

    * ``name``: qualified activity name (string).
    * ``exempted_by_exception``: optional. If present, an active
      ``system:exception`` (its version_id, stringified UUID) makes
      this activity eligible despite a workflow-rules failure. Absent
      when normal eligibility applies.

    Why ``list[dict]`` rather than ``list[str]``: the eligibility
    decision is binary at the workflow-rules layer, but with exception
    grants in play the *reason* matters too. A frontend that hides
    "via-exception" activities behind a confirmation dialog (so users
    don't accidentally consume a single-use bypass) needs to know
    which entries owe their eligibility to a granted exception. The
    flat ``exempted_by_exception`` field surfaces that fact; absence
    means "eligible the normal way."

    The result is cached on the dossier row as JSON, so the schema
    change ripples to ``GET /dossiers/{id}``: ``allowedActivities``
    entries (after auth filtering) carry the same field.

    Loops over every client-callable activity in the plugin's workflow
    and runs ``validate_workflow_rules`` against the dossier's current
    state. When rules fail, calls
    ``find_active_exception_for_activity`` (the same helper
    ``check_exceptions`` uses at activity-time) — single source of
    truth for the matching predicate. Result still depends only on
    dossier state, so caching remains safe. Cache invalidation runs
    on every activity execution, which already covers every situation
    that can change exception eligibility (grant, retract, consume).
    """
    activities = await repo.get_activities_for_dossier(dossier_id)
    activity_types = {a.type for a in activities}
    status = known_status or await derive_status(repo, dossier_id)

    eligible: list[dict] = []
    for act_def in plugin.workflow.get("activities", []):
        if act_def.get("client_callable") is False:
            continue
        valid, _ = await validate_workflow_rules(
            act_def, repo, dossier_id,
            known_status=status,
            known_activity_types=activity_types,
            plugin=plugin,
        )
        if valid:
            eligible.append({"name": act_def["name"]})
            continue

        # Workflow rules failed. If an active exception covers this
        # activity, surface it as eligible-via-exception. We don't
        # consume the exception here — that happens only at activity
        # execution time, when ``check_exceptions`` re-runs the same
        # match and the orchestrator auto-fires ``consumeException``
        # as a side-effect. Showing the activity in the eligibility
        # list is read-only.
        match = await find_active_exception_for_activity(
            repo, dossier_id, act_def["name"],
        )
        if match is not None:
            eligible.append({
                "name": act_def["name"],
                "exempted_by_exception": str(match.id),
            })

    return eligible


async def filter_by_user_auth(
    plugin: Plugin,
    eligible: list[dict],
    user: User,
    repo: Repository,
    dossier_id: UUID,
) -> list[dict]:
    """Filter the structurally-eligible entries by user authorization.

    Input shape: ``list[dict]`` from ``compute_eligible_activities`` —
    each carrying ``name`` and (optionally) ``exempted_by_exception``.

    Returns a list of ``{type, label}`` dicts — with optional flat
    fields:

    * ``not_before`` / ``not_after`` (ISO-string deadlines) when the
      activity declares deadline rules and they resolve.
    * ``exempted_by_exception`` (UUID string) when this entry is only
      eligible thanks to a granted exception. Frontends can use this
      to render "via exception" badging or require confirmation
      before consuming a single-use bypass.

    Deadline fields are present only when the activity_def declares
    the corresponding rule AND the rule resolves successfully
    (singleton missing → field omitted, because no deadline can be
    computed). Absence of a field means "no relevant deadline, don't
    display anything".

    The returned list already passed ``validate_workflow_rules``
    (or has ``exempted_by_exception`` set), so any ``not_after``
    included here is strictly in the future and any ``not_before``
    is strictly in the past (otherwise the activity wouldn't be
    eligible). No frontend needs to re-check the boundary — just
    display.

    Cheap to call per-request: one ``authorize_activity`` call per
    eligible activity, plus a ``resolve_deadline`` call per declared
    rule (ISO-only rules don't hit the DB; dict-form rules do one
    ``lookup_singleton``). For typical dossiers with a handful of
    activities and at most one or two deadline rules each, this is
    a few extra queries per response.
    """
    from ...scheduling import resolve_deadline

    allowed = []
    act_def_map = {a["name"]: a for a in plugin.workflow.get("activities", [])}
    for elig in eligible:
        act_name = elig["name"]
        act_def = act_def_map.get(act_name)
        if not act_def:
            continue
        authorized, _ = await authorize_activity(plugin, act_def, user, repo, dossier_id)
        if not authorized:
            continue

        entry: dict = {
            "type": act_def["name"],
            "label": act_def.get("label", act_def["name"]),
        }

        # Pass the exception version through to the response so the
        # frontend can branch on it. Authorization filtering happens
        # AFTER exception matching: a user who can't call the
        # activity won't see the entry at all, which is correct —
        # the exception only helps users who could otherwise call
        # the activity, never grants role-elevation.
        exc_id = elig.get("exempted_by_exception")
        if exc_id is not None:
            entry["exempted_by_exception"] = exc_id

        # Resolve declared deadlines — add as flat ISO-string fields.
        # Malformed declarations would have been caught by the
        # plugin validator at startup; we pass any remaining
        # resolution errors through (the same activity would fail
        # at execution time too, so silencing here would mask the
        # real bug). Missing-singleton returns None → field stays
        # absent, which is the documented "rule inactive" shape.
        #
        # Note: when the activity is eligible only via an exception,
        # the resolved not_before / not_after values may be in the
        # past (the very deadlines the exception is bypassing).
        # We still emit them — the frontend should display "deadline
        # passed" alongside the "via exception" badge. Suppressing
        # them here would hide what the exception is actually
        # bypassing, which is information the user wants to see
        # before they consume a single-use grant.
        not_before_decl = (act_def.get("requirements") or {}).get("not_before")
        if not_before_decl is not None:
            resolved = await resolve_deadline(
                not_before_decl, plugin, repo, dossier_id,
                rule_name="not_before",
            )
            if resolved is not None:
                entry["not_before"] = resolved.isoformat()

        not_after_decl = (act_def.get("forbidden") or {}).get("not_after")
        if not_after_decl is not None:
            resolved = await resolve_deadline(
                not_after_decl, plugin, repo, dossier_id,
                rule_name="not_after",
            )
            if resolved is not None:
                entry["not_after"] = resolved.isoformat()

        allowed.append(entry)
    return allowed


async def derive_allowed_activities(
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    user: User,
) -> list[dict]:
    """Convenience wrapper: compute structural eligibility, then filter
    by the calling user's authorization. Use when the dossier's eligible
    cache is not available."""
    eligible = await compute_eligible_activities(plugin, repo, dossier_id)
    return await filter_by_user_auth(plugin, eligible, user, repo, dossier_id)
