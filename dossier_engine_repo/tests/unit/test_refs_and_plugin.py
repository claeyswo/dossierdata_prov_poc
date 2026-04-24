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

    def test_uppercase_prefix_accepted(self):
        """The regex accepts mixed-case prefixes and local names to
        support standard RDF vocabularies (foaf:Person,
        dcterms:BibliographicResource, etc.). Only a leading-digit or
        structurally malformed prefix is rejected."""
        ref = (
            "FOAF:Person/"
            "e1000000-0000-0000-0000-000000000001@"
            "f1000000-0000-0000-0000-000000000001"
        )
        parsed = EntityRef.parse(ref)
        assert parsed is not None
        assert parsed.type == "FOAF:Person"

    def test_leading_digit_prefix_rejected(self):
        """Prefixes must start with a letter per RDF QName rules."""
        ref = (
            "1invalid:thing/"
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
        # After registration, activity names are normalized to the
        # qualified form (default prefix `oe:`).
        assert act_def["name"] == "oe:approve"

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


class TestSideEffectConditionValidation:
    """``validate_side_effect_conditions`` fails fast on malformed
    condition blocks at plugin load time. Prevents the silent-fail
    footgun where a ``from_entity:`` typo (borrowed from the status
    rule or auth scope shape) would block every invocation of the
    side effect at runtime with no warning.
    """

    def test_valid_condition_accepted(self):
        from dossier_engine.plugin import validate_side_effect_conditions

        # Valid shape passes silently.
        validate_side_effect_conditions({
            "activities": [{
                "name": "dienAanvraagIn",
                "side_effects": [{
                    "activity": "sendNotification",
                    "condition": {
                        "entity_type": "oe:beslissing",
                        "field": "content.beslissing",
                        "value": "goedgekeurd",
                    },
                }],
            }],
        })

    def test_no_condition_ignored(self):
        from dossier_engine.plugin import validate_side_effect_conditions

        # Side effect without a condition is fine.
        validate_side_effect_conditions({
            "activities": [{
                "name": "a",
                "side_effects": [{"activity": "b"}],
            }],
        })

    def test_from_entity_typo_rejected(self):
        """The classic mistake: using ``from_entity`` (the status /
        auth key) on a side-effect condition. Must fail loudly, not
        silently block the side effect at runtime."""
        from dossier_engine.plugin import validate_side_effect_conditions

        with pytest.raises(ValueError) as exc_info:
            validate_side_effect_conditions({
                "activities": [{
                    "name": "dienAanvraagIn",
                    "side_effects": [{
                        "activity": "sendNotification",
                        "condition": {
                            "from_entity": "oe:beslissing",
                            "field": "content.beslissing",
                            "value": "goedgekeurd",
                        },
                    }],
                }],
            })
        msg = str(exc_info.value)
        assert "dienAanvraagIn" in msg
        assert "sendNotification" in msg
        assert "from_entity" in msg
        assert "entity_type" in msg

    def test_missing_keys_rejected(self):
        from dossier_engine.plugin import validate_side_effect_conditions

        with pytest.raises(ValueError) as exc_info:
            validate_side_effect_conditions({
                "activities": [{
                    "name": "a",
                    "side_effects": [{
                        "activity": "b",
                        "condition": {"entity_type": "oe:x"},
                    }],
                }],
            })
        msg = str(exc_info.value)
        assert "missing keys" in msg
        assert "field" in msg and "value" in msg

    def test_non_dict_condition_rejected(self):
        """YAML-mistake case: someone wrote ``condition: some_string``
        instead of a dict. Fail with a clear type message."""
        from dossier_engine.plugin import validate_side_effect_conditions

        with pytest.raises(ValueError) as exc_info:
            validate_side_effect_conditions({
                "activities": [{
                    "name": "a",
                    "side_effects": [{
                        "activity": "b",
                        "condition": "goedgekeurd",  # bogus
                    }],
                }],
            })
        assert "must be a dict" in str(exc_info.value)

    def test_legacy_string_side_effects_skipped(self):
        """Plugins that still use list-of-strings for side_effects
        have no condition block to validate. Don't crash on them."""
        from dossier_engine.plugin import validate_side_effect_conditions

        validate_side_effect_conditions({
            "activities": [{
                "name": "a",
                "side_effects": ["foo", "bar"],
            }],
        })

    def test_empty_activities_list_ok(self):
        from dossier_engine.plugin import validate_side_effect_conditions

        validate_side_effect_conditions({"activities": []})
        validate_side_effect_conditions({})
    """Regression tests: ``side_effects:`` in workflow YAML is a
    list of dicts (``{"activity": "foo", "condition": ...}``), not
    a list of strings. The normalizer has to qualify the ``activity``
    key inside each dict so that downstream code (side_effects
    pipeline, prov_columns) can match by qualified name consistently.

    A bug in this area caused the columns PROV graph to collapse into
    a single row: bare-named side-effect activities in the DB never
    matched the qualified names in ``system_activity_types``, so the
    filter that routes them to the middle band always missed. Keep
    this regression covered — the bug was invisible to end-to-end
    tests because the graph still rendered, just wrongly.
    """

    def test_dict_form_side_effects_activity_qualified(self):
        registry = PluginRegistry()
        p = _make_plugin(workflow={
            "activities": [
                {
                    "name": "dienAanvraagIn",
                    "side_effects": [
                        {"activity": "duidVerantwoordelijkeOrganisatieAan"},
                        {"activity": "setSystemFields"},
                    ],
                },
            ],
        })
        p.name = "wf"
        registry.register(p)

        side = p.workflow["activities"][0]["side_effects"]
        assert side == [
            {"activity": "oe:duidVerantwoordelijkeOrganisatieAan"},
            {"activity": "oe:setSystemFields"},
        ]

    def test_dict_form_preserves_other_keys(self):
        """Qualifying ``activity`` must not drop the ``condition``
        key (or any other metadata carried on the entry). The
        engine's side-effect pipeline reads ``condition:
        {entity_type, field, value}`` to decide whether to run the
        entry — the normalizer must leave that block untouched."""
        registry = PluginRegistry()
        p = _make_plugin(workflow={
            "activities": [
                {
                    "name": "submit",
                    "side_effects": [
                        {
                            "activity": "followup",
                            "condition": {
                                "entity_type": "oe:beslissing",
                                "field": "content.beslissing",
                                "value": "goedgekeurd",
                            },
                        },
                    ],
                },
            ],
        })
        p.name = "wf"
        registry.register(p)

        se = p.workflow["activities"][0]["side_effects"][0]
        assert se["activity"] == "oe:followup"
        assert se["condition"] == {
            "entity_type": "oe:beslissing",
            "field": "content.beslissing",
            "value": "goedgekeurd",
        }

    def test_already_qualified_activity_is_idempotent(self):
        """Running the normalizer over YAML that already has
        qualified names must not change anything."""
        registry = PluginRegistry()
        p = _make_plugin(workflow={
            "activities": [
                {
                    "name": "oe:submit",
                    "side_effects": [
                        {"activity": "oe:followup"},
                    ],
                },
            ],
        })
        p.name = "wf"
        registry.register(p)

        side = p.workflow["activities"][0]["side_effects"]
        assert side == [{"activity": "oe:followup"}]

    def test_legacy_string_side_effects_still_work(self):
        """Back-compat: old-style YAML that used bare strings instead
        of dicts should still get qualified. This path may be dead
        in the current toelatingen plugin but external plugins might
        still use it."""
        registry = PluginRegistry()
        p = _make_plugin(workflow={
            "activities": [
                {"name": "submit", "side_effects": ["followup"]},
            ],
        })
        p.name = "wf"
        registry.register(p)

        assert p.workflow["activities"][0]["side_effects"] == ["oe:followup"]

    def test_mixed_dict_and_string_side_effects(self):
        """A plugin could have mixed legacy + new-style entries in
        the same list during migration. Both get qualified."""
        registry = PluginRegistry()
        p = _make_plugin(workflow={
            "activities": [
                {
                    "name": "submit",
                    "side_effects": [
                        "followup_a",
                        {"activity": "followup_b"},
                    ],
                },
            ],
        })
        p.name = "wf"
        registry.register(p)

        assert p.workflow["activities"][0]["side_effects"] == [
            "oe:followup_a",
            {"activity": "oe:followup_b"},
        ]


class TestConditionFnValidation:
    """Validator coverage for the function-form side-effect gate
    (``condition_fn:``). Separate from the dict-form validator class
    because the rules are different — function form is just a
    non-empty string at the shape layer; registration is cross-checked
    by a separate validator that runs after the Plugin is assembled.
    """

    def test_condition_fn_string_accepted(self):
        from dossier_engine.plugin import validate_side_effect_conditions

        validate_side_effect_conditions({
            "activities": [{
                "name": "a",
                "side_effects": [{
                    "activity": "b",
                    "condition_fn": "should_publish",
                }],
            }],
        })

    def test_condition_fn_non_string_rejected(self):
        from dossier_engine.plugin import validate_side_effect_conditions

        with pytest.raises(ValueError) as exc_info:
            validate_side_effect_conditions({
                "activities": [{
                    "name": "a",
                    "side_effects": [{
                        "activity": "b",
                        "condition_fn": {"name": "should_publish"},  # bogus
                    }],
                }],
            })
        assert "non-string" in str(exc_info.value)

    def test_condition_fn_empty_string_rejected(self):
        from dossier_engine.plugin import validate_side_effect_conditions

        with pytest.raises(ValueError) as exc_info:
            validate_side_effect_conditions({
                "activities": [{
                    "name": "a",
                    "side_effects": [{
                        "activity": "b",
                        "condition_fn": "   ",
                    }],
                }],
            })
        assert "non-string" in str(exc_info.value)

    def test_both_forms_on_same_entry_rejected(self):
        """Declaring both ``condition:`` and ``condition_fn:`` on the
        same side-effect entry is a configuration bug — the engine
        can't tell which one the author meant as authoritative. Fail
        loud at load."""
        from dossier_engine.plugin import validate_side_effect_conditions

        with pytest.raises(ValueError) as exc_info:
            validate_side_effect_conditions({
                "activities": [{
                    "name": "a",
                    "side_effects": [{
                        "activity": "b",
                        "condition": {
                            "entity_type": "oe:x",
                            "field": "content.y",
                            "value": "z",
                        },
                        "condition_fn": "should_publish",
                    }],
                }],
            })
        msg = str(exc_info.value)
        assert "condition" in msg and "condition_fn" in msg

    def test_registration_cross_check_unknown_name_rejected(self):
        from dossier_engine.plugin import (
            validate_side_effect_condition_fn_registrations,
        )

        with pytest.raises(ValueError) as exc_info:
            validate_side_effect_condition_fn_registrations(
                workflow={
                    "activities": [{
                        "name": "a",
                        "side_effects": [{
                            "activity": "b",
                            "condition_fn": "does_not_exist",
                        }],
                    }],
                },
                side_effect_conditions={},
            )
        msg = str(exc_info.value)
        assert "does_not_exist" in msg
        assert "no predicate" in msg or "not registered" in msg.lower()

    def test_registration_cross_check_known_name_accepted(self):
        from dossier_engine.plugin import (
            validate_side_effect_condition_fn_registrations,
        )

        async def should_publish(ctx):
            return True

        # No raise — registration resolves.
        validate_side_effect_condition_fn_registrations(
            workflow={
                "activities": [{
                    "name": "a",
                    "side_effects": [{
                        "activity": "b",
                        "condition_fn": "should_publish",
                    }],
                }],
            },
            side_effect_conditions={"should_publish": should_publish},
        )

    def test_registration_cross_check_lists_known_names_in_error(self):
        """Unknown-name error lists what IS registered, so the
        author can spot a typo quickly."""
        from dossier_engine.plugin import (
            validate_side_effect_condition_fn_registrations,
        )

        async def fn_a(ctx): return True
        async def fn_b(ctx): return False

        with pytest.raises(ValueError) as exc_info:
            validate_side_effect_condition_fn_registrations(
                workflow={
                    "activities": [{
                        "name": "a",
                        "side_effects": [{
                            "activity": "b",
                            "condition_fn": "typo_here",
                        }],
                    }],
                },
                side_effect_conditions={"fn_a": fn_a, "fn_b": fn_b},
            )
        msg = str(exc_info.value)
        # The known names should appear in the message, sorted.
        assert "fn_a" in msg and "fn_b" in msg


class TestRelationDeclarationsValidation:
    """Bug 78 (Round 26): ``validate_relation_declarations`` enforces
    the strict relation-type contract — workflow-level declaration
    with mandatory ``kind``, activity-level reference-by-name only,
    no Style-3-by-type fallback. Each test below names the rule it
    pins; paranoia-check discipline (Round 25) applies — each rule
    must go red if the corresponding check is removed from the
    validator.

    Tests are organised by rule:
      * Workflow-level: type required, kind required + valid, from_
        types/to_types domain-only, unknown keys rejected.
      * Activity-level: type required + must resolve, forbidden
        workflow-scope keys rejected, unknown keys rejected,
        validator/validators mutex, validators dict shape, process_
        control restrictions.
    """

    # --- Workflow-level rules ---

    def test_workflow_valid_shape_passes(self):
        """Positive control: a well-formed workflow passes silently.
        Covers both kinds, the optional from_types/to_types on domain
        relations, and the activity-level reference-only pattern."""
        from dossier_engine.plugin import validate_relation_declarations
        validate_relation_declarations({
            "relations": [
                {"type": "oe:neemtAkteVan", "kind": "process_control",
                 "description": "ack stale version"},
                {"type": "oe:betreft", "kind": "domain",
                 "from_types": ["entity"], "to_types": ["external_uri"]},
                {"type": "oe:free", "kind": "domain"},  # no from_/to_types = any
            ],
            "activities": [
                {"name": "submitAanvraag", "relations": [
                    {"type": "oe:neemtAkteVan",
                     "validator": "validate_ack"},
                ]},
                {"name": "bewerkRel", "relations": [
                    {"type": "oe:betreft", "operations": ["add", "remove"],
                     "validators": {"add": "add_fn", "remove": "rm_fn"}},
                ]},
            ],
        })

    def test_workflow_relation_missing_type_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="missing `type:`"):
            validate_relation_declarations({
                "relations": [{"kind": "domain"}],
            })

    def test_workflow_relation_missing_kind_rejected(self):
        """The root cause of Bug 78 — ``kind`` declared optional
        previously meant dispatch guessed from request shape and
        ``_relation_kind`` became dead code. Now required."""
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="kind:.*required"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo"}],
            })

    def test_workflow_relation_invalid_kind_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="must be one of"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "nonsense"}],
            })

    def test_workflow_from_types_on_process_control_rejected(self):
        """``from_types``/``to_types`` are domain-only. Process-
        control relations are activity→entity, not entity→entity,
        so ref-type constraints don't apply."""
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="only legal on `kind: domain`"):
            validate_relation_declarations({
                "relations": [{
                    "type": "oe:foo", "kind": "process_control",
                    "from_types": ["entity"],
                }],
            })

    def test_workflow_to_types_on_process_control_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="only legal on `kind: domain`"):
            validate_relation_declarations({
                "relations": [{
                    "type": "oe:foo", "kind": "process_control",
                    "to_types": ["external_uri"],
                }],
            })

    def test_workflow_unknown_key_rejected(self):
        """Typos in workflow-level declarations fail fast. This is
        the prevention for the ``_relation_kind``-style dead code —
        a field that exists but isn't wired up gets rejected instead
        of silently ignored."""
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="unknown key"):
            validate_relation_declarations({
                "relations": [{
                    "type": "oe:foo", "kind": "domain",
                    "typo_field": "oops",
                }],
            })

    def test_workflow_relation_not_dict_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="must be dicts"):
            validate_relation_declarations({
                "relations": ["oe:foo"],
            })

    def test_no_relations_section_ok(self):
        """A workflow without a ``relations:`` block is valid —
        plugins that don't use relations at all should work."""
        from dossier_engine.plugin import validate_relation_declarations
        validate_relation_declarations({})
        validate_relation_declarations({"relations": []})
        validate_relation_declarations({"activities": []})

    # --- Activity-level rules ---

    def test_activity_missing_type_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="missing `type:`"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "domain"}],
                "activities": [{"name": "a", "relations": [{}]}],
            })

    def test_activity_references_undeclared_type_rejected(self):
        """Activity can only reference types declared at workflow
        level. This is the "single source of truth" part of option C."""
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="not declared at workflow level"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "domain"}],
                "activities": [{"name": "a", "relations": [
                    {"type": "oe:bar"},  # not declared
                ]}],
            })

    def test_activity_kind_field_rejected(self):
        """Activity-level ``kind:`` is forbidden — the workflow-level
        declaration is the single source of truth (option C in the
        Round 26 design discussion)."""
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="workflow level only"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "domain"}],
                "activities": [{"name": "a", "relations": [
                    {"type": "oe:foo", "kind": "domain"},  # redundant
                ]}],
            })

    def test_activity_from_types_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="workflow level only"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "domain"}],
                "activities": [{"name": "a", "relations": [
                    {"type": "oe:foo", "from_types": ["entity"]},
                ]}],
            })

    def test_activity_description_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="workflow level only"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "domain"}],
                "activities": [{"name": "a", "relations": [
                    {"type": "oe:foo", "description": "x"},
                ]}],
            })

    def test_activity_unknown_key_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="unknown key"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "domain"}],
                "activities": [{"name": "a", "relations": [
                    {"type": "oe:foo", "typo_field": 1},
                ]}],
            })

    def test_activity_validator_and_validators_mutex(self):
        """Can't declare both ``validator:`` (single-string) and
        ``validators:`` (dict) on the same relation — one or the
        other, never both."""
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="mutually exclusive"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "domain"}],
                "activities": [{"name": "a", "relations": [{
                    "type": "oe:foo",
                    "validator": "single_fn",
                    "validators": {"add": "a", "remove": "r"},
                }]}],
            })

    def test_activity_validators_partial_dict_rejected(self):
        """``validators:`` dict must have exactly ``{add, remove}``.
        Partial dicts (only ``add:`` or only ``remove:``) rejected —
        the explicit pairing is part of the new contract (use
        ``validator:`` single-string if you only need one)."""
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match=r"exactly `\{add, remove\}`"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "domain"}],
                "activities": [{"name": "a", "relations": [{
                    "type": "oe:foo",
                    "validators": {"add": "a"},  # missing remove
                }]}],
            })

    def test_activity_validators_not_dict_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="must be a dict"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "domain"}],
                "activities": [{"name": "a", "relations": [{
                    "type": "oe:foo",
                    "validators": "not_a_dict",
                }]}],
            })

    def test_activity_validators_dict_on_process_control_rejected(self):
        """process_control relations have no remove operation; the
        ``validators: {add, remove}`` dict form makes no sense for
        them. Single ``validator:`` string is the only legal form."""
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError, match="process_control"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "process_control"}],
                "activities": [{"name": "a", "relations": [{
                    "type": "oe:foo",
                    "validators": {"add": "a", "remove": "r"},
                }]}],
            })

    def test_activity_operations_remove_on_process_control_rejected(self):
        from dossier_engine.plugin import validate_relation_declarations
        with pytest.raises(ValueError,
                           match="operations: \\[remove\\].*process_control"):
            validate_relation_declarations({
                "relations": [{"type": "oe:foo", "kind": "process_control"}],
                "activities": [{"name": "a", "relations": [{
                    "type": "oe:foo",
                    "operations": ["add", "remove"],
                }]}],
            })

    def test_activity_single_validator_on_process_control_ok(self):
        """Positive: process_control allows ``validator:`` (single-
        string) — the only legal validator form for that kind."""
        from dossier_engine.plugin import validate_relation_declarations
        validate_relation_declarations({
            "relations": [{"type": "oe:foo", "kind": "process_control"}],
            "activities": [{"name": "a", "relations": [
                {"type": "oe:foo", "validator": "validate_foo"},
            ]}],
        })


