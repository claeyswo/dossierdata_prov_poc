"""
Integration tests for the route helper modules:

* `routes/_errors.py` — `activity_error_to_http` (ActivityError
  → HTTPException with merged payload)
* `routes/_serializers.py` — `entity_version_dict` (EntityRow →
  API JSON shape, including tombstone redirect logic)
* `routes/access.py` — `check_dossier_access` + `get_visibility_from_entry`
  (authorization + visibility filtering)
* `file_refs.py` — `inject_download_urls` (walks Pydantic model
  fields, finds FileId annotations, inserts signed download
  URLs as `_url` siblings)

All four modules are pure functions that can be tested without
standing up a FastAPI TestClient. The routes themselves (the
wiring that calls these helpers inside HTTP handlers) are
exercised by the E2E `test_requests.sh` suite; this file
covers the logic that can be unit-tested directly.

Why this split rather than TestClient integration tests for
the whole route layer: setting up a TestClient requires booting
a real FastAPI app with plugin registration, Postgres init,
auth middleware, global_access config, and the workflow YAML.
That's valuable but it's a one-turn setup cost to get a single
green test. The helper-level tests here hit every branch in
the shared route logic in the same time that TestClient tests
would cover maybe 3-4 routes. Later turns can layer TestClient
tests on top once the helpers are locked down.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import Optional
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.errors import ActivityError
from dossier_engine.file_refs import FileId, inject_download_urls
from dossier_engine.routes._errors import activity_error_to_http
from dossier_engine.routes._serializers import entity_version_dict
from dossier_engine.routes.access import (
    check_dossier_access, get_visibility_from_entry,
)


D1 = UUID("11111111-1111-1111-1111-111111111111")


# --------------------------------------------------------------------
# activity_error_to_http
# --------------------------------------------------------------------


class TestActivityErrorToHttp:

    def test_plain_error_without_payload(self):
        """ActivityError with only a status code and message →
        HTTPException with the message as the plain string
        detail. No payload merge because there's nothing to
        merge."""
        e = ActivityError(422, "Missing required field")
        http = activity_error_to_http(e)

        assert http.status_code == 422
        assert http.detail == "Missing required field"

    def test_error_with_payload_merges_into_dict_detail(self):
        """When ActivityError carries a payload dict, the
        HTTPException detail becomes a dict with `detail`
        (the message) plus every payload key flattened in.
        Clients switch on the `error` discriminator."""
        e = ActivityError(
            409, "Stale derivation",
            payload={
                "error": "stale_derivation",
                "stale": {"intervening_versions": 2},
            },
        )
        http = activity_error_to_http(e)

        assert http.status_code == 409
        assert isinstance(http.detail, dict)
        assert http.detail["detail"] == "Stale derivation"
        assert http.detail["error"] == "stale_derivation"
        assert http.detail["stale"] == {"intervening_versions": 2}

    def test_empty_payload_dict_treated_as_no_payload(self):
        """An empty dict in `payload` is falsy, so the phase
        takes the no-payload branch and emits a plain-string
        detail. Documenting current behavior."""
        e = ActivityError(400, "Bad request", payload={})
        http = activity_error_to_http(e)

        assert http.status_code == 400
        assert http.detail == "Bad request"

    def test_various_status_codes_propagated(self):
        for code in (400, 401, 403, 404, 409, 422, 500):
            e = ActivityError(code, f"error-{code}")
            assert activity_error_to_http(e).status_code == code


# --------------------------------------------------------------------
# entity_version_dict
# --------------------------------------------------------------------


_UNSET = object()


def _row(
    *,
    id: UUID | None = None,
    entity_id: UUID | None = None,
    content=_UNSET,
    generated_by: UUID | None = None,
    derived_from: UUID | None = None,
    attributed_to: str = "system",
    created_at: datetime | None = None,
    schema_version: str | None = None,
    tombstoned_by: UUID | None = None,
):
    """Build an EntityRow-shaped SimpleNamespace for serializer
    tests. The serializer only reads attributes — it doesn't
    need a real SQLAlchemy row.

    `content=_UNSET` (default) yields `{"k": "v"}`; passing
    `content=None` explicitly yields None (so tombstone tests
    can seed a redacted row)."""
    return SimpleNamespace(
        id=id or uuid4(),
        entity_id=entity_id or uuid4(),
        content={"k": "v"} if content is _UNSET else content,
        generated_by=generated_by or uuid4(),
        derived_from=derived_from,
        attributed_to=attributed_to,
        created_at=created_at or datetime.now(timezone.utc),
        schema_version=schema_version,
        tombstoned_by=tombstoned_by,
    )


class TestEntityVersionDict:

    def test_basic_shape(self):
        """Happy path: a live entity row renders to a dict with
        versionId, content, generatedBy, derivedFrom,
        attributedTo, createdAt, entityId. No schemaVersion
        (legacy NULL) and no tombstone fields."""
        eid = uuid4()
        vid = uuid4()
        gen_by = uuid4()
        row = _row(
            id=vid, entity_id=eid, content={"titel": "Test"},
            generated_by=gen_by,
        )

        result = entity_version_dict(
            row, D1, "oe:aanvraag", siblings=[],
        )

        assert result["versionId"] == str(vid)
        assert result["entityId"] == str(eid)
        assert result["content"] == {"titel": "Test"}
        assert result["generatedBy"] == str(gen_by)
        assert result["derivedFrom"] is None
        assert result["attributedTo"] == "system"
        assert "createdAt" in result
        assert "schemaVersion" not in result  # legacy NULL drops
        assert "tombstonedBy" not in result

    def test_include_entity_id_false_omits_key(self):
        """When include_entity_id=False (caller is rendering
        inside a list keyed by entity_id already), the
        entityId field is omitted to avoid redundancy."""
        row = _row()
        result = entity_version_dict(
            row, D1, "oe:aanvraag", siblings=[],
            include_entity_id=False,
        )
        assert "entityId" not in result

    def test_schema_version_included_when_set(self):
        row = _row(schema_version="v2")
        result = entity_version_dict(row, D1, "oe:aanvraag", siblings=[])
        assert result["schemaVersion"] == "v2"

    def test_derived_from_stringified_when_set(self):
        parent = uuid4()
        row = _row(derived_from=parent)
        result = entity_version_dict(row, D1, "oe:aanvraag", siblings=[])
        assert result["derivedFrom"] == str(parent)

    def test_tombstoned_row_with_live_replacement(self):
        """Tombstoned row in a list with a live sibling: the
        result has content=None (redacted), tombstonedBy set
        to the redacting activity, and redirectTo pointing at
        the live sibling's URL."""
        eid = uuid4()
        tomb_activity = uuid4()
        base_time = datetime.now(timezone.utc)

        tombstoned = _row(
            entity_id=eid,
            content=None,
            tombstoned_by=tomb_activity,
            created_at=base_time,
        )
        live_replacement = _row(
            entity_id=eid,
            content={"titel": "redacted replacement"},
            created_at=base_time + timedelta(seconds=1),
        )

        result = entity_version_dict(
            tombstoned, D1, "oe:aanvraag",
            siblings=[tombstoned, live_replacement],
        )

        assert result["content"] is None
        assert result["tombstonedBy"] == str(tomb_activity)
        assert result["redirectTo"] is not None
        assert f"/{live_replacement.id}" in result["redirectTo"]
        assert str(D1) in result["redirectTo"]

    def test_tombstoned_row_with_only_tombstoned_siblings(self):
        """Re-tombstoning: every sibling is also tombstoned.
        The phase falls back to picking the latest
        (tombstoned) sibling rather than returning no
        redirectTo. That matches the docstring's 'fall back to
        all siblings if everything has been tombstoned'."""
        eid = uuid4()
        base = datetime.now(timezone.utc)

        older_tomb = _row(
            entity_id=eid, content=None, tombstoned_by=uuid4(),
            created_at=base,
        )
        newer_tomb = _row(
            entity_id=eid, content=None, tombstoned_by=uuid4(),
            created_at=base + timedelta(seconds=1),
        )

        result = entity_version_dict(
            older_tomb, D1, "oe:aanvraag",
            siblings=[older_tomb, newer_tomb],
        )

        assert result["redirectTo"] is not None
        assert f"/{newer_tomb.id}" in result["redirectTo"]

    def test_tombstoned_row_with_no_siblings_omits_redirect(self):
        """Edge case: the tombstoned row is the only one in its
        sibling list (shouldn't happen in practice since tombstones
        always have a replacement, but defensive). The dict
        carries tombstonedBy but no redirectTo."""
        tomb = _row(content=None, tombstoned_by=uuid4())

        result = entity_version_dict(
            tomb, D1, "oe:aanvraag", siblings=[tomb],
        )

        assert result["content"] is None
        assert "tombstonedBy" in result
        assert "redirectTo" not in result

    def test_live_row_with_siblings_no_redirect(self):
        """A non-tombstoned row should never carry redirectTo,
        even if its siblings include tombstoned entries. The
        redirect only applies to the tombstoned view of the
        row."""
        eid = uuid4()
        live = _row(entity_id=eid, tombstoned_by=None)
        tomb_sibling = _row(entity_id=eid, tombstoned_by=uuid4())

        result = entity_version_dict(
            live, D1, "oe:aanvraag",
            siblings=[live, tomb_sibling],
        )

        assert "redirectTo" not in result
        assert "tombstonedBy" not in result


