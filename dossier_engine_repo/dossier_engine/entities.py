"""
Common entity models provided by the engine.
These are shared across all workflow plugins.
"""

from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


class DossierAccessEntry(BaseModel):
    role: Optional[str] = None
    agents: list[str] = []
    view: list[str] = []
    activity_view: str = "related"  # "own", "related", "all"


class DossierAccess(BaseModel):
    access: list[DossierAccessEntry]


class TaskEntity(BaseModel):
    """Content model for system:task entities.

    Tasks go through this lifecycle:
        scheduled → completed           (success, first attempt)
        scheduled → scheduled → ...     (transient failure, retry with backoff)
        scheduled → dead_letter         (exhausted max_attempts, terminal)
        scheduled → cancelled           (cancel_if_activities triggered)
        scheduled → superseded          (replaced by another task with same anchor)

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
    kind: str                           # "fire_and_forget", "recorded", "scheduled_activity", "cross_dossier_activity"
    function: Optional[str] = None      # plugin task function name
    target_activity: Optional[str] = None   # for kinds 3, 4
    target_dossier: Optional[str] = None    # for kind 4 (set by worker after function call)
    result_activity_id: Optional[str] = None  # pre-generated UUID for the scheduled activity
    scheduled_for: Optional[str] = None     # ISO datetime — original schedule, immutable
    cancel_if_activities: list[str] = []
    allow_multiple: bool = False
    status: str = "scheduled"           # scheduled, completed, cancelled, superseded, dead_letter
    result: Optional[str] = None        # URI or result data after completion

    # Anchor: the specific entity this task is scoped to, used for cancel,
    # supersede, and allow_multiple matching. Stored as strings so the Pydantic
    # model is JSON-round-trippable through the database. `anchor_type` records
    # the entity type the anchor is bound to, so worker-executed scheduled tasks
    # can use it as an auto-resolve fallback for multi-cardinality used types
    # that match the anchor's type.
    anchor_entity_id: Optional[str] = None
    anchor_type: Optional[str] = None

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
