"""
Common entity models provided by the engine.
These are shared across all workflow plugins.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel
from typing import Literal, Optional, Union


class DossierAccessEntry(BaseModel):
    role: Optional[str] = None
    agents: list[str] = []
    view: list[str] = []
    # Activity visibility. Four accepted shapes:
    #
    # * ``"all"`` — every activity visible.
    # * ``"own"`` — only activities where the user is the PROV agent.
    # * ``list[str]`` — only activities whose type is in the list.
    # * ``dict`` with ``mode: "own"`` + ``include: [<types>]`` — "own"
    #   plus an unconditional include-list.
    #
    # The ``"related"`` mode was removed in Round 31; Pydantic rejects
    # it at write time (operators get a validation error on
    # ``setDossierAccess``) and ``parse_activity_view`` deny-safes it
    # at read time for any legacy entry already in the DB. See
    # ``routes/_helpers/activity_visibility.py`` module docstring for the
    # read-path semantics.
    activity_view: Union[Literal["all", "own"], list[str], dict] = "own"


class DossierAccess(BaseModel):
    access: list[DossierAccessEntry]


class TaskEntity(BaseModel):
    """Content model for system:task entities.

    Tasks go through this lifecycle:
        scheduled → completed           (success, first attempt)
        scheduled → scheduled → ...     (transient failure, retry with backoff)
        scheduled → dead_letter         (exhausted max_attempts, terminal)
        scheduled → cancelled           (cancel_if_activities triggered)
        scheduled → superseded          (replaced by another task with the same target_activity)

    Retry semantics. On execution failure, the worker increments
    `attempt_count` and either:

    * Sets `status = "dead_letter"` if `attempt_count >= max_attempts`.
      Dead-lettered tasks are terminal and never picked up by the poll
      loop again — an operator must requeue them (via the
      `--requeue-dead-letters` CLI flag) after fixing whatever was
      causing the failures.
    * Sets `next_attempt_at` to now + exponential backoff
      (`base_delay_seconds * 2**(attempt_count - 1)` ± 10% jitter)
      and leaves `status = "scheduled"` so the poll loop picks it up
      once the delay elapses. The original `scheduled_for` is
      preserved as a historical record of when the task was first
      queued; the poll filter checks both `scheduled_for <= now`
      and `next_attempt_at <= now` (when set).

    `max_attempts` and `base_delay_seconds` can be overridden per
    task when it's scheduled; absent defaults come from the TaskEntity
    defaults below. `attempt_count` starts at 0 and is only touched by
    the worker on failure.

    Error telemetry — stack traces, exception details, breadcrumbs —
    is sent to the Python `logging` system via `logger.exception(...)`
    and is NOT stored on the task entity. Deployments wire that
    logging to Sentry (or Datadog, or Honeycomb — whatever the
    platform is); the task content only carries operational state
    that the worker's poll loop needs to make retry decisions.
    `last_attempt_at` is kept because it's cheap and useful for
    human psql queries ("what tasks tried in the last hour"); the
    full error history lives in the telemetry backend, keyed by
    task_id.
    """
    kind: Literal[
        "fire_and_forget", "recorded",
        "scheduled_activity", "cross_dossier_activity",
    ]
    function: Optional[str] = None      # plugin task function name
    target_activity: Optional[str] = None   # for kinds 3, 4
    target_dossier: Optional[str] = None    # for kind 4 (set by worker after function call)
    result_activity_id: Optional[str] = None  # pre-generated UUID for the scheduled activity
    scheduled_for: Optional[str] = None     # ISO datetime — original schedule, immutable
    cancel_if_activities: list[str] = []
    allow_multiple: bool = False
    # Bug 39 (Round 32): tightened from ``str`` to ``Literal[...]``.
    # See the lifecycle diagram in the class docstring for the five
    # valid values. Legacy DB rows are safe — the set hasn't changed
    # since the initial schema migration (9d887db892c9) and all five
    # values are actively written by production code.
    status: Literal[
        "scheduled", "completed", "cancelled",
        "superseded", "dead_letter",
    ] = "scheduled"
    result: Optional[str] = None        # URI or result data after completion

    # Retry policy state. All optional with sensible defaults so existing
    # tasks in the database continue to deserialize without migration.
    attempt_count: int = 0
    max_attempts: int = 3
    base_delay_seconds: int = 60
    last_attempt_at: Optional[str] = None   # ISO datetime, most recent attempt
    next_attempt_at: Optional[str] = None   # ISO datetime, when to try again


# systemAction — generic system activity for migrations, task completions, corrections, etc.
# Replaces completeTask. Accepts any entity type in generates.
# The purpose is conveyed via a system:note entity generated alongside.
SYSTEM_ACTION_DEF = {
    "name": "systemAction",
    "label": "Systeemactie",
    "description": "Generic system activity. Used for data migrations, task completions, corrections, and other administrative operations.",
    "can_create_dossier": False,
    "client_callable": True,  # callable via API, but only by systeemgebruiker role
    "default_role": "systeem",
    "allowed_roles": ["systeem"],
    "authorization": {"access": "roles", "roles": [{"role": "systeemgebruiker"}]},
    "used": [],
    "generates": [],  # accepts any entity type — no restriction
    "status": None,
    "validators": [],
    "side_effects": [],
    "tasks": [],
}


# tombstone — irreversible content redaction.
#
# Government-mandated data deletion that breaks PROV provenance by design:
# the `used` block lists one or more versions of a single logical entity,
# whose content blobs are NULL'd in place (rows survive, schema_version
# survives, derivation edges survive — only `content` is destroyed and
# `tombstoned_by` is stamped). The `generated` block must contain exactly
# one revision of that same logical entity (the redacted replacement,
# operator-authored, normal schema validation applies) AND at least one
# system:note carrying the redaction reason.
#
# After tombstoning, GET /dossiers/{id}/entities/{type}/{eid}/{vid} for
# any of the deleted versions returns 301 Moved Permanently to the URL of
# the replacement version. The dossier-level `currentEntities` naturally
# reports the replacement as the latest version.
#
# Authorization is per-workflow: declare `tombstone.allowed_roles` at the
# workflow YAML top level. If absent, no role can tombstone in that
# workflow (deny by default).
TOMBSTONE_ACTIVITY_DEF = {
    "name": "tombstone",
    "label": "Tombstone",
    "description": (
        "Irreversibly redacts the content of one or more versions of a "
        "single logical entity, replacing them with a fresh revision "
        "authored by the operator. Breaks PROV provenance by design — "
        "use only when required by law (FOI, GDPR Article 17, etc.). "
        "The `used` block must list versions of exactly one logical "
        "entity. The `generated` block must contain exactly one revision "
        "of that entity AND at least one `system:note` describing why."
    ),
    # Built-in activities are exempt from cross-block invariants like
    # the disjoint-set rule. They operate on multiple historical versions
    # of the same logical entity by design (revising IS using doesn't
    # apply — the replacement is a redaction marker, not a normal
    # revision) and they have their own shape validators in the engine.
    "built_in": True,
    "can_create_dossier": False,
    "client_callable": True,
    "default_role": "tombstoner",
    "allowed_roles": ["tombstoner"],
    # authorization.roles is overlaid at app boot time from
    # `workflow.tombstone.allowed_roles`. Default deny-all is enforced
    # by leaving the role list empty here, so the auth check fails
    # for everyone unless the workflow explicitly opts in.
    "authorization": {"access": "roles", "roles": []},
    "used": [],
    "generates": [],
    "status": None,
    "validators": [],
    "side_effects": [],
    "tasks": [],
}


class SystemNote(BaseModel):
    """Content model for system:note entities — describes why a systemAction was performed."""
    text: str
    ticket: Optional[str] = None


# ---------------------------------------------------------------------------
# Exception grants — engine-provided entity type
# ---------------------------------------------------------------------------
# Exceptions let a workflow's administrator legally authorize one-shot
# bypass of the workflow-rules layer (requirements / forbidden /
# not_before / not_after) for a specific activity. The mechanism is
# workflow-agnostic: plugins opt in by declaring `exceptions:` at the
# top level of their workflow YAML with grant_allowed_roles and
# retract_allowed_roles. Everything else — the entity type, the three
# activities, the validator, the bypass phase — is engine-provided.
# See docs/plugin_guidebook.md under "Exception grants" for the full
# lifecycle.
#
# One logical oe:exception per (activity) in a dossier, across all
# time. Re-grants after consume or cancel are revisions of the same
# entity_id. The status field is REQUIRED (no default) — PROV
# integrity: the engine validates submissions but does not inject
# defaults into stored content, so a default would mean "engine
# silently fabricated an assertion the agent never made."


class ExceptionStatus(str, Enum):
    active = "active"
    consumed = "consumed"
    cancelled = "cancelled"


class Exception_(BaseModel):
    """Content model for ``system:exception`` entities.

    Trailing underscore because ``Exception`` is a Python builtin —
    shadowing it inside the engine package would be unfortunate. Every
    reference site uses ``Exception_`` explicitly.
    """
    # The activity this exception grants a bypass for. Stored in
    # qualified form (``oe:trekAanvraagIn``, not ``trekAanvraagIn``).
    # The engine compares against qualified activity names; the
    # validator auto-qualifies bare names at grant time so stored
    # content is always canonical.
    activity: str

    # Deadline past which the exception auto-invalidates. ``None`` is
    # a meaningful assertion — "no deadline, active until consumed or
    # cancelled." This is one of the few fields where a default is
    # OK: absence genuinely means "no deadline applies."
    granted_until: Optional[str] = None  # ISO 8601 datetime

    # Free-text justification. Required — this is the audit trail the
    # whole exception mechanism exists for.
    reason: str

    # Lifecycle status. REQUIRED — no default. See the module-level
    # docstring for why; summarizing: the engine's content-validation
    # phase validates but does not persist coerced content, so a
    # default here would falsify PROV.
    status: ExceptionStatus


# ---------------------------------------------------------------------------
# Exception grant activities
# ---------------------------------------------------------------------------
# Three built-in activities for the exception lifecycle. Registered
# per-workflow at app boot based on the YAML's top-level ``exceptions:``
# block:
#
#   exceptions:
#     grant_allowed_roles: ["beheerder"]
#     retract_allowed_roles: ["beheerder"]
#
# Absence of the block = exceptions not registered for this workflow
# (deny by default, same convention as tombstone). ``consumeException``
# is system-only and doesn't take a configurable role — it's auto-
# invoked by the engine as a side-effect of an exception-bypassed
# activity, never user-callable.
#
# Authorization on the activity defs is left with empty ``roles: []``;
# the loader overlays the plugin-supplied roles at boot time. Default
# deny-all is enforced by the empty list, same as tombstone.

GRANT_EXCEPTION_ACTIVITY_DEF = {
    "name": "grantException",
    "label": "Grant exception",
    "description": (
        "Administratively authorize bypass of the workflow-rules layer "
        "(requirements / forbidden / not_before / not_after) for a "
        "specific activity on this dossier. See the Plugin Guidebook's "
        "\"Exception grants\" section for the full lifecycle. Creates "
        "a fresh system:exception for activities never previously "
        "granted one; revises the existing entity for subsequent "
        "grants (same entity_id, new version_id, derivedFrom pointing "
        "at the prior version)."
    ),
    "built_in": True,
    "can_create_dossier": False,
    "client_callable": True,
    "default_role": "granter",
    "allowed_roles": ["granter"],
    # Overlaid at app boot from ``workflow.exceptions.grant_allowed_roles``.
    # Empty default enforces deny-all for workflows that don't opt in.
    "authorization": {"access": "roles", "roles": []},
    "used": [],
    "generates": ["system:exception"],
    "status": None,
    "validators": [
        {
            "name": "dossier_engine.builtins.exceptions.valideer_exception",
            "description": (
                "Enforces at-most-one-logical-exception-per-activity, "
                "activity immutability across revisions, required "
                "status=active on submission, and declared-activity "
                "name check."
            ),
        },
    ],
    "side_effects": [],
    "tasks": [],
}

RETRACT_EXCEPTION_ACTIVITY_DEF = {
    "name": "retractException",
    "label": "Retract exception",
    "description": (
        "Administratively cancel a granted exception. The client "
        "supplies the system:exception reference in the used block; "
        "the handler revises it with status=cancelled, preserving "
        "the activity and reason fields for audit."
    ),
    "built_in": True,
    "can_create_dossier": False,
    "client_callable": True,
    "default_role": "granter",
    "allowed_roles": ["granter"],
    # Overlaid at app boot from ``workflow.exceptions.retract_allowed_roles``.
    "authorization": {"access": "roles", "roles": []},
    "used": [{"type": "system:exception"}],
    "generates": [],
    "status": None,
    "handler": "dossier_engine.builtins.exceptions.handle_retract_exception",
    "validators": [
        {
            "name": "dossier_engine.builtins.exceptions.valideer_exception",
            "description": (
                "No-op on retract's empty generated block; shared with "
                "grantException to keep the invariant dispatch uniform."
            ),
        },
    ],
    "side_effects": [],
    "tasks": [],
}

CONSUME_EXCEPTION_ACTIVITY_DEF = {
    "name": "consumeException",
    "label": "Consume exception",
    "description": (
        "System-only activity. The engine auto-injects this as a "
        "side-effect whenever an exempted activity runs, revising the "
        "system:exception with status=consumed. Never user-callable — "
        "the mechanical enforcement of single-use-by-default."
    ),
    "built_in": True,
    "can_create_dossier": False,
    "client_callable": False,
    "default_role": "systeem",
    "allowed_roles": ["systeem"],
    "authorization": {"access": "roles", "roles": [{"role": "systeem"}]},
    "used": [
        {"type": "system:exception", "auto_resolve": "latest"},
    ],
    "generates": [],
    "status": None,
    "handler": "dossier_engine.builtins.exceptions.handle_consume_exception",
    "validators": [],
    "side_effects": [],
    "tasks": [],
}
