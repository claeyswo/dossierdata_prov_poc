"""
Plugin interface — the contract between the engine and workflow
plugins. See ``docs/plugin_guidebook.md`` and
``dossiertype_template.md`` for authoring material.

Each workflow plugin provides:
- workflow definition (YAML)
- entity Pydantic models
- handler functions
- validator functions
- task handlers
- post_activity_hook (optional): called after each activity to update search indices
- search_route_factory (optional): registers a workflow-specific search endpoint

Layout (Round 34 split):
    plugin/
    ├── __init__.py       — re-exports the public surface
    ├── model.py          — Plugin, PluginRegistry, FieldValidator dataclasses
    ├── registries.py     — build_entity_registries_from_workflow,
    │                       build_callable_registries_from_workflow,
    │                       _import_dotted[_callable]
    ├── validators.py     — 5 load-time validators + their constants
    └── normalize.py      — _normalize_plugin_activity_names (auto-qualify)
"""
from .model import FieldValidator, Plugin, PluginRegistry
from .registries import (
    _import_dotted,
    _import_dotted_callable,
    build_entity_registries_from_workflow,
    build_callable_registries_from_workflow,
)
from .validators import (
    validate_workflow_version_references,
    validate_side_effect_condition_fn_registrations,
    validate_side_effect_conditions,
    validate_relation_declarations,
    validate_relation_validator_registrations,
    validate_deadline_rules,
)
from .normalize import _normalize_plugin_activity_names

__all__ = [
    # dataclasses
    "FieldValidator",
    "Plugin",
    "PluginRegistry",
    # registry-building
    "build_entity_registries_from_workflow",
    "build_callable_registries_from_workflow",
    # validators
    "validate_workflow_version_references",
    "validate_side_effect_condition_fn_registrations",
    "validate_side_effect_conditions",
    "validate_relation_declarations",
    "validate_relation_validator_registrations",
    "validate_deadline_rules",
    # private helpers that tests/other modules import directly
    "_import_dotted",
    "_import_dotted_callable",
    "_normalize_plugin_activity_names",
]