# --------------------------------------------------------------------
# check_dossier_access + get_visibility_from_entry
# --------------------------------------------------------------------


def _user(user_id: str = "u1", *roles: str) -> User:
    return User(
        id=user_id, type="natuurlijk_persoon", name="Test",
        roles=list(roles), properties={},
    )


async def _bootstrap(repo: Repository) -> UUID:
    await repo.create_dossier(D1, "toelatingen")
    await repo.ensure_agent("system", "systeem", "Systeem", {})
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="systemAction",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


async def _seed_access_entity(
    repo: Repository, generated_by: UUID, access_entries: list[dict],
) -> None:
    """Seed a singleton oe:dossier_access entity with the
    given access control block."""
    await repo.create_entity(
        version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
        type="oe:dossier_access", generated_by=generated_by,
        content={"access": access_entries}, attributed_to="system",
    )
    await repo.session.flush()


class TestCheckDossierAccess:

    async def test_no_access_entity_returns_none(self, repo):
        """If there's no oe:dossier_access entity in the dossier,
        no access restrictions apply — every authenticated user
        gets through with entry=None."""
        await _bootstrap(repo)
        result = await check_dossier_access(repo, D1, _user())
        assert result is None

    async def test_global_access_role_match(self, repo):
        """global_access matches first — before any dossier-
        specific check. A user with the `oe:admin` role hits
        the global grant and gets its entry."""
        await _bootstrap(repo)
        global_access = [
            {"role": "oe:admin", "view": ["oe:aanvraag"]},
        ]
        result = await check_dossier_access(
            repo, D1, _user("alice", "oe:admin"),
            global_access=global_access,
        )
        assert result == {"role": "oe:admin", "view": ["oe:aanvraag"]}

    async def test_global_access_miss_falls_through_to_dossier_entity(
        self, repo,
    ):
        """global_access is checked but no entry matches the
        user's roles. Phase falls through to look at the
        dossier's access entity, which does grant access."""
        boot = await _bootstrap(repo)
        await _seed_access_entity(repo, boot, [
            {"role": "oe:reader", "view": ["oe:aanvraag"]},
        ])

        global_access = [{"role": "oe:admin"}]  # user isn't admin

        result = await check_dossier_access(
            repo, D1, _user("u", "oe:reader"),
            global_access=global_access,
        )
        assert result is not None
        assert result["role"] == "oe:reader"

    async def test_dossier_access_role_match(self, repo):
        boot = await _bootstrap(repo)
        await _seed_access_entity(repo, boot, [
            {"role": "oe:behandelaar", "activity_view": "all"},
        ])

        result = await check_dossier_access(
            repo, D1, _user("u", "oe:behandelaar"),
        )
        assert result is not None
        assert result["role"] == "oe:behandelaar"
        assert result["activity_view"] == "all"

    async def test_dossier_access_agent_id_match(self, repo):
        """A user matches an access entry via the `agents` list
        (explicit user id grant) rather than a role. Useful for
        per-user access when roles don't apply."""
        boot = await _bootstrap(repo)
        await _seed_access_entity(repo, boot, [
            {"agents": ["alice"], "activity_view": "own"},
        ])

        result = await check_dossier_access(repo, D1, _user("alice"))
        assert result is not None
        assert result["activity_view"] == "own"

    async def test_no_match_raises_403(self, repo):
        """Access entity exists with entries, but the user
        matches neither a role nor an agent list → 403."""
        boot = await _bootstrap(repo)
        await _seed_access_entity(repo, boot, [
            {"role": "oe:admin", "activity_view": "all"},
        ])

        with pytest.raises(HTTPException) as exc:
            await check_dossier_access(repo, D1, _user("stranger"))
        assert exc.value.status_code == 403

    async def test_empty_access_entity_content_returns_none(self, repo):
        """An oe:dossier_access row exists but its content is
        null or empty. Treated as 'no restrictions' — same as
        having no access entity at all."""
        boot = await _bootstrap(repo)
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
            type="oe:dossier_access", generated_by=boot,
            content=None, attributed_to="system",
        )
        await repo.session.flush()

        result = await check_dossier_access(repo, D1, _user())
        assert result is None


