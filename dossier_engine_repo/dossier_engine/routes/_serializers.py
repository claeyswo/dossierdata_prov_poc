"""
Entity serialization for response payloads.

Bulk version-listing endpoints render every version of an entity (or
every version of an entity type) as a JSON dict. The shape rules:

* Always include `versionId`, `content`, `generatedBy`, `derivedFrom`,
  `attributedTo`, `createdAt`.
* Include `entityId` unless the caller is rendering inside a list
  that's already keyed by entity_id (in which case the key carries the
  identity, no need to repeat it in every value).
* Include `schemaVersion` only when set (legacy NULL-version entities
  drop the field entirely rather than rendering `null`).
* For tombstoned versions, keep the row in the response but set
  `content: null`, add `tombstonedBy` (the activity UUID that
  performed the redaction), and add `redirectTo` pointing at the
  live replacement.

The replacement-finding logic for `redirectTo`: walk the row's
siblings (same `entity_id`, different version), prefer the live
(non-tombstoned) ones, fall back to all siblings if everything has
been tombstoned (re-tombstoning is allowed). Pick whichever has the
latest `created_at`.
"""

from __future__ import annotations

from datetime import datetime, timezone


def entity_version_dict(
    e,
    dossier_id,
    entity_type: str,
    siblings: list,
    include_entity_id: bool = True,
) -> dict:
    """Render an EntityRow as a dict for the bulk version-listing
    endpoints. See module docstring for shape rules."""
    out = {
        "versionId": str(e.id),
        "content": e.content,
        "generatedBy": str(e.generated_by) if e.generated_by else None,
        "derivedFrom": str(e.derived_from) if e.derived_from else None,
        "attributedTo": e.attributed_to,
        "createdAt": e.created_at.isoformat() if e.created_at else None,
    }
    if include_entity_id:
        out["entityId"] = str(e.entity_id)
    if e.schema_version is not None:
        out["schemaVersion"] = e.schema_version

    if e.tombstoned_by is not None:
        out["tombstonedBy"] = str(e.tombstoned_by)
        # Find the live replacement: latest sibling with same
        # entity_id that isn't this row.
        candidates = [
            s for s in siblings
            if s.entity_id == e.entity_id and s.id != e.id
        ]
        live = [c for c in candidates if c.tombstoned_by is None]
        target_pool = live if live else candidates
        if target_pool:
            replacement = max(
                target_pool,
                key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc),
            )
            out["redirectTo"] = (
                f"/dossiers/{dossier_id}/entities/{entity_type}/"
                f"{replacement.entity_id}/{replacement.id}"
            )
    return out
