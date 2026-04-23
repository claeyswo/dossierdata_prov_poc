"""Bug 13 / Round 33: ``create_app`` wires lifespan, not ``on_event``.

FastAPI deprecated ``@app.on_event("startup")`` / ``@app.on_event("shutdown")``
in 0.93 in favor of the ``lifespan`` context-manager pattern. This
codebase pins ``fastapi>=0.110.0`` (see ``pyproject.toml``) — well past
the deprecation. ``create_app`` was still using the old form. Round 33
converted both handlers to a single ``@asynccontextmanager`` lifespan
function passed to ``FastAPI(lifespan=...)``.

This test pins the shape by inspecting the resulting FastAPI app:

* ``app.router.on_startup`` and ``on_shutdown`` must be empty —
  if they're not, someone re-introduced ``@app.on_event(...)``.
* ``app.router.lifespan_context`` must not be FastAPI's built-in
  ``_DefaultLifespan`` — if it is, ``lifespan=...`` wasn't passed
  at construction.

The test does *not* fire the lifespan (that would require a real DB
and run Alembic). It's a shape test, not an execution test. Runtime
correctness of the startup path is covered by the existing
``test_alembic_startup`` and ``test_audit`` suites which exercise the
functions the lifespan calls into.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


_MINIMAL_CONFIG_YAML = """\
database:
  url: "postgresql+asyncpg://dossier:dossier@127.0.0.1:5432/dossiers_test"

iri_base:
  dossier: "https://id.erfgoed.net/dossiers/"
  ontology: "https://id.erfgoed.net/vocab/ontology#"

plugins:
  - dossier_toelatingen

auth:
  mode: "poc"

file_service:
  signing_key: "test-key"
  url: "http://localhost:8001"
  storage_root: "./file_storage"

global_access: []
global_audit_access: []
global_admin_access: []
"""


@pytest.fixture
def tmp_config_path():
    """Write the minimal config to a temp file and yield its path.

    ``create_app`` reads a yaml file path, not a dict, so we need
    actual filesystem bytes. Cleaned up after the test."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False,
    ) as f:
        f.write(_MINIMAL_CONFIG_YAML)
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


class TestCreateAppLifespan:

    def test_create_app_attaches_lifespan_not_on_event(
        self, tmp_config_path,
    ):
        """The FastAPI app returned by ``create_app`` must have its
        lifespan wired via the ``lifespan=...`` constructor argument,
        not via ``@app.on_event(...)`` handlers. This is the shape
        assertion for the Bug 13 refactor."""
        from fastapi.routing import _DefaultLifespan

        from dossier_engine.app import create_app

        app = create_app(tmp_config_path)

        # The two legacy registration lists must be empty. If either
        # has entries, someone re-introduced ``@app.on_event(...)``.
        assert app.router.on_startup == [], (
            f"app.router.on_startup should be empty after Round 33's "
            f"lifespan refactor, but has {len(app.router.on_startup)} "
            f"entries. Someone re-introduced @app.on_event('startup')?"
        )
        assert app.router.on_shutdown == [], (
            f"app.router.on_shutdown should be empty after Round 33's "
            f"lifespan refactor, but has {len(app.router.on_shutdown)} "
            f"entries. Someone re-introduced @app.on_event('shutdown')?"
        )

        # The lifespan_context must be the user-supplied function,
        # not FastAPI's default no-op. ``_DefaultLifespan`` is an
        # internal FastAPI class — importing it is a private-API
        # dependency, accepted here because this test's whole purpose
        # is to distinguish "user wired a lifespan" from "FastAPI
        # fell back to its default."
        assert not isinstance(
            app.router.lifespan_context, _DefaultLifespan
        ), (
            "app.router.lifespan_context is FastAPI's _DefaultLifespan, "
            "meaning no lifespan= was passed to FastAPI(). The Bug 13 "
            "refactor must have regressed."
        )
