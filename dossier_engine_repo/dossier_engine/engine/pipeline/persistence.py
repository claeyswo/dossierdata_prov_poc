"""
Database persistence — turning the validated activity into rows.

Three phases live here, called in order from the orchestrator:

1. `create_activity_row` — writes the `ActivityRow` and the
   `wasAssociatedWith` association linking the activity to the calling
   user. Runs after all validation passes and before the handler runs.
   The handler needs `state.activity_row` to exist so it can attribute
   any entities it generates back to the right activity id.

2. `persist_outputs` — the bulk write phase. Runs after the handler
   has potentially appended entries to `state.generated`. Writes:
     * Generated entity rows (local).
     * Generated external entity rows (URIs persisted as
       `type=external` so they show up in the PROV graph).
     * Tombstone redactions, if the active activity is the built-in
       tombstone (the replacement was just persisted, so now we null
       the originals).
     * `Used` link rows for every reference in the activity's used
       block.
     * Relation rows for every entry in `state.validated_relations`.

   Also builds `state.generated_response`, the manifest the response
   builder echoes back to the client. Each entry is a dict with
   `entity`, `type`, `content`, and optional `schemaVersion`.

The split exists because the handler runs between phases 1 and 2 — it
needs `state.activity_row` to exist (so it can stamp generated
entities with the right `wasGeneratedBy` link) but can also append to
`state.generated`, so persistence must wait until the handler is done.
"""

from __future__ import annotations

import uuid as uuid_mod
from uuid import uuid4

from ..refs import EntityRef
from ..state import ActivityState


async def create_activity_row(state: ActivityState) -> None:
    """Persist the `ActivityRow` and the `wasAssociatedWith` association.

    The activity row carries the basic provenance metadata: when it
    started and ended, which dossier it belongs to, what type it is,
    which prior activity (if any) informed this one. The association
    is the PROV `wasAssociatedWith` edge linking the activity to the
    agent (user) that ran it, with the functional role recorded.

    This function also ensures the calling user exists in the agents
    table — the association row carries a foreign key into agents, so
    the row must exist before we can write the association. We do it
    here (rather than in a separate phase) so all writes related to
    the activity's identity land in one place.

    Reads:  state.repo, state.activity_id, state.dossier_id,
            state.activity_def, state.now, state.informed_by,
            state.user, state.role
    Writes: state.activity_row
    """
    await state.repo.ensure_agent(
        state.user.id, state.user.type, state.user.name, state.user.properties,
        uri=state.user.uri,
    )

    state.activity_row = await state.repo.create_activity(
        activity_id=state.activity_id,
        dossier_id=state.dossier_id,
        type=state.activity_def["name"],
        started_at=state.now,
        ended_at=state.now,
        informed_by=state.informed_by,
    )

    await state.repo.create_association(
        association_id=uuid4(),
        activity_id=state.activity_id,
        agent_id=state.user.id,
        agent_name=state.user.name,
        agent_type=state.user.type,
        role=state.role,
    )


