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
from ..state import ActivityState, ValidatedRelation, DomainRelationEntry
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
    """Resolve a relation type's kind (``"domain"`` or
    ``"process_control"``) from the workflow-level declaration.

    Post-Bug-78 (Round 26): activity-level ``kind:`` is forbidden at
    load time (the load-time validator fails the plugin registration
    if any activity-level relation declaration includes it). Only
    workflow-level declarations carry ``kind:``, and the load-time
    validator guarantees every declared type has a valid kind.

    Therefore this function consults the workflow-level ``relations:``
    block only. Returns the kind string; raises ``KeyError`` if the
    type isn't declared (shouldn't happen — the permission gate in
    ``_parse_relations`` catches undeclared types before this runs,
    and the load-time validator catches them at plugin load). The
    raise is a defensive assertion, not an expected path.

    Before Bug 78 this function existed but was never called —
    dispatch guessed kind from request item shape, making the
    ``kind:`` field effectively decorative. It is now the
    authoritative dispatch key in ``_parse_relations``.
    """
    for e in plugin.workflow.get("relations", []) or []:
        if isinstance(e, dict) and e.get("type") == rel_type:
            kind = e.get("kind")
            if kind not in ("domain", "process_control"):
                # Load-time validator should have caught this — if we
                # get here, either validation was bypassed or the
                # workflow was mutated post-load. Raise loudly rather
                # than silently defaulting.
                raise ValueError(
                    f"Workflow-level declaration for relation "
                    f"{rel_type!r} has invalid kind={kind!r}. "
                    f"Load-time validation should have caught this; "
                    f"this indicates a validator bypass or a "
                    f"post-load mutation."
                )
            return kind
    raise KeyError(
        f"Relation type {rel_type!r} not declared at workflow level. "
        f"The permission gate in _parse_relations should have "
        f"rejected this before dispatch; reaching _relation_kind "
        f"means validation was bypassed."
    )


def _relation_type_declaration(
    plugin: Plugin, activity_def: dict, rel_type: str,
) -> dict:
    """Look up the full declaration dict for a relation type.

    Checks the activity-level ``relations:`` block first, then the
    workflow-level ``relations:`` block. Returns an empty dict if
    the type isn't declared anywhere (shouldn't happen — the
    permission gate catches undeclared types before this runs).
    """
    # Activity level
    decls = _relation_declarations(activity_def)
    if rel_type in decls:
        return decls[rel_type]
    # Workflow level
    for e in plugin.workflow.get("relations", []):
        if isinstance(e, dict) and e.get("type") == rel_type:
            return e
    return {}


