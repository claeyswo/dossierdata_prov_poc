"""
Core engine: authorization, workflow validation, activity execution.

This is the generic handler that all activities go through.
No business logic — everything is driven by the workflow YAML + plugin handlers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from ..db.models import Repository, EntityRow
from ..auth import User
from ..plugin import Plugin
from .context import ActivityContext, HandlerResult, TaskResult, _PendingEntity
from .errors import ActivityError, CardinalityError
from .lookups import lookup_singleton, resolve_from_trigger, resolve_from_prefetched
from .refs import ENTITY_REF_PATTERN, EntityRef, is_external_uri
from .pipeline.authorization import authorize_activity, validate_workflow_rules, _resolve_field
from .pipeline.eligibility import (
    compute_eligible_activities,
    derive_allowed_activities,
    filter_by_user_auth,
)
from .pipeline.status import derive_status
from .pipeline.preconditions import (
    authorize,
    check_idempotency,
    check_workflow_rules,
    ensure_dossier,
    resolve_role,
)
from .pipeline.generated import process_generated
from .pipeline.finalization import (
    build_full_response,
    determine_status,
    finalize_dossier,
    run_pre_commit_hooks,
)
from .pipeline.handlers import run_handler
from .pipeline.invariants import enforce_used_generated_disjoint
from .pipeline.persistence import create_activity_row, persist_outputs
from .pipeline.relations import process_relations
from .pipeline.side_effects import execute_side_effects
from .pipeline.tasks import cancel_matching_tasks, process_tasks
from .pipeline.tombstone import validate_tombstone
from .pipeline.used import resolve_used
from .pipeline.validators import run_custom_validators
from .response import build_replay_response
from .state import ActivityState, Caller


# (lookup_singleton, resolve_from_trigger, resolve_from_prefetched are
#  imported at the top from .lookups)


# =====================================================================
# Authorization
# =====================================================================
# (authorize_activity, _resolve_field, validate_workflow_rules are
#  imported at the top from .pipeline.authorization)
#
# (derive_status, compute_eligible_activities, filter_by_user_auth,
#  derive_allowed_activities are imported from .pipeline.status and
#  .pipeline.eligibility)


# =====================================================================
# Activity Execution
# =====================================================================
# (ActivityContext, _PendingEntity, HandlerResult, TaskResult are
#  imported at the top from .context)



async def execute_activity(
    plugin: Plugin,
    activity_def: dict,
    repo: Repository,
    dossier_id: UUID,
    activity_id: UUID,
    user: User,
    role: str,
    used_items: list[dict],
    generated_items: list[dict] | None = None,
    workflow_name: str | None = None,
    informed_by: str | None = None,
    skip_cache: bool = False,
    relation_items: list[dict] | None = None,
    remove_relation_items: list[dict] | None = None,
    caller: Caller = Caller.CLIENT,
    anchor_entity_id: UUID | None = None,
    anchor_type: str | None = None,
) -> dict:
    """
    Execute an activity.

    used_items: references to existing entities the activity reads
    generated_items: new entities or revisions the client is creating
    relation_items: generic activity→entity relations beyond used/generated,
        used for plugin-defined PROV extensions like `oe:neemtAkteVan`.
        Each item is a dict `{"entity": ref, "type": relation_type}`.
    caller: `Caller.CLIENT` (API call) or `Caller.SYSTEM` (worker or
        scheduled task). Auto-resolve of used entities only runs for
        system callers. Plain strings `"client"` and `"system"` still
        work because `Caller` inherits from `str`.
    anchor_entity_id / anchor_type: set by the worker when executing a
        scheduled task. If the activity's used block needs an entity of
        type `anchor_type` and `resolve_from_trigger` can't find it,
        the engine falls back to `get_latest_entity_by_id(anchor_entity_id)`.
        Ensures scheduled tasks can locate their anchored entity even when
        it wasn't touched by the informing activity.
    """
    if generated_items is None:
        generated_items = []
    if relation_items is None:
        relation_items = []
    if remove_relation_items is None:
        remove_relation_items = []
    now = datetime.now(timezone.utc)

    # Build the mutable state object that flows through every pipeline
    # phase. As more phases get carved out of this orchestrator, more
    # locals below will be replaced with `state.<field>` reads.
    state = ActivityState(
        plugin=plugin,
        activity_def=activity_def,
        repo=repo,
        dossier_id=dossier_id,
        activity_id=activity_id,
        user=user,
        role=role,
        used_items=used_items,
        generated_items=generated_items,
        relation_items=relation_items,
        remove_relation_items=remove_relation_items,
        workflow_name=workflow_name,
        informed_by=informed_by,
        skip_cache=skip_cache,
        caller=caller,
        anchor_entity_id=anchor_entity_id,
        anchor_type=anchor_type,
        now=now,
    )

    # Pre-execution phases: idempotency check, dossier ensure,
    # authorization, role resolution, structural workflow rules.
    replay = await check_idempotency(state)
    if replay is not None:
        return replay
    await ensure_dossier(state)
    await authorize(state)
    resolve_role(state)
    await check_workflow_rules(state)

    # Resolve the activity's `used` block: turn raw refs into EntityRows,
    # check dossier ownership, persist external URIs, and (for system
    # callers) auto-resolve any `auto_resolve: latest` slots the client
    # didn't supply.
    await resolve_used(state)

    # Enforce the disjoint-set invariant: a logical entity is never in
    # both `used` and `generated`. Revising IS using; the PROV graph
    # encodes the parent link via `wasDerivedFrom`, so re-listing the
    # parent in `used` would create a duplicate edge.
    enforce_used_generated_disjoint(state)

    # Process generated items: derivation rules, schema versioning,
    # content validation, pending-entity registration. External URIs
    # in the generated block are short-circuited to a separate list
    # for the persistence phase.
    await process_generated(state)

    # Process the activity's `relations` block: parse + resolve each
    # entry, then dispatch validators for activity-level opt-in types.
    # See pipeline/relations.py for the permission-gate vs. opt-in split.
    await process_relations(state)

    # Run any plugin-defined custom validators the activity declares.
    await run_custom_validators(state)

    # Built-in tombstone shape validation. No-op for non-tombstone
    # activities; for the tombstone built-in, validates the request shape
    # and captures the version_ids to redact in the persistence phase
    # after the replacement has been written.
    await validate_tombstone(state)

    # Persist the activity row + wasAssociatedWith association.
    await create_activity_row(state)

    # Run the activity's handler, if any. The handler can produce
    # additional generated entities (auto-filling entity_id and
    # derived_from based on cardinality), override the dossier status,
    # and append tasks. See pipeline/handlers.py.
    await run_handler(state)

    # Persist all outputs of the activity: local generated entities,
    # external entity rows, tombstone redactions (if applicable),
    # `used` link rows, and relation rows. Builds the response
    # manifest into state.generated_response.
    await persist_outputs(state)

    # Determine and stamp the activity's status contribution.
    determine_status(state)

    # Execute side effects: flush in-flight writes, then walk the
    # activity's `side_effects` list, recursively invoking each one
    # via a pared-down version of this same pipeline. Side effects
    # carry their own role and run as the system caller.
    await repo.session.flush()
    await execute_side_effects(
        plugin=plugin,
        repo=repo,
        dossier_id=dossier_id,
        trigger_activity_id=activity_id,
        side_effects=activity_def.get("side_effects", []),
    )

    # Process all tasks the activity declared (YAML + handler-appended).
    # Resolves anchors, supersedes existing scheduled tasks with the
    # same target+anchor (unless allow_multiple), persists `system:task`
    # entities for the worker to pick up.
    await process_tasks(state)

    # Cancel any prior scheduled tasks whose `cancel_if_activities`
    # includes the activity we just ran. Anchor-scoped: only cancels
    # if this activity actually advanced the anchored entity.
    await cancel_matching_tasks(state)

    # Plugin-declared synchronous pre-commit hooks. These run AFTER
    # persistence, side effects, and task scheduling but BEFORE the
    # cached_status projection and the transaction commit. Exceptions
    # roll back the whole activity — use for validation / side effects
    # that must succeed or the activity is invalid.
    await run_pre_commit_hooks(state)

    # Finalization: derive current dossier status, run post-activity
    # hook, cache status + eligible activities on the dossier row,
    # compute the user-filtered allowed list. Skipped on the bulk path.
    await finalize_dossier(state)

    return build_full_response(state)

