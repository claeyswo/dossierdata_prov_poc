"""
Relation processing — process-control and domain relations.

An activity request can carry a ``relations`` block alongside ``used``
and ``generated``. Each entry is either:

* **Process-control** (has ``entity``) — a directed edge from the
  activity to an entity, like ``oe:neemtAkteVan``. Persisted in the
  ``activity_relations`` table.

* **Domain** (has ``from`` + ``to``) — a semantic edge between two
  things (entity→entity, entity→URI, dossier→dossier). Persisted in
  the ``domain_relations`` table. Neither endpoint is the activity;
  the activity is the *provenance* of the relation.

The activity may also carry a ``remove_relations`` block (domain only)
to supersede existing domain relations.

Two policy layers control which relation types are allowed:

1. **Permission gate** — the union of the workflow's top-level
   ``relations:`` block and the activity's own ``relations:`` block
   declares which types may be sent. Anything outside is a 422.

2. **Operations gate** (domain only) — each activity's relation
   declaration can specify ``operations: [add, remove]``. If only
   ``[add]`` (the default), remove_relations for that type is rejected.

3. **Validator firing** (activity-level opt-in) — validators run
   only for types listed in the activity's OWN ``relations:`` block.
"""

from __future__ import annotations

from ..errors import ActivityError
from ..refs import EntityRef
from ..state import ActivityState
from ...plugin import Plugin


# =====================================================================
# YAML introspection helpers
# =====================================================================

def _relation_declarations(activity_def: dict) -> dict[str, dict]:
    """Parse the activity's ``relations:`` block into a dict of
    type → declaration (with kind, operations, etc.)."""
    decls = {}
    for entry in activity_def.get("relations", []) or []:
        if isinstance(entry, dict):
            t = entry.get("type")
            if t:
                decls[t] = entry
        elif isinstance(entry, str):
            decls[entry] = {"type": entry, "kind": "process_control"}
    return decls


def allowed_relation_types_for_activity(
    plugin: Plugin, activity_def: dict,
) -> set[str]:
    """Return the set of relation types this activity may carry on its
    request body (the permission gate)."""
    workflow = set()
    for e in plugin.workflow.get("relations", []):
        if isinstance(e, dict) and e.get("type"):
            workflow.add(e["type"])
        elif isinstance(e, str):
            workflow.add(e)
    activity = set(_relation_declarations(activity_def).keys())
    return workflow | activity


def _allowed_operations(activity_def: dict, rel_type: str) -> set[str]:
    """Return the set of operations (add, remove) this activity permits
    for the given relation type. Defaults to {"add"}."""
    decls = _relation_declarations(activity_def)
    decl = decls.get(rel_type, {})
    ops = decl.get("operations")
    if ops:
        return set(ops)
    return {"add"}


def _relation_kind(
    plugin: Plugin, activity_def: dict, rel_type: str,
) -> str:
    """Determine the kind (process_control or domain) for a relation
    type. Checks activity-level first, then workflow-level. Defaults
    to process_control for backwards compatibility."""
    decls = _relation_declarations(activity_def)
    if rel_type in decls:
        return decls[rel_type].get("kind", "process_control")
    for e in plugin.workflow.get("relations", []):
        if isinstance(e, dict) and e.get("type") == rel_type:
            return e.get("kind", "process_control")
    return "process_control"


# =====================================================================
# Main entry point
# =====================================================================

async def process_relations(state: ActivityState) -> None:
    """Parse ``relations`` and ``remove_relations``, then dispatch
    validators.

    Reads:  state.relation_items, state.remove_relation_items,
            state.activity_def, state.plugin, state.repo,
            state.dossier_id, state.used_rows_by_ref, state.generated
    Writes: state.validated_relations (process-control),
            state.validated_domain_relations (domain adds),
            state.validated_remove_relations (domain removes),
            state.relations_by_type
    """
    allowed = allowed_relation_types_for_activity(
        state.plugin, state.activity_def,
    )
    await _parse_relations(state, allowed)
    await _parse_remove_relations(state, allowed)
    await _dispatch_validators(state, allowed)


# =====================================================================
# Parse + resolve
# =====================================================================

async def _parse_relations(
    state: ActivityState, allowed: set[str],
) -> None:
    """Walk ``relations``, validate, resolve, and route to either
    process-control or domain state lists."""
    for rel_item in state.relation_items:
        rel_type = rel_item.get("type")
        if not rel_type:
            raise ActivityError(
                422, f"Relation item missing 'type': {rel_item}",
            )
        if rel_type not in allowed:
            raise ActivityError(
                422,
                f"Activity '{state.activity_def['name']}' does not allow "
                f"relation type '{rel_type}'. Allowed: {sorted(allowed)}",
            )
        if "add" not in _allowed_operations(state.activity_def, rel_type):
            raise ActivityError(
                422,
                f"Activity '{state.activity_def['name']}' does not allow "
                f"adding relations of type '{rel_type}'.",
            )

        # Route by whether the item has from/to (domain) or entity
        # (process-control). The request model already validated that
        # exactly one of the two forms is present.
        from_ref = rel_item.get("from") or rel_item.get("from_ref")
        is_domain = from_ref is not None

        if is_domain:
            await _handle_domain_add(state, rel_item, rel_type, from_ref)
        else:
            await _handle_process_control(state, rel_item, rel_type)


