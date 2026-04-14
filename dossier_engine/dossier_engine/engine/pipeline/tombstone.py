"""
Tombstone shape validation.

The built-in `tombstone` activity has a strict request shape that the
engine enforces directly (rather than delegating to a workflow's
plugin validators, since `tombstone` is a built-in and exists in every
workflow). This module owns that validation.

Tombstones are exempt from most cross-block invariants (notably the
disjoint `used`/`generated` rule in `pipeline/invariants.py`) because
they operate on multiple historical versions of the same logical
entity by design — that's what redaction means. The shape rules below
encode the constraints that DO apply.

The phase function `validate_tombstone(state)` runs only when the
active activity is the built-in tombstone; for every other activity it
is a no-op. It populates `state.tombstone_version_ids` with the list
of version ids the persistence phase will null out after the
replacement has been written.
"""

from __future__ import annotations

from ..errors import ActivityError
from ..state import ActivityState


async def validate_tombstone(state: ActivityState) -> None:
    """Validate the shape of a tombstone activity request and capture
    the version ids that will be redacted after persistence.

    No-op for non-tombstone activities — the function returns
    immediately if `state.activity_def["name"]` is anything other than
    `"tombstone"`.

    Tombstone shape rules:

    1. **Non-empty `used`** with at least one real entity row (no
       externals).
    2. **Single logical target**: every used row must share the same
       `entity_id` and `type`. A single tombstone activity may not
       redact two different logical entities at once.
    3. **Exactly one replacement**: `generated` must contain exactly
       one entity revision matching the target type and entity_id.
       This is the placeholder that takes over the lineage after the
       originals are nulled.
    4. **At least one reason note**: `generated` must contain at least
       one `system:note` carrying the redaction reason (FOI ticket,
       GDPR Article 17 reference, etc.).
    5. **No surprise extras**: any other generated entity is rejected.

    Re-tombstoning is intentionally allowed (no rule forbids it).
    Human error during a first redaction may leave residual content in
    the replacement that itself needs to be redacted, so the operator
    must be able to run another tombstone over a previously-tombstoned
    entity. The new tombstone simply nulls the rows again (no-op for
    already-NULL content) and overwrites `tombstoned_by` with the new
    activity id, which becomes the most recent auditable record of who
    killed this entity last.

    Reads:  state.activity_def, state.used_rows_by_ref, state.generated
    Writes: state.tombstone_version_ids
    Raises: 422 with a structured payload on every shape violation.
    """
    if state.activity_def.get("name") != "tombstone":
        return

    used_entity_rows = list(state.used_rows_by_ref.values())
    if not used_entity_rows:
        raise ActivityError(
            422,
            "Tombstone activity must reference at least one real entity "
            "version in `used` (externals are not redactable).",
            payload={"error": "tombstone_no_used_entities"},
        )

    # Rule 2: single logical target.
    target_entity_ids = {row.entity_id for row in used_entity_rows}
    target_types = {row.type for row in used_entity_rows}
    if len(target_entity_ids) != 1 or len(target_types) != 1:
        raise ActivityError(
            422,
            f"Tombstone may only target a single logical entity; got "
            f"entity_ids={sorted(str(e) for e in target_entity_ids)}, "
            f"types={sorted(target_types)}",
            payload={
                "error": "tombstone_multi_entity",
                "entity_ids": sorted(str(e) for e in target_entity_ids),
                "types": sorted(target_types),
            },
        )
    target_entity_id = next(iter(target_entity_ids))
    target_type = next(iter(target_types))

    # Rules 3, 4, 5: generated shape.
    replacements = [
        g for g in state.generated
        if g["type"] == target_type and g["entity_id"] == target_entity_id
    ]
    notes = [g for g in state.generated if g["type"] == "system:note"]
    others = [
        g for g in state.generated
        if g["type"] != "system:note"
        and not (g["type"] == target_type and g["entity_id"] == target_entity_id)
    ]

    if len(replacements) != 1:
        raise ActivityError(
            422,
            f"Tombstone must generate exactly one replacement of "
            f"{target_type}/{target_entity_id}; got {len(replacements)}",
            payload={
                "error": "tombstone_replacement_count",
                "expected": 1,
                "got": len(replacements),
                "target_type": target_type,
                "target_entity_id": str(target_entity_id),
            },
        )

    if not notes:
        raise ActivityError(
            422,
            "Tombstone must generate at least one system:note carrying "
            "the redaction reason",
            payload={"error": "tombstone_missing_reason_note"},
        )

    if others:
        raise ActivityError(
            422,
            f"Tombstone activity may only generate the replacement entity "
            f"and system:note(s); unexpected entries: "
            f"{[g['type'] for g in others]}",
            payload={
                "error": "tombstone_unexpected_generated",
                "unexpected_types": [g["type"] for g in others],
            },
        )

    state.tombstone_version_ids = [row.id for row in used_entity_rows]
