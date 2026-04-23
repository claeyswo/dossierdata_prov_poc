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
from dossier_engine.file_refs import FileId
from dossier_engine.plugin import Plugin, PluginRegistry
from dossier_engine.routes import register_routes
from pydantic import BaseModel


D1 = UUID("11111111-1111-1111-1111-111111111111")
D2 = UUID("22222222-2222-2222-2222-222222222222")


class _TestBijlage(BaseModel):
    """Minimal Bijlage stand-in with a single FileId field. Mirrors
    the real ``dossier_toelatingen.Bijlage`` shape for the fields
    that the ``inject_download_urls`` walker cares about.
    ``file_id`` is a ``FileId`` so a ``file_download_url`` sibling
    is auto-injected into the response content (Bug 57)."""
    file_id: FileId
    filename: str = ""


class _TestAanvraag(BaseModel):
    """Minimal Aanvraag stand-in with a nested list of ``_TestBijlage``.
    Used by the Bug-57 regression test to exercise download-URL
    injection through a list-of-submodels, which is the shape
    that appears in real dossier_toelatingen entities."""
    titel: str = ""
    bijlagen: list[_TestBijlage] = []


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
            # oe:aanvraag needs a real Pydantic model with a FileId
            # field so the Bug-57 regression test can assert
            # download-URL injection actually fires. The shape matches
            # the real dossier_toelatingen Aanvraag/Bijlage pair (a
            # top-level entity with a list of nested FileId-carrying
            # children), which is the interesting case — it exercises
            # both the top-level walker and the nested recursion in
            # ``inject_download_urls``.
            "oe:aanvraag": _TestAanvraag,
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
        # Bug 9 tests need a user whose roles do NOT match the test
        # app's ``global_access`` entries — otherwise ``check_dossier_access``
        # short-circuits on global_access and never consults the
        # per-dossier ``oe:dossier_access`` entity where ``activity_view``
        # actually lives. ``citizen`` has role ``aanvrager`` (not in
        # global_access) so per-dossier access rules apply.
        {
            "id": "citizen", "username": "citizen",
            "type": "natuurlijk_persoon", "name": "Citizen",
            "roles": ["aanvrager"], "properties": {},
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
    """/dossiers reads from the dossiers-common Elasticsearch index.
    When ES isn't configured (the test setup here), the endpoint
    returns 200 with an empty list and a `reason` field — dossiers
    existing in Postgres don't leak through without an index."""

    async def test_returns_empty_when_es_not_configured(self, client, repo):
        """ES isn't set up in tests → empty result regardless of
        what Postgres holds. The reason field explains why."""
        await _bootstrap_dossier(repo, D1, "test")
        await _bootstrap_dossier(repo, D2, "test")
        await _commit(repo)

        r = await client.get("/dossiers", headers={"X-POC-User": "alice"})
        assert r.status_code == 200
        body = r.json()
        assert "dossiers" in body
        assert body["dossiers"] == []
        assert body["total"] == 0
        assert "reason" in body
        assert "not configured" in body["reason"].lower()

    async def test_workflow_filter_same_empty_behavior(self, client, repo):
        """Even with a workflow filter, no ES means no hits."""
        await _bootstrap_dossier(repo, D1, "test")
        await _bootstrap_dossier(repo, D2, "other")
        await _commit(repo)

        r = await client.get(
            "/dossiers?workflow=other",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["dossiers"] == []

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
# Bug 9 — N+1 in dossier detail view
# --------------------------------------------------------------------


async def _seed_extra_activity(
    repo: Repository,
    *,
    dossier_id: UUID = D1,
    activity_type: str = "custom",
    agent_id: str = "system",
    agent_name: str = "Systeem",
    agent_type: str = "systeem",
) -> UUID:
    """Create a non-systemAction activity with a single association
    row. Returns the new activity's id. Caller commits via
    ``_commit(repo)`` after seeding all rows. Used by the Bug-9
    tests to build a dossier timeline with a controlled mix of
    system- and user-authored activities."""
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=dossier_id, type=activity_type,
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id=agent_id,
        agent_name=agent_name, agent_type=agent_type, role="test",
    ))
    await repo.session.flush()
    return act_id


