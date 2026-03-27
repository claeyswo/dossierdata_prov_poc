"""
App factory.

Creates the FastAPI app, loads plugins, registers routes.
5-line main.py calls this.
"""

from __future__ import annotations

import importlib
import yaml
from fastapi import FastAPI

from .plugin import PluginRegistry
from .auth import POCAuthMiddleware
from .db import init_db, create_tables, get_session_factory
from .routes import register_routes
from .routes.prov import register_prov_routes


def create_app(config_path: str = "config.yaml") -> FastAPI:
    with open(config_path) as f:
        config = yaml.safe_load(f)

    app = FastAPI(
        title="Dossier API",
        description="PROV-gebaseerde dossierafhandeling",
        version="0.1.0",
    )

    registry = PluginRegistry()

    # Load plugins
    for plugin_module_name in config.get("plugins", []):
        module = importlib.import_module(plugin_module_name)
        plugin = module.create_plugin()
        registry.register(plugin)

    # Collect all POC users from all plugins
    all_poc_users = []
    for plugin in registry.all_plugins():
        all_poc_users.extend(plugin.workflow.get("poc_users", []))

    # Add system user
    all_poc_users.append({
        "id": "system",
        "username": "system",
        "type": "systeem",
        "name": "Systeem",
        "roles": ["systeemgebruiker"],
        "properties": {},
    })

    auth_middleware = POCAuthMiddleware(all_poc_users)

    app.state.registry = registry
    app.state.config = config

    @app.on_event("startup")
    async def startup():
        db_url = config.get("database", {}).get("url", "sqlite+aiosqlite:///./dossiers.db")
        await init_db(db_url)
        await create_tables()

    # Register routes
    register_routes(app, registry, auth_middleware)
    register_prov_routes(app, registry, auth_middleware)

    return app