class TestRelationValidatorRegistrations:
    """Bug 78 (Round 26): cross-registry check at plugin load —
    ``plugin.relation_validators`` dict keys must be validator NAMES,
    not relation type names. A key that matches a declared workflow-
    level relation type name re-creates the Style-3 by-type-name
    fallback that Bug 78 removed, just through naming convention
    rather than through the removed fallback code path."""

    def test_no_collision_ok(self):
        """Positive: validator names that don't match any declared
        type name are fine."""
        from dossier_engine.plugin import (
            validate_relation_validator_registrations, Plugin,
        )
        async def fn(**k): pass
        plugin = Plugin(
            name="t", workflow={"relations": [
                {"type": "oe:betreft", "kind": "domain"},
            ]},
            entity_models={},
            relation_validators={"validate_betreft": fn},
        )
        # No raise.
        validate_relation_validator_registrations(plugin)

    def test_key_collides_with_declared_type_rejected(self):
        """The Style-3 hazard: a validator function registered under
        the relation type name as its key. Looks innocent but revives
        the removed by-type-name fallback through naming convention."""
        from dossier_engine.plugin import (
            validate_relation_validator_registrations, Plugin,
        )
        async def fn(**k): pass
        plugin = Plugin(
            name="t", workflow={"relations": [
                {"type": "oe:betreft", "kind": "domain"},
            ]},
            entity_models={},
            relation_validators={"oe:betreft": fn},  # collision!
        )
        with pytest.raises(ValueError, match="Style-3"):
            validate_relation_validator_registrations(plugin)

    def test_empty_dict_ok(self):
        """A plugin with no relation validators at all passes."""
        from dossier_engine.plugin import (
            validate_relation_validator_registrations, Plugin,
        )
        plugin = Plugin(
            name="t", workflow={"relations": []},
            entity_models={}, relation_validators={},
        )
        validate_relation_validator_registrations(plugin)