async def _seed_access_entity(
    repo: Repository,
    *,
    generated_by: UUID,
    access_entries: list[dict],
    dossier_id: UUID = D1,
) -> UUID:
    """Seed an ``oe:dossier_access`` singleton with the given access
    entries. The entries are the list stored under ``content.access``
    and control who may see the dossier and, via ``activity_view``,
    which activities they see in the timeline."""
    eid, _ = await _seed_entity(
        repo, generated_by, "oe:dossier_access",
        dossier_id=dossier_id,
        content={"access": access_entries},
    )
    return eid


class TestDossierDetailActivityViewFiltering:
    """Bug 9: pin the activity-view filtering behavior before the N+1
    refactor lands. These tests cover the branches where the N+1
    actually fires — ``activity_view: "own"`` and ``activity_view:
    "related"``. The existing ``TestGetDossierDetail`` class only
    exercises the ``"all"`` fast path (alice's ``oe:reader`` role
    matches the test app's ``global_access`` entry, which grants
    ``activity_view: "all"``). The N+1 is in the per-activity
    ``_is_agent`` / ``_used_ids`` closures, so the bug never fires
    for ``"all"`` — hence the need for dedicated tests.

    These are behavior-pinning tests: they assert which activities
    are visible, not how many queries are run. The refactor to
    ``load_dossier_graph_rows`` must preserve visibility exactly.
    A separate test below (``test_query_count_bounded_under_own``)
    pins the perf claim explicitly so a future regression that
    re-introduces the N+1 goes red on that specific assertion.
    """

    async def test_own_mode_filters_to_activities_where_user_is_agent(
        self, client, repo,
    ):
        """Under ``activity_view: "own"``, the citizen sees only
        activities where they are the PROV agent. System-authored
        activities are hidden. This is the aanvrager case: the
        citizen sees their own submissions but not the staff's
        review activities."""
        # Bootstrap the dossier (creates a systemAction with agent=system).
        boot_act = await _bootstrap_dossier(repo)
        # Register the citizen as an agent row in the DB (needed so
        # the association row below passes the FK check).
        await repo.ensure_agent("citizen", "natuurlijk_persoon", "Citizen", {})
        # Citizen authored one activity.
        citizen_act = await _seed_extra_activity(
            repo, activity_type="dienAanvraagIn",
            agent_id="citizen", agent_name="Citizen",
            agent_type="natuurlijk_persoon",
        )
        # System authored one activity (e.g. setDossierAccess).
        system_act = await _seed_extra_activity(
            repo, activity_type="setDossierAccess",
        )
        # Seed a dossier-access entity that grants citizen "own" view.
        # Citizen's role ``aanvrager`` is NOT in global_access, so the
        # per-dossier entry is what matches.
        await _seed_access_entity(
            repo, generated_by=boot_act,
            access_entries=[
                {"role": "aanvrager", "view": ["oe:aanvraag"],
                 "activity_view": "own"},
            ],
        )
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}", headers={"X-POC-User": "citizen"},
        )
        assert r.status_code == 200
        visible_ids = {a["id"] for a in r.json()["activities"]}

        assert str(citizen_act) in visible_ids, \
            "citizen's own activity must be visible under 'own' mode"
        assert str(boot_act) not in visible_ids, \
            "bootstrap systemAction (agent=system) hidden under 'own'"
        assert str(system_act) not in visible_ids, \
            "setDossierAccess (agent=system) hidden under 'own'"

    # Round 31: ``test_related_mode_includes_activities_touching_visible_entities``
    # was removed when the ``"related"`` mode itself was removed. The
    # deprecation is pinned in ``tests/unit/test_activity_visibility.py::
    # TestParseActivityView::test_related_string_falls_through_to_deny_safe``
    # (parse layer) and in the new Pydantic rejection tests for
    # ``DossierAccessEntry`` (write layer). A pre-Round-31 config
    # still carrying ``activity_view: "related"`` results in an empty
    # activity timeline at read time — deny-safe — rather than silent
    # semantic change.


