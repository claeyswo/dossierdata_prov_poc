"""
HTTP tests for the PROV export and visualization endpoints:

* `GET /dossiers/{id}/prov` — PROV-JSON export
* `GET /dossiers/{id}/prov/graph/timeline` — Timeline HTML graph
* `GET /dossiers/{id}/prov/graph/columns` — Columns HTML graph

The PROV-JSON endpoint returns a structured dict following the
W3C PROV-JSON serialization. The graph endpoints return HTML
pages (rendered via Jinja2 templates since the B13 refactor)
with embedded D3.js visualization code.

Test strategy: we seed a small dossier with two activities (one
generates an entity, the other uses it and generates a second),
then verify:

1. The PROV-JSON shape has the right sections (entity, activity,
   agent, wasGeneratedBy, used, wasAssociatedWith, etc.)
2. The graph endpoints return 200 with `text/html` content type
   and a valid `<!DOCTYPE html>` response
3. Edge cases: missing dossier → 404, auth required, visibility
   filtering

Uses `httpx.AsyncClient` with `ASGITransport` (same pattern as
`test_http_routes.py` and `test_http_activities.py`).
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
from dossier_engine.routes.prov import register_prov_routes


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _build_prov_test_app() -> FastAPI:
    """Build a minimal FastAPI app with PROV endpoints registered.

    The PROV routes are registered separately from the main routes
    via `register_prov_routes`, so we need to call both."""
    plugin = Plugin(
        name="test",
        workflow={
            "name": "test",
            "activities": [
                SYSTEM_ACTION_DEF,
                {
                    "name": "createEntity",
                    "label": "Create",
                    "can_create_dossier": True,
                    "client_callable": True,
                    "default_role": "oe:aanvrager",
                    "allowed_roles": ["oe:aanvrager"],
                    "authorization": {"access": "authenticated"},
                    "used": [],
                    "generates": ["oe:aanvraag"],
                    "status": "ingediend",
                    "validators": [],
                    "side_effects": [],
                    "tasks": [],
                },
            ],
            "entity_types": [
                {"type": "oe:aanvraag", "cardinality": "multiple"},
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
            "roles": ["auditor"], "properties": {},
        },
    ])

    app = FastAPI()
    app.state.registry = registry
    app.state.config = {"file_service": {"url": "http://test", "signing_key": "k"}}

    register_routes(app, registry, auth, global_access=[])
    register_prov_routes(
        app, registry, auth, global_access=[],
        global_audit_access=["auditor"],
    )
    return app


@pytest_asyncio.fixture
async def prov_client():
    app = _build_prov_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _bootstrap_with_entity(repo: Repository) -> tuple[UUID, UUID, UUID]:
    """Create D1 with one activity that generates one oe:aanvraag.
    Returns (activity_id, entity_id, version_id).
    Caller must commit."""
    await repo.create_dossier(D1, "test")
    await repo.ensure_agent("alice", "natuurlijk_persoon", "Alice", {})
    await repo.ensure_agent("system", "systeem", "Systeem", {})

    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="createEntity",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="alice",
        agent_name="Alice", agent_type="natuurlijk_persoon",
        role="oe:aanvrager",
    ))

    eid = uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type="oe:aanvraag", generated_by=act_id,
        content={"titel": "Test"}, attributed_to="alice",
    )
    await repo.session.flush()
    return act_id, eid, vid


async def _commit(repo: Repository) -> None:
    await repo.session.commit()


# --------------------------------------------------------------------
# PROV-JSON endpoint
# --------------------------------------------------------------------


class TestProvJson:

    async def test_missing_dossier_returns_404(self, prov_client, repo):
        await _commit(repo)
        r = await prov_client.get(
            f"/dossiers/{uuid4()}/prov",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404

    async def test_unauthenticated_returns_401(self, prov_client, repo):
        await _commit(repo)
        r = await prov_client.get(f"/dossiers/{D1}/prov")
        assert r.status_code == 401

    async def test_happy_path_returns_prov_json_structure(
        self, prov_client, repo,
    ):
        """One activity generating one entity. The PROV-JSON
        response should contain:
        - `prefix` section with the standard namespaces
        - `entity` section with the oe:aanvraag
        - `activity` section with the createEntity activity
        - `agent` section with alice
        - `wasGeneratedBy` linking entity → activity
        - `wasAssociatedWith` linking activity → agent
        - `wasAttributedTo` linking entity → agent
        """
        act_id, eid, vid = await _bootstrap_with_entity(repo)
        await _commit(repo)

        r = await prov_client.get(
            f"/dossiers/{D1}/prov",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        prov = r.json()

        # Prefix section
        assert "prefix" in prov
        assert "prov" in prov["prefix"]

        # Entity section — at least one entity key
        assert "entity" in prov
        entity_keys = list(prov["entity"].keys())
        assert len(entity_keys) >= 1
        # The key contains the entity type (bare, without namespace prefix)
        assert any("entities/oe:aanvraag/" in k for k in entity_keys)

        # Activity section
        assert "activity" in prov
        activity_keys = list(prov["activity"].keys())
        assert len(activity_keys) >= 1
        # Activity key contains the activity UUID
        assert any(str(act_id) in k for k in activity_keys)

        # Agent section
        assert "agent" in prov
        assert any("alice" in k for k in prov["agent"].keys())

        # Relationships
        assert "wasGeneratedBy" in prov
        assert len(prov["wasGeneratedBy"]) >= 1

        assert "wasAssociatedWith" in prov
        assert len(prov["wasAssociatedWith"]) >= 1

        assert "wasAttributedTo" in prov
        assert len(prov["wasAttributedTo"]) >= 1

    async def test_empty_dossier_returns_minimal_prov(
        self, prov_client, repo,
    ):
        """Dossier exists but has no activities and no entities.
        The PROV-JSON should still return 200 with at least a
        prefix section. Empty sections are stripped."""
        await repo.create_dossier(D1, "test")
        await repo.ensure_agent("alice", "natuurlijk_persoon", "Alice", {})
        await repo.session.flush()
        await _commit(repo)

        r = await prov_client.get(
            f"/dossiers/{D1}/prov",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        prov = r.json()
        assert "prefix" in prov
        # Empty sections are removed by the "Remove empty sections" step
        assert "entity" not in prov or prov["entity"] == {}

    async def test_derived_entity_produces_wasDerivedFrom(
        self, prov_client, repo,
    ):
        """An entity with `derived_from` set should produce a
        `wasDerivedFrom` entry in the PROV-JSON linking the
        new entity to its parent."""
        act_id, eid, vid = await _bootstrap_with_entity(repo)

        # Create a second version derived from the first
        vid2 = uuid4()
        await repo.create_entity(
            version_id=vid2, entity_id=eid, dossier_id=D1,
            type="oe:aanvraag", generated_by=act_id,
            content={"titel": "v2"}, attributed_to="alice",
            derived_from=vid,
        )
        await repo.session.flush()
        await _commit(repo)

        r = await prov_client.get(
            f"/dossiers/{D1}/prov",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        prov = r.json()

        assert "wasDerivedFrom" in prov
        derivations = prov["wasDerivedFrom"]
        assert len(derivations) >= 1

        # At least one derivation should reference the parent version
        found = False
        for d in derivations.values():
            if str(vid) in d.get("prov:usedEntity", ""):
                found = True
                break
        assert found, f"No derivation found referencing parent {vid}"

    async def test_used_entity_produces_used_relation(
        self, prov_client, repo,
    ):
        """An activity that `used` an entity should produce a
        `used` entry in the PROV-JSON."""
        act_id, eid, vid = await _bootstrap_with_entity(repo)

        # Create a second activity that uses the entity
        act2_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=act2_id, dossier_id=D1, type="createEntity",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=act2_id, agent_id="alice",
            agent_name="Alice", agent_type="natuurlijk_persoon",
            role="oe:aanvrager",
        ))
        await repo.create_used(act2_id, vid)
        await repo.session.flush()
        await _commit(repo)

        r = await prov_client.get(
            f"/dossiers/{D1}/prov",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        prov = r.json()

        assert "used" in prov
        used_entries = prov["used"]
        assert len(used_entries) >= 1

        # The used entry should reference the activity and entity
        found = False
        for u in used_entries.values():
            if str(act2_id) in u.get("prov:activity", "") and str(vid) in u.get("prov:entity", ""):
                found = True
                break
        assert found


# --------------------------------------------------------------------
# Graph endpoints (HTML)
# --------------------------------------------------------------------


class TestProvGraphTimeline:

    async def test_missing_dossier_returns_404(self, prov_client, repo):
        await _commit(repo)
        r = await prov_client.get(
            f"/dossiers/{uuid4()}/prov/graph/timeline",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404

    async def test_returns_html_with_doctype(self, prov_client, repo):
        """Happy path: returns a full HTML page with DOCTYPE,
        the dossier ID in the title, and the D3 script tag."""
        await _bootstrap_with_entity(repo)
        await _commit(repo)

        r = await prov_client.get(
            f"/dossiers/{D1}/prov/graph/timeline",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        body = r.text
        assert body.startswith("<!DOCTYPE html>")
        assert str(D1) in body
        assert "d3.min.js" in body
        assert "PROV Timeline" in body

    async def test_include_tasks_flag(self, prov_client, repo):
        """The `include_tasks` query param controls whether
        systemAction activities and system:task entities appear.
        Default is false (hidden). With ?include_tasks=true they
        should appear."""
        await _bootstrap_with_entity(repo)
        await _commit(repo)

        # Default: no tasks in the graph data
        r_default = await prov_client.get(
            f"/dossiers/{D1}/prov/graph/timeline",
            headers={"X-POC-User": "alice"},
        )
        assert r_default.status_code == 200

        # With include_tasks=true
        r_tasks = await prov_client.get(
            f"/dossiers/{D1}/prov/graph/timeline?include_tasks=true",
            headers={"X-POC-User": "alice"},
        )
        assert r_tasks.status_code == 200
        # Both should render valid HTML
        assert r_tasks.text.startswith("<!DOCTYPE html>")


class TestProvGraphColumns:

    async def test_missing_dossier_returns_404(self, prov_client, repo):
        await _commit(repo)
        r = await prov_client.get(
            f"/dossiers/{uuid4()}/prov/graph/columns",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 404

    async def test_returns_html_with_doctype(self, prov_client, repo):
        """Columns graph endpoint renders an HTML page with the
        column-layout visualization."""
        await _bootstrap_with_entity(repo)
        await _commit(repo)

        r = await prov_client.get(
            f"/dossiers/{D1}/prov/graph/columns",
            headers={"X-POC-User": "alice"},
        )
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        body = r.text
        assert body.startswith("<!DOCTYPE html>")
        assert "PROV Columns" in body
        assert "d3.min.js" in body


class TestAuditAccess:
    """The audit-level endpoints (/prov, /prov/graph/columns,
    /archive) use check_audit_access, not the ordinary dossier_access
    check. A user with ordinary dossier_access but no audit role gets
    403; the timeline endpoint stays open to them."""

    async def test_prov_json_denied_without_audit_role(self, repo):
        """User without auditor role gets 403 on /prov even though
        they have ordinary dossier_access."""
        # Custom app with a non-auditor user
        from dossier_engine.auth import POCAuthMiddleware
        from dossier_engine.plugin import Plugin, PluginRegistry
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        plugin = Plugin(
            name="testwf",
            workflow={"name": "testwf", "activities": []},
            entity_models={},
            entity_schemas={},
            handlers={},
            validators={},
            relation_validators={},
        )
        registry = PluginRegistry()
        registry.register(plugin)
        auth = POCAuthMiddleware([
            {"id": "bob", "username": "bob", "type": "natuurlijk_persoon",
             "name": "Bob", "roles": ["oe:aanvrager"], "properties": {}},
        ])

        app = FastAPI()
        app.state.registry = registry
        app.state.config = {"file_service": {"url": "http://test", "signing_key": "k"}}
        register_routes(app, registry, auth, global_access=[])
        register_prov_routes(
            app, registry, auth, global_access=[],
            global_audit_access=["auditor"],  # bob is not an auditor
        )

        await _bootstrap_with_entity(repo)
        await _commit(repo)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(
                f"/dossiers/{D1}/prov",
                headers={"X-POC-User": "bob"},
            )
            assert r.status_code == 403
            assert "audit" in r.json()["detail"].lower()

    async def test_archive_denied_without_audit_role(self, repo):
        """Same check on /archive — audit-level endpoint."""
        from dossier_engine.auth import POCAuthMiddleware
        from dossier_engine.plugin import Plugin, PluginRegistry
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        plugin = Plugin(
            name="testwf",
            workflow={"name": "testwf", "activities": []},
            entity_models={},
            entity_schemas={},
            handlers={},
            validators={},
            relation_validators={},
        )
        registry = PluginRegistry()
        registry.register(plugin)
        auth = POCAuthMiddleware([
            {"id": "bob", "username": "bob", "type": "natuurlijk_persoon",
             "name": "Bob", "roles": ["oe:aanvrager"], "properties": {}},
        ])

        app = FastAPI()
        app.state.registry = registry
        app.state.config = {"file_service": {"url": "http://test", "signing_key": "k"}}
        register_routes(app, registry, auth, global_access=[])
        register_prov_routes(
            app, registry, auth, global_access=[],
            global_audit_access=["auditor"],
        )

        await _bootstrap_with_entity(repo)
        await _commit(repo)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(
                f"/dossiers/{D1}/archive",
                headers={"X-POC-User": "bob"},
            )
            assert r.status_code == 403

    async def test_timeline_open_to_dossier_access_users(self, repo):
        """Timeline endpoint honors ordinary dossier_access — a
        user without audit role can still view their own timeline."""
        # Bob has dossier access via empty global_access list only
        # matching no entries → default-deny. We need an explicit
        # global_access entry for bob's role.
        from dossier_engine.auth import POCAuthMiddleware
        from dossier_engine.plugin import Plugin, PluginRegistry
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        plugin = Plugin(
            name="testwf",
            workflow={"name": "testwf", "activities": []},
            entity_models={},
            entity_schemas={},
            handlers={},
            validators={},
            relation_validators={},
        )
        registry = PluginRegistry()
        registry.register(plugin)
        auth = POCAuthMiddleware([
            {"id": "bob", "username": "bob", "type": "natuurlijk_persoon",
             "name": "Bob", "roles": ["oe:aanvrager"], "properties": {}},
        ])

        app = FastAPI()
        app.state.registry = registry
        app.state.config = {"file_service": {"url": "http://test", "signing_key": "k"}}
        register_routes(
            app, registry, auth,
            global_access=[{"role": "oe:aanvrager", "view": "all", "activity_view": "all"}],
        )
        register_prov_routes(
            app, registry, auth,
            global_access=[{"role": "oe:aanvrager", "view": "all", "activity_view": "all"}],
            global_audit_access=["auditor"],  # bob is NOT an auditor
        )

        await _bootstrap_with_entity(repo)
        await _commit(repo)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(
                f"/dossiers/{D1}/prov/graph/timeline",
                headers={"X-POC-User": "bob"},
            )
            # Timeline honors ordinary dossier_access — bob's
            # oe:aanvrager role is in global_access, so he gets in.
            assert r.status_code == 200, r.text
            assert "text/html" in r.headers["content-type"]
