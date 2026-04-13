"""
Lineage walking — backwards traversal of the PROV graph.

Given an entity version, follow the `wasGeneratedBy → used` chain backwards
to discover the entities that contributed to it. Two query shapes:

* `trace_chain` — picks one parent at each branch and returns a flat list
  newest-first. Use for breadcrumb-style UIs ("this signature was made on
  this beslissing, which decided on this aanvraag, which referenced this
  object").

* `trace_ancestry` — explores all parents at every branch and returns every
  ancestor entity exactly once, topologically ordered (oldest first). Use
  for audit queries ("show me everything that contributed to this entity").

Both stop at:
* root entities (`generated_by IS NULL`) — typically external references and
  the very first entity created in a dossier
* `max_depth` — defensive bound, default 50

Both can be called from anywhere that has a `Repository`. In handlers,
that's `context.repo`.
"""

from __future__ import annotations

from typing import Callable, Optional
from uuid import UUID

from .db.models import EntityRow, Repository


# How to pick one parent when an activity used multiple entities. Receives
# the candidate parent rows and the entity we walked from. Returns the chosen
# row, or None to stop the chain here.
PrimaryParentFn = Callable[[list[EntityRow], EntityRow], Optional[EntityRow]]


def default_primary_parent(parents: list[EntityRow], current: EntityRow) -> Optional[EntityRow]:
    """Default rule for picking the primary parent of `current`:

    1. A parent with the same type as `current` (the version-history walk)
    2. Otherwise the first non-external parent
    3. Otherwise the first external parent
    4. Otherwise None
    """
    if not parents:
        return None
    same_type = [p for p in parents if p.type == current.type]
    if same_type:
        # Newest first if there are multiple — usually there's just one.
        return max(same_type, key=lambda p: p.created_at)
    non_external = [p for p in parents if p.type != "external"]
    if non_external:
        return non_external[0]
    return parents[0]


async def _fetch_parents(repo: Repository, entity: EntityRow) -> list[EntityRow]:
    """Return the entities that the activity which generated `entity` used as
    inputs. Empty list if `entity` is a root (no generating activity)."""
    if entity.generated_by is None:
        return []
    used_version_ids = await repo.get_used_entity_ids_for_activity(entity.generated_by)
    parents: list[EntityRow] = []
    for vid in used_version_ids:
        parent = await repo.get_entity(vid)
        if parent is not None:
            parents.append(parent)
    return parents


async def trace_chain(
    repo: Repository,
    start_version_id: UUID,
    *,
    primary_parent: PrimaryParentFn = default_primary_parent,
    max_depth: int = 50,
) -> list[EntityRow]:
    """Walk backwards from `start_version_id`, following one parent per step.

    Returns the chain newest-first, including the starting entity. Stops at
    roots, when `primary_parent` returns None, or at `max_depth`.

    For your example (handtekening → beslissing → aanvraag → object):

        chain = await trace_chain(repo, handtekening_version_id)
        for e in chain:
            print(e.type, e.id)
        # oe:handtekening, oe:beslissing, oe:aanvraag, external
    """
    start = await repo.get_entity(start_version_id)
    if start is None:
        return []

    chain: list[EntityRow] = [start]
    current = start

    for _ in range(max_depth):
        parents = await _fetch_parents(repo, current)
        if not parents:
            break
        chosen = primary_parent(parents, current)
        if chosen is None:
            break
        chain.append(chosen)
        current = chosen

    return chain


async def trace_ancestry(
    repo: Repository,
    start_version_id: UUID,
    *,
    max_depth: int = 50,
) -> list[EntityRow]:
    """Walk backwards from `start_version_id`, following ALL parents at every
    branch.

    Returns every ancestor entity (including the start) exactly once,
    topologically ordered (ancestors before descendants). Stops at roots and
    at `max_depth` (depth is measured from the start, not total node count).

    Use this for audit queries or full-provenance views. For breadcrumb UIs
    use `trace_chain` instead.
    """
    start = await repo.get_entity(start_version_id)
    if start is None:
        return []

    # BFS with depth tracking. We want every reachable ancestor exactly once,
    # and we want to return them ancestors-first so a caller iterating the
    # list can build dependency-order computations.
    seen: dict[UUID, EntityRow] = {start.id: start}
    depth: dict[UUID, int] = {start.id: 0}
    frontier: list[EntityRow] = [start]

    while frontier:
        next_frontier: list[EntityRow] = []
        for entity in frontier:
            if depth[entity.id] >= max_depth:
                continue
            parents = await _fetch_parents(repo, entity)
            for parent in parents:
                if parent.id in seen:
                    continue
                seen[parent.id] = parent
                depth[parent.id] = depth[entity.id] + 1
                next_frontier.append(parent)
        frontier = next_frontier

    # Topological order = ancestors first. We tracked depth from the start
    # entity, so larger depth = older. Sort descending by depth, then by
    # created_at as a tiebreaker for entities at the same depth.
    return sorted(
        seen.values(),
        key=lambda e: (-depth[e.id], e.created_at),
    )


async def find_related_entity(
    repo: Repository,
    dossier_id: UUID,
    start_entity: EntityRow,
    target_type: str,
    *,
    max_hops: int = 10,
) -> Optional[EntityRow]:
    """Find an entity of `target_type` that is related to `start_entity` by
    walking backwards through the activity graph.

    Unlike `trace_chain` and `trace_ancestry` (which walk the derivation
    graph through `derived_from` and `used`), this walker traverses the
    **activity** graph: starting from the activity that generated
    `start_entity`, inspect what that activity touched (generated + used).
    If the target type appears there unambiguously, return it. Otherwise,
    expand to the activities that generated the used entities, and to the
    activity that informed this one. Repeat up to `max_hops` levels.

    This is the right tool when you need to answer "given that I have a
    beslissing, what aanvraag was it made about?" even though the beslissing
    is not derived from the aanvraag — the relationship runs through the
    doeVoorstelBeslissing activity that used the aanvraag and generated the
    beslissing.

    Returns the EntityRow (latest version of the matching entity_id) if a
    unique match is found. Returns None if:
    * the start entity is a root (no generating activity to walk from)
    * no match is found within max_hops
    * at any visited activity, multiple distinct entities of `target_type`
      are present (ambiguous — the caller must disambiguate)

    The trivial case where `start_entity.type == target_type` returns the
    start entity itself without walking.
    """
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

            # No match here — expand frontier backwards.
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
