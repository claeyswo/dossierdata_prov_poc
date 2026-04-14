"""
Core engine: authorization, workflow validation, activity execution.

This is the generic handler that all activities go through.
No business logic — everything is driven by the workflow YAML + plugin handlers.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from ..db.models import Repository, EntityRow
from ..auth import User
from ..plugin import Plugin


# =====================================================================
# Entity Reference Parsing
# =====================================================================

ENTITY_REF_PATTERN = re.compile(
    r'^(?P<prefix>[a-z_]+:[a-z_]+)/(?P<id>[0-9a-f-]+)@(?P<version>[0-9a-f-]+)$'
)


def parse_entity_ref(ref: str) -> dict | None:
    """Parse 'oe:aanvraag/id@version' into components. Returns None if external URI."""
    match = ENTITY_REF_PATTERN.match(ref)
    if match:
        return {
            "prefix": match.group("prefix"),
            "id": UUID(match.group("id")),
            "version": UUID(match.group("version")),
        }
    return None  # external URI


def is_external_uri(ref: str) -> bool:
    """Check if a reference is an external URI (not a local entity)."""
    return parse_entity_ref(ref) is None


# =====================================================================
# Cardinality-aware entity lookups
# =====================================================================

class CardinalityError(Exception):
    """Raised when code tries to look up a singleton entity of a type that
    the plugin has declared as `multiple`. Indicates a bug in engine or
    handler code — the caller should be iterating entities by type, not
    assuming a unique one."""
    pass


def _allowed_relation_types_for_activity(plugin: Plugin, activity_def: dict) -> set[str]:
    """Return the set of relation types this activity may carry on its
    request body (the permission gate / "what may be sent").

    The workflow-level `relations:` block and the activity-level `relations:`
    block are unioned — both contribute permitted types.

    Note that this is distinct from validator-firing. Under the activity-
    level opt-in dispatch contract, a relation validator runs only for
    types listed in the activity's OWN `relations:` block, not for types
    inherited from the workflow-wide allowed-set. Workflow-level
    declarations permit a type to be sent system-wide; activity-level
    declarations enable validator enforcement for that specific activity."""
    workflow = {e.get("type") for e in plugin.workflow.get("relations", []) if e.get("type")}
    activity = {e.get("type") for e in activity_def.get("relations", []) if e.get("type")}
    return workflow | activity


async def lookup_singleton(
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    entity_type: str,
) -> EntityRow | None:
    """Look up the singleton entity of `entity_type` in the dossier.

    Enforces the cardinality invariant: raises `CardinalityError` if the
    plugin declares this type as `multiple`. Callers that legitimately need
    the "most recent of a multi-cardinality type" should use
    `repo.get_latest_entity_by_id` with a specific entity_id, or
    `repo.get_entities_by_type_latest` to iterate all instances.

    This is the only place code outside `ActivityContext` should look up
    a singleton entity. Direct calls to `repo.get_singleton_entity` bypass
    the cardinality check and are only acceptable for the engine-internal
    `oe:dossier_access` path in routes/access.py.
    """
    if not plugin.is_singleton(entity_type):
        raise CardinalityError(
            f"lookup_singleton called on non-singleton type "
            f"'{entity_type}' (cardinality={plugin.cardinality_of(entity_type)}). "
            f"Use repo.get_entities_by_type_latest or "
            f"repo.get_latest_entity_by_id instead."
        )
    return await repo.get_singleton_entity(dossier_id, entity_type)


async def resolve_from_trigger(
    repo: Repository,
    trigger_activity_id: UUID,
    dossier_id: UUID,
    entity_type: str,
) -> EntityRow | None:
    """Resolve an entity of `entity_type` from the scope of a triggering
    activity. Used for side-effect auto-resolve and task anchor auto-fill.

    Resolution order:
    1. Entities **generated** by the trigger. These represent the state
       AFTER the trigger ran, so they take precedence.
    2. Entities **used** by the trigger. These are the inputs the trigger
       acted on.

    At each level, only entities matching `entity_type` are considered.
    If exactly one candidate is found, return it. If zero, fall through
    to the next level. If multiple distinct entity_ids of the same type
    are found at any level, return None — the caller must disambiguate
    (typically by raising an error).

    Returns the EntityRow or None if no unambiguous match is found.

    NOTE: this function does two queries on every call (generated + used).
    If you're calling it many times for the same trigger (e.g. multiple
    side effects all resolving from the same trigger activity), use
    `resolve_from_prefetched` after fetching the trigger's generated and
    used lists once."""
    generated = await repo.get_entities_generated_by_activity(trigger_activity_id)
    used = await repo.get_used_entities_for_activity(trigger_activity_id)
    return await resolve_from_prefetched(
        repo, dossier_id, generated, used, entity_type,
    )


async def resolve_from_prefetched(
    repo: Repository,
    dossier_id: UUID,
    trigger_generated: list[EntityRow],
    trigger_used: list[EntityRow],
    entity_type: str,
) -> EntityRow | None:
    """Same resolution logic as `resolve_from_trigger`, but the caller has
    already fetched the trigger's generated and used entity lists. Use this
    when resolving multiple types from the same trigger to avoid redundant
    queries.

    The only DB query this performs is a single `get_latest_entity_by_id`
    in the rare case where the type is found in `used` but not in
    `generated` (to handle the case where a sibling activity in the same
    transaction may have revised the entity)."""
    gen_of_type = [e for e in trigger_generated if e.type == entity_type]
    if gen_of_type:
        entity_ids = {e.entity_id for e in gen_of_type}
        if len(entity_ids) == 1:
            return gen_of_type[-1]
        return None

    used_of_type = [e for e in trigger_used if e.type == entity_type]
    if used_of_type:
        entity_ids = {e.entity_id for e in used_of_type}
        if len(entity_ids) == 1:
            return await repo.get_latest_entity_by_id(
                dossier_id, used_of_type[0].entity_id,
            )
        return None

    return None


# =====================================================================
# Authorization
# =====================================================================

async def authorize_activity(
    plugin: Plugin,
    activity_def: dict,
    user: User,
    repo: Repository,
    dossier_id: UUID | None,
) -> tuple[bool, str | None]:
    """
    Check if user is authorized to perform this activity.
    Returns (authorized, error_message).
    """
    auth = activity_def.get("authorization", {})
    access = auth.get("access", "authenticated")

    if access == "everyone":
        return True, None

    if access == "authenticated":
        if not user:
            return False, "Authentication required"
        return True, None

    if access == "roles":
        roles_config = auth.get("roles", [])
        if not roles_config:
            return True, None

        errors = []
        for role_entry in roles_config:
            if isinstance(role_entry, dict):
                if "role" in role_entry:
                    # Direct or scoped match
                    base_role = role_entry["role"]
                    scope = role_entry.get("scope")

                    if scope and dossier_id:
                        # Scoped: resolve from entity
                        try:
                            entity_type = scope["from_entity"]
                            field_path = scope["field"]
                            entity = await lookup_singleton(plugin, repo, dossier_id, entity_type)
                            if not entity:
                                errors.append(f"{base_role} — entity '{entity_type}' not found")
                                continue
                            value = _resolve_field(entity.content, field_path)
                            if value is None:
                                errors.append(f"{base_role} — field '{field_path}' is null")
                                continue
                            resolved = f"{base_role}:{value}"
                        except Exception as e:
                            errors.append(f"{base_role} — scope resolution error: {e}")
                            continue
                    else:
                        resolved = base_role

                    if resolved in user.roles:
                        return True, None
                    else:
                        errors.append(f"User does not have role '{resolved}'")

                elif "from_entity" in role_entry:
                    # Entity-derived: field value IS the role
                    if not dossier_id:
                        errors.append("Entity-derived role check requires existing dossier")
                        continue
                    try:
                        entity_type = role_entry["from_entity"]
                        field_path = role_entry["field"]
                        entity = await lookup_singleton(plugin, repo, dossier_id, entity_type)
                        if not entity:
                            errors.append(f"Entity '{entity_type}' not found")
                            continue
                        resolved = _resolve_field(entity.content, field_path)
                        if resolved is None:
                            errors.append(f"Field '{field_path}' is null")
                            continue
                        if str(resolved) in user.roles:
                            return True, None
                        else:
                            errors.append(f"User does not have role '{resolved}'")
                    except Exception as e:
                        errors.append(f"Entity-derived role error: {e}")
                        continue
            else:
                # Simple string
                if role_entry in user.roles:
                    return True, None
                errors.append(f"User does not have role '{role_entry}'")

        return False, f"Authorization failed: {'; '.join(errors)}"

    return False, f"Unknown access type: {access}"


def _resolve_field(content: dict | Any, field_path: str) -> Any:
    """Resolve a dot-notation field path in content. E.g. 'content.aanvrager.kbo'."""
    if content is None:
        return None
    parts = field_path.split(".")
    # Skip 'content.' prefix if present (since we're already in content)
    if parts[0] == "content" and len(parts) > 1:
        parts = parts[1:]
    current = content
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


# =====================================================================
# Workflow Validation
# =====================================================================

async def validate_workflow_rules(
    activity_def: dict,
    repo: Repository,
    dossier_id: UUID,
    known_status: str | None = None,
    known_activity_types: set[str] | None = None,
) -> tuple[bool, str | None]:
    """
    Check requirements and forbidden rules.
    Returns (valid, error_message).
    Pass known_status and known_activity_types to avoid redundant queries.
    """
    requirements = activity_def.get("requirements", {})
    forbidden = activity_def.get("forbidden", {})

    # Get activity history (use cached if provided)
    if known_activity_types is not None:
        completed_types = known_activity_types
    else:
        activities = await repo.get_activities_for_dossier(dossier_id)
        completed_types = {a.type for a in activities}

    # Check required activities
    for req_activity in requirements.get("activities", []):
        if req_activity and req_activity not in completed_types:
            return False, f"Required activity '{req_activity}' not completed"

    # Check required entities
    for req_entity in requirements.get("entities", []):
        if req_entity and not await repo.entity_type_exists(dossier_id, req_entity):
            return False, f"Required entity type '{req_entity}' does not exist"

    # Check required statuses
    req_statuses = requirements.get("statuses", [])
    forb_statuses = forbidden.get("statuses", [])

    current_status = known_status
    if current_status is None and (
        (req_statuses and any(s for s in req_statuses)) or
        (forb_statuses and any(s for s in forb_statuses))
    ):
        current_status = await derive_status(repo, dossier_id)

    if req_statuses and any(s for s in req_statuses):
        if current_status not in req_statuses:
            return False, f"Dossier status '{current_status}' not in required statuses {req_statuses}"

    # Check forbidden activities
    for forb_activity in forbidden.get("activities", []):
        if forb_activity and forb_activity in completed_types:
            return False, f"Forbidden activity '{forb_activity}' already completed"

    # Check forbidden statuses
    if forb_statuses and any(s for s in forb_statuses):
        if current_status in forb_statuses:
            return False, f"Dossier is in forbidden status '{current_status}'"

    return True, None


# =====================================================================
# Status Derivation
# =====================================================================

async def derive_status(
    repo: Repository,
    dossier_id: UUID,
) -> str:
    """Derive current dossier status from activity history.
    
    Every activity stores its computed_status when executed.
    We just walk backwards and return the first non-null one.
    """
    activities = await repo.get_activities_for_dossier(dossier_id)

    if not activities:
        return "nieuw"

    for activity in reversed(activities):
        if activity.computed_status:
            return activity.computed_status

    return "nieuw"


async def compute_eligible_activities(
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    known_status: str | None = None,
) -> list[str]:
    """Compute which activities are structurally allowed (workflow rules only, no user check).
    This is expensive but cacheable on the dossier row."""
    # Query once, pass to all validate calls
    activities = await repo.get_activities_for_dossier(dossier_id)
    activity_types = {a.type for a in activities}
    status = known_status or await derive_status(repo, dossier_id)

    eligible = []
    for act_def in plugin.workflow.get("activities", []):
        if act_def.get("client_callable") is False:
            continue
        valid, _ = await validate_workflow_rules(
            act_def, repo, dossier_id,
            known_status=status,
            known_activity_types=activity_types,
        )
        if valid:
            eligible.append(act_def["name"])
    return eligible


async def filter_by_user_auth(
    plugin: Plugin,
    eligible: list[str],
    user: User,
    repo: Repository,
    dossier_id: UUID,
) -> list[dict]:
    """Filter eligible activities by user authorization. Cheap per-request operation."""
    allowed = []
    act_def_map = {a["name"]: a for a in plugin.workflow.get("activities", [])}
    for act_name in eligible:
        act_def = act_def_map.get(act_name)
        if not act_def:
            continue
        authorized, _ = await authorize_activity(plugin, act_def, user, repo, dossier_id)
        if authorized:
            allowed.append({
                "type": act_def["name"],
                "label": act_def.get("label", act_def["name"]),
            })
    return allowed


async def derive_allowed_activities(
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    user: User,
) -> list[dict]:
    """Determine which activities are currently allowed for this user.
    Combines eligible check + user auth. Use when cache is not available."""
    eligible = await compute_eligible_activities(plugin, repo, dossier_id)
    return await filter_by_user_auth(plugin, eligible, user, repo, dossier_id)


# =====================================================================
# Activity Execution
# =====================================================================

class _PendingEntity:
    """Lightweight stand-in for an entity that hasn't been persisted yet.
    Quacks like EntityRow for handler context."""
    def __init__(self, content, entity_id, id, attributed_to, schema_version=None):
        self.content = content
        self.entity_id = entity_id
        self.id = id
        self.attributed_to = attributed_to
        self.created_at = None
        self.schema_version = schema_version


class ActivityContext:
    """Context passed to handlers and validators."""

    def __init__(
        self,
        repo: Repository,
        dossier_id: UUID,
        used_entities: dict[str, EntityRow],
        entity_models: dict[str, Any] | None = None,
        plugin: Plugin | None = None,
    ):
        self.repo = repo
        self.dossier_id = dossier_id
        self._used_entities = used_entities
        self._entity_models = entity_models or {}
        self._plugin = plugin

    def get_used_entity(self, entity_type: str) -> EntityRow | None:
        return self._used_entities.get(entity_type)

    def get_used_row(self, entity_type: str) -> EntityRow | None:
        """Return the EntityRow for a used entity of this type. Useful for
        handlers that need the version id to seed a lineage walk."""
        return self._used_entities.get(entity_type)

    def get_typed(self, entity_type: str) -> Any | None:
        """Get a used entity's content as a validated Pydantic model instance.
        Returns None if the entity doesn't exist or has no content.

        Routes via plugin.resolve_schema so the returned model matches the
        row's stored schema_version (rule 3: store-version-wins). Legacy
        unversioned rows (schema_version=NULL) fall back to entity_models.
        """
        entity = self._used_entities.get(entity_type)
        if not entity or not entity.content:
            return None
        if self._plugin is not None:
            model_class = self._plugin.resolve_schema(entity_type, entity.schema_version)
        else:
            model_class = self._entity_models.get(entity_type)
        if model_class:
            return model_class(**entity.content)
        return None

    def _require_singleton(self, entity_type: str) -> None:
        if self._plugin and not self._plugin.is_singleton(entity_type):
            raise CardinalityError(
                f"ActivityContext singleton lookup called on non-singleton "
                f"type '{entity_type}'. Use get_entities_latest(entity_type) "
                f"to iterate instead."
            )

    async def get_singleton_typed(self, entity_type: str) -> Any | None:
        """Get the singleton entity's content as a validated Pydantic model
        instance. Raises CardinalityError if called on a non-singleton type."""
        self._require_singleton(entity_type)
        entity = await self.repo.get_singleton_entity(self.dossier_id, entity_type)
        if not entity or not entity.content:
            return None
        if self._plugin is not None:
            model_class = self._plugin.resolve_schema(entity_type, entity.schema_version)
        else:
            model_class = self._entity_models.get(entity_type)
        if model_class:
            return model_class(**entity.content)
        return None

    # Legacy alias — will be removed once all handlers migrate.
    get_latest_typed = get_singleton_typed

    async def has_activity(self, activity_type: str) -> bool:
        activities = await self.repo.get_activities_for_dossier(self.dossier_id)
        return any(a.type == activity_type for a in activities)

    async def get_singleton_entity(self, entity_type: str) -> EntityRow | None:
        """Return the singleton entity row for this type in the dossier.
        Raises CardinalityError if called on a non-singleton type."""
        self._require_singleton(entity_type)
        return await self.repo.get_singleton_entity(self.dossier_id, entity_type)

    # Legacy alias — will be removed once all handlers migrate.
    get_latest_entity = get_singleton_entity

    async def get_entities_latest(self, entity_type: str) -> list[EntityRow]:
        """Return the latest version of each logical entity of this type.
        Works for both singleton and multi-cardinality types — for singletons
        the list has zero or one elements. For multi-cardinality types, one
        element per distinct entity_id."""
        return await self.repo.get_entities_by_type_latest(self.dossier_id, entity_type)


class HandlerResult:
    """Result returned by a handler function.
    
    Supports:
    - Single entity: HandlerResult(content={...}, status="...")
    - Multiple entities: HandlerResult(generated=[...], status="...")
    - Tasks: HandlerResult(tasks=[{...task def...}], status="...")
    - All combined

    `generated` items can be either:
    - Tuples: (type, content) — legacy shape, engine auto-fills entity_id
      and derived_from for singletons. Multi-cardinality types get a fresh
      entity_id with no derivation.
    - Dicts: {"type": ..., "content": ..., "entity_id": ..., "derived_from": ...}
      — explicit shape for handlers that need to specify which entity they're
      revising (required for multi-cardinality types). `entity_id` and
      `derived_from` are optional and auto-filled when omitted (same rules
      as tuples).
    """

    def __init__(
        self,
        content: dict | None = None,
        status: str | None = None,
        generated: list | None = None,
        tasks: list[dict] | None = None,
    ):
        # Backward compat: single content → list with type=None (resolved from generates[0])
        if content and not generated:
            self.generated = [{"type": None, "content": content}]
        else:
            # Normalize tuples to dicts.
            normalized = []
            for item in (generated or []):
                if isinstance(item, dict):
                    normalized.append(item)
                elif isinstance(item, (tuple, list)) and len(item) == 2:
                    normalized.append({"type": item[0], "content": item[1]})
                else:
                    raise ValueError(f"Invalid HandlerResult.generated item: {item}")
            self.generated = normalized
        self.status = status
        self.tasks = tasks or []


class TaskResult:
    """Result returned by a cross-dossier task function."""

    def __init__(self, target_dossier_id: str, content: dict | None = None):
        self.target_dossier_id = target_dossier_id
        self.content = content


def _resolve_schema_version_for_generated(
    activity_def: dict,
    entity_type: str,
    parent_row: "EntityRow | None",
) -> str | None:
    """Resolve the schema_version to stamp on a generated entity row.

    Reads the activity's `entities` block for per-type version config:

        entities:
          oe:aanvraag:
            new_version: v2          # required when creating fresh
            allowed_versions: [v1, v2]  # required when revising (optional per-activity)

    Rules (see README "Entity schema versioning"):
      * If the activity declares no `entities` block for this type at all →
        legacy/unversioned. Return None. The new row inherits the parent's
        sticky schema_version (rule A) via the caller, OR is stamped NULL
        if fresh. Content validates against plugin.entity_models[type].
      * Fresh entity (parent_row is None):
          - If `new_version` declared → return it.
          - If `new_version` missing → 500 misconfiguration (strict).
      * Revision (parent_row is not None):
          - If `allowed_versions` declared and parent.schema_version not in
            it → 422 unsupported_schema_version.
          - Return parent.schema_version (sticky, rule A).

    Raises ActivityError on misuse.
    """
    entities_cfg = activity_def.get("entities") or {}
    ecfg = entities_cfg.get(entity_type)
    if not ecfg:
        # Legacy path: this activity does not declare version discipline
        # for this type. If revising, inherit the parent's sticky version;
        # if fresh, stamp NULL.
        if parent_row is not None:
            return parent_row.schema_version
        return None

    if parent_row is None:
        new_version = ecfg.get("new_version")
        if not new_version:
            raise ActivityError(
                500,
                f"Activity '{activity_def.get('name')}' declares versioning "
                f"for '{entity_type}' but has no 'new_version' — cannot "
                f"create a fresh entity of a versioned type without one",
                payload={
                    "error": "missing_new_version_declaration",
                    "activity": activity_def.get("name"),
                    "entity_type": entity_type,
                },
            )
        return new_version

    # Revision path.
    stored = parent_row.schema_version  # may be None for legacy rows
    allowed = ecfg.get("allowed_versions")
    if allowed is not None:
        if stored not in allowed:
            raise ActivityError(
                422,
                f"Activity '{activity_def.get('name')}' cannot revise "
                f"'{entity_type}' entity with schema_version={stored!r}; "
                f"allowed versions are {allowed}",
                payload={
                    "error": "unsupported_schema_version",
                    "activity": activity_def.get("name"),
                    "entity_type": entity_type,
                    "stored_version": stored,
                    "allowed_versions": allowed,
                },
            )
    return stored  # sticky — revisions inherit parent's version


def _validate_tombstone_activity(
    used_rows: dict[str, "EntityRow"],
    used_refs: list[dict],
    generated: list[dict],
) -> tuple[list["UUID"], dict]:
    """Validate a tombstone activity's shape and return:
      - the list of version_ids to tombstone (from `used`)
      - the generated replacement dict (the non-system:note generated item)

    Tombstone shape rules:
      1. `used` is non-empty and every entry refers to the same logical
         entity_id and the same entity_type (one tombstone, one entity).
      2. None of the used versions are already tombstoned.
      3. `generated` contains exactly one entity revision matching the
         tombstoned entity (same entity_id, same type) — the replacement.
      4. `generated` contains at least one `system:note` (the reason).
      5. No other generated items (no surprise extras).

    Raises ActivityError(422) with structured payloads on every failure
    so the operator gets actionable diagnostics.
    """
    from uuid import UUID as _UUID

    if not used_rows or not used_refs:
        raise ActivityError(
            422,
            "Tombstone activity must list at least one version in the used block",
            payload={"error": "tombstone_no_used"},
        )

    # Collect all used rows that are real entity versions (skip externals).
    used_entity_rows = []
    for ref_dict in used_refs:
        if "version_id" not in ref_dict:
            continue
        # Find the corresponding row from used_rows_by_ref
        ref = ref_dict.get("ref") or ref_dict.get("entity")
        row = used_rows.get(ref)
        if row is None:
            # Look up via version_id as fallback
            for r in used_rows.values():
                if r.id == ref_dict["version_id"]:
                    row = r
                    break
        if row is not None:
            used_entity_rows.append(row)

    if not used_entity_rows:
        raise ActivityError(
            422,
            "Tombstone activity must reference real entity versions in used (no externals)",
            payload={"error": "tombstone_no_used_entities"},
        )

    # Rule 1: all used rows must share entity_id and type.
    target_entity_ids = {row.entity_id for row in used_entity_rows}
    target_types = {row.type for row in used_entity_rows}
    if len(target_entity_ids) != 1 or len(target_types) != 1:
        raise ActivityError(
            422,
            f"Tombstone may only target a single logical entity; got "
            f"entity_ids={sorted(str(e) for e in target_entity_ids)}, "
            f"types={sorted(target_types)}",
            payload={
                "error": "tombstone_multi_entity",
                "entity_ids": sorted(str(e) for e in target_entity_ids),
                "types": sorted(target_types),
            },
        )
    target_entity_id = next(iter(target_entity_ids))
    target_type = next(iter(target_types))

    # Rule 2 (intentional non-rule): re-tombstoning IS allowed. Human
    # error during a first redaction may leave residual content in the
    # replacement that itself needs to be redacted, so the operator must
    # be able to run another tombstone over a previously-tombstoned
    # entity. The new tombstone simply nulls the rows again (no-op for
    # already-NULL content) and overwrites `tombstoned_by` with the new
    # activity id, which becomes the most recent auditable record of who
    # killed this entity last.

    # Rule 3 + 4 + 5: generated shape.
    replacements = [
        g for g in generated
        if g["type"] == target_type and g["entity_id"] == target_entity_id
    ]
    notes = [g for g in generated if g["type"] == "system:note"]
    others = [
        g for g in generated
        if g["type"] != "system:note"
        and not (g["type"] == target_type and g["entity_id"] == target_entity_id)
    ]

    if len(replacements) != 1:
        raise ActivityError(
            422,
            f"Tombstone must generate exactly one replacement of "
            f"{target_type}/{target_entity_id}; got {len(replacements)}",
            payload={
                "error": "tombstone_replacement_count",
                "expected": 1,
                "got": len(replacements),
                "target_type": target_type,
                "target_entity_id": str(target_entity_id),
            },
        )
    if len(notes) < 1:
        raise ActivityError(
            422,
            "Tombstone must generate at least one system:note carrying the redaction reason",
            payload={"error": "tombstone_missing_reason_note"},
        )
    if others:
        raise ActivityError(
            422,
            f"Tombstone activity may only generate the replacement entity "
            f"and system:note(s); unexpected entries: "
            f"{[g['type'] for g in others]}",
            payload={
                "error": "tombstone_unexpected_generated",
                "unexpected_types": [g["type"] for g in others],
            },
        )

    # All used rows are eligible for deletion. Return their version ids
    # and the replacement dict.
    version_ids_to_kill = [row.id for row in used_entity_rows]
    return version_ids_to_kill, replacements[0]


async def execute_activity(
    plugin: Plugin,
    activity_def: dict,
    repo: Repository,
    dossier_id: UUID,
    activity_id: UUID,
    user: User,
    role: str,
    used_items: list[dict],
    generated_items: list[dict] | None = None,
    workflow_name: str | None = None,
    informed_by: str | None = None,
    skip_cache: bool = False,
    relation_items: list[dict] | None = None,
    caller: str = "client",
    anchor_entity_id: UUID | None = None,
    anchor_type: str | None = None,
) -> dict:
    """
    Execute an activity.

    used_items: references to existing entities the activity reads
    generated_items: new entities or revisions the client is creating
    relation_items: generic activity→entity relations beyond used/generated,
        used for plugin-defined PROV extensions like `oe:neemtAkteVan`.
        Each item is a dict `{"entity": ref, "type": relation_type}`.
    caller: "client" (API call) or "system" (worker/scheduled task).
        Auto-resolve of used entities only runs for system callers.
    anchor_entity_id / anchor_type: set by the worker when executing a
        scheduled task. If the activity's used block needs an entity of
        type `anchor_type` and `resolve_from_trigger` can't find it,
        the engine falls back to `get_latest_entity_by_id(anchor_entity_id)`.
        Ensures scheduled tasks can locate their anchored entity even when
        it wasn't touched by the informing activity.
    """
    if generated_items is None:
        generated_items = []
    if relation_items is None:
        relation_items = []
    now = datetime.now(timezone.utc)

    # 1. Idempotency check
    existing = await repo.get_activity(activity_id)
    if existing:
        if existing.dossier_id != dossier_id:
            raise ActivityError(409, "Activity ID already exists for different dossier")
        if existing.type != activity_def["name"]:
            raise ActivityError(409, "Activity ID already exists with different type")
        return await _build_response(plugin, repo, dossier_id, existing, user)

    # 2. Create dossier if needed
    dossier = await repo.get_dossier(dossier_id)
    if not dossier:
        if not activity_def.get("can_create_dossier"):
            raise ActivityError(404, "Dossier not found")
        if not workflow_name:
            raise ActivityError(400, "workflow field required for first activity")
        dossier = await repo.create_dossier(dossier_id, workflow_name)

    # 3. Authorize
    authorized, error = await authorize_activity(plugin, activity_def, user, repo, dossier_id)
    if not authorized:
        raise ActivityError(403, error)

    # 3b. Validate/default functional role
    allowed_roles = activity_def.get("allowed_roles", [])
    default_role = activity_def.get("default_role")
    if not role and default_role:
        role = default_role
    if not role and allowed_roles:
        role = allowed_roles[0]
    if not role:
        role = "participant"
    if allowed_roles and role not in allowed_roles:
        raise ActivityError(422, f"Role '{role}' not allowed. Allowed: {allowed_roles}")

    # 4. Validate workflow rules (skip for new dossiers)
    if not activity_def.get("can_create_dossier") or await repo.get_activities_for_dossier(dossier_id):
        valid, error = await validate_workflow_rules(activity_def, repo, dossier_id)
        if not valid:
            raise ActivityError(409, error)

    # 5. Process used items (references only) + auto-resolve.
    #
    # This block does infrastructure only: resolve each ref into a real
    # EntityRow, check dossier ownership, persist external URIs, and
    # auto-resolve missing used entries. Workflow-level interpretation of
    # "is this the right version" is left to plugin relation validators
    # (see step 6b).
    used_refs = []
    resolved_entities: dict[str, EntityRow] = {}
    # Map ref string -> EntityRow for passing to relation validators.
    used_rows_by_ref: dict[str, EntityRow] = {}

    for item in used_items:
        entity_ref = item.get("entity", "")

        if is_external_uri(entity_ref):
            # External URIs have only one "version" so no versioning concerns.
            ext_entity = await repo.ensure_external_entity(dossier_id, entity_ref)
            used_refs.append({"entity": entity_ref, "external": True, "version_id": ext_entity.id})
            continue

        parsed = parse_entity_ref(entity_ref)
        if not parsed:
            raise ActivityError(422, f"Invalid entity reference: {entity_ref}")

        entity_type = parsed["prefix"]
        existing_entity = await repo.get_entity(parsed["version"])
        if not existing_entity:
            raise ActivityError(422, f"Entity not found: {entity_ref}")
        if existing_entity.dossier_id != dossier_id:
            raise ActivityError(422, f"Entity belongs to a different dossier: {entity_ref}")
        used_refs.append({"entity": entity_ref, "version_id": parsed["version"], "type": entity_type})
        resolved_entities[entity_type] = existing_entity
        used_rows_by_ref[entity_ref] = existing_entity

    # Auto-resolve missing used entities. Only allowed for system callers
    # (worker, side effects). Client callers must supply all used references
    # explicitly so there is no ambiguity about which version they acted on.
    #
    # For system callers with an `informed_by` chain (scheduled tasks and
    # side effects), resolution first checks the informing activity's scope
    # (what it generated/used) via `resolve_from_trigger`. This handles
    # multi-cardinality types correctly by finding the specific entity
    # instance the informing activity worked on.
    #
    # Falls back to dossier-wide singleton lookup for types not in the
    # informing activity's scope. Multi-cardinality types that aren't in
    # scope and aren't resolvable from the trigger fail silently (the
    # activity runs without that entity in its used context).
    if caller == "system":
        # Parse informed_by to a UUID if it's a local activity reference.
        trigger_id: UUID | None = None
        if informed_by:
            try:
                trigger_id = UUID(informed_by)
            except (ValueError, AttributeError):
                pass  # cross-dossier URI or other non-UUID reference

        # Prefetch trigger scope once if there's anything to auto-resolve.
        # This avoids 2 queries per used entry that needs auto-resolve.
        trigger_generated_rows: list[EntityRow] = []
        trigger_used_rows: list[EntityRow] = []
        if trigger_id is not None and any(
            ud.get("auto_resolve") == "latest" and not ud.get("external")
            for ud in activity_def.get("used", [])
        ):
            trigger_generated_rows = await repo.get_entities_generated_by_activity(trigger_id)
            trigger_used_rows = await repo.get_used_entities_for_activity(trigger_id)

        for used_def in activity_def.get("used", []):
            if used_def.get("external"):
                continue
            etype = used_def["type"]
            auto = used_def.get("auto_resolve")
            if auto == "latest" and etype not in resolved_entities:
                entity = None

                # Try trigger scope first using prefetched lists.
                if trigger_id is not None:
                    entity = await resolve_from_prefetched(
                        repo, dossier_id, trigger_generated_rows, trigger_used_rows, etype,
                    )

                # Anchor fallback: if this is a worker-executed scheduled
                # task, the task's anchor may resolve the needed type even
                # when the informing activity didn't touch it. Only use
                # the anchor if its type matches what we need.
                if entity is None and anchor_entity_id is not None and anchor_type == etype:
                    entity = await repo.get_latest_entity_by_id(
                        dossier_id, anchor_entity_id,
                    )

                # Last fallback: dossier-wide singleton lookup for types
                # that are singleton-cardinality.
                if entity is None and plugin.is_singleton(etype):
                    entity = await lookup_singleton(
                        plugin, repo, dossier_id, etype,
                    )

                if entity:
                    resolved_entities[etype] = entity
                    used_refs.append({
                        "entity": f"{etype}/{entity.entity_id}@{entity.id}",
                        "version_id": entity.id,
                        "type": etype,
                        "auto_resolved": True,
                    })

    # 6. Process generated items (new entities from client)
    generated = []
    generated_externals = []  # external URIs generated by this activity
    allowed_types = activity_def.get("generates", [])

    for item in generated_items:
        entity_ref = item.get("entity", "")
        content = item.get("content")
        derived_from = item.get("derivedFrom")

        # Check if this is an external URI being generated
        if is_external_uri(entity_ref):
            generated_externals.append(entity_ref)
            continue

        if not content:
            raise ActivityError(422, f"Generated item must have content: {entity_ref}")

        parsed = parse_entity_ref(entity_ref)
        if not parsed:
            raise ActivityError(422, f"Invalid entity reference for generated item: {entity_ref}")

        entity_type = parsed["prefix"]
        entity_logical_id = parsed["id"]

        if allowed_types and entity_type not in allowed_types:
            raise ActivityError(422, f"Activity cannot generate entity type '{entity_type}'")

        # --- Derivation validation ---------------------------------------
        # A generated entity must correctly declare its derivation chain:
        #   * if no prior version of this entity_id exists, `derivedFrom`
        #     must be absent (nothing to derive from)
        #   * if a prior version exists, `derivedFrom` must be present and
        #     must point at the CURRENT LATEST version — stale derivations
        #     are rejected with 409 and the latest version is returned
        #   * `derivedFrom` must refer to an existing version
        #   * `derivedFrom` must refer to the SAME logical entity_id (no
        #     cross-entity derivation)
        latest_existing = await repo.get_latest_entity_by_id(dossier_id, entity_logical_id)

        declared_parent_version: UUID | None = None
        if derived_from:
            try:
                declared_parent_version = UUID(derived_from.split("@")[1])
            except (IndexError, ValueError):
                raise ActivityError(
                    422,
                    f"Malformed derivedFrom reference: {derived_from}",
                )

            parent_row = await repo.get_entity(declared_parent_version)
            if parent_row is None or parent_row.dossier_id != dossier_id:
                raise ActivityError(
                    422,
                    f"derivedFrom refers to unknown version: {derived_from}",
                    payload={"error": "unknown_parent", "derivedFrom": derived_from},
                )
            if parent_row.entity_id != entity_logical_id:
                raise ActivityError(
                    422,
                    f"derivedFrom must reference the same entity_id "
                    f"(parent is {parent_row.entity_id}, generated is {entity_logical_id})",
                    payload={
                        "error": "cross_entity_derivation",
                        "derivedFrom": derived_from,
                        "generated": entity_ref,
                    },
                )

        if latest_existing is not None:
            # A prior version exists — derivedFrom is mandatory and must
            # point at the latest.
            if declared_parent_version is None:
                raise ActivityError(
                    409,
                    f"Entity '{entity_type}/{entity_logical_id}' already has "
                    f"version {latest_existing.id}; generated entity must "
                    f"declare derivedFrom pointing at the latest version",
                    payload={
                        "error": "missing_derivation",
                        "entity_ref": entity_ref,
                        "latest_version": {
                            "entity": f"{entity_type}/{entity_logical_id}@{latest_existing.id}",
                            "versionId": str(latest_existing.id),
                            "content": latest_existing.content,
                        },
                    },
                )
            if declared_parent_version != latest_existing.id:
                raise ActivityError(
                    409,
                    f"Stale derivation: generated entity derives from "
                    f"{declared_parent_version} but latest is {latest_existing.id}",
                    payload={
                        "error": "stale_derivation",
                        "entity_ref": entity_ref,
                        "declared_parent": str(declared_parent_version),
                        "latest_parent": str(latest_existing.id),
                        "latest_version": {
                            "entity": f"{entity_type}/{entity_logical_id}@{latest_existing.id}",
                            "versionId": str(latest_existing.id),
                            "content": latest_existing.content,
                        },
                    },
                )
        else:
            # No prior version. A declared derivedFrom that survived the
            # earlier checks would mean the client is deriving from a version
            # of a DIFFERENT entity_id — already rejected above. Nothing else
            # to check here.
            pass

        # -----------------------------------------------------------------

        # Schema version resolution (relaxed legacy mode).
        # For activities that declare an `entities` block, validate against
        # the resolved versioned schema. For activities with no declaration,
        # fall back to plugin.entity_models (legacy) and let the new row
        # inherit the parent's sticky schema_version via the resolver.
        new_schema_version = _resolve_schema_version_for_generated(
            activity_def, entity_type, latest_existing
        )
        model_class = plugin.resolve_schema(entity_type, new_schema_version)
        if model_class:
            try:
                model_class(**content)
            except Exception as e:
                raise ActivityError(422, f"Content validation failed for {entity_type}: {e}")

        generated.append({
            "version_id": parsed["version"],
            "entity_id": parsed["id"],
            "type": entity_type,
            "content": content,
            "derived_from": UUID(derived_from.split("@")[1]) if derived_from else None,
            "ref": entity_ref,
            "schema_version": new_schema_version,
        })

        # Make available to handlers
        resolved_entities[entity_type] = _PendingEntity(
            content=content,
            entity_id=parsed["id"],
            id=parsed["version"],
            attributed_to=user.id,
            schema_version=new_schema_version,
        )

    # 6b. Process relations (generic PROV-extension edges beyond used/generated)
    #
    # Each item is {entity: ref, type: relation_type}. The activity's YAML
    # declares which relation types it allows; the workflow's top-level
    # `relations:` block provides workflow-wide defaults.
    #
    # After parsing and resolving, per-type validators registered by the
    # plugin are invoked with the full activity context (resolved used rows,
    # generated items, relation entries of their type). Validators are the
    # sole arbiters of workflow-level rules — e.g. "this used reference is
    # stale and the client hasn't acknowledged newer versions." The engine
    # does not know or care about staleness, acknowledgement, approval
    # chains, or any other workflow semantic; that all lives in validators.
    # A validator signals rejection by raising `ActivityError`.
    validated_relations: list[dict] = []
    allowed_relation_types = _allowed_relation_types_for_activity(plugin, activity_def)

    # Group incoming relations by type so validators see them all at once.
    relations_by_type: dict[str, list[dict]] = {}
    for rel_item in relation_items:
        rel_type = rel_item.get("type")
        rel_ref = rel_item.get("entity", "")
        if not rel_type:
            raise ActivityError(422, f"Relation item missing 'type': {rel_item}")
        if rel_type not in allowed_relation_types:
            raise ActivityError(
                422,
                f"Activity '{activity_def['name']}' does not allow relation "
                f"type '{rel_type}'. Allowed: {sorted(allowed_relation_types)}",
            )

        # Resolve the entity — same rules as used items, minus auto-resolve.
        if is_external_uri(rel_ref):
            raise ActivityError(
                422,
                f"Relations cannot reference external URIs: {rel_ref}",
            )
        parsed = parse_entity_ref(rel_ref)
        if not parsed:
            raise ActivityError(422, f"Invalid entity reference in relation: {rel_ref}")
        rel_entity = await repo.get_entity(parsed["version"])
        if rel_entity is None or rel_entity.dossier_id != dossier_id:
            raise ActivityError(422, f"Relation entity not found in dossier: {rel_ref}")

        relations_by_type.setdefault(rel_type, []).append({
            "ref": rel_ref,
            "entity_row": rel_entity,
            "raw": rel_item,
        })
        validated_relations.append({
            "version_id": rel_entity.id,
            "relation_type": rel_type,
            "ref": rel_ref,
        })

    # Per-type validator dispatch.
    #
    # Workflow-level `relations:` declares which relation types are
    # PERMITTED in this workflow (the allowed-set / "what may be sent").
    # Activity-level `relations:` declares which relation types this
    # specific activity OPTS IN to (the validator-firing set / "what
    # discipline applies here"). A relation validator runs only for
    # types listed in the activity's own `relations:` block — not for
    # types it merely inherits from the workflow-wide allowed-set.
    #
    # This means: a workflow can permit `oe:neemtAkteVan` system-wide,
    # but only the activities that actually depend on staleness checking
    # (e.g. `bewerkAanvraag`, `tekenBeslissing`) need to enable it. System
    # activities, side effects, and one-off built-ins like `tombstone` are
    # untouched by default.
    activity_level_relation_types: set[str] = set()
    for r in activity_def.get("relations", []) or []:
        if isinstance(r, dict):
            t = r.get("type")
        else:
            t = r
        if t:
            activity_level_relation_types.add(t)

    for rel_type in activity_level_relation_types:
        if rel_type not in allowed_relation_types:
            # Activity opted into a type that isn't permitted workflow-wide.
            # Treat as misconfiguration — fail loudly at request time so
            # the operator notices.
            raise ActivityError(
                500,
                f"Activity {activity_def.get('name')!r} opts into relation "
                f"type {rel_type!r} which is not in the workflow's allowed "
                f"relation set {sorted(allowed_relation_types)}",
                payload={
                    "error": "relation_type_not_permitted",
                    "activity": activity_def.get("name"),
                    "relation_type": rel_type,
                },
            )
        validator = plugin.relation_validators.get(rel_type)
        if validator is None:
            continue  # pure annotation — no validator, just stored
        entries = relations_by_type.get(rel_type, [])
        await validator(
            plugin=plugin,
            repo=repo,
            dossier_id=dossier_id,
            activity_def=activity_def,
            entries=entries,
            used_rows_by_ref=used_rows_by_ref,
            generated_items=generated,
        )

    # 7. Ensure agent exists
    await repo.ensure_agent(user.id, user.type, user.name, user.properties)

    # 8. Run custom validators
    for validator_def in activity_def.get("validators", []):
        validator_name = validator_def["name"]
        validator_fn = plugin.validators.get(validator_name)
        if validator_fn:
            ctx = ActivityContext(repo, dossier_id, resolved_entities, plugin.entity_models, plugin=plugin)
            result = await validator_fn(ctx)
            if result is not None and not result:
                raise ActivityError(409, f"Validator '{validator_name}' failed")

    # 8b. Built-in tombstone shape validation. Runs only for the engine's
    # built-in `tombstone` activity type. Captures the version_ids that
    # will be tombstoned after persistence (step 11c below) so we don't
    # walk the used rows twice.
    tombstone_version_ids: list[UUID] = []
    if activity_def.get("name") == "tombstone":
        tombstone_version_ids, _ = _validate_tombstone_activity(
            used_rows=used_rows_by_ref,
            used_refs=used_refs,
            generated=generated,
        )

    # 9. Create activity + association
    activity_row = await repo.create_activity(
        activity_id=activity_id,
        dossier_id=dossier_id,
        type=activity_def["name"],
        started_at=now,
        ended_at=now,
        informed_by=informed_by,
    )

    await repo.create_association(
        association_id=uuid4(),
        activity_id=activity_id,
        agent_id=user.id,
        agent_name=user.name,
        agent_type=user.type,
        role=role,
    )

    # 10. Run handler (may produce additional generated entities)
    handler_name = activity_def.get("handler")
    handler_result = None
    if handler_name:
        handler_fn = plugin.handlers.get(handler_name)
        if handler_fn:
            ctx = ActivityContext(repo, dossier_id, resolved_entities, plugin.entity_models, plugin=plugin)
            client_content = generated[0]["content"] if generated else None
            handler_result = await handler_fn(ctx, client_content)

            if isinstance(handler_result, HandlerResult):
                # Process handler-generated entities (only if client didn't send any)
                if handler_result.generated and not generated:
                    allowed_types = activity_def.get("generates", [])
                    for gen_item in handler_result.generated:
                        gen_type = gen_item.get("type")
                        gen_content = gen_item.get("content")

                        # Handler can generate external entities
                        if gen_type == "external" and isinstance(gen_content, dict) and "uri" in gen_content:
                            generated_externals.append(gen_content["uri"])
                            continue
                        if gen_type is None and allowed_types:
                            gen_type = allowed_types[0]
                        if gen_type and gen_content:
                            # If the handler explicitly specified entity_id
                            # and derived_from, use them directly. Otherwise
                            # auto-fill: singletons auto-revise the existing
                            # entity; multi-cardinality creates a fresh one.
                            explicit_entity_id = gen_item.get("entity_id")
                            explicit_derived_from = gen_item.get("derived_from")

                            if explicit_entity_id is not None:
                                entity_id_val = UUID(str(explicit_entity_id))
                                derived_from_id = UUID(str(explicit_derived_from)) if explicit_derived_from else None
                            elif plugin.is_singleton(gen_type):
                                existing = await lookup_singleton(plugin, repo, dossier_id, gen_type)
                                entity_id_val = existing.entity_id if existing else uuid4()
                                derived_from_id = existing.id if existing else None
                            else:
                                entity_id_val = uuid4()
                                derived_from_id = None

                            generated.append({
                                "version_id": uuid4(),
                                "entity_id": entity_id_val,
                                "type": gen_type,
                                "content": gen_content,
                                "derived_from": derived_from_id,
                                "ref": None,
                            })

    # 11. Persist generated entities (wasGeneratedBy only, NO used link)
    generated_response = []
    for gen in generated:
        await repo.create_entity(
            version_id=gen["version_id"],
            entity_id=gen["entity_id"],
            dossier_id=dossier_id,
            type=gen["type"],
            generated_by=activity_id,
            content=gen["content"],
            derived_from=gen.get("derived_from"),
            attributed_to=user.id,
            schema_version=gen.get("schema_version"),
        )

        response_item = {
            "entity": gen.get("ref") or f"{gen['type']}/{gen['entity_id']}@{gen['version_id']}",
            "type": gen["type"],
            "content": gen["content"],
        }
        if gen.get("schema_version") is not None:
            response_item["schemaVersion"] = gen["schema_version"]
        generated_response.append(response_item)

    # 11b. Persist generated external entities (wasGeneratedBy link)
    for ext_uri in generated_externals:
        import uuid as uuid_mod
        ext_entity_id = uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, f"{dossier_id}:{ext_uri}")
        ext_version_id = uuid4()
        await repo.create_entity(
            version_id=ext_version_id,
            entity_id=ext_entity_id,
            dossier_id=dossier_id,
            type="external",
            generated_by=activity_id,
            content={"uri": ext_uri},
            attributed_to=user.id,
        )
        generated_response.append({
            "entity": ext_uri,
            "type": "external",
            "content": {"uri": ext_uri},
        })

    # 11c. Tombstone deletion. Runs after the replacement entity has been
    # persisted (step 11) so the new revision is in place before we null
    # the originals. Per the deletion-scope decision, we only NULL the
    # `content` blob and stamp `tombstoned_by`; the rows, derivation
    # edges, schema_version, and used links survive. The replacement and
    # any system:note entities generated by this same activity are NOT in
    # `tombstone_version_ids` (they're new rows from this activity, not
    # used rows) so they're untouched.
    if tombstone_version_ids:
        await repo.tombstone_entity_versions(tombstone_version_ids, activity_id)

    # 12. Create used links (references only — no overlap with generated)
    for ref in used_refs:
        if "version_id" in ref:
            await repo.create_used(activity_id, ref["version_id"])

    # 12b. Persist relation rows (oe:neemtAkteVan and any other plugin-
    # defined PROV-extension relations).
    for rel in validated_relations:
        await repo.create_relation(
            activity_id=activity_id,
            entity_version_id=rel["version_id"],
            relation_type=rel["relation_type"],
        )

    # 13. Determine and store status
    status = activity_def.get("status")
    if status is None and handler_result and isinstance(handler_result, HandlerResult):
        status = handler_result.status
    elif isinstance(status, dict):
        entity_type = status["from_entity"]
        field_path = status["field"]
        mapping = status["mapping"]
        for gen in generated:
            if gen["type"] == entity_type:
                value = _resolve_field(gen["content"], field_path)
                if value is not None and str(value) in mapping:
                    status = mapping[str(value)]
                    break

    if isinstance(status, str):
        activity_row.computed_status = status

    # 14. Execute side effects
    await repo.session.flush()
    await _execute_side_effects(
        plugin=plugin,
        repo=repo,
        dossier_id=dossier_id,
        trigger_activity_id=activity_id,
        side_effects=activity_def.get("side_effects", []),
    )

    # 15. Process tasks (YAML-defined + handler-appended)
    #
    # Tasks may declare an `anchor_type` in YAML (or the handler may supply
    # an explicit `anchor_entity_id` in HandlerResult.tasks). The anchor is
    # the specific entity this task is scoped to — used for:
    #   * cancel matching (only cancel if the canceling activity generated
    #     a version of the anchored entity)
    #   * supersede matching (only supersede existing tasks with the same
    #     anchor entity_id)
    #   * allow_multiple semantics (one task per anchor, not per dossier)
    #   * worker auto-resolve fallback (the task's own used block can
    #     resolve the anchored entity by entity_id at execution time)
    #
    # Resolution order at schedule time:
    #   1. Handler override (HandlerResult.tasks[].anchor_entity_id)
    #   2. Engine auto-fill via resolve_from_trigger (this activity's scope)
    #   3. None (global scope — matches None-anchored tasks only)
    all_task_defs = list(activity_def.get("tasks", []))
    if handler_result and isinstance(handler_result, HandlerResult):
        all_task_defs.extend(handler_result.tasks)

    for task_def in all_task_defs:
        task_kind = task_def.get("kind", "recorded")

        if task_kind == "fire_and_forget":
            # Type 1: execute inline, no record
            fn_name = task_def.get("function")
            if fn_name:
                fn = plugin.task_handlers.get(fn_name)
                if fn:
                    try:
                        ctx = ActivityContext(repo, dossier_id, resolved_entities, plugin.entity_models, plugin=plugin)
                        await fn(ctx)
                    except Exception:
                        pass  # fire and forget
        else:
            # Types 2, 3, 4: create system:task entity
            from ..entities import TaskEntity

            # Resolve anchor. Order: handler override → engine auto-fill.
            anchor_type = task_def.get("anchor_type")
            anchor_entity_id: UUID | None = None
            handler_anchor = task_def.get("anchor_entity_id")
            if handler_anchor is not None:
                anchor_entity_id = UUID(str(handler_anchor))
            elif anchor_type:
                # Auto-fill: look at what THIS activity generated/used.
                anchor_row = await resolve_from_trigger(
                    repo, activity_id, dossier_id, anchor_type,
                )
                if anchor_row is not None:
                    anchor_entity_id = anchor_row.entity_id
                # If still None, the handler must provide it explicitly.
                # Fail loudly rather than storing a task with no anchor when
                # one was declared.
                if anchor_entity_id is None:
                    raise ActivityError(
                        500,
                        f"Cannot resolve anchor for task "
                        f"{task_def.get('target_activity') or task_def.get('function')}: "
                        f"activity '{activity_def['name']}' did not touch any "
                        f"entity of type '{anchor_type}'. The handler must "
                        f"supply anchor_entity_id explicitly.",
                    )

            task_content = TaskEntity(
                kind=task_kind,
                function=task_def.get("function"),
                target_activity=task_def.get("target_activity"),
                scheduled_for=task_def.get("scheduled_for"),
                cancel_if_activities=task_def.get("cancel_if_activities", []),
                allow_multiple=task_def.get("allow_multiple", False),
                result_activity_id=str(uuid4()),
                status="scheduled",
                anchor_entity_id=str(anchor_entity_id) if anchor_entity_id else None,
                anchor_type=anchor_type,
            )

            # Supersession: scoped by anchor_entity_id. Two tasks supersede
            # each other only if they target the same activity AND share the
            # same anchor (None == None matches global-scope tasks).
            if not task_content.allow_multiple and task_content.target_activity:
                # Flat query + Python dedup (see cancel loop below for the
                # same pattern — avoids an expensive GROUP BY subquery).
                sup_rows = await repo.get_entities_by_type(dossier_id, "system:task")
                sup_latest: dict[UUID, EntityRow] = {}
                for row in sup_rows:
                    e = sup_latest.get(row.entity_id)
                    if e is None or row.created_at > e.created_at:
                        sup_latest[row.entity_id] = row
                for existing in sup_latest.values():
                    if not existing.content:
                        continue
                    if existing.content.get("status") != "scheduled":
                        continue
                    if existing.content.get("target_activity") != task_content.target_activity:
                        continue
                    if existing.content.get("anchor_entity_id") != task_content.anchor_entity_id:
                        continue
                    # Same target, same anchor → supersede.
                    superseded_content = dict(existing.content)
                    superseded_content["status"] = "superseded"
                    await repo.create_entity(
                        version_id=uuid4(),
                        entity_id=existing.entity_id,
                        dossier_id=dossier_id,
                        type="system:task",
                        generated_by=activity_id,
                        content=superseded_content,
                        derived_from=existing.id,
                        attributed_to="system",
                    )

            # Create the task entity
            await repo.create_entity(
                version_id=uuid4(),
                entity_id=uuid4(),
                dossier_id=dossier_id,
                type="system:task",
                generated_by=activity_id,
                content=task_content.model_dump(),
                attributed_to="system",
            )

    # 16. Cancel tasks that list this activity type in cancel_if_activities.
    #
    # Scoped by anchor: a cancel fires only if the canceling activity
    # **generated** a new version of the task's anchored entity. Using
    # generated-only (not generated+used) ensures we cancel when the
    # entity's state has actually advanced, not when someone merely
    # consulted it. None-anchored tasks (no anchor declared) are treated
    # as global-scope — they cancel whenever the target activity runs.
    # Use flat index-backed query and dedupe in Python — much faster
    # than the _latest variant's GROUP BY subquery. The keep-latest pass
    # is O(n) over a small list.
    all_task_rows = await repo.get_entities_by_type(dossier_id, "system:task")
    latest_by_eid: dict[UUID, EntityRow] = {}
    for row in all_task_rows:
        existing = latest_by_eid.get(row.entity_id)
        if existing is None or row.created_at > existing.created_at:
            latest_by_eid[row.entity_id] = row
    all_task_entities = list(latest_by_eid.values())
    # The canceling activity's generated entity_ids (fresh set for this loop).
    this_activity_generated_ids: set[UUID] = {g["entity_id"] for g in generated}
    for task_entity in all_task_entities:
        if not task_entity.content:
            continue
        if task_entity.content.get("status") != "scheduled":
            continue
        cancel_list = task_entity.content.get("cancel_if_activities", [])
        if activity_def["name"] not in cancel_list:
            continue

        # Anchor scope check: if the task is anchored, the canceling
        # activity must have generated a version of that specific entity.
        task_anchor_id_str = task_entity.content.get("anchor_entity_id")
        if task_anchor_id_str is not None:
            try:
                task_anchor_id = UUID(task_anchor_id_str)
            except (ValueError, TypeError):
                continue  # malformed anchor — skip
            if task_anchor_id not in this_activity_generated_ids:
                continue  # canceling activity didn't advance this entity

        # Only cancel if task was created before this activity.
        task_created = task_entity.created_at
        if task_created is None:
            continue
        if task_created.tzinfo is None:
            task_created = task_created.replace(tzinfo=timezone.utc)
        if task_created >= now:
            continue

        cancelled_content = dict(task_entity.content)
        cancelled_content["status"] = "cancelled"
        await repo.create_entity(
            version_id=uuid4(),
            entity_id=task_entity.entity_id,
            dossier_id=dossier_id,
            type="system:task",
            generated_by=activity_id,
            content=cancelled_content,
            derived_from=task_entity.id,
            attributed_to="system",
        )

    if not skip_cache:
        # 17. Compute status once (shared by hook, cache, and response)
        current_status = await derive_status(repo, dossier_id)

        # 18. Post-activity hook (e.g. update search indices)
        if plugin.post_activity_hook:
            try:
                current_entities = await repo.get_all_latest_entities(dossier_id)
                await plugin.post_activity_hook(
                    repo=repo,
                    dossier_id=dossier_id,
                    activity_type=activity_def["name"],
                    status=current_status,
                    entities={e.type: e for e in current_entities},
                )
            except Exception as e:
                import logging
                logging.getLogger("dossier.engine").warning(f"post_activity_hook failed: {e}")

        # 19. Cache status and eligible activities on dossier row
        eligible = await compute_eligible_activities(plugin, repo, dossier_id, known_status=current_status)

        dossier = await repo.get_dossier(dossier_id)
        if dossier:
            import json as _json
            dossier.cached_status = current_status
            dossier.eligible_activities = _json.dumps(eligible)

        # 20. Build response (user-specific filtering is cheap)
        allowed = await filter_by_user_auth(plugin, eligible, user, repo, dossier_id)
    else:
        # Fast path for bulk operations: use computed_status from activity row
        current_status = activity_row.computed_status or status if isinstance(status, str) else "unknown"
        allowed = []

    return {
        "activity": {
            "id": str(activity_id),
            "type": activity_def["name"],
            "associatedWith": {
                "agent": user.id,
                "role": role,
                "name": user.name,
            },
            "startedAtTime": now.isoformat(),
            "endedAtTime": now.isoformat(),
        },
        "used": [
            {
                "entity": r["entity"],
                "type": r.get("type", "external"),
                **({"autoResolved": True} if r.get("auto_resolved") else {}),
            }
            for r in used_refs
        ],
        "generated": generated_response,
        "relations": [
            {"entity": rel["ref"], "type": rel["relation_type"]}
            for rel in validated_relations
        ],
        "dossier": {
            "id": str(dossier_id),
            "workflow": dossier.workflow if dossier else workflow_name,
            "status": current_status,
            "allowedActivities": allowed,
        },
    }

async def _execute_side_effects(
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    trigger_activity_id: UUID,
    side_effects: list[dict],
    depth: int = 0,
    max_depth: int = 10,
):
    """
    Recursively execute side effect activities.

    Each side effect:
    1. Creates an activity record (wasInformedBy the trigger)
    2. Auto-resolves used entities
    3. Runs the handler if present
    4. Stores generated entities
    5. Recursively executes its own side effects
    """
    if depth >= max_depth:
        return  # safety limit

    if not side_effects:
        return  # nothing to do — skip the agent ensure and prefetch

    await repo.ensure_agent("system", "systeem", "Systeem", {})

    # Prefetch the trigger activity's generated and used entities ONCE for
    # the whole side-effects pass. Every side effect inside this call uses
    # the same trigger, so we'd otherwise redundantly query these for each
    # auto-resolved used entry. Two queries here instead of 2N queries.
    trigger_generated = await repo.get_entities_generated_by_activity(trigger_activity_id)
    trigger_used = await repo.get_used_entities_for_activity(trigger_activity_id)

    for side_effect in side_effects:
        se_activity_name = side_effect.get("activity")
        if not se_activity_name:
            continue

        # Check condition. First try the prefetched trigger scope (no DB
        # hit), then fall back to a singleton lookup for types not in the
        # trigger's scope.
        condition = side_effect.get("condition")
        if condition:
            cond_entity_type = condition.get("entity_type")
            cond_field = condition.get("field")
            cond_expected = condition.get("value")
            cond_entity = await resolve_from_prefetched(
                repo, dossier_id, trigger_generated, trigger_used, cond_entity_type,
            )
            if cond_entity is None and plugin.is_singleton(cond_entity_type):
                cond_entity = await lookup_singleton(
                    plugin, repo, dossier_id, cond_entity_type,
                )
            if not cond_entity or _resolve_field(cond_entity.content, cond_field) != cond_expected:
                continue

        se_def = _find_activity_def(plugin, se_activity_name)
        if not se_def:
            continue

        # Must have a handler — system activities compute their output
        se_handler_name = se_def.get("handler")
        if not se_handler_name:
            continue

        se_handler_fn = plugin.handlers.get(se_handler_name)
        if not se_handler_fn:
            continue

        # Create the side effect activity
        se_activity_id = uuid4()
        se_now = datetime.now(timezone.utc)

        se_activity_row = await repo.create_activity(
            activity_id=se_activity_id,
            dossier_id=dossier_id,
            type=se_activity_name,
            started_at=se_now,
            ended_at=se_now,
            informed_by=str(trigger_activity_id),
        )

        await repo.create_association(
            association_id=uuid4(),
            activity_id=se_activity_id,
            agent_id="system",
            agent_name="Systeem",
            agent_type="systeem",
            role="systeem",
        )

        # Auto-resolve used entities. For side effects, resolution is scoped
        # to the triggering activity: first check what it generated, then
        # what it used. Falls back to dossier-wide singleton lookup only if
        # the trigger didn't touch the requested type at all (and the type
        # is singleton-cardinality). This ensures multi-cardinality types
        # resolve to the specific entity the trigger worked on, not an
        # arbitrary "latest of type."
        se_resolved = {}
        for se_used_def in se_def.get("used", []):
            if se_used_def.get("external"):
                continue
            se_type = se_used_def["type"]
            if se_used_def.get("auto_resolve") == "latest":
                # Try trigger scope first using the prefetched lists.
                se_entity = await resolve_from_prefetched(
                    repo, dossier_id, trigger_generated, trigger_used, se_type,
                )
                if se_entity is None and plugin.is_singleton(se_type):
                    # Fallback: dossier-wide singleton (e.g. a system entity
                    # not directly touched by the trigger).
                    se_entity = await lookup_singleton(
                        plugin, repo, dossier_id, se_type,
                    )
                if se_entity:
                    se_resolved[se_type] = se_entity
                    await repo.create_used(se_activity_id, se_entity.id)


        # Run handler
        se_ctx = ActivityContext(repo, dossier_id, se_resolved, plugin.entity_models, plugin=plugin)
        se_result = await se_handler_fn(se_ctx, None)

        # Store handler-computed status
        if isinstance(se_result, HandlerResult) and se_result.status:
            se_activity_row.computed_status = se_result.status

        if isinstance(se_result, HandlerResult) and se_result.generated:
            se_generates = se_def.get("generates", [])
            for gen_item in se_result.generated:
                gen_type = gen_item.get("type")
                gen_content = gen_item.get("content")

                if gen_type is None and se_generates:
                    gen_type = se_generates[0]
                if gen_type and gen_content:
                    se_version_id = uuid4()
                    explicit_entity_id = gen_item.get("entity_id")
                    explicit_derived_from = gen_item.get("derived_from")

                    if explicit_entity_id is not None:
                        entity_id_val = UUID(str(explicit_entity_id))
                        derived_from_id = UUID(str(explicit_derived_from)) if explicit_derived_from else None
                    elif plugin.is_singleton(gen_type):
                        existing = await lookup_singleton(plugin, repo, dossier_id, gen_type)
                        derived_from_id = existing.id if existing else None
                        entity_id_val = existing.entity_id if existing else uuid4()
                    else:
                        derived_from_id = None
                        entity_id_val = uuid4()

                    # Resolve parent row (if any) to stamp schema_version
                    # correctly. Revisions inherit the parent's sticky
                    # version; fresh entities use the side-effect activity's
                    # `entities.<type>.new_version` declaration.
                    se_parent_row = None
                    if derived_from_id is not None:
                        se_parent_row = await repo.get_entity(derived_from_id)
                    se_schema_version = _resolve_schema_version_for_generated(
                        se_def, gen_type, se_parent_row
                    )

                    await repo.create_entity(
                        version_id=se_version_id,
                        entity_id=entity_id_val,
                        dossier_id=dossier_id,
                        type=gen_type,
                        generated_by=se_activity_id,
                        content=gen_content,
                        derived_from=derived_from_id,
                        attributed_to="system",
                        schema_version=se_schema_version,
                    )

        # Recurse into this side effect's own side effects
        nested_side_effects = se_def.get("side_effects", [])
        if nested_side_effects:
            # Flush so nested side effects can see entities we just created
            await repo.session.flush()
            await _execute_side_effects(
                plugin=plugin,
                repo=repo,
                dossier_id=dossier_id,
                trigger_activity_id=se_activity_id,
                side_effects=nested_side_effects,
                depth=depth + 1,
                max_depth=max_depth,
            )


async def _build_response(plugin, repo, dossier_id, activity_row, user):
    """Build response for an already-existing activity (idempotency)."""
    current_status = await derive_status(repo, dossier_id)
    allowed = await derive_allowed_activities(plugin, repo, dossier_id, user)
    dossier = await repo.get_dossier(dossier_id)

    return {
        "activity": {
            "id": str(activity_row.id),
            "type": activity_row.type,
            "startedAtTime": activity_row.started_at.isoformat() if activity_row.started_at else None,
            "endedAtTime": activity_row.ended_at.isoformat() if activity_row.ended_at else None,
        },
        "used": [],
        "generated": [],
        "dossier": {
            "id": str(dossier_id),
            "workflow": dossier.workflow if dossier else "",
            "status": current_status,
            "allowedActivities": allowed,
        },
    }


# =====================================================================
# Helpers
# =====================================================================

def _find_activity_def(plugin: Plugin, activity_type: str) -> dict | None:
    for act in plugin.workflow.get("activities", []):
        if act["name"] == activity_type:
            return act
    return None


class ActivityError(Exception):
    def __init__(self, status_code: int, detail: Any, payload: dict | None = None):
        self.status_code = status_code
        self.detail = detail
        self.payload = payload
