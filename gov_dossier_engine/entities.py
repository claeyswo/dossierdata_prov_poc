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
    """Content model for system:task entities."""
    kind: str                           # "fire_and_forget", "recorded", "scheduled_activity", "cross_dossier_activity"
    function: Optional[str] = None      # plugin task function name
    target_activity: Optional[str] = None   # for kinds 3, 4
    target_dossier: Optional[str] = None    # for kind 4 (set by worker after function call)
    result_activity_id: Optional[str] = None  # pre-generated UUID for the scheduled activity
    scheduled_for: Optional[str] = None     # ISO datetime
    cancel_if_activities: list[str] = []
    allow_multiple: bool = False
    status: str = "scheduled"           # "scheduled", "completed", "cancelled", "superseded", "failed"
    result: Optional[str] = None        # URI or result data after completion
    error: Optional[str] = None         # error message if failed

    # Anchor: the specific entity this task is scoped to, used for cancel,
    # supersede, and allow_multiple matching. Stored as strings so the Pydantic
    # model is JSON-round-trippable through SQLite. `anchor_type` records the
    # entity type the anchor is bound to, so worker-executed scheduled tasks
    # can use it as an auto-resolve fallback for multi-cardinality used types
    # that match the anchor's type.
    anchor_entity_id: Optional[str] = None
    anchor_type: Optional[str] = None


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

# Keep backward compat reference
COMPLETE_TASK_ACTIVITY_DEF = SYSTEM_ACTION_DEF


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
    migration_id: Optional[str] = None