class TestGetVisibilityFromEntry:

    def test_none_entry_no_restrictions(self):
        """No matched entry (access is fully open) → no visible
        type filter, activity_view defaults to 'all'."""
        visible, mode = get_visibility_from_entry(None)
        assert visible is None
        assert mode == "all"

    def test_entry_with_no_view_key_no_restrictions(self):
        """Matched entry but it doesn't carry a `view` key →
        None for visible types (sees all) but honors the entry's
        activity_view if set."""
        visible, mode = get_visibility_from_entry(
            {"role": "oe:admin", "activity_view": "own"},
        )
        assert visible is None
        assert mode == "own"

    def test_entry_with_empty_view_sees_nothing(self):
        """Entry has `view: []` — an explicit 'see nothing'
        setting. Empty set, not None. Distinguishing these two
        matters: None means 'no filter', empty set means 'filter
        matches nothing'."""
        visible, mode = get_visibility_from_entry(
            {"view": [], "activity_view": "all"},
        )
        assert visible == set()
        assert mode == "all"

    def test_entry_with_view_list_converts_to_set(self):
        visible, mode = get_visibility_from_entry(
            {"view": ["oe:aanvraag", "oe:beslissing"]},
        )
        assert visible == {"oe:aanvraag", "oe:beslissing"}
        assert mode == "all"  # default when not specified

    def test_entry_with_activity_view_own(self):
        visible, mode = get_visibility_from_entry(
            {"view": ["oe:aanvraag"], "activity_view": "own"},
        )
        assert mode == "own"

    def test_entry_with_activity_view_related(self):
        visible, mode = get_visibility_from_entry(
            {"view": ["oe:aanvraag"], "activity_view": "related"},
        )
        assert mode == "related"


