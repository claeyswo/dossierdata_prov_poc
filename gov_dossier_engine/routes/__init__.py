"""
Route generation and API endpoints.

Generates typed FastAPI routes from workflow definitions.
Each workflow gets its own tag group in the docs.
All routes call the same generic engine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Any, Optional

from ..plugin import Plugin, PluginRegistry
from ..auth import User, POCAuthMiddleware
from ..db import get_session_factory, Repository
from ..engine import (
    execute_activity, derive_status, derive_allowed_activities,
    compute_eligible_activities, filter_by_user_auth, ActivityError,
)
from ..file_refs import inject_download_urls


def _activity_error_to_http(e: ActivityError) -> HTTPException:
    """Forward an ActivityError to an HTTPException, merging any structured
    payload so the client gets a single JSON body."""
    if e.payload:
        body = {"detail": e.detail, **e.payload}
        return HTTPException(e.status_code, detail=body)
    return HTTPException(e.status_code, detail=e.detail)


def _entity_version_dict(
    e,
    dossier_id,
    entity_type: str,
    siblings: list,
    include_entity_id: bool = True,
) -> dict:
    """Render an EntityRow as a dict for the bulk version-listing endpoints.

    For tombstoned versions (option Y from the design): keep the row in
    the response with `content: null`, add `tombstonedBy` (the activity
    UUID that performed the redaction) and `redirectTo` (a relative URL
    pointing at the live replacement). The replacement is whichever
    sibling has the same entity_id and is not itself tombstoned and has
    the latest created_at — or simply the latest sibling if everything
    is tombstoned, since re-tombstoning is allowed.
    """
    out = {
        "versionId": str(e.id),
        "content": e.content,
        "generatedBy": str(e.generated_by) if e.generated_by else None,
        "derivedFrom": str(e.derived_from) if e.derived_from else None,
        "attributedTo": e.attributed_to,
        "createdAt": e.created_at.isoformat() if e.created_at else None,
    }
    if include_entity_id:
        out["entityId"] = str(e.entity_id)
    if e.schema_version is not None:
        out["schemaVersion"] = e.schema_version

    if e.tombstoned_by is not None:
        out["tombstonedBy"] = str(e.tombstoned_by)
        # Find the live replacement: latest sibling with same entity_id
        # that isn't this row.
        candidates = [s for s in siblings if s.entity_id == e.entity_id and s.id != e.id]
        live = [c for c in candidates if c.tombstoned_by is None]
        target_pool = live if live else candidates
        if target_pool:
            replacement = max(
                target_pool,
                key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc),
            )
            out["redirectTo"] = (
                f"/dossiers/{dossier_id}/entities/{entity_type}/"
                f"{replacement.entity_id}/{replacement.id}"
            )
    return out


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


class RelationItem(BaseModel):
    """Generic activity→entity relation under a named type.

    Example for the `oe:neemtAkteVan` pattern — acknowledging newer versions
    of an entity the activity chose not to act on:
        {"entity": "oe:aanvraag/X@v3", "type": "oe:neemtAkteVan"}

    The `type` string is validated against the activity's YAML declaration
    of allowed relation types. Plugins register validators per type to
    enforce semantics (e.g. neemtAkteVan must cover every version between
    the declared used version and the current latest)."""
    entity: str
    type: str


class ActivityRequest(BaseModel):
    type: Optional[str] = None     # set from URL on typed endpoints
    workflow: Optional[str] = None  # only needed for first activity
    role: Optional[str] = None     # defaults to activity's default_role
    informed_by: Optional[str] = None  # local UUID or cross-dossier URI
    used: list[UsedItem] = []
    generated: list[GeneratedItem] = []
    relations: list[RelationItem] = []


class BatchActivityItem(BaseModel):
    """Single activity within a batch request."""
    activity_id: str               # client-generated UUID
    type: str
    role: Optional[str] = None
    informed_by: Optional[str] = None
    used: list[UsedItem] = []
    generated: list[GeneratedItem] = []
    relations: list[RelationItem] = []


class BatchActivityRequest(BaseModel):
    workflow: Optional[str] = None  # only needed if first activity creates dossier
    activities: list[BatchActivityItem]


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
    schemaVersion: Optional[str] = None


class DossierResponse(BaseModel):
    id: str
    workflow: str
    status: str
    allowedActivities: list[dict[str, str]] = []


class RelationResponse(BaseModel):
    entity: str
    type: str


class FullResponse(BaseModel):
    activity: ActivityResponse
    used: list[UsedResponse] = []
    generated: list[GeneratedResponse] = []
    relations: list[RelationResponse] = []
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

def register_routes(app: FastAPI, registry: PluginRegistry, get_user, global_access: list[dict] | None = None):
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
                        relation_items=[item.model_dump() for item in request.relations],
                        workflow_name=request.workflow,
                        informed_by=request.informed_by,
                    )
                except ActivityError as e:
                    raise _activity_error_to_http(e)

                return response

    # --- Batch activities ---

    @app.put(
        "/dossiers/{dossier_id}/activities",
        tags=["activities"],
        summary="Execute multiple activities atomically",
        description="Execute multiple activities in order within a single transaction. "
                    "If any activity fails, all are rolled back. "
                    "Entities generated by earlier activities are visible to later ones via auto-resolve.",
    )
    async def execute_batch_activities(
        dossier_id: UUID,
        request: BatchActivityRequest,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                repo = Repository(session)
                results = []

                for item in request.activities:
                    # Resolve plugin + activity def
                    result = registry.get_for_activity(item.type)
                    if not result:
                        if request.workflow:
                            plugin = registry.get(request.workflow)
                            if not plugin:
                                raise HTTPException(404, detail=f"Unknown workflow: {request.workflow}")
                            act_def = next(
                                (a for a in plugin.workflow.get("activities", []) if a["name"] == item.type),
                                None,
                            )
                            if not act_def:
                                raise HTTPException(404, detail=f"Unknown activity: {item.type}")
                        else:
                            raise HTTPException(404, detail=f"Unknown activity type: {item.type}")
                    else:
                        plugin, act_def = result

                    try:
                        activity_id = UUID(item.activity_id)
                        response = await execute_activity(
                            plugin=plugin,
                            activity_def=act_def,
                            repo=repo,
                            dossier_id=dossier_id,
                            activity_id=activity_id,
                            user=user,
                            role=item.role,
                            used_items=[u.model_dump() for u in item.used],
                            generated_items=[g.model_dump() for g in item.generated],
                            relation_items=[r.model_dump() for r in item.relations],
                            workflow_name=request.workflow,
                            informed_by=item.informed_by,
                        )
                    except ActivityError as e:
                        # Preserve the batch position in the error detail but
                        # still forward any structured payload.
                        prefix = f"Activity '{item.type}' (#{len(results)+1}) failed: "
                        if e.payload:
                            body = {"detail": f"{prefix}{e.detail}", **e.payload}
                            raise HTTPException(e.status_code, detail=body)
                        raise HTTPException(
                            e.status_code,
                            detail=f"{prefix}{e.detail}",
                        )

                    # Flush so next activity can see entities from this one
                    await repo.session.flush()
                    results.append(response)

                # Return the last activity's dossier state + all individual results
                return {
                    "activities": results,
                    "dossier": results[-1]["dossier"] if results else None,
                }

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
            access_entry = await check_dossier_access(repo, dossier_id, user, global_access)
            visible_prefixes, activity_view_mode = get_visibility_from_entry(access_entry)

            # Use cached status and eligible activities if available
            import json as _json

            if dossier.cached_status is not None:
                status = dossier.cached_status
            else:
                status = await derive_status(repo, dossier_id)

            if dossier.eligible_activities is not None:
                try:
                    eligible = _json.loads(dossier.eligible_activities)
                except (ValueError, TypeError):
                    eligible = await compute_eligible_activities(plugin, repo, dossier_id)
            else:
                eligible = await compute_eligible_activities(plugin, repo, dossier_id)

            allowed = await filter_by_user_auth(plugin, eligible, user, repo, dossier_id)

            # Get current entities — filtered by visible prefixes
            entities = await repo.get_all_latest_entities(dossier_id)
            current_entities = []
            visible_entity_version_ids = set()

            # Prepare download URL signing. The model-aware injector
            # hydrates each entity's content through its registered Pydantic
            # model and walks for fields annotated with `FileId`.
            file_config = app.state.config.get("file_service", {})
            signing_key = file_config.get("signing_key", "poc-signing-key-change-in-production")
            file_service_url = file_config.get("url", "http://localhost:8001")

            def _make_signer(dossier_id_str: str, user_id: str):
                from gov_file_service import sign_token, token_to_query_string

                def sign(file_id: str) -> str:
                    token = sign_token(
                        file_id=file_id,
                        action="download",
                        signing_key=signing_key,
                        user_id=user_id,
                        dossier_id=dossier_id_str,
                    )
                    return f"{file_service_url}/download/{file_id}?{token_to_query_string(token)}"

                return sign

            sign = _make_signer(str(dossier_id), user.id)

            for e in entities:
                if visible_prefixes is None or e.type in visible_prefixes:
                    model_class = plugin.resolve_schema(e.type, e.schema_version)
                    content = inject_download_urls(model_class, e.content, sign) if e.content else e.content

                    entity_out = {
                        "type": e.type,
                        "entityId": str(e.entity_id),
                        "versionId": str(e.id),
                        "content": content,
                        "createdAt": e.created_at.isoformat() if e.created_at else None,
                    }
                    if e.schema_version is not None:
                        entity_out["schemaVersion"] = e.schema_version
                    current_entities.append(entity_out)
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

    # --- List dossiers (stub — use workflow-specific search endpoints) ---

    @app.get(
        "/dossiers",
        tags=["dossiers"],
        summary="List dossiers (stub)",
        description="Basic dossier listing. For production, use the workflow-specific search "
                    "endpoints (e.g. /dossiers/toelatingen/search) which query Elasticsearch.",
    )
    async def list_dossiers(
        workflow: Optional[str] = None,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            from sqlalchemy import select
            from ..db.models import DossierRow

            query = select(DossierRow)
            if workflow:
                query = query.where(DossierRow.workflow == workflow)
            query = query.order_by(DossierRow.created_at.desc()).limit(100)

            result = await session.execute(query)
            dossiers = list(result.scalars().all())

            items = []
            for d in dossiers:
                items.append({
                    "id": str(d.id),
                    "workflow": d.workflow,
                    "createdAt": d.created_at.isoformat() if d.created_at else None,
                })

            return {"dossiers": items}

    # --- Register plugin search routes ---

    for plugin in registry.all_plugins():
        if plugin.search_route_factory:
            plugin.search_route_factory(app, get_user)

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
            access_entry = await check_dossier_access(repo, dossier_id, user, global_access)
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
                    _entity_version_dict(e, dossier_id, entity_type, entities)
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
            access_entry = await check_dossier_access(repo, dossier_id, user, global_access)
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
                    _entity_version_dict(e, dossier_id, entity_type, versions, include_entity_id=False)
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
            access_entry = await check_dossier_access(repo, dossier_id, user, global_access)
            visible_types, _ = get_visibility_from_entry(access_entry)
            if visible_types is not None and entity_type not in visible_types:
                raise HTTPException(403, detail=f"No access to entity type '{entity_type}'")

            entity = await repo.get_entity(version_id)
            if not entity or entity.dossier_id != dossier_id or entity.type != entity_type:
                raise HTTPException(404, detail="Entity version not found")

            # Tombstone redirect. If this version has been redacted, look
            # up the latest version of the same logical entity (which by
            # construction is the tombstone replacement, since tombstones
            # generate a new revision) and 301 to its URL. Per the
            # deletion-scope decision the row itself survives, so the
            # initial fetch and FK-walk are still cheap.
            if entity.tombstoned_by is not None:
                latest = await repo.get_latest_entity_by_id(dossier_id, entity.entity_id)
                if latest is not None and latest.id != entity.id:
                    target = (
                        f"/dossiers/{dossier_id}/entities/{entity_type}/"
                        f"{entity.entity_id}/{latest.id}"
                    )
                    return RedirectResponse(url=target, status_code=301)
                # No replacement found (shouldn't happen under normal
                # tombstone flow, but guard anyway): return 410 Gone.
                raise HTTPException(410, detail="Entity version was tombstoned and has no replacement")

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

    # --- File upload request (generates signed upload URL) ---

    @app.post(
        "/files/upload/request",
        tags=["files"],
        summary="Request a signed upload URL",
    )
    async def request_upload(
        request_body: dict,
        user: User = Depends(get_user),
    ):
        """Request a signed URL for file upload. User must be authenticated.
        Returns a file_id and upload_url to send the file to the File Service."""
        from uuid import uuid4
        from gov_file_service import sign_token, token_to_query_string

        file_config = app.state.config.get("file_service", {})
        signing_key = file_config.get("signing_key", "poc-signing-key-change-in-production")
        file_service_url = file_config.get("url", "http://localhost:8001")

        file_id = str(uuid4())
        token = sign_token(
            file_id=file_id,
            action="upload",
            signing_key=signing_key,
            user_id=user.id,
        )

        upload_url = f"{file_service_url}/upload/{file_id}?{token_to_query_string(token)}"

        return {
            "file_id": file_id,
            "upload_url": upload_url,
            "filename": request_body.get("filename", ""),
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
                        relation_items=[item.model_dump() for item in request.relations],
                        workflow_name=request.workflow,
                        informed_by=request.informed_by,
                    )
                except ActivityError as e:
                    raise _activity_error_to_http(e)

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
                desc += _format_entity_schemas_for_doc(
                    plugin, act_def, entity_type, context="used"
                )
        desc += "\n"

    # Generates with schemas
    generates = act_def.get("generates", [])
    if generates:
        desc += "### Generates\n"
        for g in generates:
            desc += f"\n#### `{g}`\n"
            desc += _format_entity_schemas_for_doc(
                plugin, act_def, g, context="generates"
            )
        desc += "\n"

    return desc


def _format_entity_schemas_for_doc(plugin, act_def, entity_type: str, context: str) -> str:
    """Render the schema section(s) for a content-bearing entity type on an
    activity, for inclusion in the OpenAPI summary.

    For activities that declare version discipline via `entities.<type>`:
      * `new_version` → "When creating a fresh entity: version X"
      * `allowed_versions` → "When revising an existing entity: accepts X, Y"
      * Each distinct version emits its own JSON schema block, labeled.

    For legacy activities (no `entities` block), emits a single unlabeled
    schema block from `entity_models[type]` — identical to pre-versioning
    behavior.
    """
    import json

    ecfg = (act_def.get("entities") or {}).get(entity_type) or {}
    new_version = ecfg.get("new_version")
    allowed_versions = list(ecfg.get("allowed_versions") or [])

    # Legacy path — no version discipline declared for this type on this activity.
    if not ecfg:
        model_class = plugin.resolve_schema(entity_type, None)
        if not model_class:
            return ""
        try:
            schema = model_class.model_json_schema()
        except Exception:
            return ""
        out = f"\n**Content schema (`{entity_type}`):**\n"
        out += f"```json\n{json.dumps(schema, indent=2)}\n```\n"
        return out

    # Versioned path — enumerate.
    out = ""

    if context == "generates" and new_version:
        out += (
            f"\n**Fresh entities are stamped as version `{new_version}`.** "
            f"The engine inherits the parent's stored version on revisions "
            f"(sticky).\n"
        )
    if allowed_versions:
        pretty = ", ".join(f"`{v}`" for v in allowed_versions)
        out += (
            f"\n**This activity accepts existing entities at version(s): "
            f"{pretty}.** Revisions of entities at other versions are "
            f"rejected with `422 unsupported_schema_version`.\n"
        )

    # Collect every version we need to render a schema for (deduped, ordered).
    versions_to_render: list[str] = []
    seen: set[str] = set()
    for v in ([new_version] if new_version else []) + allowed_versions:
        if v and v not in seen:
            versions_to_render.append(v)
            seen.add(v)

    for v in versions_to_render:
        model_class = plugin.resolve_schema(entity_type, v)
        if not model_class:
            continue
        try:
            schema = model_class.model_json_schema()
        except Exception:
            continue
        out += f"\n**Schema `{entity_type}` @ `{v}`:**\n"
        out += f"```json\n{json.dumps(schema, indent=2)}\n```\n"

    return out


from .access import check_dossier_access, get_visibility_from_entry
