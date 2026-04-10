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
