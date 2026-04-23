"""
Activity-visibility filtering.

Shared by the dossier-detail, PROV-JSON, and PROV-graph endpoints.
Each determines which activities in a dossier's timeline a given
user is allowed to see, based on the ``activity_view`` setting from
the matched access entry.

The ``activity_view`` value in an access entry can be:

* ``"all"`` — every activity is visible.
* ``"own"`` — only activities where the user is the PROV agent.
* A ``list[str]`` of activity type names — only those types.
* A ``dict`` combining a base mode with an include-list::

      activity_view:
        mode: "own"
        include: ["neemBeslissing"]

  This means "show my own activities, PLUS always show any
  ``neemBeslissing`` regardless of who performed it."

The ``"related"`` mode (activities that touched visible entities,
plus the user's own) was removed in Round 31 — it wasn't used in
production and the semantics were confusing enough that operators
couldn't describe it without looking at the code. Stale configs
still carrying ``"related"`` fall through to a deny-safe default
so the change doesn't silently flip visibility.

All forms are handled by :func:`is_activity_visible`, which takes
the raw ``activity_view`` value (string, list, or dict) and
returns True/False for a single activity. Callers loop over their
activity list and call this once per activity — the function is
deliberately stateless so it can be used in both the "build a
filtered list" pattern (PROV-JSON) and the "accumulate a skip-set"
pattern (PROV graph).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class ActivityViewMode:
    """Parsed activity-view configuration. Immutable after creation;
    safe to store on request state and pass around."""

    base: str = "all"
    """One of ``"all"``, ``"own"``, or ``"list"``. The value
    ``"related"`` was valid before Round 31 and is still preserved
    verbatim when it appears inside a dict shape's ``mode`` field;
    :func:`is_activity_visible` routes all unrecognized base values
    to a deny-safe ``return False`` so stale configs don't silently
    flip behaviour."""

    include: frozenset[str] = field(default_factory=frozenset)
    """Activity type names that are always visible regardless of the
    base mode. Empty means no unconditional includes."""

    explicit_types: frozenset[str] = field(default_factory=frozenset)
    """When base is ``"list"``, the set of allowed type names."""


def parse_activity_view(raw: str | list[str] | dict | None) -> ActivityViewMode:
    """Normalise the raw ``activity_view`` value from an access entry
    into an :class:`ActivityViewMode`.

    Accepts every form the access system produces:

    * ``None`` or ``"all"`` → show everything.
    * ``"own"`` → only activities where the user is the agent.
    * ``["dienAanvraagIn", "neemBeslissing"]`` → explicit type list.
    * ``{"mode": "own", "include": ["neemBeslissing"]}`` → combined.

    Anything else — unrecognized strings (including legacy ``"related"``,
    removed in Round 31), non-string/list/dict types — falls through to
    a deny-safe ``ActivityViewMode(base="list", explicit_types=frozenset())``.
    This means stale configs surface as empty timelines rather than silent
    semantic changes; see Round 31 writeup for rationale.
    """
    if raw is None or raw == "all":
        return ActivityViewMode(base="all")

    if raw == "own":
        return ActivityViewMode(base="own")

    if isinstance(raw, list):
        return ActivityViewMode(base="list", explicit_types=frozenset(raw))

    if isinstance(raw, dict):
        base = raw.get("mode", "own")
        include = frozenset(raw.get("include", []))
        if isinstance(base, list):
            return ActivityViewMode(
                base="list",
                explicit_types=frozenset(base),
                include=include,
            )
        # Note: ``base`` here can still be any string the caller chose
        # (including legacy ``"related"`` or typos). ``is_activity_visible``
        # evaluates the result and falls through to ``return False`` for
        # any base it doesn't recognize, which is the deny-safe shape. The
        # include list is still honoured — a stale ``mode`` doesn't erase
        # the explicit include list the operator wrote.
        return ActivityViewMode(base=base, include=include)

    # Unrecognised top-level shape (or unrecognised string that didn't
    # match ``"all"`` / ``"own"``) → deny-safe default.
    return ActivityViewMode(base="list", explicit_types=frozenset())


async def is_activity_visible(
    mode: ActivityViewMode,
    *,
    activity_type: str,
    activity_id: UUID,
    user_id: str,
    visible_entity_ids: set[UUID],
    lookup_is_agent,
    lookup_used_entity_ids,
) -> bool:
    """Evaluate whether a single activity should be visible to the
    user under the given :class:`ActivityViewMode`.

    The two ``lookup_*`` callables abstract over how agent and
    used-entity data is fetched — the dossier-detail endpoint uses
    DB queries, while the prov endpoints pre-load everything into
    dicts and look up from there.

    Parameters:

    * ``lookup_is_agent(activity_id, user_id) → bool`` — returns
      True if the user is the PROV agent for this activity.
    * ``lookup_used_entity_ids(activity_id) → set[UUID]`` — returns
      the set of entity version IDs that this activity used.
    """
    # Include-list always wins: named types are unconditionally
    # visible regardless of the base mode.
    if mode.include and activity_type in mode.include:
        return True

    if mode.base == "all":
        return True

    if mode.base == "own":
        return await lookup_is_agent(activity_id, user_id)

    if mode.base == "list":
        return activity_type in mode.explicit_types

    # Unrecognised base (including legacy ``"related"``, removed in
    # Round 31) → deny-safe. parse_activity_view already routes most
    # unknown strings through the ``base="list", explicit_types=frozenset()``
    # branch, but the dict form preserves ``mode`` verbatim so a stale
    # ``{"mode": "related"}`` config lands here.
    return False