async def _handle_domain_add(
    state: ActivityState,
    rel_item: dict,
    rel_type: str,
    from_ref: str,
) -> None:
    """Validate and stage a domain relation for persistence.

    Expands shorthand refs (``oe:type/eid@vid``, ``dossier:did``,
    etc.) to full IRIs before storing, so ``domain_relations`` rows
    always contain resolvable, self-describing URIs."""
    from ...prov_iris import expand_ref

    to_ref = rel_item.get("to")
    if not from_ref or not to_ref:
        raise ActivityError(
            422,
            f"Domain relation '{rel_type}' requires both 'from' "
            f"and 'to': {rel_item}",
        )

    # Expand shorthand → full IRI.
    from_iri = expand_ref(from_ref, state.dossier_id)
    to_iri = expand_ref(to_ref, state.dossier_id)

    state.validated_domain_relations.append({
        "relation_type": rel_type,
        "from_ref": from_iri,
        "to_ref": to_iri,
    })
    state.relations_by_type.setdefault(rel_type, []).append({
        "from_ref": from_iri,
        "to_ref": to_iri,
        "raw": rel_item,
    })


async def _handle_process_control(
    state: ActivityState,
    rel_item: dict,
    rel_type: str,
) -> None:
    """Validate and stage a process-control relation for persistence."""
    rel_ref = rel_item.get("entity", "")
    parsed = EntityRef.parse(rel_ref)
    if parsed is None:
        raise ActivityError(
            422,
            f"Invalid entity reference in relation: {rel_ref} "
            f"(process-control relations cannot reference external URIs)",
        )
    rel_entity = await state.repo.get_entity(parsed.version_id)
    if rel_entity is None or rel_entity.dossier_id != state.dossier_id:
        raise ActivityError(
            422, f"Relation entity not found in dossier: {rel_ref}",
        )
    state.relations_by_type.setdefault(rel_type, []).append({
        "ref": rel_ref,
        "entity_row": rel_entity,
        "raw": rel_item,
    })
    state.validated_relations.append({
        "version_id": rel_entity.id,
        "relation_type": rel_type,
        "ref": rel_ref,
    })


async def _parse_remove_relations(
    state: ActivityState, allowed: set[str],
) -> None:
    """Walk ``remove_relations``, validate type + operation permission.

    Refs are expanded to full IRIs so the supersede query matches
    against the stored (expanded) values in domain_relations."""
    from ...prov_iris import expand_ref

    for item in state.remove_relation_items:
        rel_type = item.get("type")
        from_ref = item.get("from") or item.get("from_ref")
        to_ref = item.get("to")

        if not rel_type:
            raise ActivityError(
                422, f"remove_relations item missing 'type': {item}",
            )
        if not from_ref or not to_ref:
            raise ActivityError(
                422,
                f"remove_relations item requires 'from' and 'to': {item}",
            )
        if rel_type not in allowed:
            raise ActivityError(
                422,
                f"Activity '{state.activity_def['name']}' does not allow "
                f"relation type '{rel_type}'. Allowed: {sorted(allowed)}",
            )
        if "remove" not in _allowed_operations(state.activity_def, rel_type):
            raise ActivityError(
                422,
                f"Activity '{state.activity_def['name']}' does not allow "
                f"removing relations of type '{rel_type}'. "
                f"Allowed operations: "
                f"{sorted(_allowed_operations(state.activity_def, rel_type))}",
            )

        # Expand shorthand → full IRI (must match what was stored).
        from_iri = expand_ref(from_ref, state.dossier_id)
        to_iri = expand_ref(to_ref, state.dossier_id)

        state.validated_remove_relations.append({
            "relation_type": rel_type,
            "from_ref": from_iri,
            "to_ref": to_iri,
        })


# =====================================================================
# Validator dispatch
# =====================================================================

async def _dispatch_validators(
    state: ActivityState, allowed: set[str],
) -> None:
    """Invoke registered validators for activity-level opt-in types."""
    activity_level_types = set(
        _relation_declarations(state.activity_def).keys()
    )

    for rel_type in activity_level_types:
        if rel_type not in allowed:
            raise ActivityError(
                500,
                f"Activity {state.activity_def.get('name')!r} opts into "
                f"relation type {rel_type!r} which is not in the workflow's "
                f"allowed relation set {sorted(allowed)}",
                payload={
                    "error": "relation_type_not_permitted",
                    "activity": state.activity_def.get("name"),
                    "relation_type": rel_type,
                },
            )

        validator = state.plugin.relation_validators.get(rel_type)
        if validator is None:
            continue

        entries = state.relations_by_type.get(rel_type, [])
        await validator(
            plugin=state.plugin,
            repo=state.repo,
            dossier_id=state.dossier_id,
            activity_def=state.activity_def,
            entries=entries,
            used_rows_by_ref=state.used_rows_by_ref,
            generated_items=state.generated,
        )
