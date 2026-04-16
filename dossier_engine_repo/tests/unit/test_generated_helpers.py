"""
Unit tests for `_resolve_schema_version` and `_validate_content`
from `engine.pipeline.generated`.

These two helpers handle the versioning half of `process_generated`
(the derivation-chain half is covered by
`tests/integration/test_derivation_validation.py`). They're pure
functions — no DB, no async, no `state.repo` reads. The schema
resolver takes an `activity_def` dict plus an optional "parent row"
object with a `.schema_version` attribute and decides what version
stamp the new row should carry. The content validator runs a dict
through a Pydantic model looked up via `state.plugin.resolve_schema`.

We use SimpleNamespace for the parent row and plugin stubs because
the functions only read specific attributes — `.schema_version` on
the parent, `.plugin.resolve_schema(type, version)` on the state.
Hand-building the real dataclasses would add noise without
catching anything the stubs miss.

**The versioning spec from dossiertype_template.md**

An activity may declare:

    entities:
      oe:aanvraag:
        new_version: v2
        allowed_versions: [v1, v2]

* `new_version` is used when creating a **fresh** entity of this
  type. It's required — an activity that declares `entities` for a
  type but forgets `new_version` is misconfigured and returns 500.
* `allowed_versions` is checked when **revising** an existing
  entity. The parent's stored version must be in the list,
  otherwise the activity can't revise that row (422).
* When neither the activity nor the parent has opinions, the legacy
  unversioned path kicks in — return None and let the plugin's
  default `entity_models[type]` handle content validation.

Branches:

`_resolve_schema_version`:
* no entities declaration + no parent → None (legacy creation)
* no entities declaration + parent → parent's version (legacy sticky)
* declared + no parent + new_version set → new_version
* declared + no parent + new_version missing → 500
* declared + parent + no allowed_versions → parent's version (sticky)
* declared + parent + allowed_versions containing parent's version → parent's version
* declared + parent + allowed_versions missing parent's version → 422
* declared + parent with None version + allowed_versions → 422

`_validate_content`:
* no model registered → skip silently
* valid content → no exception
* invalid content → 422 with the field error

These tests run in microseconds and catch every versioning
corner-case I could enumerate from the source. If someone ever
adds a new branch (e.g. "inherited_version: true" as a shortcut),
this file is where the new test goes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.pipeline.generated import (
    _resolve_schema_version, _validate_content,
)


def _parent_row(schema_version: str | None):
    """Build a stub parent row that only carries `.schema_version`.
    `_resolve_schema_version` doesn't touch any other attribute, and
    constructing a real EntityRow would require a full async DB
    fixture for a pure-sync test — not worth it."""
    return SimpleNamespace(schema_version=schema_version)


class TestResolveSchemaVersion:

    def test_no_declaration_no_parent_returns_none(self):
        """Legacy creation path: activity doesn't declare
        versioning for this type and the entity is fresh. Returns
        None → plugin's default `entity_models[type]` handles
        content validation, no version stamp on the row."""
        result = _resolve_schema_version(
            activity_def={},
            entity_type="oe:aanvraag",
            parent_row=None,
        )
        assert result is None

    def test_no_declaration_with_parent_returns_parent_version(self):
        """Legacy sticky revision: activity doesn't declare
        versioning, parent already has a schema_version. The new
        row inherits the parent's version unchanged. This is what
        preserves versioning across revisions even when individual
        activities are unopinionated about the schema."""
        result = _resolve_schema_version(
            activity_def={},
            entity_type="oe:aanvraag",
            parent_row=_parent_row("v1"),
        )
        assert result == "v1"

    def test_no_declaration_with_legacy_parent_returns_none(self):
        """Edge case: parent row from before versioning was
        introduced (schema_version is None in the DB). No
        declaration either. Returns None — both pre-versioning."""
        result = _resolve_schema_version(
            activity_def={},
            entity_type="oe:aanvraag",
            parent_row=_parent_row(None),
        )
        assert result is None

    def test_declared_fresh_entity_returns_new_version(self):
        """Activity declares `new_version: v2` for the type, entity
        is fresh. The new row gets stamped with v2."""
        result = _resolve_schema_version(
            activity_def={
                "name": "dienAanvraagIn",
                "entities": {
                    "oe:aanvraag": {"new_version": "v2"},
                },
            },
            entity_type="oe:aanvraag",
            parent_row=None,
        )
        assert result == "v2"

    def test_declared_fresh_entity_without_new_version_raises_500(self):
        """Activity declares `entities` for the type but forgot
        `new_version`. Trying to create a fresh entity is a
        misconfigured activity — 500, not 422. The fix is on the
        plugin side, not the client side, so it's not a user
        error."""
        with pytest.raises(ActivityError) as exc:
            _resolve_schema_version(
                activity_def={
                    "name": "dienAanvraagIn",
                    "entities": {
                        "oe:aanvraag": {"allowed_versions": ["v1", "v2"]},
                    },
                },
                entity_type="oe:aanvraag",
                parent_row=None,
            )
        assert exc.value.status_code == 500
        assert exc.value.payload["error"] == "missing_new_version_declaration"
        assert exc.value.payload["activity"] == "dienAanvraagIn"
        assert exc.value.payload["entity_type"] == "oe:aanvraag"

    def test_declared_revision_without_allowed_list_sticky(self):
        """Activity declares versioning but doesn't constrain which
        versions it's allowed to revise. Sticky behavior: inherit
        the parent's version."""
        result = _resolve_schema_version(
            activity_def={
                "entities": {
                    "oe:aanvraag": {"new_version": "v2"},
                },
            },
            entity_type="oe:aanvraag",
            parent_row=_parent_row("v1"),
        )
        assert result == "v1"

    def test_declared_revision_allowed_version_passes(self):
        """Activity declares `allowed_versions: [v1, v2]`, parent
        has v1. The revision is allowed and the new row inherits
        v1 (sticky). The `allowed_versions` check is a gate, not a
        migrator — the version only changes if `new_version` kicks
        in on a fresh creation."""
        result = _resolve_schema_version(
            activity_def={
                "entities": {
                    "oe:aanvraag": {
                        "new_version": "v2",
                        "allowed_versions": ["v1", "v2"],
                    },
                },
            },
            entity_type="oe:aanvraag",
            parent_row=_parent_row("v1"),
        )
        assert result == "v1"

    def test_declared_revision_disallowed_version_rejected(self):
        """Parent has `schema_version=v0` but the activity only
        allows `[v1, v2]`. This is the 422
        `unsupported_schema_version` path — client must upgrade
        the entity via a migration activity before this revision
        can apply."""
        with pytest.raises(ActivityError) as exc:
            _resolve_schema_version(
                activity_def={
                    "name": "bewerkAanvraag",
                    "entities": {
                        "oe:aanvraag": {
                            "new_version": "v2",
                            "allowed_versions": ["v1", "v2"],
                        },
                    },
                },
                entity_type="oe:aanvraag",
                parent_row=_parent_row("v0"),
            )
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "unsupported_schema_version"
        assert exc.value.payload["stored_version"] == "v0"
        assert exc.value.payload["allowed_versions"] == ["v1", "v2"]

    def test_declared_revision_legacy_parent_rejected_when_version_required(
        self,
    ):
        """Parent row is a pre-versioning artifact (schema_version
        is None in the DB). Activity declares
        `allowed_versions: [v1, v2]`. None is not in the list, so
        the revision is rejected with 422 — the client has to run
        a migration activity first to stamp the legacy row with
        v1, then retry the revision."""
        with pytest.raises(ActivityError) as exc:
            _resolve_schema_version(
                activity_def={
                    "entities": {
                        "oe:aanvraag": {
                            "new_version": "v2",
                            "allowed_versions": ["v1", "v2"],
                        },
                    },
                },
                entity_type="oe:aanvraag",
                parent_row=_parent_row(None),
            )
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "unsupported_schema_version"
        assert exc.value.payload["stored_version"] is None


