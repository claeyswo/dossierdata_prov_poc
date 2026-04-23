"""
Generated-item processing.

This phase takes the `generated` block from the activity request and
turns each entry into a normalized dict ready for persistence. Three
distinct concerns are interleaved in the original logic — this module
splits them into named helper functions so the top-level phase reads
as a five-step recipe:

1. **Type gate** — is this entity type allowed on this activity?
2. **Derivation validation** — does the declared `derivedFrom` chain
   match the dossier's actual lineage for this logical entity?
3. **Schema version resolution** — what version stamp does the new row
   get, given the activity's `entities` declarations and the parent's
   stored version?
4. **Content validation** — does the content parse against the resolved
   Pydantic model?
5. **Pending-entity registration** — make the in-flight entity visible
   to handlers via `context.get_typed()` before it hits the database.

External URIs in the generated block are short-circuited to a separate
list (`state.generated_externals`) for the persistence phase to handle
later.
"""

from __future__ import annotations

from uuid import UUID

from ...db.models import EntityRow
from ..context import _PendingEntity
from ..errors import ActivityError
from ..refs import EntityRef, is_external_uri
from ..state import ActivityState


async def process_generated(state: ActivityState) -> None:
    """Validate and normalize every entry in the activity's `generated` block.

    External URIs go to `state.generated_externals` for the persistence
    phase to record as `type=external` rows.

    Local entities go through the full validate-resolve-validate
    pipeline below, then are appended to `state.generated` as
    normalized dicts and registered in `state.resolved_entities` as
    `_PendingEntity` stand-ins so handlers can read them via
    `context.get_typed` before they're persisted.

    Reads:  state.generated_items, state.activity_def, state.repo,
            state.dossier_id, state.plugin, state.user
    Writes: state.generated, state.generated_externals,
            state.resolved_entities
    Raises: 422 on missing content, malformed refs, disallowed types,
            cross-entity derivation, content validation failures.
            409 on stale or missing derivation chains.
            500 on misconfigured activity (versioning declared without
            new_version).
    """
    allowed_types = state.activity_def.get("generates", [])

    for item in state.generated_items:
        entity_ref = item.get("entity", "")
        content = item.get("content")
        derived_from_ref = item.get("derivedFrom")

        if is_external_uri(entity_ref):
            state.generated_externals.append(entity_ref)
            continue

        if not content:
            raise ActivityError(422, f"Generated item must have content: {entity_ref}")

        parsed = EntityRef.parse(entity_ref)
        if parsed is None:
            raise ActivityError(
                422, f"Invalid entity reference for generated item: {entity_ref}",
            )

        entity_type = parsed.type
        entity_logical_id = parsed.entity_id

        if allowed_types and entity_type not in allowed_types:
            raise ActivityError(
                422, f"Activity cannot generate entity type '{entity_type}'",
            )

        latest_existing = await state.repo.get_latest_entity_by_id(
            state.dossier_id, entity_logical_id,
        )

        await _validate_derivation(
            state=state,
            entity_ref=entity_ref,
            entity_type=entity_type,
            entity_logical_id=entity_logical_id,
            derived_from_ref=derived_from_ref,
            latest_existing=latest_existing,
        )

        new_schema_version = _resolve_schema_version(
            state.activity_def, entity_type, latest_existing,
        )
        _validate_content(state, entity_type, new_schema_version, content)

        derived_from_version = _parse_derived_from_version(derived_from_ref)

        state.generated.append({
            "version_id": parsed.version_id,
            "entity_id": parsed.entity_id,
            "type": entity_type,
            "content": content,
            "derived_from": derived_from_version,
            "ref": entity_ref,
            "schema_version": new_schema_version,
        })

        # Make the pending entity visible to handlers via context.get_typed.
        # Bug 20 (Round 30): populate every EntityRow-equivalent field so
        # handlers and the lineage walker can read the pending entity
        # uniformly. Before this, ``type``, ``dossier_id``,
        # ``generated_by``, and ``derived_from`` were missing from
        # _PendingEntity's constructor — reading any of them from a
        # pending row raised AttributeError. See the _PendingEntity
        # docstring for the full rationale.
        state.resolved_entities[entity_type] = _PendingEntity(
            content=content,
            entity_id=parsed.entity_id,
            id=parsed.version_id,
            attributed_to=state.user.id,
            schema_version=new_schema_version,
            type=entity_type,
            dossier_id=state.dossier_id,
            generated_by=state.activity_id,
            derived_from=derived_from_version,
        )


