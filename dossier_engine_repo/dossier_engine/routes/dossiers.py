"""
Dossier-level read endpoints.

Two routes:

* `GET /dossiers/{id}` — full dossier detail: workflow, status,
  allowed-activity list filtered for the calling user, current entity
  snapshot (one version per logical entity, filtered by the user's
  dossier_access visibility), and the activity log (also filtered by
  the user's view mode).
* `GET /dossiers` — basic listing across all dossiers, optionally
  filtered by workflow. Stub — production callers use the workflow-
  specific Elasticsearch-backed search endpoints.

The detail endpoint does a fair bit of work:

1. **Cache hit path**: cached `status` and `eligible_activities` on
   the dossier row are used when present, falling back to a fresh
   `derive_status` + `compute_eligible_activities` pass otherwise.
   The cache is updated by the engine's finalization phase after every
   activity, so a cache miss only happens if the dossier was created
   outside the engine flow or if the cache hasn't been warmed yet.
2. **File URL signing**: every entity's content is walked through its
   registered Pydantic model; fields annotated with `FileId` are
   replaced with signed download URLs scoped to the calling user and
   dossier. Tokens carry an HMAC over `(file_id, action, user_id,
   dossier_id, expires)` and the file service refuses requests that
   don't match.
3. **Visibility filtering**: the calling user's `dossier_access`
   entry resolves to a set of visible entity-type prefixes and an
   activity-view mode (`all`, `own`, `related`). Entities outside
   the visible prefixes are dropped from `currentEntities`. Activities
   are filtered per the view mode: `own` shows only activities where
   the user is the agent, `related` shows activities that touched
   visible entities (plus the user's own).
"""

from __future__ import annotations

import json as _json
from typing import Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select

from dossier_common.signing import sign_token, token_to_query_string

from ..auth import User
from ..db import Repository, get_session_factory
from ..db.models import AssociationRow, DossierRow
from ..engine import (
    compute_eligible_activities,
    derive_status,
    filter_by_user_auth,
)
from ..file_refs import inject_download_urls
from ._models import DossierDetailResponse
from .access import check_dossier_access, get_visibility_from_entry


