"""
Entity lookup helpers used across the engine.

Three functions:

* `lookup_singleton` — the only sanctioned way for engine code to fetch
  a singleton entity. Enforces the cardinality invariant by checking
  the plugin's declaration before going to the repo. Calling
  `repo.get_singleton_entity` directly bypasses the check and is only
  acceptable from the dossier-access path that lives in
  `routes/access.py`.

* `resolve_from_trigger` — given an activity that triggered the current
  one (a side effect's parent, or a scheduled task's informing activity),
  find an entity of the requested type by inspecting what the trigger
  generated and used. Used to feed entity references into side effects
  and worker-executed tasks without making the operator restate them.

* `resolve_from_prefetched` — same logic as `resolve_from_trigger`, but
  takes the trigger's generated and used lists as parameters. Use this
  when resolving multiple types from the same trigger to avoid the
  redundant queries `resolve_from_trigger` would issue.
"""

from __future__ import annotations

from uuid import UUID

from ..db.models import Repository, EntityRow
from ..plugin import Plugin
from .errors import CardinalityError


async def lookup_singleton(
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    entity_type: str,
) -> EntityRow | None:
    """Look up the singleton entity of `entity_type` in the dossier.

    Raises `CardinalityError` if the plugin has declared `entity_type`
    as multi-cardinality. Callers that legitimately need "the most
    recent of a multi-cardinality type" should use
    `repo.get_latest_entity_by_id` with a specific entity_id, or
    `repo.get_entities_by_type_latest` to iterate all instances.
    """
    if not plugin.is_singleton(entity_type):
        raise CardinalityError(
            f"lookup_singleton called on non-singleton type "
            f"'{entity_type}' (cardinality={plugin.cardinality_of(entity_type)}). "
            f"Use repo.get_entities_by_type_latest or "
            f"repo.get_latest_entity_by_id instead."
        )
    return await repo.get_singleton_entity(dossier_id, entity_type)


async def resolve_from_trigger(
    repo: Repository,
    trigger_activity_id: UUID,
    dossier_id: UUID,
    entity_type: str,
) -> EntityRow | None:
    """Resolve an entity of `entity_type` from a triggering activity's scope.

    Used by side-effect auto-resolve and task anchor auto-fill. Resolution
    order:

    1. Entities **generated** by the trigger. These represent the state
       AFTER the trigger ran, so they take precedence.
    2. Entities **used** by the trigger. These are the inputs the trigger
       acted on.

    At each level, only entities matching `entity_type` are considered.
    Exactly one match → return it. Zero matches → fall through to the
    next level. Multiple distinct entity_ids of the same type at any
    level → return None and let the caller raise.

    Note: this function does two queries on every call (generated +
    used). When resolving multiple types from the same trigger, fetch
    the lists once and call `resolve_from_prefetched` instead.
    """
    generated = await repo.get_entities_generated_by_activity(trigger_activity_id)
    used = await repo.get_used_entities_for_activity(trigger_activity_id)
    return await resolve_from_prefetched(
        repo, dossier_id, generated, used, entity_type,
    )


async def resolve_from_prefetched(
    repo: Repository,
    dossier_id: UUID,
    trigger_generated: list[EntityRow],
    trigger_used: list[EntityRow],
    entity_type: str,
) -> EntityRow | None:
    """Same resolution logic as `resolve_from_trigger`, with the trigger's
    generated and used lists supplied by the caller.

    Use this when resolving several types from the same trigger to avoid
    redundant queries. The only DB query this performs is a single
    `get_latest_entity_by_id` in the rare case where the type is found
    in `used` but not `generated` (so we can return the most recent
    version, not the historical one the trigger consumed).
    """
    gen_of_type = [e for e in trigger_generated if e.type == entity_type]
    if gen_of_type:
        entity_ids = {e.entity_id for e in gen_of_type}
        if len(entity_ids) == 1:
            return gen_of_type[-1]
        return None

    used_of_type = [e for e in trigger_used if e.type == entity_type]
    if used_of_type:
        entity_ids = {e.entity_id for e in used_of_type}
        if len(entity_ids) == 1:
            return await repo.get_latest_entity_by_id(
                dossier_id, used_of_type[0].entity_id,
            )
        return None

    return None
