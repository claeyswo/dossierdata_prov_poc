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
                            entity = await repo.get_latest_entity(dossier_id, entity_type)
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
                        entity = await repo.get_latest_entity(dossier_id, entity_type)
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
    def __init__(self, content, entity_id, id, attributed_to):
        self.content = content
        self.entity_id = entity_id
        self.id = id
        self.attributed_to = attributed_to
        self.created_at = None


class ActivityContext:
    """Context passed to handlers and validators."""

    def __init__(self, repo: Repository, dossier_id: UUID, used_entities: dict[str, EntityRow],
                 entity_models: dict[str, Any] | None = None):
        self.repo = repo
        self.dossier_id = dossier_id
        self._used_entities = used_entities
        self._entity_models = entity_models or {}

    def get_used_entity(self, entity_type: str) -> EntityRow | None:
        return self._used_entities.get(entity_type)

    def get_typed(self, entity_type: str) -> Any | None:
        """Get a used entity's content as a validated Pydantic model instance.
        Returns None if the entity doesn't exist or has no content."""
        entity = self._used_entities.get(entity_type)
        if not entity or not entity.content:
            return None
        model_class = self._entity_models.get(entity_type)
        if model_class:
            return model_class(**entity.content)
        return None

    async def get_latest_typed(self, entity_type: str) -> Any | None:
        """Get the latest entity's content as a validated Pydantic model instance."""
        entity = await self.repo.get_latest_entity(self.dossier_id, entity_type)
        if not entity or not entity.content:
            return None
        model_class = self._entity_models.get(entity_type)
        if model_class:
            return model_class(**entity.content)
        return None

    async def has_activity(self, activity_type: str) -> bool:
        activities = await self.repo.get_activities_for_dossier(self.dossier_id)
        return any(a.type == activity_type for a in activities)

    async def get_latest_entity(self, entity_type: str) -> EntityRow | None:
        return await self.repo.get_latest_entity(self.dossier_id, entity_type)


class HandlerResult:
    """Result returned by a handler function.
    
    Supports:
    - Single entity: HandlerResult(content={...}, status="...")
    - Multiple entities: HandlerResult(generated=[("oe:type", {...}), ...], status="...")
    - Tasks: HandlerResult(tasks=[{...task def...}], status="...")
    - All combined
    """

    def __init__(
        self,
        content: dict | None = None,
        status: str | None = None,
        generated: list[tuple[str, dict]] | None = None,
        tasks: list[dict] | None = None,
    ):
        # Backward compat: single content → list with type=None (resolved from generates[0])
        if content and not generated:
            self.generated = [(None, content)]
        else:
            self.generated = generated or []
        self.status = status
        self.tasks = tasks or []