# =====================================================================
# Obs 95 / Round 28: dotted-path callable resolution
# =====================================================================


class TestImportDottedCallable:
    """``_import_dotted_callable`` is the underlying resolver used by
    ``build_callable_registries_from_workflow``. It mirrors
    ``_import_dotted`` but accepts any Python object (callables,
    ``FieldValidator`` instances) rather than requiring a ``BaseModel``
    subclass. These tests cover its error-attribution behaviour — the
    ``context`` kwarg is what makes plugin-load errors actionable by
    naming the activity + YAML field where the bad path came from.
    """

    def test_resolves_a_real_callable(self):
        from dossier_engine.plugin import _import_dotted_callable
        # Use a stable stdlib function so this test has no dep on the
        # toelatingen plugin being installed.
        obj = _import_dotted_callable("os.path.join")
        from os.path import join as real_join
        assert obj is real_join

    def test_non_string_raises(self):
        from dossier_engine.plugin import _import_dotted_callable
        import pytest
        with pytest.raises(ValueError, match="Invalid dotted path"):
            _import_dotted_callable(None)
        with pytest.raises(ValueError, match="Invalid dotted path"):
            _import_dotted_callable(42)

    def test_missing_dot_raises(self):
        from dossier_engine.plugin import _import_dotted_callable
        import pytest
        with pytest.raises(ValueError, match="must be a fully-qualified"):
            _import_dotted_callable("just_a_name")

    def test_bad_module_raises_with_context(self):
        from dossier_engine.plugin import _import_dotted_callable
        import pytest
        with pytest.raises(ValueError, match="Cannot import module"):
            _import_dotted_callable("no.such.module.function")

    def test_missing_attribute_raises_with_context(self):
        from dossier_engine.plugin import _import_dotted_callable
        import pytest
        with pytest.raises(ValueError, match="has no attribute"):
            _import_dotted_callable(
                "os.path.no_such_function",
                context="activity 'x' handler",
            )
        # Context string appears in the message so operators know
        # which YAML field produced the bad path.
        try:
            _import_dotted_callable(
                "os.path.no_such_function",
                context="activity 'x' handler",
            )
        except ValueError as e:
            assert "activity 'x' handler" in str(e)

    def test_empty_context_omits_where_fragment(self):
        """When context is the default empty string, the error message
        shouldn't contain a dangling `(in )` fragment."""
        from dossier_engine.plugin import _import_dotted_callable
        try:
            _import_dotted_callable("os.path.no_such_function")
        except ValueError as e:
            assert "(in " not in str(e)


