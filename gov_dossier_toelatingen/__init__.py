"""
Toelatingen beschermd erfgoed plugin.

Provides:
- workflow definition
- entity models
- handlers
- validators
- task handlers
- post_activity_hook (updates search indices)
- search_route_factory (registers /dossiers/toelatingen/search)
"""

from __future__ import annotations

import logging
import os
import yaml

from gov_dossier_engine.plugin import Plugin
from gov_dossier_engine.entities import DossierAccess

from .entities import (
    Aanvraag,
    Beslissing,
    Handtekening,
    VerantwoordelijkeOrganisatie,
    Behandelaar,
    SystemFields,
)
from .handlers import HANDLERS
from .validators import VALIDATORS
from .relation_validators import RELATION_VALIDATORS
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
    """Register toelatingen-specific search endpoint."""
    from fastapi import Depends, Query
    from gov_dossier_engine.auth import User

    @app.get(
        "/dossiers/toelatingen/search",
        tags=["toelatingen"],
        summary="Search toelatingen dossiers",
        description="Search the toelatingen Elasticsearch index. "
                    "POC stub — returns empty results. "
                    "In production, queries the dossiers-toelatingen index.",
    )
    async def search_toelatingen(
        q: str = Query(None, description="Full-text search query"),
        gemeente: str = Query(None, description="Filter by gemeente"),
        status: str = Query(None, description="Filter by status"),
        user: User = Depends(get_user),
    ):
        # In production:
        # query = build_es_query(q=q, gemeente=gemeente, status=status, user=user)
        # results = await es.search(index="dossiers-toelatingen", body=query)
        # return {"results": [hit["_source"] for hit in results["hits"]["hits"]]}

        return {
            "message": "Search stub — Elasticsearch not connected",
            "query": {"q": q, "gemeente": gemeente, "status": status},
            "results": [],
        }


def create_plugin() -> Plugin:
    """Create and return the toelatingen plugin."""

    workflow_path = os.path.join(os.path.dirname(__file__), "workflow.yaml")
    with open(workflow_path) as f:
        workflow = yaml.safe_load(f)

    entity_models = {
        "oe:aanvraag": Aanvraag,
        "oe:beslissing": Beslissing,
        "oe:handtekening": Handtekening,
        "oe:verantwoordelijke_organisatie": VerantwoordelijkeOrganisatie,
        "oe:behandelaar": Behandelaar,
        "oe:system_fields": SystemFields,
        "oe:dossier_access": DossierAccess,
    }

    return Plugin(
        name=workflow["name"],
        workflow=workflow,
        entity_models=entity_models,
        handlers=HANDLERS,
        validators=VALIDATORS,
        relation_validators=RELATION_VALIDATORS,
        task_handlers=TASK_HANDLERS,
        post_activity_hook=update_search_index,
        search_route_factory=register_search_routes,
    )
