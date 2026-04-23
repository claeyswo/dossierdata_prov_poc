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
    build_callable_registries_from_workflow,
    validate_workflow_version_references,
    validate_side_effect_conditions,
    validate_side_effect_condition_fn_registrations,
)

logger = logging.getLogger("toelatingen.index")


async def update_search_index(repo, dossier_id, activity_type, status, entities):
    """Post-activity hook — upsert both indices.

    Writes to the toelatingen-specific index and to the engine's
    common index after each activity completes. Silent no-op when
    Elasticsearch isn't configured (DOSSIER_ES_URL empty).

    `entities` is a dict of entity_type → latest EntityRow, supplied
    by the engine. We read aanvraag.content and beslissing.content
    from there. The access entity isn't in that dict (it's a
    singleton side-effect entity), so we fetch it from the repo.
    """
    from .search import build_toelatingen_doc, index_one as index_toel
    from dossier_engine.search.common_index import (
        build_common_doc, index_one as index_common,
    )

    aanvraag = entities.get("oe:aanvraag")
    beslissing = entities.get("oe:beslissing")

    # Access entity drives ACL — fetch the latest.
    access = await repo.get_singleton_entity(dossier_id, "oe:dossier_access")
    access_content = access.content if access else None

    aanvraag_content = aanvraag.content if aanvraag else None
    beslissing_content = beslissing.content if beslissing else None

    # Toelatingen-specific doc
    specific_doc = build_toelatingen_doc(
        dossier_id, aanvraag_content, beslissing_content, access_content,
    )

    # Common doc
    onderwerp = (aanvraag_content or {}).get("onderwerp") if aanvraag_content else None
    common_doc = build_common_doc(
        dossier_id, "toelatingen", onderwerp, access_content,
    )

    logger.info(
        "[INDEX] dossier=%s status=%s activity=%s acl_size=%d",
        dossier_id, status, activity_type, len(specific_doc["__acl__"]),
    )

    await index_toel(specific_doc)
    await index_common(common_doc)


def register_search_routes(app, get_user):
    """Register toelatingen search + admin endpoints:

    * GET /toelatingen/dossiers
    * POST /toelatingen/admin/search/recreate
    * POST /toelatingen/admin/search/reindex
    * POST /toelatingen/admin/search/reindex-all (toel + common)
    """
    from fastapi import Depends, Query, HTTPException
    from dossier_engine.auth import User
    from dossier_engine.db import get_session_factory
    from dossier_engine.db.models import Repository
    from .search import (
        search_toelatingen as es_search,
        recreate_index as es_recreate,
        reindex_all as es_reindex,
        reindex_common_too as es_reindex_both,
    )

    def _require_admin(user: User) -> None:
        """Gate admin endpoints on global_admin_access roles. Reads
        the role list from the search module's registration (set by
        the engine at app startup). Default-deny on misconfiguration."""
        from dossier_engine.search import get_global_admin_access
        admin_roles = get_global_admin_access()
        if not admin_roles:
            raise HTTPException(
                403,
                detail=(
                    "Admin endpoints require global_admin_access to "
                    "be configured in config.yaml."
                ),
            )
        if not any(r in user.roles for r in admin_roles):
            raise HTTPException(
                403,
                detail="Admin endpoints require a global_admin_access role.",
            )

    @app.get(
        "/toelatingen/dossiers",
        tags=["toelatingen"],
        summary="Search toelatingen dossiers",
        description=(
            "Searches the dossiers-toelatingen index with fuzzy match "
            "on onderwerp and exact filters on gemeente and "
            "beslissing. Results are filtered to dossiers the current "
            "user may see (ACL = user.roles ∪ user.id)."
        ),
    )
    async def search_toelatingen_endpoint(
        q: str | None = Query(None, description="Fuzzy search on aanvraag.onderwerp"),
        gemeente: str | None = Query(None, description="Exact filter on aanvraag.gemeente"),
        beslissing: str | None = Query(None, description="Exact filter on beslissing"),
        limit: int = Query(50, ge=1, le=500),
        user: User = Depends(get_user),
    ):
        return await es_search(
            user=user, q=q, gemeente=gemeente, beslissing=beslissing,
            limit=limit,
        )

    @app.post(
        "/toelatingen/admin/search/recreate",
        tags=["admin"],
        summary="Drop and recreate the toelatingen index",
        description=(
            "DESTRUCTIVE. Drops the dossiers-toelatingen index (if any) "
            "and creates it with the current mapping. Does NOT re-index "
            "data — call /toelatingen/admin/search/reindex after this. "
            "Requires an audit role."
        ),
    )
    async def recreate_toel(user: User = Depends(get_user)):
        _require_admin(user)
        return await es_recreate()

    @app.post(
        "/toelatingen/admin/search/reindex",
        tags=["admin"],
        summary="Re-index every toelatingen dossier",
        description=(
            "Walks every toelatingen dossier in Postgres and indexes "
            "it into dossiers-toelatingen. Does not touch the common "
            "index — use /reindex-all for that. Requires an audit role."
        ),
    )
    async def reindex_toel(user: User = Depends(get_user)):
        _require_admin(user)
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)
            return await es_reindex(repo)

    @app.post(
        "/toelatingen/admin/search/reindex-all",
        tags=["admin"],
        summary="Re-index toelatingen dossiers into both indices",
        description=(
            "Walks every toelatingen dossier and upserts into both "
            "dossiers-toelatingen AND dossiers-common. Useful after "
            "a mapping change affecting both. Requires an audit role."
        ),
    )
    async def reindex_toel_and_common(user: User = Depends(get_user)):
        _require_admin(user)
        registry = app.state.registry
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)
            return await es_reindex_both(repo, registry)


