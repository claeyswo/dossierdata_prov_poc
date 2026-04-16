"""
HTTP-level route tests using `httpx.AsyncClient` with FastAPI's
`ASGITransport`.

These tests stand up a minimal FastAPI app with the
dossier_engine routes registered and drive them through httpx's
async client, which calls the app's ASGI interface directly in
the same event loop as pytest-asyncio. That keeps the database
engine singleton usable across the test-body seeding and the
route-handler execution — no cross-loop trap, no separate thread.

Why `AsyncClient` over `TestClient`: FastAPI's `TestClient` runs
handlers in a background thread with its own event loop (via
Starlette's `BlockingPortal`). The pre-initialized asyncpg
engine from `conftest.py` is bound to pytest-asyncio's loop, so
TestClient handlers using it hit "Task got Future attached to a
different loop". `AsyncClient(transport=ASGITransport(app=app))`
avoids this by executing the ASGI call inside the current
async context.

Rather than loading the real `dossier_toelatingen` plugin from
config.yaml, the `_build_test_app()` helper constructs a minimal
synthetic plugin with just what the routes need: a workflow
name, an entity_types block, the SYSTEM_ACTION_DEF, and nothing
else. That keeps test failures pinned to route logic rather
than plugin logic.

Routes covered:

* `GET /dossiers` — list, optional workflow filter, auth
* `GET /dossiers/{id}` — detail with access check
* `GET /dossiers/{id}/entities/{type}` — all versions of a type
* `GET /dossiers/{id}/entities/{type}/{entity_id}` — logical entity
* `GET /dossiers/{id}/entities/{type}/{entity_id}/{version_id}` —
  single version, tombstone 301 redirect
* `POST /files/upload/request` — signed upload URL minting

The PUT activity route and the prov routes are not covered here
— they're the biggest routes in the subpackage and deserve their
own focused turn.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dossier_engine.auth import POCAuthMiddleware
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.entities import SYSTEM_ACTION_DEF, SystemNote, TaskEntity
from dossier_engine.plugin import Plugin, PluginRegistry
from dossier_engine.routes import register_routes


D1 = UUID("11111111-1111-1111-1111-111111111111")
D2 = UUID("22222222-2222-2222-2222-222222222222")


def _build_test_app() -> FastAPI:
    """Build a minimal FastAPI app with test plugin + routes.

    Test plugin shape:
    * workflow name "test"
    * multi-cardinality oe:aanvraag, oe:bijlage
    * singleton oe:dossier_access
    * SYSTEM_ACTION_DEF in activities
    * No handlers / validators / task_handlers — the GET routes
      we test here don't need them.

    Two POC users are registered:
    * alice — no roles, normal user
    * admin — roles=["oe:admin"], matches global_access
    """
    plugin = Plugin(
        name="test",
        workflow={
            "name": "test",
            "activities": [SYSTEM_ACTION_DEF],
            "entity_types": [
                {"type": "oe:aanvraag", "cardinality": "multiple"},
                {"type": "oe:bijlage", "cardinality": "multiple"},
                {"type": "oe:dossier_access", "cardinality": "single"},
                {"type": "system:task", "cardinality": "multiple"},
                {"type": "system:note", "cardinality": "multiple"},
            ],
            "relations": [],
            "poc_users": [],
        },
        entity_models={
            "system:task": TaskEntity,
            "system:note": SystemNote,
        },
    )

    registry = PluginRegistry()
    registry.register(plugin)

    auth = POCAuthMiddleware([
        {
            "id": "alice", "username": "alice",
            "type": "natuurlijk_persoon", "name": "Alice",
            "roles": ["oe:reader"], "properties": {},
        },
        {
            "id": "admin", "username": "admin",
            "type": "natuurlijk_persoon", "name": "Admin",
            "roles": ["oe:admin"], "properties": {},
        },
    ])

    app = FastAPI()
    app.state.registry = registry
    app.state.config = {
        "file_service": {
            "url": "http://test.local:8001",
            "signing_key": "test-key",
        },
    }
    register_routes(
        app, registry, auth,
        global_access=[
            {"role": "oe:admin", "view": "all", "activity_view": "all"},
            {"role": "oe:reader", "view": "all", "activity_view": "all"},
        ],
    )
    return app


@pytest_asyncio.fixture
async def client():
    """Yield an httpx AsyncClient wired to the test FastAPI app
    via ASGITransport. The app runs in pytest-asyncio's loop
    so it shares the DB engine singleton with the rest of the
    test fixtures — no cross-loop trap."""
    app = _build_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
    ) as c:
        yield c


async def _bootstrap_dossier(
    repo: Repository, dossier_id: UUID = D1, workflow: str = "test",
) -> UUID:
    """Create a dossier + bootstrap systemAction activity.
    Does NOT commit — caller calls `_commit(repo)` after seeding."""
    await repo.create_dossier(dossier_id, workflow)
    await repo.ensure_agent("system", "systeem", "Systeem", {})
    await repo.ensure_agent("alice", "natuurlijk_persoon", "Alice", {})
    await repo.ensure_agent("admin", "natuurlijk_persoon", "Admin", {})
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=dossier_id, type="systemAction",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


async def _seed_entity(
    repo: Repository,
    generated_by: UUID,
    entity_type: str,
    *,
    dossier_id: UUID = D1,
    entity_id: UUID | None = None,
    content: dict | None = None,
):
    eid = entity_id or uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=dossier_id,
        type=entity_type, generated_by=generated_by,
        content=content if content is not None else {"k": "v"},
        attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid


async def _commit(repo: Repository) -> None:
    """Commit pending writes so the HTTP handlers' fresh
    sessions see the data."""
    await repo.session.commit()


# --------------------------------------------------------------------
# Auth header handling
# --------------------------------------------------------------------


class TestAuthHeader:

    async def test_missing_header_returns_401(self, client, repo):
        await _commit(repo)
        r = await client.get("/dossiers")
        assert r.status_code == 401
        assert "X-POC-User" in r.json()["detail"]

    async def test_unknown_user_returns_401(self, client, repo):
        await _commit(repo)
        r = await client.get("/dossiers", headers={"X-POC-User": "nobody"})
        assert r.status_code == 401
        assert "Unknown" in r.json()["detail"]

    async def test_known_user_passes_auth(self, client, repo):
        await _commit(repo)
        r = await client.get("/dossiers", headers={"X-POC-User": "alice"})
        assert r.status_code == 200


# --------------------------------------------------------------------
# GET /dossiers (list)
# --------------------------------------------------------------------


class TestListDossiers:

    async def test_returns_all_dossiers(self, client, repo):
        await _bootstrap_dossier(repo, D1, "test")
        await _bootstrap_dossier(repo, D2, "test")
        await _commit(repo)

        r = await client.get("/dossiers", headers={"X-POC-User": "alice"})
        assert r.status_code == 200
        body = r.json()
        assert "dossiers" in body
        assert len(body["dossiers"]) == 2
        ids = {d["id"] for d in body["dossiers"]}
        assert str(D1) in ids
        assert str(D2) in ids

    async def test_workflow_filter(self, client, repo):
        await _bootstrap_dossier(repo, D1, "test")
        await _bootstrap_dossier(repo, D2, "other")
        await _commit(repo)

        r = await client.get(
            "/dossiers?workflow=other",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["dossiers"]) == 1
        assert body["dossiers"][0]["id"] == str(D2)

    async def test_empty_list_when_no_dossiers(self, client, repo):
        await _commit(repo)
        r = await client.get("/dossiers", headers={"X-POC-User": "alice"})
        assert r.status_code == 200
        assert r.json()["dossiers"] == []


# --------------------------------------------------------------------
# GET /dossiers/{id}
# --------------------------------------------------------------------


class TestGetDossierDetail:

    async def test_missing_dossier_returns_404(self, client, repo):
        await _commit(repo)
        r = await client.get(
            f"/dossiers/{uuid4()}",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404

    async def test_existing_dossier_returns_detail(self, client, repo):
        await _bootstrap_dossier(repo)
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}", headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == str(D1)
        assert body["workflow"] == "test"
        assert "status" in body
        assert "allowedActivities" in body


# --------------------------------------------------------------------
# GET /dossiers/{id}/entities/{type}
# --------------------------------------------------------------------


class TestGetEntitiesByType:

    async def test_returns_all_versions(self, client, repo):
        boot = await _bootstrap_dossier(repo)
        eid = uuid4()
        await _seed_entity(
            repo, boot, "oe:aanvraag",
            entity_id=eid, content={"titel": "v1"},
        )
        await asyncio.sleep(0.002)
        await _seed_entity(
            repo, boot, "oe:aanvraag",
            entity_id=eid, content={"titel": "v2"},
        )
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["versions"]) == 2
        assert body["versions"][0]["content"] == {"titel": "v1"}
        assert body["versions"][1]["content"] == {"titel": "v2"}

    async def test_no_entities_returns_404(self, client, repo):
        await _bootstrap_dossier(repo)
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:absent",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404
        assert "oe:absent" in r.json()["detail"]

    async def test_missing_dossier_returns_404(self, client, repo):
        await _commit(repo)
        r = await client.get(
            f"/dossiers/{uuid4()}/entities/oe:aanvraag",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404


# --------------------------------------------------------------------
# GET /dossiers/{id}/entities/{type}/{entity_id}
# --------------------------------------------------------------------


class TestGetLogicalEntityVersions:

    async def test_returns_versions_of_one_logical_entity(self, client, repo):
        boot = await _bootstrap_dossier(repo)
        target_eid = uuid4()
        other_eid = uuid4()
        await _seed_entity(
            repo, boot, "oe:aanvraag",
            entity_id=target_eid, content={"titel": "target"},
        )
        await _seed_entity(
            repo, boot, "oe:aanvraag",
            entity_id=other_eid, content={"titel": "other"},
        )
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{target_eid}",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["versions"]) == 1
        assert body["versions"][0]["content"] == {"titel": "target"}
        assert body["entity_id"] == str(target_eid)

    async def test_entity_not_found_returns_404(self, client, repo):
        boot = await _bootstrap_dossier(repo)
        await _seed_entity(repo, boot, "oe:aanvraag")
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{uuid4()}",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404


# --------------------------------------------------------------------
# GET /dossiers/{id}/entities/{type}/{entity_id}/{version_id}
# --------------------------------------------------------------------


class TestGetEntityVersion:

    async def test_returns_single_version(self, client, repo):
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(
            repo, boot, "oe:aanvraag", content={"titel": "sole"},
        )
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{eid}/{vid}",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["versionId"] == str(vid)
        assert body["content"] == {"titel": "sole"}

    async def test_tombstoned_version_redirects_301(self, client, repo):
        """Tombstoned version → 301 redirect. The response
        location points at the latest (replacement) version."""
        boot = await _bootstrap_dossier(repo)
        eid = uuid4()
        _, tomb_vid = await _seed_entity(
            repo, boot, "oe:aanvraag",
            entity_id=eid, content={"titel": "to be redacted"},
        )
        await asyncio.sleep(0.002)
        _, replacement_vid = await _seed_entity(
            repo, boot, "oe:aanvraag",
            entity_id=eid, content={"titel": "redacted replacement"},
        )
        # Retroactively tombstone the first version
        first_row = await repo.get_entity(tomb_vid)
        first_row.tombstoned_by = boot
        first_row.content = None
        await repo.session.flush()
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{eid}/{tomb_vid}",
            headers={"X-POC-User": "alice"},
            follow_redirects=False,
        )
        assert r.status_code == 301
        assert str(replacement_vid) in r.headers["location"]

    async def test_unknown_version_returns_404(self, client, repo):
        boot = await _bootstrap_dossier(repo)
        eid, _ = await _seed_entity(repo, boot, "oe:aanvraag")
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{eid}/{uuid4()}",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404

    async def test_wrong_type_returns_404(self, client, repo):
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")
        await _seed_entity(repo, boot, "oe:bijlage")
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:bijlage/{eid}/{vid}",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404


# --------------------------------------------------------------------
# POST /files/upload/request
# --------------------------------------------------------------------


class TestFileUploadRequest:

    async def test_returns_signed_upload_url(self, client, repo):
        await _commit(repo)
        r = await client.post(
            "/files/upload/request",
            json={"filename": "test.pdf"},
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "file_id" in body
        assert "upload_url" in body
        assert body["filename"] == "test.pdf"
        assert body["file_id"] in body["upload_url"]
        # The signature query-string key is `signature=`, not `sig=`.
        assert "signature=" in body["upload_url"]
        assert "http://test.local:8001" in body["upload_url"]

    async def test_missing_filename_still_works(self, client, repo):
        await _commit(repo)
        r = await client.post(
            "/files/upload/request",
            json={},
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["filename"] == ""
        assert "file_id" in body

    async def test_unauthenticated_returns_401(self, client, repo):
        await _commit(repo)
        r = await client.post(
            "/files/upload/request",
            json={"filename": "x"},
        )
        assert r.status_code == 401