class TaskResult:
    """Result returned by a cross-dossier task function."""

    def __init__(self, target_dossier_id: str, content: dict | None = None):
        self.target_dossier_id = target_dossier_id
        self.content = content


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
) -> dict:
    """
    Execute an activity.

    used_items: references to existing entities or external URIs (read-only)
    generated_items: new entities or revisions the client is creating
    """
    if generated_items is None:
        generated_items = []
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

    # 5. Process used items (references only) + auto-resolve
    used_refs = []
    resolved_entities: dict[str, EntityRow] = {}

    for item in used_items:
        entity_ref = item.get("entity", "")

        if is_external_uri(entity_ref):
            # Persist as external entity (idempotent)
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
        used_refs.append({"entity": entity_ref, "version_id": parsed["version"], "type": entity_type})
        resolved_entities[entity_type] = existing_entity

    # Auto-resolve missing used entities
    for used_def in activity_def.get("used", []):
        if used_def.get("external"):
            continue
        etype = used_def["type"]
        auto = used_def.get("auto_resolve")
        if auto == "latest" and etype not in resolved_entities:
            entity = await repo.get_latest_entity(dossier_id, etype)
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

        if allowed_types and entity_type not in allowed_types:
            raise ActivityError(422, f"Activity cannot generate entity type '{entity_type}'")

        model_class = plugin.entity_models.get(entity_type)
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
        })

        # Make available to handlers
        resolved_entities[entity_type] = _PendingEntity(
            content=content,
            entity_id=parsed["id"],
            id=parsed["version"],
            attributed_to=user.id,
        )

    # 7. Ensure agent exists
    await repo.ensure_agent(user.id, user.type, user.name, user.properties)

    # 8. Run custom validators
    for validator_def in activity_def.get("validators", []):
        validator_name = validator_def["name"]
        validator_fn = plugin.validators.get(validator_name)
        if validator_fn:
            ctx = ActivityContext(repo, dossier_id, resolved_entities, plugin.entity_models)
            result = await validator_fn(ctx)
            if result is not None and not result:
                raise ActivityError(409, f"Validator '{validator_name}' failed")

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
            ctx = ActivityContext(repo, dossier_id, resolved_entities, plugin.entity_models)
            client_content = generated[0]["content"] if generated else None
            handler_result = await handler_fn(ctx, client_content)

            if isinstance(handler_result, HandlerResult):
                # Process handler-generated entities (only if client didn't send any)
                if handler_result.generated and not generated:
                    allowed_types = activity_def.get("generates", [])
                    for gen_type, gen_content in handler_result.generated:
                        # Handler can generate external entities
                        if gen_type == "external" and isinstance(gen_content, dict) and "uri" in gen_content:
                            generated_externals.append(gen_content["uri"])
                            continue
                        if gen_type is None and allowed_types:
                            gen_type = allowed_types[0]
                        if gen_type and gen_content:
                            # Check if this is a revision of an existing entity
                            existing = await repo.get_latest_entity(dossier_id, gen_type)
                            generated.append({
                                "version_id": uuid4(),
                                "entity_id": existing.entity_id if existing else uuid4(),
                                "type": gen_type,
                                "content": gen_content,
                                "derived_from": existing.id if existing else None,
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
        )

        generated_response.append({
            "entity": gen.get("ref") or f"{gen['type']}/{gen['entity_id']}@{gen['version_id']}",
            "type": gen["type"],
            "content": gen["content"],
        })

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

    # 12. Create used links (references only — no overlap with generated)
    for ref in used_refs:
        if "version_id" in ref:
            await repo.create_used(activity_id, ref["version_id"])

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
                        ctx = ActivityContext(repo, dossier_id, resolved_entities, plugin.entity_models)
                        await fn(ctx)
                    except Exception:
                        pass  # fire and forget
        else:
            # Types 2, 3, 4: create system:task entity
            from ..entities import TaskEntity
            task_content = TaskEntity(
                kind=task_kind,
                function=task_def.get("function"),
                target_activity=task_def.get("target_activity"),
                scheduled_for=task_def.get("scheduled_for"),
                cancel_if_activities=task_def.get("cancel_if_activities", []),
                allow_multiple=task_def.get("allow_multiple", False),
                result_activity_id=str(uuid4()),
                status="scheduled",
            )

            # Check for existing scheduled tasks with same target_activity
            if not task_content.allow_multiple and task_content.target_activity:
                existing_tasks = await repo.get_entities_by_type(dossier_id, "system:task")
                for existing in existing_tasks:
                    if existing.content and \
                       existing.content.get("target_activity") == task_content.target_activity and \
                       existing.content.get("status") == "scheduled":
                        # Supersede old task
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

    # 16. Cancel tasks that list this activity type in cancel_if_activities
    all_task_entities = await repo.get_entities_by_type(dossier_id, "system:task")
    for task_entity in all_task_entities:
        if not task_entity.content:
            continue
        if task_entity.content.get("status") != "scheduled":
            continue
        cancel_list = task_entity.content.get("cancel_if_activities", [])
        if activity_def["name"] in cancel_list:
            # Only cancel if task was created before this activity
            task_created = task_entity.created_at
            if task_created:
                # Ensure both are comparable (SQLite returns naive datetimes)
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

    await repo.ensure_agent("system", "systeem", "Systeem", {})

    for side_effect in side_effects:
        se_activity_name = side_effect.get("activity")
        if not se_activity_name:
            continue

        # Check condition
        condition = side_effect.get("condition")
        if condition:
            cond_entity_type = condition.get("entity_type")
            cond_field = condition.get("field")
            cond_expected = condition.get("value")
            cond_entity = await repo.get_latest_entity(dossier_id, cond_entity_type)
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

        # Auto-resolve used entities
        se_resolved = {}
        for se_used_def in se_def.get("used", []):
            if se_used_def.get("external"):
                continue
            se_type = se_used_def["type"]
            if se_used_def.get("auto_resolve") == "latest":
                se_entity = await repo.get_latest_entity(dossier_id, se_type)
                if se_entity:
                    se_resolved[se_type] = se_entity
                    await repo.create_used(se_activity_id, se_entity.id)


        # Run handler
        se_ctx = ActivityContext(repo, dossier_id, se_resolved, plugin.entity_models)
        se_result = await se_handler_fn(se_ctx, None)

        # Store handler-computed status
        if isinstance(se_result, HandlerResult) and se_result.status:
            se_activity_row.computed_status = se_result.status

        if isinstance(se_result, HandlerResult) and se_result.generated:
            se_generates = se_def.get("generates", [])
            for gen_type, gen_content in se_result.generated:
                if gen_type is None and se_generates:
                    gen_type = se_generates[0]
                if gen_type and gen_content:
                    se_version_id = uuid4()
                    existing = await repo.get_latest_entity(dossier_id, gen_type)
                    derived_from_id = existing.id if existing else None
                    entity_id_val = existing.entity_id if existing else uuid4()

                    await repo.create_entity(
                        version_id=se_version_id,
                        entity_id=entity_id_val,
                        dossier_id=dossier_id,
                        type=gen_type,
                        generated_by=se_activity_id,
                        content=gen_content,
                        derived_from=derived_from_id,
                        attributed_to="system",
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
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
