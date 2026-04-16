"""
Unit tests for `engine.refs` and `plugin.Plugin` / `PluginRegistry`.

Both modules are pure in-process logic — no DB, no async —
so they live under `tests/unit/` rather than `tests/integration/`.

`refs` exposes the two parsers every phase that handles an entity
reference depends on:

* `parse_entity_ref(ref)` — returns a dict with `prefix`, `id`,
  `version` on a canonical match, or None on anything else.
* `is_external_uri(ref)` — True iff `parse_entity_ref` returns None.

The `Plugin` class exposes the lookups every part of the engine
uses to make decisions:

* `cardinality_of(entity_type)` — "single" or "multiple", checking
  the workflow config first, then engine defaults, then falling
  back to "single".
* `is_singleton(entity_type)` — cardinality == "single".
* `resolve_schema(entity_type, schema_version)` — looks up a
  Pydantic model via `entity_schemas` (versioned), falling back
  to `entity_models` (legacy).
* `find_activity_def(activity_type)` — linear scan of workflow
  activities.

And `PluginRegistry` exposes the cross-plugin lookups:

* `register` / `get` — basic registration and name lookup.
* `get_for_activity` — reverse lookup by activity name across
  all registered plugins.
* `all_plugins` / `all_workflow_names` — introspection.

Every single function here is tested for every branch. No DB,
no async, no fixtures beyond the test itself.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import BaseModel

from dossier_engine.engine.refs import EntityRef, is_external_uri
from dossier_engine.plugin import Plugin, PluginRegistry


# --------------------------------------------------------------------
# EntityRef.parse / is_external_uri
# --------------------------------------------------------------------


class TestEntityRefParse:
    """Parsing the canonical `prefix:type/entity_id@version_id` form
    into a typed EntityRef. Non-canonical inputs return None — callers
    treat that as 'external URI or not a ref'."""

    def test_canonical_ref_parses(self):
        """A well-formed `prefix:type/uuid@uuid` parses into an
        EntityRef with the three typed fields populated."""
        ref = (
            "oe:aanvraag/"
            "e1000000-0000-0000-0000-000000000001@"
            "f1000000-0000-0000-0000-000000000001"
        )
        result = EntityRef.parse(ref)
        assert result is not None
        assert result.type == "oe:aanvraag"
        assert result.entity_id == UUID("e1000000-0000-0000-0000-000000000001")
        assert result.version_id == UUID("f1000000-0000-0000-0000-000000000001")

    def test_system_prefix_parses(self):
        """Prefixes with underscores in the type segment (like
        `system:task`) parse successfully. The regex allows
        `[a-z_]+:[a-z_]+`."""
        eid = UUID("11111111-1111-1111-1111-111111111111")
        vid = UUID("22222222-2222-2222-2222-222222222222")
        ref = f"system:task/{eid}@{vid}"
        result = EntityRef.parse(ref)
        assert result is not None
        assert result.type == "system:task"
        assert result.entity_id == eid
        assert result.version_id == vid

    def test_https_url_returns_none(self):
        """A full URL is not a canonical ref and returns None.
        The caller should treat None as 'external URI'."""
        assert EntityRef.parse("https://example.org/foo") is None

    def test_bare_string_returns_none(self):
        """Something that doesn't even have a type/uuid shape
        just returns None."""
        assert EntityRef.parse("not-a-ref") is None
        assert EntityRef.parse("") is None

    def test_none_input_returns_none(self):
        """Parse accepts None (saves a guard at every call site)."""
        assert EntityRef.parse(None) is None

    def test_uppercase_prefix_rejected(self):
        """The regex is case-sensitive on the prefix: uppercase
        letters don't match, so an uppercased prefix routes to
        external. This is documented current behavior — locking
        it in so a future refactor has to update the test to
        relax the regex."""
        ref = (
            "OE:aanvraag/"
            "e1000000-0000-0000-0000-000000000001@"
            "f1000000-0000-0000-0000-000000000001"
        )
        assert EntityRef.parse(ref) is None

    def test_missing_version_rejected(self):
        """A ref with only prefix/id (no @version) doesn't match
        the canonical form."""
        assert EntityRef.parse(
            "oe:aanvraag/e1000000-0000-0000-0000-000000000001"
        ) is None

    def test_missing_at_separator_rejected(self):
        """`prefix/id-version` without the @ separator doesn't
        match."""
        assert EntityRef.parse(
            "oe:aanvraag/"
            "e1000000-0000-0000-0000-000000000001-"
            "f1000000-0000-0000-0000-000000000001"
        ) is None

    def test_str_roundtrip(self):
        """str(EntityRef(...)) produces the canonical string;
        parsing that string back yields an equal EntityRef. This
        is the invariant that lets us use EntityRef as the single
        source of truth for the wire format."""
        eid = UUID("33333333-3333-3333-3333-333333333333")
        vid = UUID("44444444-4444-4444-4444-444444444444")
        original = EntityRef(type="oe:aanvraag", entity_id=eid, version_id=vid)
        roundtripped = EntityRef.parse(str(original))
        assert roundtripped == original

    def test_entityref_is_hashable(self):
        """Frozen dataclass → hashable → usable as dict key / set
        element. Relied on by invariant checks that dedupe refs."""
        eid = UUID("55555555-5555-5555-5555-555555555555")
        vid = UUID("66666666-6666-6666-6666-666666666666")
        ref = EntityRef(type="oe:aanvraag", entity_id=eid, version_id=vid)
        assert {ref} == {ref}  # no TypeError
        d = {ref: "value"}
        assert d[ref] == "value"


class TestIsExternalUri:

    def test_canonical_ref_is_not_external(self):
        ref = (
            "oe:aanvraag/"
            "e1000000-0000-0000-0000-000000000001@"
            "f1000000-0000-0000-0000-000000000001"
        )
        assert is_external_uri(ref) is False

    def test_url_is_external(self):
        assert is_external_uri("https://example.org/foo") is True
        assert is_external_uri("http://id.erfgoed.net/") is True

    def test_bare_string_is_external(self):
        """Any non-canonical string is treated as external.
        This is the 'assume URI' fallback — documented in
        test_resolve_used.py and test_process_generated.py."""
        assert is_external_uri("not-a-ref") is True
        assert is_external_uri("") is True


# --------------------------------------------------------------------
# Plugin.cardinality_of / is_singleton
# --------------------------------------------------------------------


def _make_plugin(
    workflow: dict | None = None,
    entity_models: dict | None = None,
    entity_schemas: dict | None = None,
) -> Plugin:
    """Construct a minimal Plugin for unit tests."""
    return Plugin(
        name="test",
        workflow=workflow or {},
        entity_models=entity_models or {},
        entity_schemas=entity_schemas or {},
    )


class TestCardinalityOf:

    def test_workflow_declared_single(self):
        """Workflow's `entity_types` block declares a type as
        single → `cardinality_of` returns 'single'."""
        plugin = _make_plugin(workflow={
            "entity_types": [
                {"type": "oe:aanvraag", "cardinality": "single"},
            ],
        })
        assert plugin.cardinality_of("oe:aanvraag") == "single"
        assert plugin.is_singleton("oe:aanvraag") is True

    def test_workflow_declared_multiple(self):
        plugin = _make_plugin(workflow={
            "entity_types": [
                {"type": "oe:bijlage", "cardinality": "multiple"},
            ],
        })
        assert plugin.cardinality_of("oe:bijlage") == "multiple"
        assert plugin.is_singleton("oe:bijlage") is False

    def test_engine_default_for_system_task(self):
        """`system:task` has an engine-level default of 'multiple',
        applied when the workflow doesn't declare it."""
        plugin = _make_plugin()  # empty workflow
        assert plugin.cardinality_of("system:task") == "multiple"
        assert plugin.is_singleton("system:task") is False

    def test_engine_default_for_system_note(self):
        plugin = _make_plugin()
        assert plugin.cardinality_of("system:note") == "multiple"

    def test_engine_default_for_dossier_access(self):
        """`oe:dossier_access` is the engine-level singleton
        default — one access record per dossier."""
        plugin = _make_plugin()
        assert plugin.cardinality_of("oe:dossier_access") == "single"
        assert plugin.is_singleton("oe:dossier_access") is True

    def test_engine_default_for_external(self):
        plugin = _make_plugin()
        assert plugin.cardinality_of("external") == "multiple"

    def test_workflow_overrides_engine_default(self):
        """If the workflow explicitly declares a system type's
        cardinality, that wins over the engine default."""
        plugin = _make_plugin(workflow={
            "entity_types": [
                {"type": "oe:dossier_access", "cardinality": "multiple"},
            ],
        })
        assert plugin.cardinality_of("oe:dossier_access") == "multiple"

    def test_unknown_type_defaults_to_single(self):
        """A type that's neither in workflow.entity_types nor in
        engine defaults defaults to 'single'. This is the lenient
        fallback — plugins that don't bother to declare get the
        safer default."""
        plugin = _make_plugin()
        assert plugin.cardinality_of("oe:made_up") == "single"

    def test_invalid_cardinality_string_defaults_to_single(self):
        """If the workflow has a typo like `cardinality: many`,
        the value is sanitized to 'single' rather than passed
        through unchanged. Lenient fallback again."""
        plugin = _make_plugin(workflow={
            "entity_types": [
                {"type": "oe:x", "cardinality": "many"},  # typo
            ],
        })
        assert plugin.cardinality_of("oe:x") == "single"


