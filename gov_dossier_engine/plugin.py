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
            model: "gov_dossier_toelatingen.entities.Aanvraag"  # default/unversioned
            schemas:                                            # optional
              v1: "gov_dossier_toelatingen.entities.Aanvraag"
              v2: "gov_dossier_toelatingen.entities.AanvraagV2"

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

    # Called after each activity completes (inside the transaction).
    # Signature: async def hook(repo, dossier_id, activity_type, status, entities) -> None
    # Use to update Elasticsearch indices.
    post_activity_hook: Callable | None = None

    # Called during route registration. Receives (app, get_user) and should
    # register workflow-specific search endpoints like /dossiers/{workflow_name}/...
    search_route_factory: Callable | None = None

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


class PluginRegistry:
    """Registry of all loaded plugins."""

    def __init__(self):
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin):
        self._plugins[plugin.name] = plugin

    def get(self, workflow_name: str) -> Plugin | None:
        return self._plugins.get(workflow_name)

    def get_for_activity(self, activity_type: str) -> tuple[Plugin, dict] | None:
        """Find which plugin owns an activity type. Returns (plugin, activity_def)."""
        for plugin in self._plugins.values():
            for act in plugin.workflow.get("activities", []):
                if act["name"] == activity_type:
                    return plugin, act
        return None

    def all_plugins(self) -> list[Plugin]:
        return list(self._plugins.values())

    def all_workflow_names(self) -> list[str]:
        return list(self._plugins.keys())
