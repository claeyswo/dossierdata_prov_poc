"""
Route generation and API endpoints.

Generates typed FastAPI routes from workflow definitions.
Each workflow gets its own tag group in the docs.
All routes call the same generic engine.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, Optional

from ..plugin import Plugin, PluginRegistry
from ..auth import User, POCAuthMiddleware
from ..db import get_session_factory, Repository
from ..engine import execute_activity, derive_status, derive_allowed_activities, ActivityError


# =====================================================================
# Request / Response Models
# =====================================================================

class UsedItem(BaseModel):
    """Reference to an existing entity or external URI."""
    entity: str


class GeneratedItem(BaseModel):
    """New entity or new version of an existing entity."""
    entity: str
    content: dict[str, Any]
    derivedFrom: Optional[str] = None


class ActivityRequest(BaseModel):
    type: Optional[str] = None     # set from URL on typed endpoints
    workflow: Optional[str] = None  # only needed for first activity
    role: Optional[str] = None     # defaults to activity's default_role
    used: list[UsedItem] = []
    generated: list[GeneratedItem] = []


class AssociatedWith(BaseModel):
    agent: str
    role: str
    name: str


class ActivityResponse(BaseModel):
    id: str
    type: str
    associatedWith: Optional[AssociatedWith] = None
    startedAtTime: Optional[str] = None
    endedAtTime: Optional[str] = None


class UsedResponse(BaseModel):
    entity: str
    type: str = "unknown"


class GeneratedResponse(BaseModel):
    entity: str
    type: str
    content: Optional[dict[str, Any]] = None


class DossierResponse(BaseModel):
    id: str
    workflow: str
    status: str
    allowedActivities: list[dict[str, str]] = []


class FullResponse(BaseModel):
    activity: ActivityResponse
    used: list[UsedResponse] = []
    generated: list[GeneratedResponse] = []
    dossier: DossierResponse


class DossierDetailResponse(BaseModel):
    id: str
    workflow: str
    status: str
    allowedActivities: list[dict[str, str]] = []
    currentEntities: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []


# =====================================================================
# Route Registration
# =====================================================================

def register_routes(app: FastAPI, registry: PluginRegistry, get_user):
    """Register all routes — generic endpoints + per-workflow typed wrappers."""

    # --- Generic activity endpoint ---

    @app.put(
        "/dossiers/{dossier_id}/activities/{activity_id}",
        response_model=FullResponse,
        tags=["activities"],
        summary="Execute an activity",
    )
    async def put_activity(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User = Depends(get_user),
    ):
        # Find the plugin for this activity
        if not request.type:
            raise HTTPException(422, detail="'type' is required on the generic endpoint")
        result = registry.get_for_activity(request.type)
        if not result:
            # Maybe it's a new dossier — use the workflow field
            if request.workflow:
                plugin = registry.get(request.workflow)
                if not plugin:
                    raise HTTPException(404, detail=f"Unknown workflow: {request.workflow}")
                act_def = None
                for a in plugin.workflow.get("activities", []):
                    if a["name"] == request.type:
                        act_def = a
                        break
                if not act_def:
                    raise HTTPException(404, detail=f"Unknown activity: {request.type}")
            else:
                raise HTTPException(404, detail=f"Unknown activity type: {request.type}")
        else:
            plugin, act_def = result

        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                repo = Repository(session)

                try:
                    response = await execute_activity(
                        plugin=plugin,
                        activity_def=act_def,
                        repo=repo,
                        dossier_id=dossier_id,
                        activity_id=activity_id,
                        user=user,
                        role=request.role,
                        used_items=[item.model_dump() for item in request.used],
                        generated_items=[item.model_dump() for item in request.generated],
                        workflow_name=request.workflow,
                    )
                except ActivityError as e:
                    raise HTTPException(e.status_code, detail=e.detail)

                return response

    # --- Get dossier ---

    @app.get(
        "/dossiers/{dossier_id}",
        response_model=DossierDetailResponse,
        tags=["dossiers"],
        summary="Get dossier details",
    )
    async def get_dossier(
        dossier_id: UUID,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            plugin = registry.get(dossier.workflow)
            if not plugin:
                raise HTTPException(500, detail=f"Plugin not found for workflow: {dossier.workflow}")

            # Check dossier_access
            access_entry = await check_dossier_access(repo, dossier_id, user)
            visible_prefixes, activity_view_mode = get_visibility_from_entry(access_entry)

            status = await derive_status(repo, dossier_id)
            allowed = await derive_allowed_activities(plugin, repo, dossier_id, user)

            # Get current entities — filtered by visible prefixes
            entities = await repo.get_all_latest_entities(dossier_id)
            current_entities = []
            visible_entity_version_ids = set()
            for e in entities:
                # type IS the prefix (e.g. "oe:aanvraag")
                # Filter: only include if type is in visible set (or no filtering)
                if visible_prefixes is None or e.type in visible_prefixes:
                    current_entities.append({
                        "type": e.type,
                        "entityId": str(e.entity_id),
                        "versionId": str(e.id),
                        "content": e.content,
                        "createdAt": e.created_at.isoformat() if e.created_at else None,
                    })
                    visible_entity_version_ids.add(e.id)

            # Get activity history — filtered by activity_view_mode
            activities = await repo.get_activities_for_dossier(dossier_id)
            activity_list = []

            for a in activities:
                include = False

                if activity_view_mode == "all":
                    include = True
                elif activity_view_mode == "own":
                    # Only activities where this user is the agent
                    from sqlalchemy import select
                    from ..db.models import AssociationRow
                    assoc_result = await session.execute(
                        select(AssociationRow)
                        .where(AssociationRow.activity_id == a.id)
                        .where(AssociationRow.agent_id == user.id)
                    )
                    if assoc_result.scalar_one_or_none():
                        include = True
                elif activity_view_mode == "related":
                    # Activities that touch visible entities
                    used_ids = await repo.get_used_entity_ids_for_activity(a.id)
                    if used_ids & visible_entity_version_ids:
                        include = True
                    # Also include if user is the agent
                    from sqlalchemy import select
                    from ..db.models import AssociationRow
                    assoc_result = await session.execute(
                        select(AssociationRow)
                        .where(AssociationRow.activity_id == a.id)
                        .where(AssociationRow.agent_id == user.id)
                    )
                    if assoc_result.scalar_one_or_none():
                        include = True

                if include:
                    activity_list.append({
                        "id": str(a.id),
                        "type": a.type,
                        "startedAtTime": a.started_at.isoformat() if a.started_at else None,
                        "informedBy": str(a.informed_by) if a.informed_by else None,
                    })

            return DossierDetailResponse(
                id=str(dossier_id),
                workflow=dossier.workflow,
                status=status,
                allowedActivities=allowed,
                currentEntities=current_entities,
                activities=activity_list,
            )

    # --- List dossiers ---

    @app.get(
        "/dossiers",
        tags=["dossiers"],
        summary="List dossiers",
    )
    async def list_dossiers(
        workflow: Optional[str] = None,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            from sqlalchemy import select
            from ..db.models import DossierRow

            repo = Repository(session)

            query = select(DossierRow)
            if workflow:
                query = query.where(DossierRow.workflow == workflow)
            query = query.order_by(DossierRow.created_at.desc())

            result = await session.execute(query)
            dossiers = list(result.scalars().all())

            # TODO: filter by dossier_access entity

            items = []
            for d in dossiers:
                plugin = registry.get(d.workflow)
                status = "unknown"
                if plugin:
                    status = await derive_status(repo, d.id)
                items.append({
                    "id": str(d.id),
                    "workflow": d.workflow,
                    "status": status,
                    "createdAt": d.created_at.isoformat() if d.created_at else None,
                })

            return {"dossiers": items}

    # --- Entity endpoints ---

    @app.get(
        "/dossiers/{dossier_id}/entities/{entity_type}",
        tags=["entities"],
        summary="Get all versions of an entity type",
        description="Returns all versions of a given entity type in this dossier, ordered by creation time. Respects dossier_access visibility.",
    )
    async def get_entity_versions(
        dossier_id: UUID,
        entity_type: str,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            # Check dossier_access
            access_entry = await check_dossier_access(repo, dossier_id, user)
            visible_types, _ = get_visibility_from_entry(access_entry)
            if visible_types is not None and entity_type not in visible_types:
                raise HTTPException(403, detail=f"No access to entity type '{entity_type}'")

            entities = await repo.get_entities_by_type(dossier_id, entity_type)
            if not entities:
                raise HTTPException(404, detail=f"No entities of type '{entity_type}' found")

            return {
                "dossier_id": str(dossier_id),
                "entity_type": entity_type,
                "versions": [
                    {
                        "versionId": str(e.id),
                        "entityId": str(e.entity_id),
                        "content": e.content,
                        "generatedBy": str(e.generated_by),
                        "derivedFrom": str(e.derived_from) if e.derived_from else None,
                        "attributedTo": e.attributed_to,
                        "createdAt": e.created_at.isoformat() if e.created_at else None,
                    }
                    for e in entities
                ],
            }

    @app.get(
        "/dossiers/{dossier_id}/entities/{entity_type}/{entity_id}",
        tags=["entities"],
        summary="Get all versions of a specific logical entity",
        description="Returns all versions of a specific logical entity (by entity_id), ordered by creation time.",
    )
    async def get_logical_entity_versions(
        dossier_id: UUID,
        entity_type: str,
        entity_id: UUID,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            # Check dossier_access
            access_entry = await check_dossier_access(repo, dossier_id, user)
            visible_types, _ = get_visibility_from_entry(access_entry)
            if visible_types is not None and entity_type not in visible_types:
                raise HTTPException(403, detail=f"No access to entity type '{entity_type}'")

            entities = await repo.get_entity_versions(dossier_id, entity_id)
            # Filter by type to be safe
            versions = [e for e in entities if e.type == entity_type]
            if not versions:
                raise HTTPException(404, detail=f"Entity not found")

            return {
                "dossier_id": str(dossier_id),
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "versions": [
                    {
                        "versionId": str(e.id),
                        "content": e.content,
                        "generatedBy": str(e.generated_by),
                        "derivedFrom": str(e.derived_from) if e.derived_from else None,
                        "attributedTo": e.attributed_to,
                        "createdAt": e.created_at.isoformat() if e.created_at else None,
                    }
                    for e in versions
                ],
            }

    @app.get(
        "/dossiers/{dossier_id}/entities/{entity_type}/{entity_id}/{version_id}",
        tags=["entities"],
        summary="Get a specific entity version",
        description="Returns a single entity version by its version ID.",
    )
    async def get_entity_version(
        dossier_id: UUID,
        entity_type: str,
        entity_id: UUID,
        version_id: UUID,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            # Check dossier_access
            access_entry = await check_dossier_access(repo, dossier_id, user)
            visible_types, _ = get_visibility_from_entry(access_entry)
            if visible_types is not None and entity_type not in visible_types:
                raise HTTPException(403, detail=f"No access to entity type '{entity_type}'")

            entity = await repo.get_entity(version_id)
            if not entity or entity.dossier_id != dossier_id or entity.type != entity_type:
                raise HTTPException(404, detail="Entity version not found")

            return {
                "dossier_id": str(dossier_id),
                "entity_type": entity_type,
                "entity_id": str(entity.entity_id),
                "versionId": str(entity.id),
                "content": entity.content,
                "generatedBy": str(entity.generated_by),
                "derivedFrom": str(entity.derived_from) if entity.derived_from else None,
                "attributedTo": entity.attributed_to,
                "createdAt": entity.created_at.isoformat() if entity.created_at else None,
            }

    # --- Per-workflow typed route wrappers for docs ---

    for plugin in registry.all_plugins():
        workflow_name = plugin.name

        for act_def in plugin.workflow.get("activities", []):
            # Skip activities not callable by clients
            if act_def.get("client_callable") is False:
                continue

            act_name = act_def["name"]
            act_label = act_def.get("label", act_name)
            act_desc = _build_activity_description(act_def, plugin)

            _register_typed_route(
                app, registry, get_user,
                workflow_name, act_name, act_label, act_desc,
            )


def _register_typed_route(
    app: FastAPI,
    registry: PluginRegistry,
    get_user,
    workflow_name: str,
    act_name: str,
    act_label: str,
    act_desc: str,
):
    """Register a typed route for a specific activity.

    Uses the generic ActivityRequest model (avoids Pydantic dynamic model issues).
    Entity schemas are documented in the endpoint description markdown.
    """

    @app.put(
        f"/dossiers/{{dossier_id}}/activities/{{activity_id}}/{act_name}",
        response_model=FullResponse,
        tags=[workflow_name],
        summary=act_label,
        description=act_desc,
    )
    async def endpoint(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User = Depends(get_user),
    ):
        request.type = act_name
        if not request.workflow:
            request.workflow = workflow_name

        result = registry.get_for_activity(act_name)
        if not result:
            p = registry.get(workflow_name)
            ad = next((a for a in p.workflow["activities"] if a["name"] == act_name), None)
        else:
            p, ad = result

        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                repo = Repository(session)
                try:
                    response = await execute_activity(
                        plugin=p,
                        activity_def=ad,
                        repo=repo,
                        dossier_id=dossier_id,
                        activity_id=activity_id,
                        user=user,
                        role=request.role,
                        used_items=[item.model_dump() for item in request.used],
                        generated_items=[item.model_dump() for item in request.generated],
                        workflow_name=request.workflow,
                    )
                except ActivityError as e:
                    raise HTTPException(e.status_code, detail=e.detail)

                return response

    # Give unique name for FastAPI route registration
    endpoint.__name__ = f"typed_{act_name}"
    endpoint.__qualname__ = f"typed_{act_name}"


def _build_activity_description(act_def: dict, plugin: Plugin) -> str:
    """Generate rich markdown description for Swagger docs, including entity schemas."""
    desc = f"{act_def.get('description', '')}\n\n"

    # Authorization
    auth = act_def.get("authorization", {})
    roles = auth.get("roles", [])
    if roles:
        desc += "### Authorization\n"
        for r in roles:
            if isinstance(r, dict) and "role" in r:
                scope = r.get("scope")
                if scope:
                    desc += f"- `{r['role']}` scoped from `{scope['from_entity']}.{scope['field']}`\n"
                else:
                    desc += f"- `{r['role']}`\n"
            elif isinstance(r, dict) and "from_entity" in r:
                desc += f"- Entity-derived from `{r['from_entity']}.{r['field']}`\n"
            else:
                desc += f"- `{r}`\n"
        desc += "\n"

    # Requirements
    reqs = act_def.get("requirements", {})
    if any(reqs.get(k) for k in ["activities", "entities", "statuses"]):
        desc += "### Requirements\n"
        for a in reqs.get("activities", []):
            if a:
                desc += f"- Activity: `{a}`\n"
        for e in reqs.get("entities", []):
            if e:
                desc += f"- Entity: `{e}`\n"
        for s in reqs.get("statuses", []):
            if s:
                desc += f"- Status: `{s}`\n"
        desc += "\n"

    # Used entities with schemas
    used_defs = act_def.get("used", [])
    if used_defs:
        desc += "### Used entities\n"
        for u in used_defs:
            ext = " (external URI)" if u.get("external") else ""
            req = "**required**" if u.get("required") else "optional"
            auto = f", auto-resolve: `{u['auto_resolve']}`" if u.get("auto_resolve") else ""
            accept = u.get("accept", "any")
            desc += f"\n#### `{u['type']}` — {accept}, {req}{auto}{ext}\n"
            if u.get("description"):
                desc += f"{u['description']}\n"

            # Add entity schema if it's a content-bearing type
            if not u.get("external") and accept in ("new", "any"):
                entity_type = u.get("type", "")
                model_class = plugin.entity_models.get(entity_type)
                if model_class:
                    try:
                        schema = model_class.model_json_schema()
                        import json
                        desc += f"\n**Content schema (`{entity_type}`):**\n"
                        desc += f"```json\n{json.dumps(schema, indent=2)}\n```\n"
                    except Exception:
                        pass
        desc += "\n"

    # Generates with schemas
    generates = act_def.get("generates", [])
    if generates:
        desc += "### Generates\n"
        for g in generates:
            desc += f"\n#### `{g}`\n"
            model_class = plugin.entity_models.get(g)
            if model_class:
                try:
                    schema = model_class.model_json_schema()
                    import json
                    desc += f"\n**Content schema:**\n"
                    desc += f"```json\n{json.dumps(schema, indent=2)}\n```\n"
                except Exception:
                    pass
        desc += "\n"

    return desc


from .access import check_dossier_access, get_visibility_from_entry