class TestDossierDetailQueryCount:
    """Bug 9: pin the perf claim. A dossier with N activities under
    ``activity_view: "own"`` must issue O(1) queries total, not O(N).

    Mechanism: SQLAlchemy's ``before_cursor_execute`` event fires
    once per statement the engine dispatches. We count SELECTs only
    (DDL/transaction-control statements are noise for this
    assertion). The pre-Bug-9-fix count would be roughly
    ``4 + 2*N`` for N activities under 'own'/'related'; the
    post-fix count is a constant ~5-7.

    The assertion uses a ceiling, not an exact number, because:
    (a) incidental query-count changes elsewhere in the handler
    shouldn't false-red this test; (b) the specific query layout
    may evolve. What we pin is "N must not appear as a factor." A
    ceiling of 20 for N=10 activities still catches any regression
    that re-introduces per-activity DB round-trips (which would
    push the count to ~24).
    """

    async def test_query_count_bounded_under_own_mode_with_many_activities(
        self, client, repo,
    ):
        from sqlalchemy import event

        boot_act = await _bootstrap_dossier(repo)
        await repo.ensure_agent("citizen", "natuurlijk_persoon", "Citizen", {})

        # Seed 10 system-authored activities (citizen sees none of them
        # under "own" mode — all hidden; but each one still requires
        # the visibility check, which is where the N+1 would fire).
        for i in range(10):
            await _seed_extra_activity(
                repo, activity_type=f"sys_act_{i}",
            )

        # Use role ``aanvrager`` — not in the test app's global_access,
        # so the per-dossier entry is what resolves, which means the
        # ``activity_view: "own"`` branch actually fires. With ``alice``
        # (role ``oe:reader``, in global_access with activity_view=all),
        # the fast path short-circuits and the N+1 never runs — the
        # test would pass under a broken handler.
        await _seed_access_entity(
            repo, generated_by=boot_act,
            access_entries=[
                {"role": "aanvrager", "view": ["oe:aanvraag"],
                 "activity_view": "own"},
            ],
        )
        await _commit(repo)

        # Count SELECTs issued during the HTTP request. Listen on the
        # async engine's sync_engine — that's where SQLAlchemy's core
        # events fire for async sessions. The engine is the module-
        # global initialized by ``init_db`` at suite startup.
        from dossier_engine.db import session as _db_session
        engine = _db_session._engine
        select_count = 0

        def _before_execute(conn, cursor, statement, parameters, context, executemany):
            nonlocal select_count
            # Count only SELECTs. CREATE/INSERT/COMMIT/ROLLBACK are
            # noise for the N+1 claim, which is about read-path
            # amplification.
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(engine.sync_engine, "before_cursor_execute", _before_execute)
        try:
            r = await client.get(
                f"/dossiers/{D1}", headers={"X-POC-User": "citizen"},
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _before_execute)

        assert r.status_code == 200, (
            f"request failed with {r.status_code}: {r.text}"
        )

        # Ceiling is deliberately set aggressively so this test is
        # RED on the pre-fix N+1 and GREEN on the fixed handler.
        # Pre-fix for N=11 activities under 'own' mode: ~5 base
        # queries + 11 per-activity _is_agent selects = ~16 SELECTs
        # (measured). Post-fix with ``load_dossier_graph_rows``:
        # 10 SELECTs total, independent of N (measured). Ceiling of
        # 12 gives 2 queries of headroom for future incidental
        # growth in the fixed path, while still catching any
        # regression that re-introduces per-activity DB round-trips
        # (which would push the count above the ceiling for N=11,
        # and catastrophically above it for larger N).
        assert select_count <= 12, (
            f"dossier detail issued {select_count} SELECTs for a "
            f"dossier with 11 activities — N+1 regression suspected. "
            f"Expected O(1) under 'own' mode, got what looks like "
            f"O(N). Check routes/dossiers.py::get_dossier for a "
            f"loop that calls per-activity async helpers without "
            f"preloading."
        )


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

    # --- Bug 62 regression tests -----------------------------------

    async def test_bug62_wrong_entity_id_in_url_returns_404(
        self, client, repo,
    ):
        """Bug 62: ``GET /dossiers/{id}/entities/{type}/{eid}/{vid}``
        must 404 when the URL's ``entity_id`` segment doesn't match
        the version's actual ``entity_id`` field. Before the fix,
        the endpoint checked ``dossier_id`` and ``type`` but not
        ``entity_id`` — so a caller passing any UUID as ``{eid}``
        would get the version back as long as the version existed
        in that dossier with that type, resulting in silent
        mis-attribution (response ``entity_id`` differed from URL
        ``entity_id``).

        Seed two independent aanvragen in the same dossier + type so
        both mismatch paths are exercised: asking for A's version
        under B's eid must 404 even though A's version is a valid
        real entity in the system."""
        boot = await _bootstrap_dossier(repo)
        # A: seed normally — get its real eid and vid back.
        eid_a, vid_a = await _seed_entity(
            repo, boot, "oe:aanvraag", content={"titel": "A"},
        )
        # B: independent logical entity, same type, same dossier.
        eid_b, _ = await _seed_entity(
            repo, boot, "oe:aanvraag", content={"titel": "B"},
        )
        assert eid_a != eid_b  # sanity
        await _commit(repo)

        # The real A version under A's eid — sanity check, should 200.
        r_ok = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{eid_a}/{vid_a}",
            headers={"X-POC-User": "alice"},
        )
        assert r_ok.status_code == 200, "sanity: correct URL must 200"

        # A's vid under B's eid — must 404. Before Bug 62 fix this
        # returned 200 with A's entity (silent mis-attribution).
        r_mismatch = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{eid_b}/{vid_a}",
            headers={"X-POC-User": "alice"},
        )
        assert r_mismatch.status_code == 404, (
            f"Bug 62 regression: entity_id mismatch should 404, "
            f"got {r_mismatch.status_code} body={r_mismatch.json()}"
        )

    async def test_bug62_random_entity_id_returns_404(
        self, client, repo,
    ):
        """A completely random (never-seeded) entity_id in the URL
        must also 404 — not just a real-but-wrong eid. Guards against
        a future refactor that might check "entity_id exists in this
        dossier" instead of "entity_id matches the version's field"."""
        boot = await _bootstrap_dossier(repo)
        _, vid = await _seed_entity(repo, boot, "oe:aanvraag")
        await _commit(repo)

        random_eid = uuid4()
        r = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{random_eid}/{vid}",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404

    # --- Bug 57 regression tests -----------------------------------

    async def test_bug57_single_version_injects_file_download_urls(
        self, client, repo,
    ):
        """Bug 57: `GET /dossiers/{id}/entities/{type}/{eid}/{vid}`
        must inject ``file_download_url`` siblings for FileId fields
        in the entity's content. Before the fix, the endpoint returned
        ``content`` verbatim — clients saw raw file_ids with no
        download URL — which meant downloads broke unless the same
        entity was also read via the dossier-detail route. This test
        pins the fix: after Bug 57, the response carries download
        URLs on file_id fields at every level of the content tree
        (top-level and nested through lists of submodels)."""
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(
            repo, boot, "oe:aanvraag",
            content={
                "titel": "met bijlagen",
                "bijlagen": [
                    {"file_id": "f-0001", "filename": "plan.pdf"},
                    {"file_id": "f-0002", "filename": "foto.jpg"},
                ],
            },
        )
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{eid}/{vid}",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        content = r.json()["content"]

        # Structural: injected sibling keys present on each bijlage.
        assert "bijlagen" in content
        assert len(content["bijlagen"]) == 2
        for i, bijlage in enumerate(content["bijlagen"]):
            assert "file_id" in bijlage, (
                f"bijlagen[{i}]: file_id should still be present"
            )
            assert "file_download_url" in bijlage, (
                f"bijlagen[{i}]: Bug 57 regression — "
                f"file_download_url missing from response content"
            )
            # URL shape: points at the configured file_service URL
            # (see _build_test_app's config: http://test.local:8001)
            # and carries a query-string token. Not asserting the full
            # token shape — that's the signing helper's job, tested
            # elsewhere — just that a token is there.
            url = bijlage["file_download_url"]
            assert url.startswith("http://test.local:8001/download/"), url
            assert bijlage["file_id"] in url
            assert "?" in url, f"expected query-string token in {url}"

    async def test_bug57_no_model_registered_returns_content_unchanged(
        self, client, repo,
    ):
        """Defensive fallback: if no plugin registers a model for this
        entity type, ``inject_download_urls`` receives ``None`` as the
        model class and returns content unchanged. The endpoint stays
        usable — clients just don't get injected URLs, same as
        pre-Bug-57 behaviour. Pin this so a future refactor doesn't
        accidentally 500 on unknown types. ``oe:bijlage`` is
        declared in ``entity_types`` but has no entry in
        ``entity_models`` in the test plugin, so it exercises the
        fallback path."""
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(
            repo, boot, "oe:bijlage",
            content={"file_id": "f-orphan", "filename": "orphan.pdf"},
        )
        await _commit(repo)

        r = await client.get(
            f"/dossiers/{D1}/entities/oe:bijlage/{eid}/{vid}",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        content = r.json()["content"]
        # file_id preserved; no file_download_url (no model class to
        # drive the walker).
        assert content["file_id"] == "f-orphan"
        assert "file_download_url" not in content

    async def test_bug57_token_carries_dossier_and_user_scope(
        self, client, repo,
    ):
        """Bug 47 / Round 11 lineage: download tokens are
        dossier+user-scoped, so a token minted for dossier A + user
        alice can't pull a file from dossier B even if the file_id
        matches. Pin the scope binding here too — the entities route
        mints tokens the same way ``dossiers.py`` does, and this
        test guards against a future refactor that drops one of the
        two scopes from the signer closure."""
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(
            repo, boot, "oe:aanvraag",
            content={
                "titel": "scoped",
                "bijlagen": [{"file_id": "f-scope", "filename": "p.pdf"}],
            },
        )
        await _commit(repo)

        # Two requests, same entity, different users. The tokens
        # should differ — same file_id, same dossier, different user.
        r_alice = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{eid}/{vid}",
            headers={"X-POC-User": "alice"},
        )
        r_admin = await client.get(
            f"/dossiers/{D1}/entities/oe:aanvraag/{eid}/{vid}",
            headers={"X-POC-User": "admin"},
        )
        url_alice = r_alice.json()["content"]["bijlagen"][0]["file_download_url"]
        url_admin = r_admin.json()["content"]["bijlagen"][0]["file_download_url"]
        # Same file_id path, but different query strings because
        # the token carries user_id.
        assert url_alice.split("?")[0] == url_admin.split("?")[0]
        assert url_alice != url_admin, (
            "Bug 47 regression: tokens for different users must differ"
        )


