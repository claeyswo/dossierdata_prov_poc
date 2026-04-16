"""
Activity-graph traversal for sideways entity lookup.

Given an entity, find a related entity of a different type by walking
the PROV activity graph backwards. Unlike a pure derivation walk
(which follows `derived_from` and `used` edges from one version to
its parents), this walker inspects every activity it visits in full:
both the entities the activity used AND the entities it co-generated,
plus the activity it was informed by.

The canonical use case is anchoring a scheduled task to an entity
that the triggering activity didn't touch directly. For example,
`tekenBeslissing` uses a `beslissing` but not the `aanvraag` the
beslissing was made about. The handler that runs afterwards still
needs the aanvraag's `entity_id` to anchor a `trekAanvraagIn`
scheduled task — walking from the beslissing through its generating
activity (`doeVoorstelBeslissing`) finds the aanvraag in that
activity's used block.

Semantics:

* Starts at `start_entity.generated_by` and walks backwards through
  `used` entities' generating activities AND through `informed_by`.
* At each visited activity, checks both `generated` and `used` for
  an entity of `target_type`.
* Returns the match if exactly one distinct `entity_id` of that type
  appears at a visited activity.
* Returns None on ambiguity (multiple distinct entity_ids of the
  target type at one activity) — the caller must disambiguate.
* Returns None if the start entity is a root (no generating activity
  to walk from) or if the max_hops budget is exhausted.
* Returns the start entity itself if `start_entity.type == target_type`
  (trivial case, no walk needed).
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from .db.models import EntityRow, Repository


async def find_related_entity(
    repo: Repository,
    dossier_id: UUID,
    start_entity: EntityRow,
    target_type: str,
    *,
    max_hops: int = 10,
) -> Optional[EntityRow]:
    """Find an entity of `target_type` related to `start_entity` by
    walking the activity graph backwards. See module docstring for
    semantics."""
    if start_entity.type == target_type:
        return start_entity

    if start_entity.generated_by is None:
        # External or root entity — no activity to walk from.
        return None

    visited_activities: set[UUID] = set()
    frontier: list[UUID] = [start_entity.generated_by]

    for _ in range(max_hops):
        if not frontier:
            return None

        next_frontier: list[UUID] = []
        for activity_id in frontier:
            if activity_id in visited_activities:
                continue
            visited_activities.add(activity_id)

            # What did this activity touch? generated + used.
            generated = await repo.get_entities_generated_by_activity(activity_id)
            used = await repo.get_used_entities_for_activity(activity_id)
            all_touched = generated + used

            # Target type present at this activity?
            candidates = [e for e in all_touched if e.type == target_type]
            if candidates:
                entity_ids = {e.entity_id for e in candidates}
                if len(entity_ids) == 1:
                    # Return the current latest version of this entity_id.
                    return await repo.get_latest_entity_by_id(
                        dossier_id, candidates[0].entity_id,
                    )
                return None  # ambiguous at this activity

            # No match here — expand the frontier backwards.
            # (a) Through each used entity's generating activity.
            for used_entity in used:
                if used_entity.generated_by is not None:
                    next_frontier.append(used_entity.generated_by)
            # (b) Through the informed_by chain.
            activity_row = await repo.get_activity(activity_id)
            if activity_row is not None and activity_row.informed_by is not None:
                next_frontier.append(activity_row.informed_by)

        frontier = next_frontier

    return None
