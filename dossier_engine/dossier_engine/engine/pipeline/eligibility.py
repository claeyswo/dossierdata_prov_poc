"""
Eligibility computation: which activities can the user run right now?

Two layers:

* `compute_eligible_activities` — returns the names of all activities
  in the workflow that pass the structural preconditions
  (`validate_workflow_rules`) for the current dossier state. This is
  expensive (it evaluates every activity) and the result is cacheable
  on the dossier row, since it depends only on dossier state, not on
  the calling user.

* `filter_by_user_auth` — given a list of structurally-eligible activity
  names and a user, returns just those the user is authorized to run.
  Cheap per-request; keep separate from the structural pass so the
  expensive part can be cached.

* `derive_allowed_activities` — convenience that combines both. Use
  when no cache is available.
"""

from __future__ import annotations

from uuid import UUID

from ...auth import User
from ...db.models import Repository
from ...plugin import Plugin
from .authorization import authorize_activity, validate_workflow_rules
from .status import derive_status


async def compute_eligible_activities(
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    known_status: str | None = None,
) -> list[str]:
    """Return names of activities whose structural preconditions are met.

    Loops over every client-callable activity in the plugin's workflow
    and runs `validate_workflow_rules` against the dossier's current
    state. Result depends only on dossier state, so it's safe to cache
    on the dossier row and invalidate on every activity execution.
    """
    activities = await repo.get_activities_for_dossier(dossier_id)
    activity_types = {a.type for a in activities}
    status = known_status or await derive_status(repo, dossier_id)

    eligible = []
    for act_def in plugin.workflow.get("activities", []):
        if act_def.get("client_callable") is False:
            continue
        valid, _ = await validate_workflow_rules(
            act_def, repo, dossier_id,
            known_status=status,
            known_activity_types=activity_types,
        )
        if valid:
            eligible.append(act_def["name"])
    return eligible


async def filter_by_user_auth(
    plugin: Plugin,
    eligible: list[str],
    user: User,
    repo: Repository,
    dossier_id: UUID,
) -> list[dict]:
    """Filter a list of eligible activity names by user authorization.

    Returns a list of `{type, label}` dicts, ready to drop into a
    response body. Cheap to call per-request — does one
    `authorize_activity` call per eligible activity.
    """
    allowed = []
    act_def_map = {a["name"]: a for a in plugin.workflow.get("activities", [])}
    for act_name in eligible:
        act_def = act_def_map.get(act_name)
        if not act_def:
            continue
        authorized, _ = await authorize_activity(plugin, act_def, user, repo, dossier_id)
        if authorized:
            allowed.append({
                "type": act_def["name"],
                "label": act_def.get("label", act_def["name"]),
            })
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
