"""
App factory.

Creates the FastAPI app, loads plugins, registers routes.
5-line main.py calls this.
"""

from __future__ import annotations

import copy
import importlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .plugin import PluginRegistry
from .auth import POCAuthMiddleware, User, SYSTEM_USER
from .db import init_db, get_session_factory
from .routes import register_routes
from .routes.prov import register_prov_routes

_log = logging.getLogger("dossier.app")


# Factored to ``dossier_engine.db.alembic`` in Round 34 so this file
# can stay focused on FastAPI wiring. Re-exported from ``dossier_engine.app``
# for back-compat — existing call sites (and tests that do
# ``app_module._run_alembic_migrations(...)``) continue to work.
from .db.alembic import _run_alembic_migrations  # noqa: F401,E402


# System user used by the worker and side effects.
# Canonical definition lives in ``dossier_engine.auth``; re-imported
# above so existing ``from dossier_engine.app import SYSTEM_USER``
# callers keep working.


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


def _validate_plugin_prefixes(plugin, ns_registry) -> None:
    """Walk a plugin's workflow YAML and verify every qualified type
    name uses a declared prefix.

    Covers: entity types, workflow-level relation types, activity-level
    relation types, and activity `generates`/`used`/`tombstones`
    declarations. Raises ValueError on first unknown prefix with a
    clear path to the offending declaration.
    """
    wf = plugin.workflow

    # Activity name validation — ``name`` may be bare or qualified.
    # Qualified forms must use a declared prefix.
    for act in wf.get("activities", []) or []:
        if isinstance(act, dict):
            name = act.get("name", "")
            if name and ":" in name:
                try:
                    ns_registry.validate_type(name)
                except ValueError as e:
                    raise ValueError(
                        f"In plugin '{plugin.name}', activity name {name!r}: {e}"
                    ) from None

    # Entity type declarations
    for et in wf.get("entity_types", []) or []:
        t = et.get("type") if isinstance(et, dict) else et
        if t and isinstance(t, str):
            try:
                ns_registry.validate_type(t)
            except ValueError as e:
                raise ValueError(
                    f"In plugin '{plugin.name}', entity_types[...]: {e}"
                ) from None

    # Workflow-level relation declarations
    for rel in wf.get("relations", []) or []:
        if isinstance(rel, dict):
            t = rel.get("type")
            if t:
                try:
                    ns_registry.validate_type(t)
                except ValueError as e:
                    raise ValueError(
                        f"In plugin '{plugin.name}', relations[...]: {e}"
                    ) from None

    # Activity-level declarations
    for act in wf.get("activities", []) or []:
        if not isinstance(act, dict):
            continue
        act_name = act.get("name", "?")
        # generates
        for gen in act.get("generates", []) or []:
            if isinstance(gen, str):
                try:
                    ns_registry.validate_type(gen)
                except ValueError as e:
                    raise ValueError(
                        f"In plugin '{plugin.name}', activity '{act_name}' "
                        f"generates[...]: {e}"
                    ) from None
        # used
        for used in act.get("used", []) or []:
            t = used.get("type") if isinstance(used, dict) else used
            if t and isinstance(t, str):
                try:
                    ns_registry.validate_type(t)
                except ValueError as e:
                    raise ValueError(
                        f"In plugin '{plugin.name}', activity '{act_name}' "
                        f"used[...]: {e}"
                    ) from None
        # activity-level relations
        for rel in act.get("relations", []) or []:
            if isinstance(rel, dict):
                t = rel.get("type")
                if t:
                    try:
                        ns_registry.validate_type(t)
                    except ValueError as e:
                        raise ValueError(
                            f"In plugin '{plugin.name}', activity '{act_name}' "
                            f"relations[...]: {e}"
                        ) from None


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config, registry = load_config_and_registry(config_path)

    # Configure the IRI namespace used for PROV entity/activity/agent
    # IRIs and for classify_ref(). Must happen before any route that
    # generates or parses IRIs is registered.
    iri_base = config.get("iri_base", {})
    if iri_base:
        from .prov.iris import configure_iri_base
        configure_iri_base(
            dossier_prefix=iri_base.get("dossier", "https://id.erfgoed.net/dossiers/"),
            ontology_ns=iri_base.get("ontology", "https://id.erfgoed.net/vocab/ontology#"),
        )

    # Build the namespace registry. Seeded with built-in RDF/PROV
    # prefixes; app-level `namespaces:` in config.yaml adds globals;
    # each plugin can add its own workflow-specific prefixes.
    from .prov.namespaces import NamespaceRegistry, set_namespaces
    ns_registry = NamespaceRegistry()

    # The workflow ontology prefix comes from `iri_base.ontology`.
    # Register it as the plugin's default prefix, name it "oe" by
    # default (overrideable via `iri_base.ontology_prefix`).
    default_prefix = iri_base.get("ontology_prefix", "oe")
    default_iri = iri_base.get("ontology", "https://id.erfgoed.net/vocab/ontology#")
    ns_registry.register(default_prefix, default_iri)
    ns_registry.default_workflow_prefix = default_prefix

    # App-level shared namespaces (FOAF, Dublin Core, etc.).
    for prefix, iri in (config.get("namespaces") or {}).items():
        ns_registry.register(prefix, iri)

    # Per-plugin namespaces declared in each workflow.yaml.
    for plugin in registry.all_plugins():
        for prefix, iri in (plugin.workflow.get("namespaces") or {}).items():
            ns_registry.register(prefix, iri)

    # Validate every entity type / relation type referenced by every
    # plugin. Fails fast at startup on typo'd or undeclared prefixes.
    # (Activity names themselves are normalized to qualified form
    # inside PluginRegistry.register — see plugin/.)
    for plugin in registry.all_plugins():
        _validate_plugin_prefixes(plugin, ns_registry)

    # Bug 78 (Round 26): relation-type contract validation. Enforces
    # the "types declared once at workflow level with kind; activities
    # reference by name only" model. Two passes:
    #   1. Shape-check the workflow + activity YAML. Runs on the raw
    #      workflow dict — no Plugin object needed.
    #   2. Cross-check plugin.relation_validators dict keys against
    #      declared relation type names (reject Style-3 collisions).
    from .plugin import (
        validate_relation_declarations,
        validate_relation_validator_registrations,
        validate_deadline_rules,
    )
    for plugin in registry.all_plugins():
        validate_relation_declarations(plugin.workflow)
        validate_relation_validator_registrations(plugin)
        validate_deadline_rules(plugin.workflow)

    set_namespaces(ns_registry)

    # Bug 13 (Round 33): lifespan replaces the deprecated
    # ``@app.on_event("startup")`` / ``@app.on_event("shutdown")``
    # pattern. Single async-context-manager function — startup runs
    # before ``yield``, shutdown after. The execution order is
    # unchanged from the old pair of handlers (FastAPI calls lifespan
    # once the ASGI server is ready); this is purely a deprecation
    # migration, not a timing refactor. ``config`` is captured by
    # closure, same as the old handlers did.
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # --- Startup: audit logging → DB init → Alembic ---
        # Audit logging wires up first — errors emitted during DB init
        # or migrations may themselves need to go through audit.
        # Safe no-op if the audit log path isn't writable (dev/test).
        # Reads `audit.log_path` from config, falling back to the
        # `DOSSIER_AUDIT_LOG_PATH` env var, then to the module default.
        #
        # `or {}` handles the case where the `audit:` key is present in
        # config.yaml but has no non-commented children — YAML parses
        # `audit:` with only commented lines under it as `None`, not as
        # an empty dict, so `config.get("audit", {})` returns `None`
        # and a subsequent `.get()` call would raise AttributeError.
        from .observability.audit import configure_audit_logging
        audit_config = config.get("audit") or {}
        configure_audit_logging(
            path=audit_config.get("log_path"),
            max_bytes=audit_config.get("max_bytes", 100 * 1024 * 1024),
            backup_count=audit_config.get("backup_count", 10),
        )

        db_url = config.get("database", {}).get("url")
        if not db_url:
            raise RuntimeError(
                "database.url is required in config (Postgres connection string)"
            )
        await init_db(db_url)

        # Migrations. Runs to HEAD, or raises RuntimeError — no
        # silent fallback. See ``_run_alembic_migrations`` for the
        # fail-fast rationale.
        _run_alembic_migrations(db_url)

        yield

        # --- Shutdown: close the search client ---
        from .search import close_client
        await close_client()

    app = FastAPI(
        title="Dossier API",
        description="PROV-gebaseerde dossierafhandeling",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Sentry before CORS — we want Sentry to see the full request
    # lifecycle, including any CORS preflight handling, not just the
    # subset that survives the CORS filter. No-op if sentry_sdk isn't
    # installed or SENTRY_DSN isn't set, so dev and test environments
    # run unchanged. The FastAPI integration instruments via the ASGI
    # middleware stack internally; no explicit add_middleware() call
    # needed here.
    from .observability.sentry import init_sentry_fastapi
    init_sentry_fastapi(app)

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
        "uri": SYSTEM_USER.uri,
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

    # --- Startup + shutdown ---
    # Wired via the ``lifespan`` context manager passed to
    # ``FastAPI(lifespan=...)`` above. The two ``@app.on_event(...)``
    # handlers that used to live here were deprecated by FastAPI 0.93
    # and removed in Round 33 (Bug 13). See the ``lifespan`` function
    # earlier in ``create_app`` for the startup and shutdown code.

    # Register routes
    global_access = config.get("global_access", [])
    global_audit_access = config.get("global_audit_access", [])
    global_admin_access = config.get("global_admin_access", [])

    # Make global_access and global_admin_access available to the
    # search module so indexers include global roles in __acl__ and
    # plugin admin endpoints can gate on admin roles without needing
    # to have them plumbed through the factory signature.
    from .search import configure_global_access, configure_global_admin_access
    configure_global_access(global_access)
    configure_global_admin_access(global_admin_access)

    register_routes(app, registry, auth_middleware, global_access)
    register_prov_routes(
        app, registry, auth_middleware, global_access, global_audit_access
    )

    # Admin search routes (common index only; plugins register their
    # own workflow-specific admin endpoints via search_route_factory).
    from .routes.admin_search import register_admin_search_routes
    register_admin_search_routes(
        app, registry, auth_middleware, global_admin_access,
    )

    return app
