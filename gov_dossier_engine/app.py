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
from .auth import POCAuthMiddleware, User
from .db import init_db, create_tables, get_session_factory
from .routes import register_routes
from .routes.prov import register_prov_routes


# System user used by the worker and side effects
SYSTEM_USER = User(
    id="system",
    type="systeem",
    name="Systeem",
    roles=["systeemgebruiker"],
    properties={},
)


def load_config_and_registry(config_path: str = "config.yaml") -> tuple[dict, PluginRegistry]:
    """Load config and build plugin registry. Shared by app and worker."""
    with open(config_path) as f:
        config = yaml.safe_load(f)

    registry = PluginRegistry()

    from .entities import TaskEntity, SystemNote, SYSTEM_ACTION_DEF, TOMBSTONE_ACTIVITY_DEF

    for plugin_module_name in config.get("plugins", []):
        module = importlib.import_module(plugin_module_name)
        plugin = module.create_plugin()
        plugin.entity_models["system:task"] = TaskEntity
        plugin.entity_models["system:note"] = SystemNote
        plugin.workflow.setdefault("activities", []).append(SYSTEM_ACTION_DEF)

        # Built-in tombstone activity. Per-workflow allowed_roles are read
        # from the YAML's top-level `tombstone:` block — absent means the
        # role list stays empty and no one can tombstone in this workflow
        # (deny by default).
        import copy
        ts_def = copy.deepcopy(TOMBSTONE_ACTIVITY_DEF)
        ts_cfg = plugin.workflow.get("tombstone") or {}
        ts_roles = ts_cfg.get("allowed_roles") or []
        if ts_roles:
            ts_def["allowed_roles"] = list(ts_roles)
            ts_def["authorization"]["roles"] = [{"role": r} for r in ts_roles]
            ts_def["default_role"] = ts_roles[0]
        plugin.workflow["activities"].append(ts_def)

        registry.register(plugin)

    return config, registry


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config, registry = load_config_and_registry(config_path)

    app = FastAPI(
        title="Dossier API",
        description="PROV-gebaseerde dossierafhandeling",
        version="0.1.0",
    )

    # Collect all POC users from all plugins
    all_poc_users = []
    for plugin in registry.all_plugins():
        all_poc_users.extend(plugin.workflow.get("poc_users", []))

    # Add system user
    all_poc_users.append({
        "id": SYSTEM_USER.id,
        "username": "system",
        "type": SYSTEM_USER.type,
        "name": SYSTEM_USER.name,
        "roles": SYSTEM_USER.roles,
        "properties": SYSTEM_USER.properties,
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
    global_access = config.get("global_access", [])
    register_routes(app, registry, auth_middleware, global_access)
    register_prov_routes(app, registry, auth_middleware, global_access)

    return app
