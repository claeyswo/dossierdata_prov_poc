"""
Post-execution finalization phases.

After persistence, the handler, side effects, and tasks have all run,
the orchestrator does three final things:

1. **Determine the activity's status** — what dossier status this
   activity computes. This may be a literal string from the activity
   YAML, an override from the handler's `HandlerResult.status`, or a
   value resolved from a `from_entity` / `field` / `mapping` rule.
   Stored on `activity_row.computed_status` so future calls to
   `derive_status` can find it.

2. **Finalize dossier state** — derive the current dossier status
   (which may have advanced past `final_status` if side effects or
   tasks triggered more activities), call the plugin's
   `post_activity_hook` (search index updates, etc.), cache the
   status and eligible activities on the dossier row, and compute
   the user-filtered allowed-activities list for the response.
   Skipped on the bulk path (`state.skip_cache`).

3. **Build the response dict** — the JSON-serializable shape the
   route layer returns to the client. Always runs.
"""

from __future__ import annotations

import json
import logging

from ..state import ActivityState
from .authorization import _resolve_field
from .eligibility import compute_eligible_activities, filter_by_user_auth
from .status import derive_status

_log = logging.getLogger("dossier.engine")


def determine_status(state: ActivityState) -> None:
    """Resolve the activity's contribution to dossier status and
    stamp it on the activity row.

    Three sources, tried in order:

    1. **Literal string** in the activity YAML (`status: "ingediend"`).
    2. **Handler override** (`HandlerResult.status`) when the YAML
       value is None.
    3. **Mapped from entity content** when YAML carries a dict like
       `{from_entity: "oe:beslissing", field: "content.beslissing",
       mapping: {goedgekeurd: "toelating_verleend", ...}}` — read the
       field from the named generated entity and look up its value
       in the mapping.

    The resolved string (if any) is written to
    `state.activity_row.computed_status`. `derive_status` later walks
    activity rows newest-first to find the dossier's current status.

    Reads:  state.activity_def, state.handler_result, state.generated,
            state.activity_row
    Writes: state.activity_row.computed_status (in place),
            state.final_status (mirrored for downstream readers)
    """
    from ..context import HandlerResult  # local to avoid circular at module load

    status = state.activity_def.get("status")

    if status is None and isinstance(state.handler_result, HandlerResult):
        status = state.handler_result.status
    elif isinstance(status, dict):
        entity_type = status["from_entity"]
        field_path = status["field"]
        mapping = status["mapping"]
        for gen in state.generated:
            if gen["type"] == entity_type:
                value = _resolve_field(gen["content"], field_path)
                if value is not None and str(value) in mapping:
                    status = mapping[str(value)]
                    break

    if isinstance(status, str):
        state.activity_row.computed_status = status
        state.final_status = status


async def finalize_dossier(state: ActivityState) -> None:
    """Update dossier-level state after the activity has fully run.

    This phase runs the post-activity hook, caches the status and
    eligible-activities list on the dossier row, and computes the
    user-filtered allowed-activities list for the response.

    Bulk path (`state.skip_cache`) shortcuts everything: the cache
    isn't updated, the post-activity hook doesn't fire, and
    `state.current_status` falls back to whatever `computed_status`
    the current activity stamped (or `"unknown"` if it didn't stamp
    anything). Bulk callers are responsible for calling finalization
    once at the end of the batch instead of after every activity.

    Reads:  state.skip_cache, state.repo, state.dossier_id,
            state.plugin, state.activity_def, state.user,
            state.activity_row, state.final_status
    Writes: state.current_status, state.allowed_activities
    """
    if state.skip_cache:
        # Fast path: use whatever the current activity stamped on its row.
        state.current_status = (
            state.activity_row.computed_status
            or state.final_status
            or "unknown"
        )
        state.allowed_activities = []
        return

    # 17. Compute current status from the activity log.
    state.current_status = await derive_status(state.repo, state.dossier_id)

    # 18. Post-activity hook — typically updates search indices.
    if state.plugin.post_activity_hook is not None:
        try:
            current_entities = await state.repo.get_all_latest_entities(state.dossier_id)
            await state.plugin.post_activity_hook(
                repo=state.repo,
                dossier_id=state.dossier_id,
                activity_type=state.activity_def["name"],
                status=state.current_status,
                entities={e.type: e for e in current_entities},
            )
        except Exception as e:
            _log.warning(f"post_activity_hook failed: {e}")

    # 19. Cache status + eligible activities on the dossier row.
    eligible = await compute_eligible_activities(
        state.plugin, state.repo, state.dossier_id, known_status=state.current_status,
    )
    dossier_row = await state.repo.get_dossier(state.dossier_id)
    if dossier_row is not None:
        dossier_row.cached_status = state.current_status
        dossier_row.eligible_activities = json.dumps(eligible)

    # 20. User-filtered allowed list for the response.
    state.allowed_activities = await filter_by_user_auth(
        state.plugin, eligible, state.user, state.repo, state.dossier_id,
    )


def build_full_response(state: ActivityState) -> dict:
    """Assemble the response dict the route layer returns.

    Reads:  state.activity_id, state.activity_def, state.user,
            state.role, state.now, state.used_refs,
            state.generated_response, state.validated_relations,
            state.dossier_id, state.dossier, state.workflow_name,
            state.current_status, state.allowed_activities
    Writes: nothing
    Returns: the full response dict (activity, used, generated,
             relations, dossier).
    """
    return {
        "activity": {
            "id": str(state.activity_id),
            "type": state.activity_def["name"],
            "associatedWith": {
                "agent": state.user.id,
                "role": state.role,
                "name": state.user.name,
            },
            "startedAtTime": state.now.isoformat(),
            "endedAtTime": state.now.isoformat(),
        },
        "used": [
            {
                "entity": r["entity"],
                "type": r.get("type", "external"),
                **({"autoResolved": True} if r.get("auto_resolved") else {}),
            }
            for r in state.used_refs
        ],
        "generated": state.generated_response,
        "relations": [
            {"entity": rel["ref"], "type": rel["relation_type"]}
            for rel in state.validated_relations
        ],
        "dossier": {
            "id": str(state.dossier_id),
            "workflow": (
                state.dossier.workflow if state.dossier else state.workflow_name
            ),
            "status": state.current_status,
            "allowedActivities": state.allowed_activities,
        },
    }
