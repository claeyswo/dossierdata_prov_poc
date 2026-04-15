"""
App factory.

Creates the FastAPI app, loads plugins, registers routes.
5-line main.py calls this.
"""

from __future__ import annotations

import copy
import importlib
import logging
import os
import subprocess
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .plugin import PluginRegistry
from .auth import POCAuthMiddleware, User
from .db import init_db, create_tables, get_session_factory
from .routes import register_routes
from .routes.prov import register_prov_routes

_log = logging.getLogger("dossier.app")


# System user used by the worker and side effects
SYSTEM_USER = User(
    id="system",
    type="systeem",
    name="Systeem",
    roles=["systeemgebruiker"],
    properties={},
)


def load_config_and_registry(config_path: str = "config.yaml") -> tuple[dict, PluginRegistry]:
    """Load config and build plugin registry. Shared by app and worker.

    The `file_service.storage_root` path is resolved against the
    config file's parent directory (not process cwd), so the same
    config works regardless of where uvicorn is launched from. The
    `database.url` is taken verbatim — Postgres URLs are
    location-independent.
    """
    config_path_obj = Path(config_path).resolve()
    config_dir = config_path_obj.parent
    with open(config_path_obj) as f:
        config = yaml.safe_load(f)

    storage_root = config.get("file_service", {}).get("storage_root", "")
    if storage_root.startswith("./"):
        abs_storage = (config_dir / storage_root[2:]).resolve()
        config.setdefault("file_service", {})["storage_root"] = str(abs_storage)

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

    # --- CORS ---
    # Allow all origins in development. In production, restrict to
    # the frontend's actual origin(s) via config:
    #   cors:
    #     allowed_origins: ["https://app.example.be"]
    cors_config = config.get("cors", {})
    allowed_origins = cors_config.get("allowed_origins", ["*"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Health check ---
    @app.get("/health", tags=["system"])
    async def health():
        """Liveness probe. Returns 200 if the process is up.

        For a readiness probe that checks the DB connection, use
        /health/ready (below)."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["system"])
    async def health_ready():
        """Readiness probe. Returns 200 if the DB connection works."""
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                from sqlalchemy import text
                await session.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(503, detail=f"Database not ready: {e}")

    # --- Startup: DB init + Alembic migrations ---
    @app.on_event("startup")
    async def startup():
        db_url = config.get("database", {}).get("url")
        if not db_url:
            raise RuntimeError(
                "database.url is required in config (Postgres connection string)"
            )
        await init_db(db_url)

        # Run Alembic migrations to HEAD via subprocess.
        # Subprocess is needed because alembic's env.py calls
        # asyncio.run() internally, which can't nest inside
        # uvicorn's already-running event loop.
        alembic_ini = Path(__file__).parent.parent / "alembic.ini"
        if alembic_ini.exists():
            env = {**os.environ, "DOSSIER_DB_URL": db_url}
            result = subprocess.run(
                ["python3", "-m", "alembic", "upgrade", "head"],
                cwd=str(alembic_ini.parent),
                capture_output=True, text=True, env=env,
            )
            if result.returncode == 0:
                _log.info("Alembic migrations applied successfully")
            else:
                _log.warning(
                    f"Alembic migration failed (rc={result.returncode}), "
                    f"falling back to create_tables: {result.stderr}"
                )
                await create_tables()
        else:
            await create_tables()

    # Register routes
    global_access = config.get("global_access", [])
    register_routes(app, registry, auth_middleware, global_access)
    register_prov_routes(app, registry, auth_middleware, global_access)

    return app