def register(app: FastAPI, *, registry, get_user, global_access) -> None:
    """Register dossier read endpoints on the FastAPI app."""

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
        async with session_factory() as session, session.begin():
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            plugin = registry.get(dossier.workflow)
            if not plugin:
                raise HTTPException(
                    500,
                    detail=f"Plugin not found for workflow: {dossier.workflow}",
                )

            access_entry = await check_dossier_access(
                repo, dossier_id, user, global_access,
            )
            visible_prefixes, activity_view_mode = get_visibility_from_entry(access_entry)

            # Use cached status + eligible activities if the engine has
            # warmed them; fall back to fresh computation otherwise.
            if dossier.cached_status is not None:
                status = dossier.cached_status
            else:
                status = await derive_status(repo, dossier_id)

            if dossier.eligible_activities is not None:
                try:
                    eligible = _json.loads(dossier.eligible_activities)
                except (ValueError, TypeError):
                    eligible = await compute_eligible_activities(
                        plugin, repo, dossier_id,
                    )
            else:
                eligible = await compute_eligible_activities(
                    plugin, repo, dossier_id,
                )

            allowed = await filter_by_user_auth(
                plugin, eligible, user, repo, dossier_id,
            )

            # Render visible current entities with file download URLs
            # injected. The signer closure binds dossier_id and user_id
            # so each token carries the right scope.
            entities = await repo.get_all_latest_entities(dossier_id)
            file_config = app.state.config.get("file_service", {})
            signing_key = file_config.get(
                "signing_key", "poc-signing-key-change-in-production",
            )
            file_service_url = file_config.get("url", "http://localhost:8001")

            def _make_signer(dossier_id_str: str, user_id: str):
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

            current_entities = []
            visible_entity_version_ids = set()
            for e in entities:
                if visible_prefixes is None or e.type in visible_prefixes:
                    model_class = plugin.resolve_schema(e.type, e.schema_version)
                    content = (
                        inject_download_urls(model_class, e.content, sign)
                        if e.content else e.content
                    )
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

            # Activity log filtered per the calling user's view mode.
            from ._activity_visibility import parse_activity_view, is_activity_visible
            parsed_view = parse_activity_view(activity_view_mode)
            activities = await repo.get_activities_for_dossier(dossier_id)

            async def _is_agent(act_id, uid):
                return await _user_is_agent(session, act_id, uid)

            async def _used_ids(act_id):
                return await repo.get_used_entity_ids_for_activity(act_id)

            activity_list = []
            for a in activities:
                visible = await is_activity_visible(
                    parsed_view,
                    activity_type=a.type,
                    activity_id=a.id,
                    user_id=user.id,
                    visible_entity_ids=visible_entity_version_ids,
                    lookup_is_agent=_is_agent,
                    lookup_used_entity_ids=_used_ids,
                )
                if visible:
                    activity_list.append({
                        "id": str(a.id),
                        "type": a.type,
                        "startedAtTime": a.started_at.isoformat() if a.started_at else None,
                        "informedBy": str(a.informed_by) if a.informed_by else None,
                    })

            # Audit: successful dossier read. Emitted after all access
            # checks passed but before the response is serialized, so
            # a late JSON encoding error wouldn't lose the audit record
            # of the access attempt.
            # NOTE: use `dossier_status` (not `status`) in the audit
            # payload — `status` is one of Wazuh's 13 reserved static
            # field names and can produce accidental rule matches
            # against built-in rules that key on the static `status`
            # slot.
            from ..audit import emit_audit
            emit_audit(
                action="dossier.read",
                actor_id=user.id,
                actor_name=user.name,
                target_type="Dossier",
                target_id=str(dossier_id),
                outcome="allowed",
                dossier_id=str(dossier_id),
                workflow=dossier.workflow,
                dossier_status=status,
            )

            # Load active domain relations for the response.
            domain_rels = await repo.get_active_domain_relations(dossier_id)
            domain_relations_out = [
                {
                    "type": r.relation_type,
                    "from": r.from_ref,
                    "to": r.to_ref,
                    "createdBy": str(r.created_by_activity_id),
                    "createdAt": r.created_at.isoformat() if r.created_at else None,
                }
                for r in domain_rels
            ]

            return DossierDetailResponse(
                id=str(dossier_id),
                workflow=dossier.workflow,
                status=status,
                allowedActivities=allowed,
                currentEntities=current_entities,
                activities=activity_list,
                domainRelations=domain_relations_out,
            )

    @app.get(
        "/dossiers",
        tags=["dossiers"],
        summary="List dossiers (stub)",
        description=(
            "Basic dossier listing. For production, use the workflow-"
            "specific search endpoints (e.g. /dossiers/toelatingen/search) "
            "which query Elasticsearch."
        ),
    )
    async def list_dossiers(
        workflow: Optional[str] = None,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            query = select(DossierRow)
            if workflow:
                query = query.where(DossierRow.workflow == workflow)
            query = query.order_by(DossierRow.created_at.desc()).limit(100)

            result = await session.execute(query)
            dossiers = list(result.scalars().all())

            items = [
                {
                    "id": str(d.id),
                    "workflow": d.workflow,
                    "createdAt": d.created_at.isoformat() if d.created_at else None,
                }
                for d in dossiers
            ]
            return {"dossiers": items}


async def _user_is_agent(session, activity_id: UUID, user_id: str) -> bool:
    """Return True if `user_id` has an association row for `activity_id`."""
    result = await session.execute(
        select(AssociationRow)
        .where(AssociationRow.activity_id == activity_id)
        .where(AssociationRow.agent_id == user_id)
    )
    return result.scalar_one_or_none() is not None
