"""
Integration tests for workflow-scoped reference data and validation
endpoints.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI

from dossier_engine.routes import register_routes
from dossier_engine.auth import POCAuthMiddleware


def _build_toelatingen_app() -> FastAPI:
    """Build a FastAPI app with the real toelatingen plugin loaded."""
    from dossier_engine.plugin import PluginRegistry
    import dossier_toelatingen

    plugin = dossier_toelatingen.create_plugin()
    registry = PluginRegistry()
    registry.register(plugin)

    auth = POCAuthMiddleware(plugin.workflow.get("poc_users", []))

    app = FastAPI()
    app.state.registry = registry
    app.state.config = {"file_service": {"url": "http://test", "signing_key": "k"}}
    register_routes(app, registry, auth, global_access=[])
    return app


@pytest_asyncio.fixture
async def activity_client():
    """AsyncClient wired to the toelatingen app."""
    app = _build_toelatingen_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
    ) as c:
        yield c


class TestReferenceData:

    async def test_get_all_reference_data(self, activity_client):
        """GET /{workflow}/reference returns all lists."""
        r = await activity_client.get("/toelatingen/reference")
        assert r.status_code == 200
        body = r.json()
        assert "bijlagetypes" in body
        assert "handelingen" in body
        assert "beslissingstypes" in body
        assert "gemeenten" in body

    async def test_get_single_list(self, activity_client):
        """GET /{workflow}/reference/{name} returns one list."""
        r = await activity_client.get("/toelatingen/reference/bijlagetypes")
        assert r.status_code == 200
        body = r.json()
        items = body["items"]
        assert len(items) > 0
        keys = [i["key"] for i in items]
        assert "foto" in keys
        assert "detailplan" in keys

    async def test_get_gemeenten(self, activity_client):
        """Gemeenten reference data includes nis_code."""
        r = await activity_client.get("/toelatingen/reference/gemeenten")
        assert r.status_code == 200
        items = r.json()["items"]
        brugge = next(i for i in items if i["key"] == "brugge")
        assert brugge["nis_code"] == "31005"

    async def test_unknown_list_returns_404(self, activity_client):
        """GET /{workflow}/reference/{bad} returns 404 with available names."""
        r = await activity_client.get("/toelatingen/reference/nonexistent")
        assert r.status_code == 404
        assert "Available" in r.json()["detail"]

    async def test_unknown_workflow_returns_404(self, activity_client):
        """GET /{bad_workflow}/reference returns 404."""
        r = await activity_client.get("/nonexistent/reference")
        assert r.status_code == 404


class TestValidation:

    async def test_list_validators(self, activity_client):
        """GET /{workflow}/validate lists registered validators."""
        r = await activity_client.get("/toelatingen/validate")
        assert r.status_code == 200
        names = r.json()["validators"]
        assert "erfgoedobject" in names
        assert "handeling" in names

    async def test_validate_erfgoedobject_valid(self, activity_client):
        """POST /{workflow}/validate/erfgoedobject — known URI."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={"uri": "https://id.erfgoed.net/erfgoedobjecten/10001"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is True
        assert body["label"] == "Stadhuis Brugge"
        assert body["type"] == "monument"
        assert body["gemeente"] == "Brugge"

    async def test_validate_erfgoedobject_invalid(self, activity_client):
        """POST /{workflow}/validate/erfgoedobject — unknown URI."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={"uri": "https://id.erfgoed.net/erfgoedobjecten/99999"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert "niet gevonden" in body["error"]

    async def test_validate_erfgoedobject_bad_format(self, activity_client):
        """POST /{workflow}/validate/erfgoedobject — wrong URI scheme."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={"uri": "http://example.com/something"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert "Ongeldig formaat" in body["error"]

    async def test_validate_handeling_valid(self, activity_client):
        """POST /{workflow}/validate/handeling — allowed combo."""
        r = await activity_client.post(
            "/toelatingen/validate/handeling",
            json={
                "erfgoedobject_uri": "https://id.erfgoed.net/erfgoedobjecten/10001",
                "handeling": "restauratie",
            },
        )
        assert r.status_code == 200
        assert r.json()["valid"] is True

    async def test_validate_handeling_invalid(self, activity_client):
        """POST /{workflow}/validate/handeling — disallowed combo."""
        r = await activity_client.post(
            "/toelatingen/validate/handeling",
            json={
                "erfgoedobject_uri": "https://id.erfgoed.net/erfgoedobjecten/20001",
                "handeling": "sloop_deel",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert "niet toegelaten" in body["error"]
        assert "landschap" in body["error"]

    async def test_unknown_validator_returns_404(self, activity_client):
        """POST /{workflow}/validate/{bad} returns 404."""
        r = await activity_client.post(
            "/toelatingen/validate/nonexistent",
            json={},
        )
        assert r.status_code == 404

    async def test_validate_missing_fields(self, activity_client):
        """POST with empty body returns 422 — the Pydantic model
        catches the missing required field before our validator runs."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={},
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        # FastAPI's validation error includes the field name.
        assert any("uri" in str(e.get("loc", "")) for e in detail)
