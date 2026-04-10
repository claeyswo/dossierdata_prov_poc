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

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from pydantic import BaseModel


@dataclass
class Plugin:
    """A workflow plugin registration."""

    name: str  # workflow name, e.g. "toelatingen"
    workflow: dict  # parsed workflow YAML
    entity_models: dict[str, type[BaseModel]]  # entity_type_name → Pydantic model
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