async def persist_outputs(state: ActivityState) -> None:
    """Persist everything the activity produced and link it up.

    Runs after `create_activity_row` and after the handler phase.
    Writes, in order:

    1. Local generated entities (`wasGeneratedBy` only — no `used`
       link, since the parent is encoded via `derived_from`).
    2. External entities (URIs the activity emitted, persisted as
       `type=external` rows so they appear in the PROV graph).
    3. Tombstone redactions if the active activity is the built-in
       tombstone — the redacted version_ids were captured by
       `validate_tombstone` earlier.
    4. `used` link rows for every reference in the used block.
    5. Relation rows for every entry in `validated_relations`.

    Also builds `state.generated_response` — the manifest of persisted
    entities that the response builder will echo back. Local entities
    get an entry with their full ref, type, content, and (if set)
    `schemaVersion`. Externals get a simpler shape: ref URI + type
    `"external"` + a content dict carrying just the URI.

    Reads:  state.repo, state.dossier_id, state.activity_id,
            state.user, state.generated, state.generated_externals,
            state.tombstone_version_ids, state.used_refs,
            state.validated_relations
    Writes: state.generated_response (rebuilt from scratch each call)
    """
    state.generated_response = []

    # 1. Persist local generated entities.
    for gen in state.generated:
        await state.repo.create_entity(
            version_id=gen["version_id"],
            entity_id=gen["entity_id"],
            dossier_id=state.dossier_id,
            type=gen["type"],
            generated_by=state.activity_id,
            content=gen["content"],
            derived_from=gen.get("derived_from"),
            attributed_to=state.user.id,
            schema_version=gen.get("schema_version"),
        )

        response_item = {
            "entity": gen.get("ref") or str(EntityRef(
                type=gen["type"],
                entity_id=gen["entity_id"],
                version_id=gen["version_id"],
            )),
            "type": gen["type"],
            "content": gen["content"],
        }
        if gen.get("schema_version") is not None:
            response_item["schemaVersion"] = gen["schema_version"]
        state.generated_response.append(response_item)

    # 2. Persist generated externals.
    for ext_uri in state.generated_externals:
        # Deterministic entity_id derived from dossier + URI so the same
        # external referenced multiple times in the dossier collapses
        # to one logical entity.
        ext_entity_id = uuid_mod.uuid5(
            uuid_mod.NAMESPACE_URL, f"{state.dossier_id}:{ext_uri}",
        )
        ext_version_id = uuid4()
        await state.repo.create_entity(
            version_id=ext_version_id,
            entity_id=ext_entity_id,
            dossier_id=state.dossier_id,
            type="external",
            generated_by=state.activity_id,
            content={"uri": ext_uri},
            attributed_to=state.user.id,
        )
        state.generated_response.append({
            "entity": ext_uri,
            "type": "external",
            "content": {"uri": ext_uri},
        })

    # 3. Tombstone redactions. Per the deletion-scope decision (option a),
    # we only NULL the `content` blob and stamp `tombstoned_by`; the rows,
    # derivation edges, schema_version, and used links survive. Runs AFTER
    # the replacement (step 1 above) is in place, so the new revision
    # exists before the originals are nulled. The replacement and any
    # system:note entities generated by this same activity are not in
    # `tombstone_version_ids` (they're new rows, not used rows) so they
    # are untouched.
    if state.tombstone_version_ids:
        await state.repo.tombstone_entity_versions(
            state.tombstone_version_ids, state.activity_id,
        )

    # 4. `Used` link rows. References only — no overlap with generated,
    # which is enforced earlier by `enforce_used_generated_disjoint`.
    # UsedRef always has a version_id; no guard needed.
    for ref in state.used_refs:
        await state.repo.create_used(state.activity_id, ref.version_id)

    # 5. Relation rows (oe:neemtAkteVan and any other plugin-defined
    # PROV-extension relations).
    for rel in state.validated_relations:
        await state.repo.create_relation(
            activity_id=state.activity_id,
            entity_version_id=rel.version_id,
            relation_type=rel.relation_type,
        )

    # 6. Domain relations — semantic links between entities/URIs.
    # These go into the domain_relations table, not activity_relations.
    for rel in state.validated_domain_relations:
        await state.repo.create_domain_relation(
            dossier_id=state.dossier_id,
            relation_type=rel.relation_type,
            from_ref=rel.from_ref,
            to_ref=rel.to_ref,
            created_by_activity_id=state.activity_id,
        )

    # 7. Domain relation removals — supersede active relations.
    for rel in state.validated_remove_relations:
        await state.repo.supersede_domain_relation(
            dossier_id=state.dossier_id,
            relation_type=rel.relation_type,
            from_ref=rel.from_ref,
            to_ref=rel.to_ref,
            superseded_by_activity_id=state.activity_id,
        )
