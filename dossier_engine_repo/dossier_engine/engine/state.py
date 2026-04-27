"""
Mutable state object for the activity execution pipeline.

The orchestrator (`engine.execute_activity`) builds an `ActivityState`
from its arguments and threads it through every pipeline phase. Each
phase reads the fields it needs and writes the fields it produces. The
phase function's docstring documents which fields it reads and writes,
under `Reads:` and `Writes:` sections, so the data flow is visible
without needing to read function bodies.

Why mutable, not pure-functional?
================================
This was a deliberate trade-off. A pure-functional pipeline would have
each phase return a new state object — safer in theory but the function
signatures and threading boilerplate would dominate the orchestrator
and obscure the shape we're trying to make readable. The mutable
discipline keeps the orchestrator a 25-line "table of contents" that
reads top-to-bottom like the numbered phases in the design brief.

The convention is: a phase only writes to the fields its docstring
declares. If a phase needs to read a field its predecessor was supposed
to set, but the predecessor didn't run (e.g. early return), the phase
reads `None` and decides what to do — usually skip itself.

Initialization
==============
Most fields default to None or empty collections. The orchestrator
populates the input fields (the ones derived from request parameters)
when constructing the state object, then the phases populate everything
else as they run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from ..auth import User
from ..db.models import ActivityRow, EntityRow, Repository
from ..plugin import Plugin


# ===================================================================
# Typed pipeline data
# ===================================================================
#
# These dataclasses replace `list[dict]` / `dict[str, ...]` fields in
# ActivityState. The shape used to be documented only in the state's
# docstrings; making them real types means:
#
#   * IDE autocomplete works on the fields
#   * A typo in a field name (``version_di`` instead of ``version_id``)
#     is caught at construction, not at the silent downstream read
#   * Optional vs required is expressed in the signature, not inferred
#     by reading `.get(key)` call sites
#
# The shapes mirror what the previous dict-based code used, so the
# migration is mechanical. Each field is individually documented; no
# docstring-only shapes remain.


@dataclass(frozen=True)
class UsedRef:
    """A resolved `used` reference, produced by the `used` phase.

    Versioned entity reference that the activity reads. For local
    entities, ``type`` is set. For external URIs, ``external=True``
    and ``type`` stays None (externals aren't typed in our schema).
    For system-caller auto-resolution (worker scheduled activities),
    ``auto_resolved=True``.
    """

    entity: str
    version_id: UUID
    type: str | None = None
    external: bool = False
    auto_resolved: bool = False


@dataclass(frozen=True)
class ValidatedRelation:
    """A process-control relation staged for persistence.

    Process-control relations link an activity to an entity (the
    ``used`` equivalent for relations — e.g. ``oe:neemtAkteVan``).
    One row per validated relation, persisted to the
    ``activity_relations`` table.
    """

    version_id: UUID
    relation_type: str
    ref: str


@dataclass(frozen=True)
class DomainRelationEntry:
    """A domain relation staged for persistence or removal.

    Domain relations link two entities (or an entity and an external
    URI) with an ontological predicate — e.g.
    ``<aanvraag> oe:betreft <erfgoedobject>``. Refs are stored as
    full IRIs; ``expand_ref`` resolves shorthand before staging.

    Shared between ``validated_domain_relations`` (add) and
    ``validated_remove_relations`` (remove) since the shape is
    identical — the phase that produced it knows which is which.
    """

    relation_type: str
    from_ref: str  # full IRI
    to_ref: str    # full IRI


class Caller(str, Enum):
    """Who initiated this activity execution.

    * `CLIENT` — a user-facing API call. The client supplies `used`,
      `generated`, and `relations` blocks explicitly; auto-resolve of
      unlisted used entities is NOT performed.
    * `SYSTEM` — a worker or scheduled task. The caller may omit used
      entries entirely and the engine auto-resolves them via the
      trigger activity's scope. Used for side effects and for worker-
      executed scheduled activities.

    Inheriting from `str` makes the enum values compare equal to their
    underlying strings, so legacy call sites that pass `"system"` or
    `"client"` still work during a migration period.
    """
    CLIENT = "client"
    SYSTEM = "system"


@dataclass
class ActivityState:
    """All state that flows through the activity execution pipeline.

    Phases mutate this object as they run. Each phase function declares
    which fields it reads and writes via its docstring `Reads:` and
    `Writes:` sections — that's the discipline that keeps mutation
    tractable.
    """

    # ---- Inputs (set by orchestrator from request parameters) ----

    plugin: Plugin
    activity_def: dict
    repo: Repository
    dossier_id: UUID
    activity_id: UUID
    user: User
    role: str
    used_items: list[dict]
    generated_items: list[dict]
    relation_items: list[dict]
    remove_relation_items: list[dict] = field(default_factory=list)
    workflow_name: str | None = None
    informed_by: str | None = None
    skip_cache: bool = False
    caller: Caller = Caller.CLIENT

    # The wall-clock instant the orchestrator started executing this
    # activity. Used as `started_at` on the activity row and for any
    # phase that needs a stable "now" (e.g. task scheduling).
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ---- Phase outputs ----

    # Set by `ensure_dossier`. The dossier row, either fetched or
    # freshly created. None until the phase runs.
    dossier: Any = None

    # Set by the `used` phase. Each entry is a `UsedRef` — a resolved
    # versioned reference to an entity the activity reads. Used by
    # persistence to create used-link rows, by invariant checks for
    # overlap detection, and by relation validators that need to look
    # up entities by their original ref.
    used_refs: list[UsedRef] = field(default_factory=list)

    # Set by the `used` phase. Maps entity type → most recent
    # `EntityRow` of that type that the activity touched. Passed to
    # handlers via `ActivityContext` so they can call
    # `context.get_typed("oe:foo")` and get back a Pydantic instance.
    resolved_entities: dict[str, EntityRow] = field(default_factory=dict)

    # Set by the `used` phase. Maps a raw ref string → its resolved
    # `EntityRow`. Passed to relation validators which need to look up
    # entities by their original ref form (e.g. for `oe:neemtAkteVan`
    # ack target lookups).
    used_rows_by_ref: dict[str, EntityRow] = field(default_factory=dict)

    # Set by the `generated` phase. Each item is a dict with `type`,
    # `entity_id`, `version_id`, `content`, optional `derived_from`,
    # optional `schema_version`. This is the canonical list of entities
    # that will be persisted in the persistence phase.
    generated: list[dict] = field(default_factory=list)

    # Set by the `generated` phase. External URIs the activity emits.
    # Persisted as `type=external` rows so they show up in the PROV graph.
    generated_externals: list[str] = field(default_factory=list)

    # Set by the `relations` phase. Process-control relations ready
    # to be persisted to `activity_relations`. Each entry is a
    # `ValidatedRelation`.
    validated_relations: list[ValidatedRelation] = field(default_factory=list)

    # Set by the `relations` phase. Maps relation type → list of raw
    # entries the client sent for that type. Used by the validator
    # dispatch loop to feed the right slice into each registered
    # validator.
    relations_by_type: dict[str, list[dict]] = field(default_factory=dict)

    # Set by the `relations` phase for domain-kind relations (those
    # with `from` + `to` instead of `entity`). Each entry is a
    # `DomainRelationEntry` with full-IRI refs.
    validated_domain_relations: list[DomainRelationEntry] = field(default_factory=list)

    # Set by the `relations` phase for domain relations to remove.
    # Same shape as add — `DomainRelationEntry`.
    validated_remove_relations: list[DomainRelationEntry] = field(default_factory=list)

    # Set by the `tombstone` phase iff the activity is the built-in
    # `tombstone` activity. List of version_ids whose content should be
    # nulled after the replacement is persisted.
    tombstone_version_ids: list[UUID] = field(default_factory=list)

    # Set by the `check_exceptions` phase when the activity's workflow
    # rules would fail but an active matching ``oe:exception`` exists.
    # Carries the exception's version_id so later phases can identify
    # which exception to consume. When non-None:
    #   * ``check_workflow_rules`` skips the structural-rules check —
    #     the exception is an explicit administrative override.
    #   * ``execute_side_effects`` injects a ``consumeException``
    #     follow-up activity into the effective side-effect list,
    #     which revises the exception with ``status: consumed`` —
    #     enforcing the single-use-by-default contract.
    # The exception itself is appended to ``used_refs`` /
    # ``resolved_entities`` / ``used_rows_by_ref`` by the same phase
    # so the PROV graph correctly records "this activity used the
    # exception". That usage edge is what makes ``consumeException``'s
    # side-effect auto-resolve work: its ``used: [oe:exception]``
    # slot gets filled from the trigger's used list via
    # ``resolve_from_prefetched``.
    exempted_by_exception: UUID | None = None

    # Set by the persistence phase. The list of dicts that goes into
    # the activity response's `generated` array — one entry per
    # persisted entity (local + external), with `entity` ref string,
    # `type`, `content`, and optional `schemaVersion`. Used by the
    # response builder to give the client a manifest of what was
    # actually written.
    generated_response: list[dict] = field(default_factory=list)

    # Set by the `create_activity_row` phase. The persisted activity row.
    activity_row: ActivityRow | None = None

    # Set by the `handler` phase. The result the plugin handler returned,
    # if the activity has a handler. None for handler-less activities.
    handler_result: Any = None

    # Set by the `handler` phase. Generated entities the handler appended
    # via `HandlerResult.generated`. Merged into `generated` before
    # persistence.
    handler_generated: list[dict] = field(default_factory=list)

    # Set by the `handler` phase. Tasks the handler appended via
    # `HandlerResult.tasks`. Merged with the activity's YAML tasks
    # before the tasks phase runs.
    handler_tasks: list[dict] = field(default_factory=list)

    # Set by the `handler` phase. Status override returned by the
    # handler, if any. Takes precedence over the activity's YAML status.
    handler_status: str | None = None

    # Set by the `status` phase. The final dossier status after this
    # activity runs. Stored on the activity row and used by the
    # post-activity hook and the response.
    final_status: str | None = None

    # Set by the `finalize` phase. The dossier's status as derived
    # AFTER side effects + tasks have run (which may have moved the
    # status forward via further activities). Used by the post-activity
    # hook and the response. Distinct from `final_status` which is the
    # status the *current* activity computed for itself.
    current_status: str | None = None

    # Set by the `finalize` phase. The list of `{type, label}` dicts
    # describing which activities the calling user may run next. Empty
    # when `skip_cache` is true (bulk path).
    allowed_activities: list[dict] = field(default_factory=list)