# --------------------------------------------------------------------
# inject_download_urls
# --------------------------------------------------------------------


class _SimpleModel(BaseModel):
    """A plain model with no FileId fields. inject_download_urls
    should pass through unchanged."""
    titel: str
    bedrag: float = 0.0


class _WithFileIdModel(BaseModel):
    """Model with a FileId field. inject_download_urls should
    inject a `file_download_url` sibling for any set field_id."""
    naam: str
    file_id: FileId


class _WithOptionalFileIdModel(BaseModel):
    """FileId can be Optional — the phase must handle None
    without emitting an empty sibling."""
    naam: str
    file_id: Optional[FileId] = None


class _NestedModel(BaseModel):
    """A nested model: the parent has a list of children, each
    with a FileId. inject_download_urls should recurse."""
    titel: str
    bijlagen: list[_WithFileIdModel]


def _fake_sign(file_id: str) -> str:
    """Fake signer returning a deterministic URL string for
    assertion-friendly tests."""
    return f"https://files.example/download/{file_id}?sig=fake"


class TestInjectDownloadUrls:

    def test_none_model_passes_through(self):
        """When no model class is registered for the entity
        type, the content is returned unchanged — we can't
        walk the shape to find FileId fields without the
        model."""
        content = {"arbitrary": {"nested": [1, 2, 3]}}
        result = inject_download_urls(None, content, _fake_sign)
        assert result is content

    def test_none_content_passes_through(self):
        """Tombstoned entities have content=None — the helper
        must handle that gracefully and return None without
        crashing."""
        result = inject_download_urls(_SimpleModel, None, _fake_sign)
        assert result is None

    def test_non_dict_content_passes_through(self):
        """Defensive: if content is a string or int for some
        reason, the phase returns it unchanged."""
        assert inject_download_urls(_SimpleModel, "abc", _fake_sign) == "abc"
        assert inject_download_urls(_SimpleModel, 42, _fake_sign) == 42

    def test_model_with_no_file_id_fields_no_injection(self):
        """A model with no FileId fields produces output
        identical to input (modulo dict-copy semantics)."""
        content = {"titel": "Aanvraag", "bedrag": 100.0}
        result = inject_download_urls(_SimpleModel, content, _fake_sign)
        assert result == {"titel": "Aanvraag", "bedrag": 100.0}

    def test_file_id_field_gets_url_sibling(self):
        """The model has `file_id: FileId`. The output retains
        the original field plus an added `file_download_url` sibling
        carrying the signed download URL."""
        content = {"naam": "bijlage.pdf", "file_id": "abc-123"}
        result = inject_download_urls(_WithFileIdModel, content, _fake_sign)

        assert result["naam"] == "bijlage.pdf"
        assert result["file_id"] == "abc-123"
        assert result["file_download_url"] == "https://files.example/download/abc-123?sig=fake"

    def test_optional_file_id_none_no_sibling(self):
        """Optional FileId set to None: no sibling URL added.
        Adding `file_download_url: null` would be noise for clients."""
        content = {"naam": "bijlage.pdf", "file_id": None}
        result = inject_download_urls(
            _WithOptionalFileIdModel, content, _fake_sign,
        )

        assert result["file_id"] is None
        assert "file_download_url" not in result

    def test_unknown_field_passed_through_unchanged(self):
        """The content has an extra key the model doesn't know
        about. Pass-through — legacy fields survive."""
        content = {
            "naam": "test",
            "file_id": "xyz",
            "legacy_field": "should_survive",
        }
        result = inject_download_urls(_WithFileIdModel, content, _fake_sign)

        assert result["legacy_field"] == "should_survive"
        assert result["file_download_url"] == "https://files.example/download/xyz?sig=fake"

    def test_nested_model_recurses(self):
        """A parent model with a list of children that each
        have a FileId. The walker recurses into the list and
        injects URLs for each child's file_id."""
        content = {
            "titel": "Aanvraag met bijlagen",
            "bijlagen": [
                {"naam": "foto1.jpg", "file_id": "f1"},
                {"naam": "foto2.jpg", "file_id": "f2"},
            ],
        }
        result = inject_download_urls(_NestedModel, content, _fake_sign)

        assert result["titel"] == "Aanvraag met bijlagen"
        assert len(result["bijlagen"]) == 2
        assert result["bijlagen"][0]["file_download_url"] == "https://files.example/download/f1?sig=fake"
        assert result["bijlagen"][1]["file_download_url"] == "https://files.example/download/f2?sig=fake"