# --------------------------------------------------------------------
# POST /files/upload/request
# --------------------------------------------------------------------


class TestFileUploadRequest:

    async def test_returns_signed_upload_url(self, client, repo):
        await _commit(repo)
        did = str(uuid4())
        r = await client.post(
            "/files/upload/request",
            json={"filename": "test.pdf", "dossier_id": did},
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "file_id" in body
        assert "upload_url" in body
        assert body["filename"] == "test.pdf"
        assert body["dossier_id"] == did
        assert body["file_id"] in body["upload_url"]
        # The signature query-string key is `signature=`, not `sig=`.
        assert "signature=" in body["upload_url"]
        assert "http://test.local:8001" in body["upload_url"]
        # dossier_id is signed into the token; verify it appears in
        # the upload_url's query string so the file_service can
        # verify the signature and stamp intended_dossier_id into
        # the .meta file.
        assert f"dossier_id={did}" in body["upload_url"]

    async def test_missing_dossier_id_returns_422(self, client, repo):
        """Previously this was 'missing filename is fine'. Since
        the Bug 47 fix, dossier_id is required — the binding
        between upload and dossier is established at token mint
        time. Missing dossier_id → 422 with a clear message."""
        await _commit(repo)
        r = await client.post(
            "/files/upload/request",
            json={"filename": "test.pdf"},
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 422
        assert "dossier_id is required" in r.json()["detail"]

    async def test_filename_still_optional(self, client, repo):
        """Filename is still optional — only dossier_id is required
        for the token to be mintable."""
        await _commit(repo)
        did = str(uuid4())
        r = await client.post(
            "/files/upload/request",
            json={"dossier_id": did},
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
            json={"filename": "x", "dossier_id": str(uuid4())},
        )
        assert r.status_code == 401
