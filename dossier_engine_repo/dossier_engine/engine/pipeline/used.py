"""
Used-item resolution.

The `used` block of an activity request lists everything the activity
references — local entities (by full ref), external URIs, and (for
system callers) auto-resolved types declared in the activity YAML.
This module turns those raw refs into resolved `EntityRow` objects,
checks dossier ownership, persists external URIs as `type=external`
rows so they show up in the PROV graph, and fills in any auto-resolve
slots the client left blank.

The phase function `resolve_used` is the entry point. It does no
validation of the entities themselves — that's a workflow-level concern
and lives in plugin relation validators (e.g. `validate_neemt_akte_van`).
This phase is pure infrastructure: turn refs into rows.
"""

from __future__ import annotations

from uuid import UUID

from ..errors import ActivityError
from ..lookups import lookup_singleton, resolve_from_prefetched
from ..refs import EntityRef, is_external_uri
from ..state import ActivityState, Caller


async def resolve_used(state: ActivityState) -> None:
    """Resolve every entry in the activity's `used` block to an EntityRow.

    Two passes:

    1. **Explicit refs** — every entry the client sent. External URIs
       are persisted via `ensure_external_entity` and recorded as
       `{external: True}` refs. Local entity refs are parsed, looked up
       by version, checked for dossier ownership, and recorded with
       their type and version id.

    2. **Auto-resolve** — only for system callers (worker, side
       effects). For every entry in the activity definition's `used`
       block that has `auto_resolve: latest` and wasn't supplied
       explicitly, try to find the entity via (in order) the informing
       activity's scope, the worker-supplied anchor, or a dossier-wide
       singleton lookup. Auto-resolved entries are appended to
       `state.used_refs` with `auto_resolved: True` for downstream
       awareness.

    Reads:  state.used_items, state.repo, state.dossier_id,
            state.activity_def, state.caller, state.informed_by,
            state.anchor_entity_id, state.anchor_type, state.plugin
    Writes: state.used_refs, state.resolved_entities,
            state.used_rows_by_ref
    Raises: 422 on invalid refs, missing entities, or cross-dossier refs.
    """
    await _resolve_explicit(state)
    if state.caller == Caller.SYSTEM:
        await _auto_resolve_for_system_caller(state)


async def _resolve_explicit(state: ActivityState) -> None:
    """First pass: turn every ref the client supplied into a row.

    External URIs are persisted on the fly (so the PROV graph carries
    them) and recorded with `external: True`. Local refs are looked up
    by version_id and checked to make sure they belong to this dossier
    — references across dossier boundaries are always a 422 because
    they would silently break PROV closure.
    """
    for item in state.used_items:
        entity_ref = item.get("entity", "")

        if is_external_uri(entity_ref):
            ext_entity = await state.repo.ensure_external_entity(state.dossier_id, entity_ref)
            state.used_refs.append({
                "entity": entity_ref,
                "external": True,
                "version_id": ext_entity.id,
            })
            continue

        parsed = EntityRef.parse(entity_ref)
        if parsed is None:
            raise ActivityError(422, f"Invalid entity reference: {entity_ref}")

        entity_type = parsed.type
        existing_entity = await state.repo.get_entity(parsed.version_id)
        if not existing_entity:
            raise ActivityError(422, f"Entity not found: {entity_ref}")
        if existing_entity.dossier_id != state.dossier_id:
            raise ActivityError(
                422, f"Entity belongs to a different dossier: {entity_ref}",
            )

        state.used_refs.append({
            "entity": entity_ref,
            "version_id": parsed.version_id,
            "type": entity_type,
        })
        state.resolved_entities[entity_type] = existing_entity
        state.used_rows_by_ref[entity_ref] = existing_entity


async def _auto_resolve_for_system_caller(state: ActivityState) -> None:
    """Second pass: fill in `auto_resolve: latest` slots for system callers.

    Client requests must be explicit about which version of an entity
    they used — they're the source of truth and ambiguity isn't
    acceptable. System callers (the worker running scheduled tasks, the
    engine running side effects) are different: they're acting on
    behalf of an earlier activity and the engine knows where to look
    for the entities they should reference.

    Resolution strategy, applied in order until something matches:

    1. **Trigger scope** — if `informed_by` points to a previous local
       activity, look at what that activity generated and used. This
       handles multi-cardinality types correctly because the trigger's
       scope tells us *which specific instance* it worked on. The
       trigger's generated and used lists are prefetched once for the
       whole loop to avoid N×2 queries.

    2. **Anchor** — for worker-executed scheduled tasks, an anchor
       entity may have been supplied at task scheduling time. If the
       anchor's type matches what we need, use it.

    3. **Singleton lookup** — for entity types declared as singletons,
       fall back to whatever the dossier's most recent version of that
       type is.

    Multi-cardinality types that aren't found by any of these strategies
    fail silently — the activity runs without that entity in its
    resolved set, and downstream phases that need it will raise.
    """
    trigger_id = _parse_local_trigger_id(state.informed_by)

    # Prefetch trigger scope once if any auto-resolve slot needs it.
    trigger_generated_rows: list = []
    trigger_used_rows: list = []
    needs_trigger_scope = trigger_id is not None and any(
        ud.get("auto_resolve") == "latest" and not ud.get("external")
        for ud in state.activity_def.get("used", [])
    )
    if needs_trigger_scope:
        trigger_generated_rows = await state.repo.get_entities_generated_by_activity(trigger_id)
        trigger_used_rows = await state.repo.get_used_entities_for_activity(trigger_id)

    for used_def in state.activity_def.get("used", []):
        if used_def.get("external"):
            continue
        etype = used_def["type"]
        if used_def.get("auto_resolve") != "latest":
            continue
        if etype in state.resolved_entities:
            continue  # client supplied it explicitly

        entity = None

        if trigger_id is not None:
            entity = await resolve_from_prefetched(
                state.repo, state.dossier_id,
                trigger_generated_rows, trigger_used_rows, etype,
            )

        if entity is None and state.anchor_entity_id is not None and state.anchor_type == etype:
            entity = await state.repo.get_latest_entity_by_id(
                state.dossier_id, state.anchor_entity_id,
            )

        if entity is None and state.plugin.is_singleton(etype):
            entity = await lookup_singleton(
                state.plugin, state.repo, state.dossier_id, etype,
            )

        if entity is not None:
            state.resolved_entities[etype] = entity
            state.used_refs.append({
                "entity": str(EntityRef(
                    type=etype,
                    entity_id=entity.entity_id,
                    version_id=entity.id,
                )),
                "version_id": entity.id,
                "type": etype,
                "auto_resolved": True,
            })


def _parse_local_trigger_id(informed_by: str | None) -> UUID | None:
    """Parse `informed_by` into a UUID iff it's a local activity reference.

    `informed_by` can also be a cross-dossier URI or other non-UUID
    string — those are not local references and return None, meaning
    "we can't use this for trigger-scope resolution."
    """
    if not informed_by:
        return None
    try:
        return UUID(informed_by)
    except (ValueError, AttributeError):
        return None
