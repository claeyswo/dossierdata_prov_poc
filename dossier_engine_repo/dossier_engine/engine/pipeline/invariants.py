"""
Cross-block invariants enforced between phases.

Some rules don't belong inside any single phase because they describe
relationships *between* phases' inputs or outputs. This module is the
home for those.

Today there's one such rule:

* `enforce_used_generated_disjoint` — a logical entity is never in
  both `used` and `generated` for the same activity. Revising IS
  using; the PROV graph already encodes the parent-child link via
  `wasDerivedFrom`, so listing the parent in `used` would create a
  duplicate edge.

The function is called from the orchestrator between `resolve_used`
and `process_generated`, so by the time it runs, `state.used_refs` is
populated (from `resolve_used`) and `state.generated_items` is still
the raw client input. We compare logical entity identifiers from both
sides and raise on overlap.
"""

from __future__ import annotations

from ..errors import ActivityError
from ..refs import EntityRef
from ..state import ActivityState


def enforce_used_generated_disjoint(state: ActivityState) -> None:
    """Reject the activity if any logical entity appears in both
    `used` and `generated`.

    A logical entity is identified by:

    * Its `entity_id` UUID for local entities (so v1 and v2 of the
      same `oe:aanvraag` are the same logical entity).
    * Its full URI string for external entities.

    Both flavors are checked the same way — externals that appear in
    `used` must not also appear in `generated`, and vice versa.

    Reads:  state.used_refs, state.generated_items
    Writes: nothing
    Raises: 422 with `error: used_generated_overlap` listing the
            offending references.

    The check is structural — it doesn't need the generated block to
    have been processed yet, just parsed enough to extract the logical
    identifiers. That lets us run it before `process_generated` so we
    fail fast on malformed requests instead of doing derivation
    validation on entities the client also claimed to use.

    Built-in activities (those with `built_in: true` in their activity
    definition) are exempt. They operate on multiple historical
    versions of the same logical entity by design — tombstone, for
    example, lists every version it's redacting in `used` and produces
    a single replacement in `generated`. Built-ins have their own
    shape validators that handle these cases correctly.
    """
    if state.activity_def.get("built_in"):
        return

    # Collect logical identifiers from the used block. We care about
    # the entity_id for local refs and the URI string for externals.
    used_local_ids: set = set()
    used_external_uris: set = set()
    for ref in state.used_refs:
        entity_str = ref.get("entity", "")
        if ref.get("external"):
            used_external_uris.add(entity_str)
            continue
        parsed = EntityRef.parse(entity_str)
        if parsed is not None:
            used_local_ids.add(parsed.entity_id)

    if not used_local_ids and not used_external_uris:
        return  # nothing in `used` — can't overlap

    # Walk the generated items and look for collisions.
    overlaps: list[dict] = []
    for item in state.generated_items:
        entity_str = item.get("entity", "")

        parsed = EntityRef.parse(entity_str)
        if parsed is None:
            # Not a canonical ref — treat as external URI.
            if entity_str in used_external_uris:
                overlaps.append({
                    "entity": entity_str,
                    "kind": "external",
                })
            continue

        if parsed.entity_id in used_local_ids:
            overlaps.append({
                "entity": entity_str,
                "kind": "local",
                "entity_id": str(parsed.entity_id),
            })

    if not overlaps:
        return

    raise ActivityError(
        422,
        f"Logical entity appears in both `used` and `generated` of the "
        f"same activity. Revising an entity IS using it — the parent "
        f"version is implied by `derivedFrom` and must not be re-listed "
        f"in `used`. Overlapping entities: "
        f"{[o['entity'] for o in overlaps]}",
        payload={
            "error": "used_generated_overlap",
            "overlaps": overlaps,
        },
    )
