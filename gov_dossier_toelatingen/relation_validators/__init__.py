"""
Relation validators for the toelatingen workflow.

These are invoked by the engine when processing the generic `relations`
block on an activity request. Each validator is registered under a relation
type string and receives the full activity context (resolved used rows,
pending generated items, the relation entries of its type). Validators
raise `ActivityError` to reject the request; returning normally means
"accepted." The engine does not interpret any return value.

The oe:neemtAkteVan validator owns the entire staleness story: it detects
stale used references by querying the repo, cross-references them against
its incoming relation entries, and raises 409 `stale_used_reference` if
any stale used items aren't covered by matching acknowledgements. The
engine itself knows nothing about staleness.
"""

from __future__ import annotations

from uuid import UUID

from gov_dossier_engine.engine import ActivityError
from gov_dossier_engine.plugin import Plugin
from gov_dossier_engine.db.models import Repository, EntityRow


async def validate_neemt_akte_van(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    activity_def: dict,
    entries: list[dict],
    used_rows_by_ref: dict[str, EntityRow],
    generated_items: list[dict],
) -> None:
    """Enforce: for every used reference that is NOT the latest version of
    its entity_id, the client must supply an `oe:neemtAkteVan` relation for
    every intervening newer version. Otherwise raise 409 stale_used_reference.

    Also rejects (422 unrelated_acknowledgement) any neemtAkteVan entry that
    doesn't correspond to an intervening version of some stale used entry —
    acknowledging an unrelated entity is a client bug.
    """
    # Step 1: detect stale used references by querying the repo.
    # Each stale entry records the declared version, the current latest,
    # and the list of intervening version ids (strictly newer than
    # declared, oldest-first).
    stale: list[dict] = []
    for ref, row in used_rows_by_ref.items():
        latest = await repo.get_latest_entity_by_id(dossier_id, row.entity_id)
        if latest is None or latest.id == row.id:
            continue
        all_versions = await repo.get_entity_versions(dossier_id, row.entity_id)
        intervening = [
            v.id for v in all_versions
            if v.created_at > row.created_at
        ]
        stale.append({
            "entity_ref": ref,
            "entity_type": row.type,
            "entity_logical_id": row.entity_id,
            "declared_version": row.id,
            "latest_version": latest.id,
            "intervening_version_ids": intervening,
        })

    # Step 2: validate each incoming neemtAkteVan entry corresponds to an
    # intervening version of some stale entry, AND track which stale
    # entries are fully acknowledged.
    intervening_to_stale: dict[UUID, dict] = {}
    for s in stale:
        for v in s["intervening_version_ids"]:
            intervening_to_stale[v] = s

    covered_per_stale: dict[UUID, set[UUID]] = {
        s["entity_logical_id"]: set() for s in stale
    }

    for entry in entries:
        ack_row = entry["entity_row"]
        ack_version = ack_row.id
        ref = entry["ref"]

        stale_entry = intervening_to_stale.get(ack_version)
        if stale_entry is None:
            raise ActivityError(
                422,
                f"oe:neemtAkteVan acknowledges {ref} but that version is not "
                f"an intervening version of any stale used reference in this "
                f"activity. Only newer versions of entities listed in `used` "
                f"can be acknowledged.",
                payload={
                    "error": "unrelated_acknowledgement",
                    "acknowledged": ref,
                },
            )
        covered_per_stale[stale_entry["entity_logical_id"]].add(ack_version)

    # Step 3: any stale entry whose intervening versions aren't all covered
    # is a hard error.
    unsatisfied = []
    for s in stale:
        needed = set(s["intervening_version_ids"])
        if not needed:
            continue
        if covered_per_stale[s["entity_logical_id"]] < needed:
            unsatisfied.append(s)

    if unsatisfied:
        first = unsatisfied[0]
        latest_row = await repo.get_entity(first["latest_version"])
        raise ActivityError(
            409,
            f"Used reference {first['entity_ref']} is stale: latest version "
            f"of {first['entity_type']}/{first['entity_logical_id']} is "
            f"{first['latest_version']}. To proceed with an older version, "
            f"acknowledge the newer versions via 'oe:neemtAkteVan'.",
            payload={
                "error": "stale_used_reference",
                "stale": [
                    {
                        "entity_ref": s["entity_ref"],
                        "entity_type": s["entity_type"],
                        "declared_version": str(s["declared_version"]),
                        "latest_version": str(s["latest_version"]),
                        "intervening_versions": [
                            str(v) for v in s["intervening_version_ids"]
                        ],
                    }
                    for s in unsatisfied
                ],
                "latest_version": {
                    "entity": f"{first['entity_type']}/{first['entity_logical_id']}@{first['latest_version']}",
                    "versionId": str(first["latest_version"]),
                    "content": latest_row.content if latest_row else None,
                },
            },
        )


RELATION_VALIDATORS = {
    "oe:neemtAkteVan": validate_neemt_akte_van,
}