# --------------------------------------------------------------------
# Plugin.resolve_schema
# --------------------------------------------------------------------


class _V1(BaseModel):
    a: int


class _V2(BaseModel):
    a: int
    b: int


class TestResolveSchema:

    def test_none_version_uses_entity_models(self):
        """schema_version=None → legacy path, look up in
        `entity_models` directly."""
        plugin = _make_plugin(
            entity_models={"oe:aanvraag": _V1},
        )
        result = plugin.resolve_schema("oe:aanvraag", None)
        assert result is _V1

    def test_versioned_lookup_in_entity_schemas(self):
        """schema_version is set → look up in `entity_schemas`
        keyed by `(type, version)`."""
        plugin = _make_plugin(
            entity_schemas={
                ("oe:aanvraag", "v1"): _V1,
                ("oe:aanvraag", "v2"): _V2,
            },
        )
        assert plugin.resolve_schema("oe:aanvraag", "v1") is _V1
        assert plugin.resolve_schema("oe:aanvraag", "v2") is _V2

    def test_versioned_miss_falls_back_to_entity_models(self):
        """Versioned lookup misses (e.g. the schema_version is
        set but no versioned model is registered for it) → falls
        back to the legacy `entity_models` entry. This keeps the
        legacy path alive when plugins partially adopt
        versioning."""
        plugin = _make_plugin(
            entity_models={"oe:aanvraag": _V1},
            entity_schemas={("oe:beslissing", "v1"): _V2},
        )
        # (type, version) not in entity_schemas → fallback
        result = plugin.resolve_schema("oe:aanvraag", "v99")
        assert result is _V1

    def test_no_model_registered_returns_none(self):
        """Neither entity_schemas nor entity_models has anything
        for this type → None. Callers should treat None as
        'skip content validation'."""
        plugin = _make_plugin()
        assert plugin.resolve_schema("oe:unknown", None) is None
        assert plugin.resolve_schema("oe:unknown", "v1") is None


