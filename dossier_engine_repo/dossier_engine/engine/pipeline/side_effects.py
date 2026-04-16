"""
Side-effect activity execution.

When an activity declares `side_effects: [{activity: ...}, ...]` in
its YAML, each named activity is executed automatically after the
triggering activity has persisted its outputs. Side effects run as
the system caller (`agent="system"`, `role="systeem"`) and are
recursively allowed to declare their own side effects, up to a depth
limit.

Side effects are a deliberately pared-down form of the main pipeline:

* No client `used`/`generated`/`relations` blocks — the side effect
  computes everything from its handler.
* No custom validators.
* No tombstone shape check.
* No status-determining-from-content rules (system handlers return
  `HandlerResult.status` directly).
* No tasks scheduling.
* No finalization (the triggering activity's finalization runs once
  at the end and reflects the cumulative side-effect chain).

What side effects DO have:

* **Conditions.** A side effect can carry a `condition: {entity_type,
  field, value}` block — only run if the condition entity exists and
  its field equals the expected value. Used for "only run this if
  the user/entity is of a certain type."
* **Auto-resolved used entities.** Each side effect's used block
  declares types with `auto_resolve: latest`; the engine looks at the
  triggering activity's generated + used entities first (the trigger
  scope), then falls back to dossier-wide singleton lookup if the
  type wasn't touched by the trigger.
* **Schema versioning** for generated entities, via the same
  `_resolve_schema_version` helper the main pipeline uses.
* **Recursive side-effect chains.** A side effect can declare its own
  `side_effects:` block, which runs after it persists. Depth is
  capped to prevent runaway chains.

The function `execute_side_effects` is the recursive entry point.
The orchestrator calls it once per top-level activity with the
activity's own `side_effects` list and depth=0.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from ..context import ActivityContext, HandlerResult
from ..lookups import lookup_singleton, resolve_from_prefetched
from ...db.models import Repository
from ...plugin import Plugin
from ._identity import resolve_handler_generated_identity
from .authorization import _resolve_field
from .generated import _resolve_schema_version


async def execute_side_effects(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    trigger_activity_id: UUID,
    side_effects: list[dict],
    depth: int = 0,
    max_depth: int = 10,
) -> None:
    """Recursively execute side effect activities.

    For each side effect entry:
    1. Check its condition (if any) — skip if condition not met.
    2. Look up the activity definition + handler.
    3. Create the side effect activity row + system association.
    4. Auto-resolve its used entities from the trigger's scope,
       falling back to singleton lookup.
    5. Run the handler.
    6. Persist any handler-generated entities, with schema_version
       resolved per the side-effect activity's declarations.
    7. Recursively execute the side effect's own side effects.

    The trigger's generated + used entity lists are prefetched once
    and reused for every side effect in this call, avoiding 2N redundant
    queries when the chain auto-resolves N entities of trigger types.

    Errors are not swallowed — a side effect raising an `ActivityError`
    will propagate up and abort the entire chain. Side effects are part
    of the activity's transaction, so failure rolls back the whole
    activity.
    """
    if depth >= max_depth:
        return  # safety limit
    if not side_effects:
        return  # nothing to do — skip the agent ensure and prefetch

    await repo.ensure_agent("system", "systeem", "Systeem", {})

    # Prefetch the trigger activity's generated + used entities ONCE for
    # the whole side-effects pass. Every side effect inside this call
    # uses the same trigger, so without this we'd redundantly query
    # these for each auto-resolved used entry. Two queries here instead
    # of 2N queries.
    trigger_generated = await repo.get_entities_generated_by_activity(trigger_activity_id)
    trigger_used = await repo.get_used_entities_for_activity(trigger_activity_id)

    for side_effect in side_effects:
        await _execute_one_side_effect(
            plugin=plugin,
            repo=repo,
            dossier_id=dossier_id,
            trigger_activity_id=trigger_activity_id,
            trigger_generated=trigger_generated,
            trigger_used=trigger_used,
            side_effect=side_effect,
            depth=depth,
            max_depth=max_depth,
        )


async def _execute_one_side_effect(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    trigger_activity_id: UUID,
    trigger_generated: list,
    trigger_used: list,
    side_effect: dict,
    depth: int,
    max_depth: int,
) -> None:
    """Execute a single side effect entry. See `execute_side_effects`
    for the high-level contract."""
    se_activity_name = side_effect.get("activity")
    if not se_activity_name:
        return

    # Skip if the conditional gate fails.
    if not await _condition_met(
        plugin=plugin,
        repo=repo,
        dossier_id=dossier_id,
        trigger_generated=trigger_generated,
        trigger_used=trigger_used,
        condition=side_effect.get("condition"),
    ):
        return

    se_def = plugin.find_activity_def(se_activity_name)
    if se_def is None:
        return

    # Side effects must compute their output via a handler — they have
    # no client `generated` block to fall back on.
    se_handler_name = se_def.get("handler")
    if not se_handler_name:
        return
    se_handler_fn = plugin.handlers.get(se_handler_name)
    if se_handler_fn is None:
        return

    # Create the activity row + system association.
    se_activity_id = uuid4()
    se_now = datetime.now(timezone.utc)

    se_activity_row = await repo.create_activity(
        activity_id=se_activity_id,
        dossier_id=dossier_id,
        type=se_activity_name,
        started_at=se_now,
        ended_at=se_now,
        informed_by=str(trigger_activity_id),
    )
    await repo.create_association(
        association_id=uuid4(),
        activity_id=se_activity_id,
        agent_id="system",
        agent_name="Systeem",
        agent_type="systeem",
        role="systeem",
    )

    # Auto-resolve used entities from the trigger's scope.
    se_resolved = await _auto_resolve_used(
        plugin=plugin,
        repo=repo,
        dossier_id=dossier_id,
        se_def=se_def,
        se_activity_id=se_activity_id,
        trigger_generated=trigger_generated,
        trigger_used=trigger_used,
    )

    # Run the handler.
    se_ctx = ActivityContext(
        repo=repo,
        dossier_id=dossier_id,
        used_entities=se_resolved,
        entity_models=plugin.entity_models,
        plugin=plugin,
    )
    se_result = await se_handler_fn(se_ctx, None)

    # Stamp computed status, if the handler returned one.
    if isinstance(se_result, HandlerResult) and se_result.status:
        se_activity_row.computed_status = se_result.status

    # Persist any handler-generated entities.
    if isinstance(se_result, HandlerResult) and se_result.generated:
        await _persist_se_generated(
            plugin=plugin,
            repo=repo,
            dossier_id=dossier_id,
            se_def=se_def,
            se_activity_id=se_activity_id,
            handler_generated=se_result.generated,
        )

    # Recurse into nested side effects, if any.
    nested = se_def.get("side_effects", [])
    if nested:
        # Flush so nested side effects can see entities we just created.
        await repo.session.flush()
        await execute_side_effects(
            plugin=plugin,
            repo=repo,
            dossier_id=dossier_id,
            trigger_activity_id=se_activity_id,
            side_effects=nested,
            depth=depth + 1,
            max_depth=max_depth,
        )


async def _condition_met(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    trigger_generated: list,
    trigger_used: list,
    condition: dict | None,
) -> bool:
    """Check a side effect's `condition: {entity_type, field, value}` gate.

    Returns True if no condition is declared, or if the condition
    entity exists in the trigger's scope (or as a dossier-wide
    singleton fallback) and its field matches the expected value.
    """
    if not condition:
        return True

    cond_entity_type = condition.get("entity_type")
    cond_field = condition.get("field")
    cond_expected = condition.get("value")

    cond_entity = await resolve_from_prefetched(
        repo, dossier_id, trigger_generated, trigger_used, cond_entity_type,
    )
    if cond_entity is None and plugin.is_singleton(cond_entity_type):
        cond_entity = await lookup_singleton(
            plugin, repo, dossier_id, cond_entity_type,
        )
    if not cond_entity:
        return False
    return _resolve_field(cond_entity.content, cond_field) == cond_expected


async def _auto_resolve_used(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    se_def: dict,
    se_activity_id: UUID,
    trigger_generated: list,
    trigger_used: list,
) -> dict:
    """Auto-resolve a side effect's used entities from the trigger's scope.

    For each `used:` declaration on the side effect activity:
    * Skip externals (side effects don't get external inputs).
    * Skip entries without `auto_resolve: latest` (side effects don't
      take explicit refs from anywhere — the only way to populate used
      is auto-resolve).
    * Look in the trigger's generated entities first, then the
      trigger's used entities, then fall back to dossier-wide singleton
      lookup if the type is singleton-cardinality.
    * If found, write the `used` link row and add to the resolved dict
      that gets passed to the handler.

    Multi-cardinality types only resolve from trigger scope — never
    fall back to "latest of type" from the dossier, because that
    would be ambiguous when several instances exist.

    Returns the dict mapping `entity_type` to the resolved row, ready
    to hand to ActivityContext.
    """
    resolved: dict = {}
    for se_used_def in se_def.get("used", []):
        if se_used_def.get("external"):
            continue
        if se_used_def.get("auto_resolve") != "latest":
            continue

        se_type = se_used_def["type"]
        se_entity = await resolve_from_prefetched(
            repo, dossier_id, trigger_generated, trigger_used, se_type,
        )
        if se_entity is None and plugin.is_singleton(se_type):
            se_entity = await lookup_singleton(
                plugin, repo, dossier_id, se_type,
            )

        if se_entity is not None:
            resolved[se_type] = se_entity
            await repo.create_used(se_activity_id, se_entity.id)

    return resolved


async def _persist_se_generated(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    se_def: dict,
    se_activity_id: UUID,
    handler_generated: list[dict],
) -> None:
    """Persist the entities a side-effect handler returned in its
    `HandlerResult.generated` list.

    For each entry:
    * Default the type from `se_def["generates"][0]` if not set.
    * Resolve identity: explicit entity_id+derived_from override,
      else singleton revise-or-mint, else fresh entity_id.
    * Stamp schema_version using `_resolve_schema_version` against the
      side-effect activity's declarations and the parent row (if any).
    * Persist with `attributed_to="system"`.
    """
    se_generates = se_def.get("generates", [])

    for gen_item in handler_generated:
        identity = await resolve_handler_generated_identity(
            plugin=plugin,
            repo=repo,
            dossier_id=dossier_id,
            gen_item=gen_item,
            allowed_types=se_generates,
        )
        if identity is None:
            continue

        # Resolve schema_version: revisions inherit the parent's sticky
        # version; fresh entities get the side-effect activity's
        # `entities.<type>.new_version` declaration.
        se_parent_row = None
        if identity.derived_from_id is not None:
            se_parent_row = await repo.get_entity(identity.derived_from_id)
        se_schema_version = _resolve_schema_version(
            se_def, identity.gen_type, se_parent_row,
        )

        await repo.create_entity(
            version_id=uuid4(),
            entity_id=identity.entity_id,
            dossier_id=dossier_id,
            type=identity.gen_type,
            generated_by=se_activity_id,
            content=gen_item["content"],
            derived_from=identity.derived_from_id,
            attributed_to="system",
            schema_version=se_schema_version,
        )