def _validate_ref_types(
    rel_type: str,
    from_ref: str,
    to_ref: str,
    declaration: dict,
) -> None:
    """Validate that ``from_ref`` and ``to_ref`` match the declared
    ``from_types`` and ``to_types`` on a domain relation type.

    Uses ``classify_ref`` on the *original* (pre-expansion) ref
    so that both shorthand and expanded forms work.

    Skips validation if ``from_types`` / ``to_types`` are not
    declared — the constraint is opt-in per relation type.

    Raises ``ActivityError(422)`` on mismatch.
    """
    from ...prov_iris import classify_ref

    from_types = declaration.get("from_types")
    if from_types:
        actual = classify_ref(from_ref)
        if actual not in from_types:
            raise ActivityError(
                422,
                f"Relation '{rel_type}': 'from' ref must be one of "
                f"{from_types}, got '{actual}' "
                f"(ref: {from_ref}).",
            )

    to_types = declaration.get("to_types")
    if to_types:
        actual = classify_ref(to_ref)
        if actual not in to_types:
            raise ActivityError(
                422,
                f"Relation '{rel_type}': 'to' ref must be one of "
                f"{to_types}, got '{actual}' "
                f"(ref: {to_ref}).",
            )


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
    process-control or domain state lists.

    **Bug 78 (Round 26): dispatch is driven by the workflow-level
    ``kind:`` declaration**, not by request-item shape. The request
    item's shape (``entity`` vs ``from+to``) is validated against the
    declared kind; mismatch is a 422 with an informative message
    naming the type and its declared kind. Prior behaviour guessed
    kind from shape, which meant a plugin author could declare
    ``kind: domain`` and a client could silently get process-control
    dispatch by sending the wrong shape — the ``kind:`` field was
    effectively decorative."""
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

        # Bug 78: resolve kind from the workflow-level declaration
        # (single source of truth; load-time validator enforces it's
        # always present and ∈ {domain, process_control}). Then check
        # the request item's shape against the declared kind.
        kind = _relation_kind(state.plugin, state.activity_def, rel_type)
        from_ref = rel_item.get("from") or rel_item.get("from_ref")
        has_entity = rel_item.get("entity") is not None
        has_domain_shape = from_ref is not None

        if kind == "domain":
            if has_entity:
                raise ActivityError(
                    422,
                    f"Relation type {rel_type!r} is declared as "
                    f"`kind: domain` (entity→entity semantic). The "
                    f"request sent an `entity:` field (process-control "
                    f"shape). Use `from:` + `to:` for domain relations."
                )
            if not has_domain_shape:
                raise ActivityError(
                    422,
                    f"Relation type {rel_type!r} is declared as "
                    f"`kind: domain`. The request item requires "
                    f"`from:` + `to:` fields: {rel_item!r}"
                )
            await _handle_domain_add(state, rel_item, rel_type, from_ref)
        else:  # process_control
            if has_domain_shape:
                raise ActivityError(
                    422,
                    f"Relation type {rel_type!r} is declared as "
                    f"`kind: process_control` (activity→entity "
                    f"semantic). The request sent `from:`/`to:` fields "
                    f"(domain shape). Use `entity:` for process-"
                    f"control relations."
                )
            if not has_entity:
                raise ActivityError(
                    422,
                    f"Relation type {rel_type!r} is declared as "
                    f"`kind: process_control`. The request item "
                    f"requires an `entity:` field: {rel_item!r}"
                )
            await _handle_process_control(state, rel_item, rel_type)


async def _handle_domain_add(
    state: ActivityState,
    rel_item: dict,
    rel_type: str,
    from_ref: str,
) -> None:
    """Validate and stage a domain relation for persistence.

    Validates ``from_types`` / ``to_types`` constraints on the
    *original* refs (before expansion), then expands shorthand refs
    to full IRIs for storage."""
    from ...prov_iris import expand_ref

    to_ref = rel_item.get("to")
    if not from_ref or not to_ref:
        raise ActivityError(
            422,
            f"Domain relation '{rel_type}' requires both 'from' "
            f"and 'to': {rel_item}",
        )

    # Validate ref kinds against declared from_types / to_types.
    decl = _relation_type_declaration(
        state.plugin, state.activity_def, rel_type,
    )
    _validate_ref_types(rel_type, from_ref, to_ref, decl)

    # Expand shorthand → full IRI.
    from_iri = expand_ref(from_ref, state.dossier_id)
    to_iri = expand_ref(to_ref, state.dossier_id)

    state.validated_domain_relations.append(DomainRelationEntry(
        relation_type=rel_type,
        from_ref=from_iri,
        to_ref=to_iri,
    ))
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
    state.validated_relations.append(ValidatedRelation(
        version_id=rel_entity.id,
        relation_type=rel_type,
        ref=rel_ref,
    ))


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

        # Bug 78 defense-in-depth: remove operations are legal only on
        # domain relations. Load-time validation forbids
        # ``operations: [remove]`` on process_control activity
        # declarations, so the permission gate above already catches
        # the problem — but pinning the kind check here means a
        # future regression (e.g. someone loosens the load-time
        # validator) still fails loud rather than dispatching a
        # remove against a process_control relation.
        kind = _relation_kind(state.plugin, state.activity_def, rel_type)
        if kind != "domain":
            raise ActivityError(
                422,
                f"Relation type {rel_type!r} is declared as "
                f"`kind: {kind}`. Remove operations are only legal "
                f"on `kind: domain` relations (process_control "
                f"relations are stateless annotations with no remove "
                f"semantic)."
            )

        # Validate ref kinds against declared from_types / to_types.
        decl = _relation_type_declaration(
            state.plugin, state.activity_def, rel_type,
        )
        _validate_ref_types(rel_type, from_ref, to_ref, decl)

        # Expand shorthand → full IRI (must match what was stored).
        from_iri = expand_ref(from_ref, state.dossier_id)
        to_iri = expand_ref(to_ref, state.dossier_id)

        state.validated_remove_relations.append(DomainRelationEntry(
            relation_type=rel_type,
            from_ref=from_iri,
            to_ref=to_iri,
        ))


# =====================================================================
# Validator dispatch
# =====================================================================

def _resolve_validator(
    plugin: Plugin, activity_def: dict, rel_type: str, operation: str,
):
    """Find the validator callable for a relation type + operation.

    Lookup order:
    1. Activity-level YAML ``validators:`` dict with per-operation
       keys (``add`` and ``remove``). Domain relations only — load-
       time validation (Bug 78) forbids this form on process_control
       relations::

           relations:
             - type: "oe:betreft"
               validators:
                 add: "validate_betreft_target"
                 remove: "validate_betreft_removable"

    2. Activity-level YAML ``validator:`` string (single-validator
       form, fires for all operations). Works for both kinds::

           relations:
             - type: "oe:neemtAkteVan"
               validator: "validate_neemtAkteVan"

    Returns None if no validator is registered.

    **Bug 78 (Round 26) removed Style 3** — the prior plugin-level
    ``relation_validators[rel_type]`` fallback. Activities must now
    declare the validator explicitly via style 1 or 2, or run without
    validation. The load-time
    ``validate_relation_validator_registrations`` rejects plugins
    whose ``relation_validators`` dict uses a declared relation type
    name as a key, to prevent Style 3 from being silently re-created
    by convention.
    """
    decls = _relation_declarations(activity_def)
    decl = decls.get(rel_type, {})

    # Style 1: per-operation validators dict.
    validators_dict = decl.get("validators")
    if isinstance(validators_dict, dict):
        validator_name = validators_dict.get(operation)
        if validator_name:
            fn = plugin.relation_validators.get(validator_name)
            if fn:
                return fn

    # Style 2: single validator string on the declaration.
    validator_name = decl.get("validator")
    if validator_name:
        fn = plugin.relation_validators.get(validator_name)
        if fn:
            return fn

    # No validator declared for this type+operation — return None.
    # The caller (``_dispatch_validators``) treats None as "skip
    # validation," consistent with opt-in semantics.
    return None


async def _dispatch_validators(
    state: ActivityState, allowed: set[str],
) -> None:
    """Invoke registered validators for activity-level opt-in types.

    For domain relations, validators are resolved per-operation:
    add-entries use the ``add`` validator, remove-entries use the
    ``remove`` validator. If no per-operation validator is declared,
    falls back to the type-level validator.

    For process-control relations (which are always adds), the
    type-level validator fires as before.
    """
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

        # Collect add-entries (from relations_by_type) and
        # remove-entries (from validated_remove_relations).
        # Note: validated_remove_relations holds DomainRelationEntry
        # frozen dataclasses — use attribute access, not dict subscript.
        # Matches the persistence-phase reader at persistence.py:208-213.
        add_entries = state.relations_by_type.get(rel_type, [])
        remove_entries = [
            r for r in state.validated_remove_relations
            if r.relation_type == rel_type
        ]

        # Dispatch add validator. Fires even with empty entries —
        # the validator may enforce "at least one relation required."
        add_validator = _resolve_validator(
            state.plugin, state.activity_def, rel_type, "add",
        )
        if add_validator:
            await add_validator(
                plugin=state.plugin,
                repo=state.repo,
                dossier_id=state.dossier_id,
                activity_def=state.activity_def,
                entries=add_entries,
                used_rows_by_ref=state.used_rows_by_ref,
                generated_items=state.generated,
            )

        # Dispatch remove validator.
        if remove_entries:
            remove_validator = _resolve_validator(
                state.plugin, state.activity_def, rel_type, "remove",
            )
            if remove_validator:
                await remove_validator(
                    plugin=state.plugin,
                    repo=state.repo,
                    dossier_id=state.dossier_id,
                    activity_def=state.activity_def,
                    entries=remove_entries,
                    used_rows_by_ref=state.used_rows_by_ref,
                    generated_items=state.generated,
                )