class TestBuildCallableRegistries:
    """``build_callable_registries_from_workflow`` is the Obs 95 /
    Round 28 replacement for hand-built per-plugin registry dicts. The
    tests use ``os.path.join`` etc. as stable real callables — any
    module-level importable name works; the tests are about the
    traversal of the workflow dict, not about the specific callable
    shapes.
    """

    def test_empty_workflow_returns_eight_empty_dicts(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        result = build_callable_registries_from_workflow({})
        assert set(result.keys()) == {
            "handlers", "validators", "task_handlers",
            "status_resolvers", "task_builders",
            "side_effect_conditions", "relation_validators",
            "field_validators",
        }
        for reg_name, reg in result.items():
            assert reg == {}, f"{reg_name!r} should be empty"

    def test_activity_level_handler_resolves(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"activities": [
            {"name": "act1", "handler": "os.path.join"},
        ]}
        result = build_callable_registries_from_workflow(wf)
        from os.path import join
        assert result["handlers"] == {"os.path.join": join}

    def test_activity_level_status_resolver_resolves(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"activities": [
            {"name": "act1", "status_resolver": "os.path.join"},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert "os.path.join" in result["status_resolvers"]

    def test_task_builders_list(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"activities": [
            {"name": "act1", "task_builders": ["os.path.join", "os.path.split"]},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert set(result["task_builders"].keys()) == {
            "os.path.join", "os.path.split",
        }

    def test_validators_list_uses_name_key(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"activities": [
            {"name": "act1", "validators": [
                {"name": "os.path.join", "description": "x"},
            ]},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert "os.path.join" in result["validators"]

    def test_tasks_list_uses_function_key(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"activities": [
            {"name": "act1", "tasks": [
                {"kind": "recorded", "function": "os.path.join"},
            ]},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert "os.path.join" in result["task_handlers"]

    def test_side_effect_condition_fn(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"activities": [
            {"name": "act1", "side_effects": [
                {"activity": "other", "condition_fn": "os.path.join"},
            ]},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert "os.path.join" in result["side_effect_conditions"]

    def test_workflow_level_relation_types_validator(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"relation_types": [
            {"type": "oe:x", "validator": "os.path.join"},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert "os.path.join" in result["relation_validators"]

    def test_workflow_level_relation_types_validators_dict(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"relation_types": [
            {"type": "oe:x", "validators": {
                "add": "os.path.join", "remove": "os.path.split",
            }},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert set(result["relation_validators"].keys()) == {
            "os.path.join", "os.path.split",
        }

    def test_activity_level_relation_validator(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"activities": [
            {"name": "act1", "relations": [
                {"type": "oe:x", "validator": "os.path.join"},
            ]},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert "os.path.join" in result["relation_validators"]

    def test_deduplication_across_activities(self):
        """If two activities reference the same path, it resolves once
        and ends up once in the registry."""
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"activities": [
            {"name": "a", "handler": "os.path.join"},
            {"name": "b", "handler": "os.path.join"},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert len(result["handlers"]) == 1
        assert "os.path.join" in result["handlers"]

    def test_field_validators_block_uses_url_key(self):
        """field_validators is the one registry where the key is NOT
        the dotted path — it's the URL segment. The dotted path is only
        used for resolution.
        """
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"field_validators": {
            "my_url_key": "os.path.join",
        }}
        result = build_callable_registries_from_workflow(wf)
        assert "my_url_key" in result["field_validators"]
        assert "os.path.join" not in result["field_validators"]
        from os.path import join
        assert result["field_validators"]["my_url_key"] is join

    def test_bad_path_fails_fast_with_context(self):
        """A typo in the YAML dotted path should raise at build time
        with enough context that the operator can find the bad YAML
        field.
        """
        from dossier_engine.plugin import build_callable_registries_from_workflow
        import pytest
        wf = {"activities": [
            {"name": "myactivity", "handler": "no.such.module.anything"},
        ]}
        with pytest.raises(ValueError) as exc:
            build_callable_registries_from_workflow(wf)
        msg = str(exc.value)
        assert "myactivity" in msg
        assert "handler" in msg

    def test_bad_path_in_side_effect_mentions_side_effect(self):
        from dossier_engine.plugin import build_callable_registries_from_workflow
        import pytest
        wf = {"activities": [
            {"name": "act1", "side_effects": [
                {"activity": "downstream", "condition_fn": "bad.path.fn"},
            ]},
        ]}
        with pytest.raises(ValueError) as exc:
            build_callable_registries_from_workflow(wf)
        assert "act1" in str(exc.value)
        assert "downstream" in str(exc.value)
        assert "condition_fn" in str(exc.value)

    def test_non_string_values_are_silently_skipped(self):
        """YAML shape drift (e.g. None, list-where-string-expected)
        should not crash the builder — shape validation lives in the
        other plugin validators (validate_relation_declarations etc.).
        The builder's job is just to resolve what is clearly a path.
        """
        from dossier_engine.plugin import build_callable_registries_from_workflow
        wf = {"activities": [
            {"name": "a", "handler": None},
            {"name": "b", "status_resolver": 42},
            {"name": "c", "task_builders": [None, "os.path.join", 99]},
        ]}
        result = build_callable_registries_from_workflow(wf)
        assert result["handlers"] == {}
        assert result["status_resolvers"] == {}
        assert "os.path.join" in result["task_builders"]

    def test_toelatingen_plugin_loads_via_builder(self):
        """End-to-end: the real toelatingen plugin's create_plugin()
        builds all eight registries using the builder and ends up with
        every expected callable reachable via its dotted path. This
        pins the Obs 95 migration at the integration-seam level.
        """
        from dossier_toelatingen import create_plugin
        plugin = create_plugin()

        # Every registry is keyed by dotted path except field_validators.
        for reg_name in (
            "handlers", "validators", "task_handlers",
            "status_resolvers", "task_builders",
            "side_effect_conditions", "relation_validators",
        ):
            reg = getattr(plugin, reg_name)
            for key in reg:
                assert "." in key, (
                    f"{reg_name!r} key {key!r} is not a dotted path"
                )

        # field_validators is keyed by URL segment.
        assert "erfgoedobject" in plugin.field_validators
        assert "handeling" in plugin.field_validators

        # Spot-check one key from each non-empty registry:
        assert "dossier_toelatingen.handlers.set_dossier_access" in plugin.handlers
        assert "dossier_toelatingen.validators.valideer_indiening" in plugin.validators
        assert "dossier_toelatingen.tasks.send_ontvangstbevestiging" in plugin.task_handlers
        assert "dossier_toelatingen.handlers.resolve_beslissing_status" in plugin.status_resolvers
        assert "dossier_toelatingen.handlers.schedule_trekAanvraag_if_onvolledig" in plugin.task_builders
        assert "dossier_toelatingen.relation_validators.validate_neemt_akte_van" in plugin.relation_validators


# =====================================================================
# Bug 20 / Round 30: _PendingEntity field parity with EntityRow
# =====================================================================


class TestPendingEntityFieldParity:
    """``_PendingEntity`` is a duck-typed stand-in for ``EntityRow``
    used inside the engine's generated-phase so handlers can read
    entities the current activity is generating before they hit
    the database. The class's own docstring says:

        When you add a column to EntityRow, also add it here, or
        context.get_typed will fail with AttributeError on pending
        entities.

    That rule had drifted. Five EntityRow columns were missing from
    ``_PendingEntity`` (``type``, ``dossier_id``, ``generated_by``,
    ``derived_from``, ``tombstoned_by``) — which produced an
    AttributeError when a pending entity was passed to the lineage
    walker via a task builder. Concrete crash path:
    ``schedule_trekAanvraag_if_onvolledig`` → ``_build_trekAanvraag_task``
    → ``find_related_entity(beslissing_pending, "oe:aanvraag")`` →
    ``lineage.py:123 start_entity.type`` → 💥.

    This test is the maintenance guard: it enumerates every
    ``EntityRow`` column at test time and asserts each is a readable
    attribute on a ``_PendingEntity`` instance. Adding a new column
    to ``EntityRow`` without updating ``_PendingEntity`` goes red here.
    """

    def test_pending_entity_has_every_entity_row_column(self):
        from uuid import uuid4

        from dossier_engine.db.models import EntityRow
        from dossier_engine.engine.context import _PendingEntity

        # Construct a pending entity with the same constructor shape
        # production code uses. Values are placeholder — the test is
        # about attribute presence, not attribute correctness.
        pending = _PendingEntity(
            content={"any": "value"},
            entity_id=uuid4(),
            id=uuid4(),
            attributed_to="test",
            schema_version=None,
            type="oe:test",
            dossier_id=uuid4(),
            generated_by=uuid4(),
            derived_from=None,
        )

        entity_row_columns = set(EntityRow.__table__.columns.keys())
        missing = []
        for col_name in sorted(entity_row_columns):
            if not hasattr(pending, col_name):
                missing.append(col_name)

        assert not missing, (
            f"_PendingEntity is missing EntityRow columns: {missing}. "
            f"See the class's own docstring: every EntityRow column "
            f"must be readable on _PendingEntity or context.get_typed "
            f"(and anything that reads the row through it) will fail "
            f"with AttributeError on pending entities."
        )

    def test_pending_entity_tombstoned_by_is_none(self):
        """Pending entities cannot be tombstoned — the row doesn't
        exist yet, so there's no version to mark as dead. The
        invariant is structural, not a design choice: tombstoning
        happens in the persistence phase, which only runs after
        the current activity's pending entities are written out."""
        from uuid import uuid4

        from dossier_engine.engine.context import _PendingEntity

        pending = _PendingEntity(
            content={}, entity_id=uuid4(), id=uuid4(),
            attributed_to="t", schema_version=None,
            type="oe:x", dossier_id=uuid4(),
            generated_by=uuid4(), derived_from=None,
        )
        assert pending.tombstoned_by is None

    def test_pending_entity_created_at_is_none(self):
        """Same reasoning as tombstoned_by: ``created_at`` is set by
        the database at INSERT time (``default=lambda: datetime.now(...)``
        on the column). For a not-yet-persisted pending entity, the
        canonical value is None — callers that care about creation
        time of a pending entity are asking the wrong question; use
        the activity's ``started_at`` instead."""
        from uuid import uuid4

        from dossier_engine.engine.context import _PendingEntity

        pending = _PendingEntity(
            content={}, entity_id=uuid4(), id=uuid4(),
            attributed_to="t", schema_version=None,
            type="oe:x", dossier_id=uuid4(),
            generated_by=uuid4(), derived_from=None,
        )
        assert pending.created_at is None


# =====================================================================
# Bug 4 / Round 31: Repository constructor type annotation resolvable
# =====================================================================


class TestRepositoryAnnotations:
    """``Repository.__init__`` historically had a ``session: Session``
    annotation where ``Session`` was never imported. The code ran
    fine at runtime because ``from __future__ import annotations`` at
    the top of ``db/models.py`` stringifies all annotations, so the
    unresolved name never actually needed to resolve. But anything
    that calls ``typing.get_type_hints(...)`` — IDE tooling, runtime
    validators like FastAPI's dependency injection when a repo is
    passed via Depends, static type checkers — hit a ``NameError``.

    The intended type is ``AsyncSession``: every method on Repository
    uses ``await self.session.execute(...)`` and ``await
    self.session.get(...)``, and ``AsyncSession`` is the type that
    supports those signatures. Round 31 corrects the annotation.

    This test pins the resolution by calling ``get_type_hints`` — the
    exact operation that used to fail. If the annotation regresses
    (say, to ``"Session"`` or some new typo), this test goes red with
    ``NameError: name 'Session' is not defined`` in the failure trace.
    """

    def test_repository_init_annotations_resolve(self):
        import typing

        from sqlalchemy.ext.asyncio import AsyncSession

        from dossier_engine.db.models import Repository

        # get_type_hints actually evaluates stringified annotations in
        # their module context. On the pre-fix code this raised NameError
        # for ``Session``; post-fix it returns cleanly.
        hints = typing.get_type_hints(Repository.__init__)
        assert "session" in hints
        assert hints["session"] is AsyncSession


# =====================================================================
# Bug 27 / Round 31: DossierAccessEntry.activity_view contract
# =====================================================================


class TestDossierAccessEntryActivityView:
    """Round 31 tightened ``DossierAccessEntry.activity_view`` from an
    open ``str`` to ``Literal["all", "own"] | list[str] | dict``. The
    ``"related"`` mode was removed. These tests pin the resulting
    write-time contract — Pydantic rejects ``"related"`` and other
    unknown strings, accepts the four documented shapes, and defaults
    to ``"own"`` (the deny-more default; was ``"related"`` pre-Round-31).

    Two-layer defense for ``"related"``:
    * Write-time (this file): Pydantic rejects. Operators get a clean
      ``ValidationError`` when constructing or updating an access entry.
    * Read-time (``test_activity_visibility.py``): ``parse_activity_view``
      routes the legacy string to deny-safe, so DB entries written
      before Round 31 don't silently flip semantics.
    """

    def test_default_activity_view_is_own(self):
        """Pre-Round-31 default was ``"related"`` — the broadest mode
        of the three strings. Round 31 changed it to ``"own"`` (the
        narrower mode) so access entries that omit ``activity_view``
        err on the side of less visibility. ``"all"`` would be too
        permissive for an unspecified default; ``"own"`` matches the
        aanvrager-case of "show me my stuff" which is the most
        common intent when the field is left blank."""
        from dossier_engine.entities import DossierAccessEntry
        entry = DossierAccessEntry()
        assert entry.activity_view == "own"

    def test_accepts_all_string(self):
        from dossier_engine.entities import DossierAccessEntry
        entry = DossierAccessEntry(activity_view="all")
        assert entry.activity_view == "all"

    def test_accepts_own_string(self):
        from dossier_engine.entities import DossierAccessEntry
        entry = DossierAccessEntry(activity_view="own")
        assert entry.activity_view == "own"

    def test_accepts_list_of_types(self):
        from dossier_engine.entities import DossierAccessEntry
        entry = DossierAccessEntry(
            activity_view=["dienAanvraagIn", "neemBeslissing"],
        )
        assert entry.activity_view == [
            "dienAanvraagIn", "neemBeslissing",
        ]

    def test_accepts_dict_with_mode_and_include(self):
        from dossier_engine.entities import DossierAccessEntry
        entry = DossierAccessEntry(
            activity_view={
                "mode": "own",
                "include": ["neemBeslissing"],
            },
        )
        assert entry.activity_view == {
            "mode": "own",
            "include": ["neemBeslissing"],
        }

    def test_rejects_related_string(self):
        """The primary Round 31 deprecation pin. ``"related"`` was
        removed as a supported mode — attempts to create or
        deserialize an access entry with ``activity_view: "related"``
        now fail at validation time. Any caller going through Pydantic
        (the ``setDossierAccess`` side effect, plugin entity
        validation via ``oe:dossier_access`` model binding) gets a
        clean error at write time. See the module-level docstring
        for the read-time counterpart."""
        from pydantic import ValidationError

        from dossier_engine.entities import DossierAccessEntry
        try:
            DossierAccessEntry(activity_view="related")
        except ValidationError:
            return
        raise AssertionError(
            "DossierAccessEntry should have rejected "
            "activity_view='related' but accepted it. The Literal "
            "type on the field must have regressed."
        )

    def test_rejects_unknown_string(self):
        """Any string that isn't ``"all"`` or ``"own"`` is rejected —
        not just ``"related"``. Guards against typos and forward-
        compatibility assumptions (operators writing what they
        *think* will be a future mode)."""
        from pydantic import ValidationError

        from dossier_engine.entities import DossierAccessEntry
        try:
            DossierAccessEntry(activity_view="banana")
        except ValidationError:
            return
        raise AssertionError(
            "DossierAccessEntry should have rejected "
            "activity_view='banana' but accepted it."
        )

    def test_dossier_access_wrapper_composes_entries(self):
        """The outer ``DossierAccess`` container just validates a list
        of entries. Pins the composition so a future refactor that
        tries to move validation logic up to the container goes red
        if it forgets to delegate per-entry."""
        from dossier_engine.entities import DossierAccess
        access = DossierAccess(access=[
            {"role": "oe:reader", "view": ["oe:aanvraag"],
             "activity_view": "own"},
            {"role": "oe:admin", "view": [], "activity_view": "all"},
        ])
        assert len(access.access) == 2
        assert access.access[0].activity_view == "own"
        assert access.access[1].activity_view == "all"


# =====================================================================
# Bug 39 / Round 32: TaskEntity.status + TaskEntity.kind contracts
# =====================================================================


class TestTaskEntityStatusAndKind:
    """Round 32 tightened two fields on ``TaskEntity`` that had been
    typed as bare ``str`` with the valid values only documented as
    inline comments: ``status`` (5 values) and ``kind`` (4 values).
    Same shape as Bug 27's ``DossierAccessEntry.activity_view`` fix,
    simpler in that there's no policy decision — all nine values
    are kept, just the types are narrowed.

    Both fields are re-validated at read time via ``context.get_typed``
    when ``system:task`` content is loaded through the
    ``plugin.entity_models`` registration (see ``app.py:128``). No
    production code path exercises that re-validation for tasks
    today (the worker reads ``task.content`` as a raw dict), but the
    path exists, which means the tightened types must be correct on
    any data currently in the DB — not just on the write path. All
    nine values are actively written by production code and no
    historical migration introduced outliers, so the tightening is
    backwards-compatible with existing task entities.
    """

    # --- status ---

    def test_default_status_is_scheduled(self):
        """A freshly-constructed ``TaskEntity`` defaults to
        ``status="scheduled"``. Matches the ``tasks.py::_schedule_recorded_task``
        production call site which passes ``status="scheduled"``
        explicitly, and the docstring's lifecycle diagram which
        starts every task at scheduled."""
        from dossier_engine.entities import TaskEntity
        task = TaskEntity(kind="recorded")
        assert task.status == "scheduled"

    def test_status_accepts_all_five_values(self):
        """The five statuses in the lifecycle diagram are all
        valid. Pin every value so a future refactor that drops
        one by accident goes red here."""
        from dossier_engine.entities import TaskEntity
        for status in [
            "scheduled", "completed", "cancelled",
            "superseded", "dead_letter",
        ]:
            task = TaskEntity(kind="recorded", status=status)
            assert task.status == status

    def test_status_rejects_unknown_value(self):
        """Anything outside the five documented statuses is
        rejected at construction time. Guards against typos and
        forward-compatibility assumptions."""
        from pydantic import ValidationError

        from dossier_engine.entities import TaskEntity
        try:
            TaskEntity(kind="recorded", status="pending")
        except ValidationError:
            return
        raise AssertionError(
            "TaskEntity should have rejected status='pending' "
            "but accepted it. The Literal type on the status "
            "field must have regressed."
        )

    # --- kind ---

    def test_kind_accepts_all_four_values(self):
        """The four task kinds in the ``TaskEntity`` docstring's
        inline comment are all valid. Pin every value."""
        from dossier_engine.entities import TaskEntity
        for kind in [
            "fire_and_forget", "recorded",
            "scheduled_activity", "cross_dossier_activity",
        ]:
            task = TaskEntity(kind=kind)
            assert task.kind == kind

    def test_kind_rejects_unknown_value(self):
        """Anything outside the four documented kinds is rejected.
        Catches typos at both the YAML-defined task level (via
        ``tasks.py::process_tasks`` which reads ``task_def.get("kind",
        "recorded")``) and the HandlerResult-returned task level."""
        from pydantic import ValidationError

        from dossier_engine.entities import TaskEntity
        try:
            TaskEntity(kind="async_fire")
        except ValidationError:
            return
        raise AssertionError(
            "TaskEntity should have rejected kind='async_fire' "
            "but accepted it. The Literal type on the kind "
            "field must have regressed."
        )

    def test_kind_is_required(self):
        """``kind`` has no default. The production call site at
        ``tasks.py:166`` always passes it explicitly, and the
        ``task_def.get("kind", "recorded")`` default happens one
        layer up (at the YAML-read level). Pin the no-default
        shape so a future change that adds a default like
        ``kind = "recorded"`` has to go through this test
        deliberately."""
        from pydantic import ValidationError

        from dossier_engine.entities import TaskEntity
        try:
            TaskEntity()  # no kind supplied
        except ValidationError:
            return
        raise AssertionError(
            "TaskEntity() should have failed — `kind` is "
            "required and has no default. If you added a "
            "default, update this test and add reasoning in "
            "the Round 32 writeup about why."
        )


class TestDeadlineRuleValidation:
    """``validate_deadline_rules`` runs at plugin load against the
    raw workflow dict. It shape-checks every ``requirements.not_before``
    and ``forbidden.not_after`` declaration in every activity and
    enforces the core semantic rule: entity field references must
    point at singleton types, because multi-cardinality types have
    no unambiguous 'which instance's deadline applies' answer.

    The runtime resolver in ``engine.scheduling.resolve_deadline``
    also defends against non-singleton references, but this
    startup-time check fails at deploy rather than at first-user-
    click — which is usually what plugin authors want.
    """

    # --- accepted shapes ------------------------------------------

    def test_no_rules_passes(self):
        from dossier_engine.plugin import validate_deadline_rules
        validate_deadline_rules({
            "activities": [{"name": "x"}, {"name": "y"}],
        })  # no raise

    def test_iso_string_passes(self):
        from dossier_engine.plugin import validate_deadline_rules
        validate_deadline_rules({
            "activities": [{
                "name": "trekAanvraagIn",
                "forbidden": {"not_after": "2026-12-31T23:59:59Z"},
            }],
        })

    def test_dict_form_with_singleton_passes(self):
        """Entity is declared with cardinality 'single' — the default.
        Rule references it. Passes."""
        from dossier_engine.plugin import validate_deadline_rules
        validate_deadline_rules({
            "entity_types": [
                {"type": "oe:permit", "cardinality": "single"},
            ],
            "activities": [{
                "name": "renewPermit",
                "forbidden": {
                    "not_after": {
                        "from_entity": "oe:permit",
                        "field": "expires_at",
                    },
                },
            }],
        })

    def test_dict_form_undeclared_type_defaults_to_single(self):
        """Type not in entity_types → default cardinality single →
        passes. System / engine / external types work this way."""
        from dossier_engine.plugin import validate_deadline_rules
        validate_deadline_rules({
            "activities": [{
                "name": "x",
                "forbidden": {
                    "not_after": {
                        "from_entity": "some_system_type",
                        "field": "deadline",
                    },
                },
            }],
        })

    def test_dict_form_with_offset_passes(self):
        from dossier_engine.plugin import validate_deadline_rules
        validate_deadline_rules({
            "entity_types": [
                {"type": "oe:permit", "cardinality": "single"},
            ],
            "activities": [{
                "name": "sendReminder",
                "forbidden": {
                    "not_after": {
                        "from_entity": "oe:permit",
                        "field": "expires_at",
                        "offset": "-7d",
                    },
                },
            }],
        })

    def test_both_rules_on_same_activity_passes(self):
        from dossier_engine.plugin import validate_deadline_rules
        validate_deadline_rules({
            "activities": [{
                "name": "approvePermit",
                "requirements": {"not_before": "2026-01-01T00:00:00Z"},
                "forbidden": {"not_after": "2026-12-31T23:59:59Z"},
            }],
        })

    # --- the big one: singletons-only ---------------------------

    def test_multi_cardinality_type_rejected(self):
        """The whole point of the validator. If the plugin author
        puts a multi-cardinality type in a deadline rule, fail at
        load with an actionable error."""
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError) as exc:
            validate_deadline_rules({
                "entity_types": [
                    {"type": "oe:aanvraag", "cardinality": "multiple"},
                ],
                "activities": [{
                    "name": "doSomething",
                    "forbidden": {
                        "not_after": {
                            "from_entity": "oe:aanvraag",
                            "field": "registered_at",
                        },
                    },
                }],
            })
        msg = str(exc.value)
        assert "doSomething" in msg
        assert "oe:aanvraag" in msg
        assert "not_after" in msg
        assert "singleton" in msg

    def test_multi_cardinality_rejected_on_not_before_too(self):
        """Same check fires for requirements.not_before — we iterate
        both rules. Also tests that the activity-name-in-message
        branch works for not_before."""
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError) as exc:
            validate_deadline_rules({
                "entity_types": [
                    {"type": "oe:beslissing", "cardinality": "multiple"},
                ],
                "activities": [{
                    "name": "earliestAction",
                    "requirements": {
                        "not_before": {
                            "from_entity": "oe:beslissing",
                            "field": "taken_at",
                        },
                    },
                }],
            })
        assert "earliestAction" in str(exc.value)
        assert "not_before" in str(exc.value)

    # --- shape errors --------------------------------------------

    def test_relative_offset_string_rejected(self):
        """'+20d' at the top level means 'relative to now' which
        has no meaning for a deadline. Reject at load with a clear
        explanation rather than letting it surface at runtime."""
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError, match="relative offset"):
            validate_deadline_rules({
                "activities": [{
                    "name": "x",
                    "forbidden": {"not_after": "+20d"},
                }],
            })

    def test_wrong_type_rejected(self):
        """List, int — not string and not dict."""
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError, match="must be an ISO 8601 string or a dict"):
            validate_deadline_rules({
                "activities": [{
                    "name": "x",
                    "forbidden": {"not_after": 42},
                }],
            })

    def test_dict_missing_from_entity_rejected(self):
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError, match="'from_entity' and 'field'"):
            validate_deadline_rules({
                "activities": [{
                    "name": "x",
                    "forbidden": {"not_after": {"field": "expires_at"}},
                }],
            })

    def test_dict_missing_field_rejected(self):
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError, match="'from_entity' and 'field'"):
            validate_deadline_rules({
                "activities": [{
                    "name": "x",
                    "forbidden": {"not_after": {"from_entity": "oe:permit"}},
                }],
            })

    def test_unknown_dict_key_rejected(self):
        """Catches ``offet:`` typos that would otherwise be silently
        ignored at runtime. The validator rejects any unknown key
        with the full allowed list in the error message so the
        author sees what they meant."""
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError, match="unknown key"):
            validate_deadline_rules({
                "activities": [{
                    "name": "x",
                    "forbidden": {
                        "not_after": {
                            "from_entity": "oe:permit",
                            "field": "expires_at",
                            "offet": "-7d",  # typo
                        },
                    },
                }],
            })

    def test_offset_wrong_type_rejected(self):
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError, match="offset must be a string"):
            validate_deadline_rules({
                "activities": [{
                    "name": "x",
                    "forbidden": {
                        "not_after": {
                            "from_entity": "oe:permit",
                            "field": "expires_at",
                            "offset": 7,  # should be "+7d"
                        },
                    },
                }],
            })

    def test_from_entity_wrong_type_rejected(self):
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError, match="from_entity must be a string"):
            validate_deadline_rules({
                "activities": [{
                    "name": "x",
                    "forbidden": {
                        "not_after": {
                            "from_entity": ["oe:permit"],  # list, not str
                            "field": "expires_at",
                        },
                    },
                }],
            })

    # --- error message quality ---------------------------------

    def test_error_mentions_activity_name(self):
        """When a workflow has many activities, the author needs to
        know which one has the bad rule. The activity name MUST be
        in every error raised by this validator."""
        from dossier_engine.plugin import validate_deadline_rules
        with pytest.raises(ValueError, match="'badActivity'"):
            validate_deadline_rules({
                "activities": [
                    {"name": "goodOne"},
                    {"name": "alsoGood"},
                    {
                        "name": "badActivity",
                        "forbidden": {"not_after": "+20d"},
                    },
                ],
            })
