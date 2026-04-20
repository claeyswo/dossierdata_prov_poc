"""
Toelatingen beschermd erfgoed plugin.

Provides:
- workflow definition
- entity models
- handlers
- validators
- task handlers
- post_activity_hook (updates search indices)
"""

from __future__ import annotations

import logging
import os
import yaml

from dossier_engine.plugin import (
    Plugin,
    build_entity_registries_from_workflow,
    validate_workflow_version_references,
)

from .handlers import HANDLERS
from .validators import VALIDATORS
from .relation_validators import RELATION_VALIDATORS
from .field_validators import FIELD_VALIDATORS
from .tasks import TASK_HANDLERS

logger = logging.getLogger("toelatingen.index")


async def update_search_index(repo, dossier_id, activity_type, status, entities):
    """
    Post-activity hook: update Elasticsearch indices.

    Called after each activity completes, inside the same transaction.
    Updates both the common index (shared fields across all workflows)
    and the toelatingen-specific index.

    In production, this would call Elasticsearch. For POC, just logs.
    """
    aanvraag_entity = entities.get("oe:aanvraag")

    # Common index document (shared across all workflow types)
    common_doc = {
        "dossier_id": str(dossier_id),
        "workflow": "toelatingen",
        "status": status,
        "last_activity": activity_type,
    }

    # Toelatingen-specific index document
    specific_doc = dict(common_doc)
    if aanvraag_entity and aanvraag_entity.content:
        specific_doc.update({
            "onderwerp": aanvraag_entity.content.get("onderwerp"),
            "gemeente": aanvraag_entity.content.get("gemeente"),
            "handeling": aanvraag_entity.content.get("handeling"),
            "object_uri": aanvraag_entity.content.get("object"),
        })
        aanvrager = aanvraag_entity.content.get("aanvrager", {})
        if isinstance(aanvrager, dict):
            specific_doc["aanvrager_kbo"] = aanvrager.get("kbo")
            specific_doc["aanvrager_rrn"] = aanvrager.get("rrn")

    beslissing_entity = entities.get("oe:beslissing")
    if beslissing_entity and beslissing_entity.content:
        specific_doc["beslissing"] = beslissing_entity.content.get("beslissing")

    logger.info(f"[INDEX] dossier={dossier_id} status={status} activity={activity_type}")
    logger.debug(f"[INDEX] common: {common_doc}")
    logger.debug(f"[INDEX] specific: {specific_doc}")

    # In production:
    # await es.index(index="dossiers-common", id=str(dossier_id), document=common_doc)
    # await es.index(index="dossiers-toelatingen", id=str(dossier_id), document=specific_doc)


def register_search_routes(app, get_user):
    """Register toelatingen search endpoint at /toelatingen/dossiers.

    This replaces a generic engine-level listing. The toelatingen
    workflow exposes rich query parameters (gemeente, status,
    handeling, date range, aanvrager identity) that are specific to
    this workflow's entity content and wouldn't make sense for other
    workflows. Each plugin owns its own search endpoint.
    """
    from fastapi import Depends, Query
    from sqlalchemy import select
    from dossier_engine.auth import User
    from dossier_engine.db import get_session_factory
    from dossier_engine.db.models import DossierRow

    @app.get(
        "/toelatingen/dossiers",
        tags=["toelatingen"],
        summary="List/search toelatingen dossiers",
        description=(
            "List and search toelatingen dossiers. In production "
            "queries the `dossiers-toelatingen` Elasticsearch index. "
            "In the POC this falls back to a simple Postgres filter "
            "on workflow name."
        ),
    )
    async def search_toelatingen(
        q: str = Query(None, description="Full-text search over aanvraag content"),
        gemeente: str = Query(None, description="Filter by gemeente (e.g. 'brugge')"),
        status: str = Query(None, description="Filter by dossier status"),
        handeling: str = Query(None, description="Filter by handeling type"),
        limit: int = Query(100, ge=1, le=500),
        user: User = Depends(get_user),
    ):
        # In production:
        # query = build_es_query(q=q, gemeente=gemeente, status=status,
        #                        handeling=handeling, user=user)
        # results = await es.search(index="dossiers-toelatingen", body=query)
        # return {"results": [hit["_source"] for hit in results["hits"]["hits"]]}

        # POC: Postgres fallback with workflow filter only.
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            stmt = (
                select(DossierRow)
                .where(DossierRow.workflow == "toelatingen")
                .order_by(DossierRow.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            dossiers = list(result.scalars().all())

            return {
                "query": {
                    "q": q, "gemeente": gemeente,
                    "status": status, "handeling": handeling,
                },
                "results": [
                    {
                        "id": str(d.id),
                        "workflow": d.workflow,
                        "createdAt": d.created_at.isoformat() if d.created_at else None,
                    }
                    for d in dossiers
                ],
                "note": "POC stub — Elasticsearch not connected. Filters "
                        "are not yet applied; only workflow filtering runs.",
            }


def create_plugin() -> Plugin:
    """Create and return the toelatingen plugin.

    Entity model and versioned schema registries are built from the
    workflow YAML's `entity_types` block — each entry declares its
    `model` (default/unversioned) and optional `schemas` mapping
    version strings to fully-qualified Pydantic class paths. This is
    the single source of truth for the versioning picture; the engine
    cross-checks every activity's `new_version` / `allowed_versions`
    against it at load time.
    """

    workflow_path = os.path.join(os.path.dirname(__file__), "workflow.yaml")
    with open(workflow_path) as f:
        workflow = yaml.safe_load(f)

    entity_models, entity_schemas = build_entity_registries_from_workflow(workflow)
    validate_workflow_version_references(workflow, entity_schemas)

    # Build the constants object. Precedence: env vars (via
    # BaseSettings) > workflow.yaml's constants.values > class defaults.
    # The `constants:` block is optional — omitted means use all
    # defaults + env overrides.
    from .constants import ToelatingenConstants
    yaml_constants = (workflow.get("constants") or {}).get("values", {}) or {}
    constants = ToelatingenConstants(**yaml_constants)

    return Plugin(
        name=workflow["name"],
        workflow=workflow,
        entity_models=entity_models,
        entity_schemas=entity_schemas,
        handlers=HANDLERS,
        validators=VALIDATORS,
        relation_validators=RELATION_VALIDATORS,
        field_validators=FIELD_VALIDATORS,
        task_handlers=TASK_HANDLERS,
        post_activity_hook=update_search_index,
        search_route_factory=register_search_routes,
        constants=constants,
    )