async def _validate_derivation(
    *,
    state: ActivityState,
    entity_ref: str,
    entity_type: str,
    entity_logical_id: UUID,
    derived_from_ref: str | None,
    latest_existing: EntityRow | None,
) -> None:
    """Check that a generated entity's `derivedFrom` chain is consistent
    with the dossier's actual lineage for this logical entity.

    The rules:

    * If a prior version of this `entity_id` exists, `derivedFrom` is
      mandatory and must point at the current latest version. Stale
      derivations are rejected with 409 and the latest version is
      returned in the error payload so the client can rebase.
    * If `derivedFrom` is supplied, the referenced version must exist
      in the same dossier and must belong to the **same logical entity**
      — cross-entity derivation is always a 422.
    * If no prior version exists and no `derivedFrom` is supplied,
      this is a fresh creation and there's nothing to check.

    All errors carry structured payloads (`error`, `latest_version`,
    `entity_ref`) so clients can show actionable diagnostics.
    """
    declared_parent_version = _parse_derived_from_version(derived_from_ref)

    if derived_from_ref is not None:
        parent_row = await state.repo.get_entity(declared_parent_version)
        if parent_row is None or parent_row.dossier_id != state.dossier_id:
            raise ActivityError(
                422,
                f"derivedFrom refers to unknown version: {derived_from_ref}",
                payload={"error": "unknown_parent", "derivedFrom": derived_from_ref},
            )
        if parent_row.entity_id != entity_logical_id:
            raise ActivityError(
                422,
                f"derivedFrom must reference the same entity_id "
                f"(parent is {parent_row.entity_id}, generated is {entity_logical_id})",
                payload={
                    "error": "cross_entity_derivation",
                    "derivedFrom": derived_from_ref,
                    "generated": entity_ref,
                },
            )

    if latest_existing is None:
        return  # fresh entity; nothing to compare against

    if declared_parent_version is None:
        # Client is generating an entity whose logical id already
        # exists in the dossier, but didn't declare `derivedFrom`. This
        # is a malformed request: every revision must explicitly point
        # at the version it derives from. As with `invalid_derivation_chain`
        # this is a 422 because the client view is stale, not a workflow
        # conflict to negotiate around.
        raise ActivityError(
            422,
            f"Missing derivation chain: entity "
            f"'{entity_type}/{entity_logical_id}' already has version "
            f"{latest_existing.id}, so the generated entity must declare "
            f"`derivedFrom` pointing at it. Refresh your view of the "
            f"dossier and retry.",
            payload={
                "error": "missing_derivation_chain",
                "entity_ref": entity_ref,
                "latest_version": _latest_version_payload(
                    entity_type, entity_logical_id, latest_existing,
                ),
            },
        )

    if declared_parent_version != latest_existing.id:
        # The client's view of this entity's lineage is stale: they're
        # trying to revise an older version while a newer one already
        # exists. This is never legitimate — `derivedFrom` must always
        # point at the actual latest version of the logical entity.
        # The fix is on the client side: refresh the dossier, find the
        # current latest version, and re-derive from it.
        #
        # Note: this is *not* the same as `stale_used_reference`. That
        # one fires when you READ a non-latest version (which is
        # legitimate if you ack newer versions via `oe:neemtAkteVan`).
        # `invalid_derivation_chain` fires when you try to REVISE from
        # a non-latest version, which has no legitimate use case
        # because revising IS using — and a logical entity is never in
        # both `used` and `generated` for the same activity.
        raise ActivityError(
            422,
            f"Invalid derivation chain: generated entity derives from "
            f"{declared_parent_version} but the current latest version "
            f"of {entity_type}/{entity_logical_id} is {latest_existing.id}. "
            f"Your view of the dossier is stale — refresh and re-derive "
            f"from the current latest version.",
            payload={
                "error": "invalid_derivation_chain",
                "entity_ref": entity_ref,
                "declared_parent": str(declared_parent_version),
                "latest_parent": str(latest_existing.id),
                "latest_version": _latest_version_payload(
                    entity_type, entity_logical_id, latest_existing,
                ),
            },
        )


