"""
Relation processing — the generic PROV-extension edge mechanism.

An activity request can carry a `relations` block alongside `used` and
`generated`. Each entry is a `{entity, type}` pair declaring an
arbitrary directed relationship from the activity to the named entity,
under a plugin-defined relation type (e.g. `oe:neemtAkteVan`).

Two distinct policy layers control which relation types are allowed:

1. **Permission gate (workflow + activity level)** — the workflow's
   top-level `relations:` block declares which types are *permitted*
   anywhere in the workflow; an activity's own `relations:` block
   adds activity-specific permissions. The union of these two sets is
   what the client may send. Anything outside it is a 422.

2. **Validator firing (activity level only)** — a relation validator
   runs only for relation types listed in the activity's OWN
   `relations:` block. Workflow-level declarations permit a type to
   be sent but do NOT cause validators to fire on every activity. This
   is the activity-level opt-in dispatch contract: each activity
   declares which relation-type validators it wants enforced.

   Why this split? Without it, a workflow-wide `oe:neemtAkteVan`
   declaration would force the validator to run on every activity,
   including ones where staleness checking doesn't apply (system
   side-effects, built-ins, write-only activities). With opt-in,
   only the activities that genuinely care (e.g. read-only
   `doeVoorstelBeslissing`) opt in.

The module exposes one public phase function `process_relations`,
which parses + resolves the incoming entries, then dispatches the
validators for activity-level opt-in types. Two private helpers
implement the sub-phases.
"""

from __future__ import annotations

from ..errors import ActivityError
from ..refs import EntityRef
from ..state import ActivityState
from ...plugin import Plugin


def allowed_relation_types_for_activity(plugin: Plugin, activity_def: dict) -> set[str]:
    """Return the set of relation types this activity may carry on its
    request body (the permission gate / "what may be sent").

    The workflow-level `relations:` block and the activity-level
    `relations:` block are unioned — both contribute permitted types.

    This is distinct from validator-firing. Under the activity-level
    opt-in dispatch contract, a relation validator runs only for types
    listed in the activity's OWN `relations:` block, not for types
    inherited from the workflow-wide allowed-set.
    """
    workflow = {
        e.get("type")
        for e in plugin.workflow.get("relations", [])
        if e.get("type")
    }
    activity = {
        e.get("type")
        for e in activity_def.get("relations", []) or []
        if isinstance(e, dict) and e.get("type")
    }
    return workflow | activity


async def process_relations(state: ActivityState) -> None:
    """Parse the request's `relations` block, then dispatch validators.

    Reads:  state.relation_items, state.activity_def, state.plugin,
            state.repo, state.dossier_id, state.used_rows_by_ref,
            state.generated
    Writes: state.validated_relations, state.relations_by_type
    Raises: 422 on invalid relation type, external URI in relation,
            unknown entity ref, cross-dossier ref, missing type.
            500 if an activity opts into a relation type the workflow
            doesn't permit (misconfiguration).
            ActivityError from any validator that rejects the request.
    """
    allowed = allowed_relation_types_for_activity(state.plugin, state.activity_def)
    await _parse_and_resolve(state, allowed)
    await _dispatch_validators(state, allowed)


async def _parse_and_resolve(state: ActivityState, allowed: set[str]) -> None:
    """Walk every entry the client sent under `relations`, validate its
    shape, resolve its entity reference, and group by relation type for
    the dispatch phase.

    Each entry must:
    * Carry a `type` field that's in the workflow's allowed set.
    * Reference a local entity (external URIs are explicitly rejected
      because relation semantics depend on dossier-internal lineage).
    * Resolve to a real entity row in the same dossier.

    On success, two state fields are populated:
    * `state.validated_relations` — the canonical persistence list,
      one dict per entry with `version_id`, `relation_type`, `ref`.
    * `state.relations_by_type` — the dispatch input, mapping each
      relation type to the list of raw entries (with resolved
      `entity_row`) that were sent for that type.
    """
    for rel_item in state.relation_items:
        rel_type = rel_item.get("type")
        rel_ref = rel_item.get("entity", "")

        if not rel_type:
            raise ActivityError(422, f"Relation item missing 'type': {rel_item}")
        if rel_type not in allowed:
            raise ActivityError(
                422,
                f"Activity '{state.activity_def['name']}' does not allow "
                f"relation type '{rel_type}'. Allowed: {sorted(allowed)}",
            )

        parsed = EntityRef.parse(rel_ref)
        if parsed is None:
            # Not a canonical ref — either an external URI or malformed.
            # Relations can't reference externals; external URIs reaching
            # this point means the caller passed something outside the
            # relations protocol.
            raise ActivityError(
                422,
                f"Invalid entity reference in relation: {rel_ref} "
                f"(relations cannot reference external URIs)",
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


async def _dispatch_validators(state: ActivityState, allowed: set[str]) -> None:
    """Invoke each registered validator for the relation types the
    activity has opted into.

    Activity-level opt-in: only relation types listed in the activity's
    own `relations:` block trigger validator firing. The workflow-wide
    set permits types but doesn't force the validator on every activity.

    The activity's opt-in set must be a subset of the workflow's
    allowed set — if not, the activity is misconfigured and we raise
    a 500 so the operator sees the problem at request time. (Catching
    this at workflow-load time would be cleaner; this is a fallback
    for runtime safety.)

    For each opted-in type that has a registered validator, the
    validator receives the full activity context: resolved used rows,
    pending generated items, the relation entries of its type. The
    validator raises `ActivityError` to reject the request, or returns
    normally to accept.
    """
    activity_level_types: set[str] = set()
    for r in state.activity_def.get("relations", []) or []:
        if isinstance(r, dict):
            t = r.get("type")
        else:
            t = r
        if t:
            activity_level_types.add(t)

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
            continue  # pure annotation — no validator, just stored

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