def create_plugin() -> Plugin:
    """Create and return the toelatingen plugin.

    Entity model and versioned schema registries are built from the
    workflow YAML's ``entity_types`` block — each entry declares its
    ``model`` (default/unversioned) and optional ``schemas`` mapping
    version strings to fully-qualified Pydantic class paths. This is
    the single source of truth for the versioning picture; the engine
    cross-checks every activity's ``new_version`` / ``allowed_versions``
    against it at load time.

    The eight Callable registries (handlers, validators, task_handlers,
    status_resolvers, task_builders, side_effect_conditions,
    relation_validators, field_validators) are built from the same YAML
    via ``build_callable_registries_from_workflow`` — each activity
    declaration names its callables by fully-qualified dotted path,
    which the engine resolves at load time (Obs 95 / Round 28). This
    plugin therefore does not maintain any per-registry short-name
    dicts of its own — all registration happens in ``workflow.yaml``.
    """

    workflow_path = os.path.join(os.path.dirname(__file__), "workflow.yaml")
    with open(workflow_path) as f:
        workflow = yaml.safe_load(f)

    entity_models, entity_schemas = build_entity_registries_from_workflow(workflow)
    callables = build_callable_registries_from_workflow(workflow)

    validate_workflow_version_references(workflow, entity_schemas)
    validate_side_effect_conditions(workflow)
    validate_side_effect_condition_fn_registrations(
        workflow, callables["side_effect_conditions"],
    )

    # Build the constants object. Precedence: env vars (via
    # BaseSettings) > workflow.yaml's constants.values > class defaults.
    # The `constants:` block is optional — omitted means use all
    # defaults + env overrides.
    from .constants import ToelatingenConstants
    yaml_constants = (workflow.get("constants") or {}).get("values", {}) or {}
    constants = ToelatingenConstants(**yaml_constants)

    from .search import build_common_doc_for_dossier as _build_common_doc

    return Plugin(
        name=workflow["name"],
        workflow=workflow,
        entity_models=entity_models,
        entity_schemas=entity_schemas,
        handlers=callables["handlers"],
        status_resolvers=callables["status_resolvers"],
        task_builders=callables["task_builders"],
        side_effect_conditions=callables["side_effect_conditions"],
        validators=callables["validators"],
        relation_validators=callables["relation_validators"],
        field_validators=callables["field_validators"],
        task_handlers=callables["task_handlers"],
        post_activity_hook=update_search_index,
        search_route_factory=register_search_routes,
        build_common_doc_for_dossier=_build_common_doc,
        constants=constants,
    )