def _parse_derived_from_version(derived_from_ref: str | None) -> UUID | None:
    """Extract the version UUID from a `derivedFrom` ref string.

    Returns None if the ref is None. Raises 422 if the ref is present
    but malformed.
    """
    if derived_from_ref is None:
        return None
    parsed = EntityRef.parse(derived_from_ref)
    if parsed is None:
        raise ActivityError(422, f"Malformed derivedFrom reference: {derived_from_ref}")
    return parsed.version_id


def _latest_version_payload(
    entity_type: str, entity_logical_id: UUID, row: EntityRow,
) -> dict:
    """Build the `latest_version` block returned to clients on derivation
    errors, so they can re-issue against the current head."""
    return {
        "entity": str(EntityRef(
            type=entity_type,
            entity_id=entity_logical_id,
            version_id=row.id,
        )),
        "versionId": str(row.id),
        "content": row.content,
    }


def _resolve_schema_version(
    activity_def: dict,
    entity_type: str,
    parent_row: EntityRow | None,
) -> str | None:
    """Decide what `schema_version` to stamp on a new row, given the
    activity's per-type version declaration and the parent row's stored
    version.

    Reads the activity's `entities` block:

        entities:
          oe:aanvraag:
            new_version: v2          # required when creating fresh
            allowed_versions: [v1, v2]  # optional, used when revising

    Rules (see `dossiertype_template.md` for the full spec):

    * **No `entities` declaration for this type** — legacy/unversioned
      path. Returns the parent's sticky version when revising, or None
      when creating fresh. Content validates against the plugin's
      default `entity_models[type]`.

    * **Fresh entity** (`parent_row is None`) — must have a declared
      `new_version`, otherwise the activity is misconfigured (500).

    * **Revision** (`parent_row is not None`) — the new row inherits
      the parent's stored version (sticky / rule A). If the activity
      declares `allowed_versions` and the parent's version isn't in
      it, that's a 422 `unsupported_schema_version`.

    Returns the version string to stamp, or None for the legacy path.
    """
    entities_cfg = activity_def.get("entities") or {}
    ecfg = entities_cfg.get(entity_type)
    if not ecfg:
        if parent_row is not None:
            return parent_row.schema_version
        return None

    if parent_row is None:
        new_version = ecfg.get("new_version")
        if not new_version:
            raise ActivityError(
                500,
                f"Activity '{activity_def.get('name')}' declares versioning "
                f"for '{entity_type}' but has no 'new_version' — cannot "
                f"create a fresh entity of a versioned type without one",
                payload={
                    "error": "missing_new_version_declaration",
                    "activity": activity_def.get("name"),
                    "entity_type": entity_type,
                },
            )
        return new_version

    stored = parent_row.schema_version  # may be None for legacy rows
    allowed = ecfg.get("allowed_versions")
    if allowed is not None and stored not in allowed:
        raise ActivityError(
            422,
            f"Activity '{activity_def.get('name')}' cannot revise "
            f"'{entity_type}' entity with schema_version={stored!r}; "
            f"allowed versions are {allowed}",
            payload={
                "error": "unsupported_schema_version",
                "activity": activity_def.get("name"),
                "entity_type": entity_type,
                "stored_version": stored,
                "allowed_versions": allowed,
            },
        )
    return stored


def _validate_content(
    state: ActivityState,
    entity_type: str,
    schema_version: str | None,
    content: dict,
) -> None:
    """Run the content dict through the resolved Pydantic model.

    Looks up the model via `plugin.resolve_schema(entity_type,
    schema_version)`, which routes to the versioned schema if one is
    registered or falls back to the plugin's default `entity_models`
    entry. If no model is registered, validation is skipped — the
    plugin has opted out of typed validation for this type.
    """
    model_class = state.plugin.resolve_schema(entity_type, schema_version)
    if model_class is None:
        return
    try:
        model_class(**content)
    except Exception as e:
        raise ActivityError(
            422, f"Content validation failed for {entity_type}: {e}",
        )