# --------------------------------------------------------------------
# Plugin.find_activity_def
# --------------------------------------------------------------------


class TestFindActivityDef:

    def test_found_returns_full_def(self):
        plugin = _make_plugin(workflow={
            "activities": [
                {"name": "dienAanvraagIn", "can_create_dossier": True},
                {"name": "bewerkAanvraag"},
            ],
        })
        result = plugin.find_activity_def("dienAanvraagIn")
        assert result is not None
        assert result["name"] == "dienAanvraagIn"
        assert result["can_create_dossier"] is True

    def test_not_found_returns_none(self):
        plugin = _make_plugin(workflow={
            "activities": [{"name": "dienAanvraagIn"}],
        })
        assert plugin.find_activity_def("nonexistent") is None

    def test_empty_workflow_returns_none(self):
        plugin = _make_plugin()
        assert plugin.find_activity_def("anything") is None

    def test_first_match_wins(self):
        """If two activities have the same name (which shouldn't
        happen but isn't prevented), the first one in the list
        wins. Locking in the linear-scan behavior."""
        plugin = _make_plugin(workflow={
            "activities": [
                {"name": "dup", "label": "first"},
                {"name": "dup", "label": "second"},
            ],
        })
        result = plugin.find_activity_def("dup")
        assert result["label"] == "first"


# --------------------------------------------------------------------
# PluginRegistry
# --------------------------------------------------------------------


class TestPluginRegistry:

    def test_register_and_get(self):
        registry = PluginRegistry()
        plugin = _make_plugin(workflow={"name": "toelatingen"})
        plugin.name = "toelatingen"
        registry.register(plugin)
        assert registry.get("toelatingen") is plugin

    def test_get_missing_returns_none(self):
        registry = PluginRegistry()
        assert registry.get("nonexistent") is None

    def test_get_for_activity_finds_owner(self):
        """Reverse lookup: given an activity name, return the
        (plugin, activity_def) pair that owns it. Walks all
        plugins linearly."""
        registry = PluginRegistry()

        p1 = _make_plugin(workflow={
            "activities": [{"name": "submit"}],
        })
        p1.name = "workflow_a"
        p2 = _make_plugin(workflow={
            "activities": [{"name": "review"}, {"name": "approve"}],
        })
        p2.name = "workflow_b"
        registry.register(p1)
        registry.register(p2)

        result = registry.get_for_activity("approve")
        assert result is not None
        plugin, act_def = result
        assert plugin is p2
        assert act_def["name"] == "approve"

    def test_get_for_activity_missing_returns_none(self):
        registry = PluginRegistry()
        p = _make_plugin(workflow={"activities": [{"name": "x"}]})
        p.name = "wf"
        registry.register(p)
        assert registry.get_for_activity("nonexistent") is None

    def test_all_plugins_and_names(self):
        registry = PluginRegistry()
        p1 = _make_plugin()
        p1.name = "a"
        p2 = _make_plugin()
        p2.name = "b"
        registry.register(p1)
        registry.register(p2)

        plugins = registry.all_plugins()
        assert len(plugins) == 2
        assert set(registry.all_workflow_names()) == {"a", "b"}

    def test_register_same_name_overwrites(self):
        """Registering two plugins with the same name results in
        the second one replacing the first. This is how you'd
        hot-reload a plugin in development."""
        registry = PluginRegistry()
        first = _make_plugin()
        first.name = "wf"
        second = _make_plugin()
        second.name = "wf"
        registry.register(first)
        registry.register(second)

        assert registry.get("wf") is second
        assert len(registry.all_plugins()) == 1