# --------------------------------------------------------------------
# _validate_content
# --------------------------------------------------------------------


class _Aanvraag(BaseModel):
    """Stub Pydantic model for testing content validation. Matches
    the shape the toelatingen plugin uses but with just two fields."""
    titel: str
    bedrag: float


class _StubPlugin:
    """Minimal plugin stub: implements only `resolve_schema`, the
    one method `_validate_content` calls. The mapping dict lets
    each test set up exactly the (type, version) → model mapping
    it needs."""
    def __init__(self, schemas: dict[tuple[str, str | None], type | None]):
        self._schemas = schemas

    def resolve_schema(self, entity_type: str, schema_version: str | None):
        return self._schemas.get((entity_type, schema_version))


def _state_with_plugin(plugin) -> SimpleNamespace:
    """`_validate_content` reads `state.plugin.resolve_schema`.
    Nothing else on state. SimpleNamespace is sufficient."""
    return SimpleNamespace(plugin=plugin)


class TestValidateContent:

    def test_no_model_registered_skips_validation(self):
        """When `plugin.resolve_schema` returns None, content
        validation is skipped — the plugin has opted out of
        typed validation for this type. The function returns
        cleanly; the caller still persists whatever dict came in."""
        state = _state_with_plugin(_StubPlugin({}))
        # No exception even though content is obviously not a
        # valid anything:
        _validate_content(
            state,
            entity_type="oe:aanvraag",
            schema_version=None,
            content={"literally": "anything"},
        )

    def test_valid_content_passes(self):
        """Content dict matches the Pydantic model's shape.
        Function returns without raising."""
        state = _state_with_plugin(_StubPlugin({
            ("oe:aanvraag", "v1"): _Aanvraag,
        }))
        _validate_content(
            state,
            entity_type="oe:aanvraag",
            schema_version="v1",
            content={"titel": "Test aanvraag", "bedrag": 1500.0},
        )

    def test_invalid_content_raises_422(self):
        """Content dict is missing a required field. Pydantic
        raises ValidationError, the function translates it to a
        422 with the field error in the message. Clients should
        see a useful diagnostic, not an internal traceback."""
        state = _state_with_plugin(_StubPlugin({
            ("oe:aanvraag", "v1"): _Aanvraag,
        }))
        with pytest.raises(ActivityError) as exc:
            _validate_content(
                state,
                entity_type="oe:aanvraag",
                schema_version="v1",
                content={"titel": "Test aanvraag"},  # bedrag missing
            )
        assert exc.value.status_code == 422
        assert "oe:aanvraag" in str(exc.value)

    def test_wrong_type_content_raises_422(self):
        """Content has the right fields but one has the wrong type.
        Pydantic catches it, translates to 422."""
        state = _state_with_plugin(_StubPlugin({
            ("oe:aanvraag", "v1"): _Aanvraag,
        }))
        with pytest.raises(ActivityError) as exc:
            _validate_content(
                state,
                entity_type="oe:aanvraag",
                schema_version="v1",
                content={
                    "titel": "Test aanvraag",
                    "bedrag": "not a number",
                },
            )
        assert exc.value.status_code == 422

    def test_resolve_schema_receives_version_parameter(self):
        """The version parameter flows through to
        `plugin.resolve_schema(type, version)`. A plugin with
        different models per version must be able to distinguish
        them — this test proves the version parameter isn't
        accidentally dropped."""
        class _AanvraagV2(BaseModel):
            titel: str
            # v2 has an extra required field
            aanvrager_email: str

        state = _state_with_plugin(_StubPlugin({
            ("oe:aanvraag", "v1"): _Aanvraag,
            ("oe:aanvraag", "v2"): _AanvraagV2,
        }))

        # v1 content validates against v1 model (no email required).
        _validate_content(
            state, "oe:aanvraag", "v1",
            {"titel": "x", "bedrag": 100.0},
        )

        # v2 content without email fails — proves v2 model was
        # actually used.
        with pytest.raises(ActivityError):
            _validate_content(
                state, "oe:aanvraag", "v2",
                {"titel": "x", "bedrag": 100.0},
            )

        # v2 content with email passes.
        _validate_content(
            state, "oe:aanvraag", "v2",
            {"titel": "x", "aanvrager_email": "a@b.c"},
        )
