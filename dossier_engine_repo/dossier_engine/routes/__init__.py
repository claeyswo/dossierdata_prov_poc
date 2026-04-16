"""
Route registration orchestrator.

The dossier API's HTTP surface is split across leaf modules in this
subpackage — `activities.py`, `dossiers.py`, `entities.py`, `files.py`,
plus the existing `access.py`, `prov.py`, `prov_columns.py`. Each
exposes a `register(app, *, deps...)` function that takes the FastAPI
app and the dependencies it needs.

`register_routes` is the single entry point app startup calls. It
walks every registrar in turn, plus the per-plugin search-route
factory loop. The order doesn't matter — none of the registrars
depend on each other's routes existing — but it's kept stable for
predictable OpenAPI ordering.

This module also re-exports the leaf modules' public types and
helpers under their original underscore-prefixed names so any
external code that still imports from `dossier_engine.routes`
keeps working. New callers should import from the leaf modules
directly.
"""

from __future__ import annotations

from fastapi import FastAPI

from ..plugin import PluginRegistry

from . import activities as _activities_routes
from . import dossiers as _dossiers_routes
from . import entities as _entities_routes
from . import files as _files_routes

# Back-compat re-exports — leaf module symbols under the names the
# pre-Stage-6 monolith used. New code should import from the leaf
# modules directly.
from ._errors import activity_error_to_http as _activity_error_to_http
from ._models import (
    ActivityRequest,
    ActivityResponse,
    AssociatedWith,
    BatchActivityItem,
    BatchActivityRequest,
    DossierDetailResponse,
    DossierResponse,
    FullResponse,
    GeneratedItem,
    GeneratedResponse,
    RelationItem,
    RelationResponse,
    UsedItem,
    UsedResponse,
)
from ._serializers import entity_version_dict as _entity_version_dict
from ._typed_doc import (
    build_activity_description as _build_activity_description,
    format_entity_schemas_for_doc as _format_entity_schemas_for_doc,
)
from .access import check_dossier_access, get_visibility_from_entry


def register_routes(
    app: FastAPI,
    registry: PluginRegistry,
    get_user,
    global_access: list[dict] | None = None,
) -> None:
    """Register every HTTP route the dossier API exposes.

    Walks each leaf module's `register` function in turn, plus the
    per-plugin search-route factory loop. Called once at app startup.
    """
    _activities_routes.register(
        app,
        registry=registry,
        get_user=get_user,
        global_access=global_access,
    )
    _dossiers_routes.register(
        app,
        registry=registry,
        get_user=get_user,
        global_access=global_access,
    )
    _entities_routes.register(
        app,
        get_user=get_user,
        global_access=global_access,
    )
    _files_routes.register(app, get_user=get_user)

    # Per-plugin search route factories (Elasticsearch-backed search
    # endpoints declared by individual workflow plugins).
    for plugin in registry.all_plugins():
        if plugin.search_route_factory:
            plugin.search_route_factory(app, get_user)
