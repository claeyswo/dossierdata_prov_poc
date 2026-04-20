"""
Plugin interface.

Each workflow plugin provides:
- workflow definition (YAML)
- entity Pydantic models
- handler functions
- validator functions
- task handlers
- post_activity_hook (optional): called after each activity to update search indices
- search_route_factory (optional): registers a workflow-specific search endpoint
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from pydantic import BaseModel


def _import_dotted(path: str) -> type[BaseModel]:
    """Resolve a fully-qualified 'pkg.module.ClassName' string to a class.

    Raises ValueError with a clear message on failure — callers should let
    this propagate at plugin load time so misconfiguration fails fast.
    """
    if "." not in path:
        raise ValueError(
            f"Invalid model path {path!r}: must be a fully-qualified "
            f"'package.module.ClassName' string"
        )
    module_path, _, class_name = path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ValueError(
            f"Cannot import module {module_path!r} for model {path!r}: {e}"
        ) from e
    try:
        cls = getattr(module, class_name)
    except AttributeError as e:
        raise ValueError(
            f"Module {module_path!r} has no class {class_name!r} "
            f"(referenced as {path!r})"
        ) from e
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise ValueError(
            f"{path!r} does not resolve to a Pydantic BaseModel subclass"
        )
    return cls


def build_entity_registries_from_workflow(
    workflow: dict,
) -> tuple[dict[str, type[BaseModel]], dict[tuple[str, str], type[BaseModel]]]:
    """Walk the workflow's `entity_types` block and build the plugin's
    `entity_models` and `entity_schemas` registries by resolving dotted
    paths via importlib.

    YAML shape:

        entity_types:
          - type: "oe:aanvraag"
            model: "dossier_toelatingen.entities.Aanvraag"  # default/unversioned
            schemas:                                            # optional
              v1: "dossier_toelatingen.entities.Aanvraag"
              v2: "dossier_toelatingen.entities.AanvraagV2"

    Rules:
      * `model` is optional. If present, it populates `entity_models[type]`
        and serves as the legacy-path default for this type.
      * `schemas` is optional. Each entry populates
        `entity_schemas[(type, version)]`. Types without `schemas` stay
        unversioned and fall back to `model`.
      * Either `model` or `schemas` must be present for a type to contribute
        anything. Types with neither are structural-only (cardinality decl
        only) and are silently skipped here.
      * Paths must resolve via `_import_dotted` or plugin load fails.

    After this function returns, the engine may still inject additional
    models (e.g. `system:task`) into the returned `entity_models` dict —
    that's fine, the dict is plain.
    """
    entity_models: dict[str, type[BaseModel]] = {}
    entity_schemas: dict[tuple[str, str], type[BaseModel]] = {}

    for et in workflow.get("entity_types", []):
        type_name = et.get("type")
        if not type_name:
            continue

        model_path = et.get("model")
        if model_path:
            entity_models[type_name] = _import_dotted(model_path)

        schemas = et.get("schemas") or {}
        for version, path in schemas.items():
            entity_schemas[(type_name, str(version))] = _import_dotted(path)

    return entity_models, entity_schemas


def validate_workflow_version_references(
    workflow: dict,
    entity_schemas: dict[tuple[str, str], type[BaseModel]],
) -> None:
    """Cross-check every `new_version` / `allowed_versions` string on every
    activity against the declared `entity_schemas` registry.

    Fails fast with ValueError at plugin load time if an activity references
    a version that isn't declared. Prevents the silent-runtime-fallback
    footgun where an activity declares `new_version: v3` but the type only
    has `v1` and `v2` registered.
    """
    declared: dict[str, set[str]] = {}
    for (type_name, version) in entity_schemas:
        declared.setdefault(type_name, set()).add(version)

    for act in workflow.get("activities", []):
        entities_cfg = act.get("entities") or {}
        for type_name, ecfg in entities_cfg.items():
            versions_referenced: set[str] = set()
            nv = ecfg.get("new_version")
            if nv:
                versions_referenced.add(str(nv))
            for av in ecfg.get("allowed_versions") or []:
                versions_referenced.add(str(av))

            if not versions_referenced:
                continue

            available = declared.get(type_name, set())
            missing = versions_referenced - available
            if missing:
                raise ValueError(
                    f"Activity {act.get('name')!r} references schema "
                    f"version(s) {sorted(missing)} for entity type "
                    f"{type_name!r}, but the workflow's entity_types "
                    f"block only declares {sorted(available) or 'none'}"
                )


@dataclass
class FieldValidator:
    """A field-level validator with optional request/response models
    for OpenAPI documentation.

    When ``request_model`` and ``response_model`` are provided, the
    engine generates a typed endpoint with proper schema documentation
    in the Swagger UI. Without them, the endpoint accepts/returns
    generic JSON.

    Example::

        FieldValidator(
            fn=validate_erfgoedobject,
            request_model=ErfgoedobjectRequest,
            response_model=ErfgoedobjectResponse,
            summary="Valideer erfgoedobject URI",
            description="Controleer of de URI verwijst naar een gekend erfgoedobject.",
        )
    """
    fn: Callable
    request_model: type[BaseModel] | None = None
    response_model: type[BaseModel] | None = None
    summary: str | None = None
    description: str | None = None


@dataclass
class Plugin:
    """A workflow plugin registration."""

    name: str  # workflow name, e.g. "toelatingen"
    workflow: dict  # parsed workflow YAML
    entity_models: dict[str, type[BaseModel]]  # entity_type_name → Pydantic model (legacy/default)

    # Versioned schemas: (entity_type, schema_version) → Pydantic model.
    # Optional. When an entity row has a non-NULL schema_version, the engine
    # routes lookups through this registry first. NULL schema_version always
    # falls back to entity_models (legacy path). Plugins that don't version
    # anything can leave this empty.
    entity_schemas: dict[tuple[str, str], type[BaseModel]] = field(default_factory=dict)

    handlers: dict[str, Callable] = field(default_factory=dict)  # handler_name → async function
    validators: dict[str, Callable] = field(default_factory=dict)  # validator_name → async function
    task_handlers: dict[str, Callable] = field(default_factory=dict)  # task_name → async function

    # Validators for custom PROV-extension relations (e.g. oe:neemtAkteVan).
    # Keyed by relation type string. Each validator receives the full
    # activity context (resolved used rows, pending generated items, the
    # relation entries of its type) and raises ActivityError to reject the
    # request. Returning normally means "accepted". The engine imposes no
    # semantics on the return value — validators own their own failure
    # conditions and payload shapes. Signature:
    #   async def validator(*, plugin, repo, dossier_id, activity_def,
    #                       entries, used_rows_by_ref, generated_items) -> None
    relation_validators: dict[str, Callable] = field(default_factory=dict)

    # Lightweight field-level validators callable between activities
    # via POST /{workflow}/validate/{name}. Each entry is either a
    # bare async callable (legacy) or a FieldValidator with request/
    # response models for OpenAPI documentation.
    field_validators: dict[str, "Callable | FieldValidator"] = field(default_factory=dict)

    # Called after each activity completes (inside the transaction).
    # Signature: async def hook(repo, dossier_id, activity_type, status, entities) -> None
    # Use to update Elasticsearch indices.
    post_activity_hook: Callable | None = None

    # Called after persistence but BEFORE the cached_status / eligible_activities
    # projection and BEFORE transaction commit. Unlike post_activity_hook,
    # exceptions raised here are NOT swallowed — they propagate and roll the
    # whole activity back. Use for synchronous validation / side effects that
    # MUST succeed or the activity should be rejected: PKI signature checks,
    # external ID reservations, mandatory file service operations, etc.
    #
    # Signature:
    #   async def hook(*, repo, dossier_id, plugin, activity_def,
    #                     generated_items, used_rows, user) -> None
    #
    # Hooks run in declaration order. First raise wins — subsequent hooks
    # don't run. Raise ActivityError for structured HTTP responses; any other
    # exception becomes a 500.
    pre_commit_hooks: list[Callable] = field(default_factory=list)

    # Called during route registration. Receives (app, get_user) and should
    # register workflow-specific search endpoints like /dossiers/{workflow_name}/...
    search_route_factory: Callable | None = None

    # Workflow-scoped constants/config. A Pydantic BaseSettings instance
    # populated at plugin load from (in precedence order, highest wins):
    #   1. Environment variables — operator escape hatch, secrets
    #   2. workflow.yaml's `constants.values` block — plugin author's
    #      domain-level tuning
    #   3. Pydantic class defaults
    # Handlers access this via context.constants; hooks and factories
    # access via plugin.constants. None if the plugin doesn't declare
    # a constants class.
    constants: Any = None

    # Defaults for engine-provided types. system:task and system:note are
    # multi-cardinality (many per dossier); oe:dossier_access is a singleton.
    # These are overlaid by plugin workflow declarations if present.
    _ENGINE_CARDINALITIES: dict = field(
        default_factory=lambda: {
            "system:task": "multiple",
            "system:note": "multiple",
            "oe:dossier_access": "single",
            "external": "multiple",
        },
        repr=False,
    )

    def cardinality_of(self, entity_type: str) -> str:
        """Return the declared cardinality of an entity type: 'single' or
        'multiple'. Checks the workflow's `entity_types` block first, then
        falls back to engine defaults for system/external types, then
        defaults to 'single' for anything unknown."""
        for et in self.workflow.get("entity_types", []):
            if et.get("type") == entity_type:
                c = et.get("cardinality", "single")
                return c if c in ("single", "multiple") else "single"
        return self._ENGINE_CARDINALITIES.get(entity_type, "single")

    def is_singleton(self, entity_type: str) -> bool:
        return self.cardinality_of(entity_type) == "single"

    def resolve_schema(
        self, entity_type: str, schema_version: str | None
    ) -> type[BaseModel] | None:
        """Resolve the Pydantic model class for an entity of a given type
        and schema version.

        Resolution rules:
        - If `schema_version` is set, look it up in `entity_schemas`. If not
          found there, fall back to `entity_models[entity_type]` — this keeps
          the legacy path available when a plugin introduces versioning for
          some types but not others.
        - If `schema_version` is None (legacy/unversioned row, or a plugin
          that doesn't version this type), use `entity_models[entity_type]`.
        - Returns None if nothing matches, in which case callers should skip
          content validation / typed access.
        """
        if schema_version is not None:
            model = self.entity_schemas.get((entity_type, schema_version))
            if model is not None:
                return model
        return self.entity_models.get(entity_type)

    def find_activity_def(self, activity_type: str) -> dict | None:
        """Return the activity definition dict for `activity_type`, or
        None if this plugin's workflow doesn't declare it.

        Accepts bare or qualified input. Compares by the *local
        name* portion since the stored YAML may have been registered
        via ``PluginRegistry.register`` (which qualifies everything
        to ``oe:``) or may still be bare if the plugin wasn't
        registered (test fixtures constructing Plugin directly).

        A linear scan — workflows have a few dozen activities at
        most, so the cost is negligible compared to caching.
        """
        from .activity_names import local_name
        target_local = local_name(activity_type)
        for act in self.workflow.get("activities", []):
            if local_name(act.get("name", "")) == target_local:
                return act
        return None


class PluginRegistry:
    """Registry of all loaded plugins."""

    def __init__(self):
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin):
        """Register a plugin.

        Normalizes all activity names to qualified form (``oe:foo``
        instead of bare ``foo``). This runs on every registration
        path — ``create_app`` and direct test fixtures — so the rest
        of the engine always sees consistent qualified names.
        """
        _normalize_plugin_activity_names(plugin)
        self._plugins[plugin.name] = plugin

    def get(self, workflow_name: str) -> Plugin | None:
        return self._plugins.get(workflow_name)

    def get_for_activity(self, activity_type: str) -> tuple[Plugin, dict] | None:
        """Find which plugin owns an activity type. Returns (plugin, activity_def).

        Accepts both bare (``submit``) and qualified (``oe:submit``)
        forms — bare names are qualified to the default prefix first.
        The registry stores activities with qualified names, so the
        lookup always compares qualified-to-qualified.
        """
        from .activity_names import qualify
        qualified = qualify(activity_type)
        for plugin in self._plugins.values():
            for act in plugin.workflow.get("activities", []):
                if act["name"] == qualified:
                    return plugin, act
        return None

    def all_plugins(self) -> list[Plugin]:
        return list(self._plugins.values())

    def all_workflow_names(self) -> list[str]:
        return list(self._plugins.keys())


def _normalize_plugin_activity_names(plugin: Plugin) -> None:
    """Normalize activity names to qualified form in-place.

    Qualifies bare activity names (``dienAanvraagIn``) to the
    workflow's default prefix (``oe:dienAanvraagIn``). Also
    qualifies cross-references in ``requirements``, ``forbidden``,
    ``side_effects``, ``tasks.cancel_if_activities``, and
    ``tasks.target_activity``.

    Called from ``PluginRegistry.register``, so it runs for every
    plugin load regardless of entry point. Idempotent — running it
    twice is a no-op.

    The default prefix comes from the namespace registry if
    configured; otherwise falls back to ``oe``. In test fixtures
    that skip ``create_app``, this fallback is correct for the
    current toelatingen workflow.
    """
    from .activity_names import qualify

    # Default prefix: registry if configured, else "oe".
    try:
        from .namespaces import namespaces
        default_prefix = namespaces().default_workflow_prefix
    except (RuntimeError, ImportError):
        default_prefix = "oe"

    wf = plugin.workflow
    for act in wf.get("activities", []) or []:
        if not isinstance(act, dict):
            continue
        name = act.get("name")
        if name and ":" not in name:
            act["name"] = qualify(name, default_prefix)

        # `requirements` and `forbidden` are dicts with sub-keys
        # `activities`, `statuses`, `entities`. Only the `activities`
        # list contains cross-references to other activity names.
        for field_key in ("requirements", "forbidden"):
            block = act.get(field_key)
            if isinstance(block, dict):
                act_refs = block.get("activities") or []
                if isinstance(act_refs, list):
                    block["activities"] = [
                        qualify(r, default_prefix) if isinstance(r, str) else r
                        for r in act_refs
                    ]

        # `side_effects` is a list of activity names — cross-references
        # fired after the current activity succeeds.
        side = act.get("side_effects") or []
        if isinstance(side, list):
            act["side_effects"] = [
                qualify(r, default_prefix) if isinstance(r, str) else r
                for r in side
            ]

        # Tasks can reference cancel_if_activities by name
        for task in act.get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            cancel = task.get("cancel_if_activities") or []
            if isinstance(cancel, list):
                task["cancel_if_activities"] = [
                    qualify(r, default_prefix) if isinstance(r, str) else r
                    for r in cancel
                ]
            target = task.get("target_activity")
            if isinstance(target, str) and ":" not in target:
                task["target_activity"] = qualify(target, default_prefix)
