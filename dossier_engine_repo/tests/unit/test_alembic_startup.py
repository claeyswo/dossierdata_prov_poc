"""Regression tests for Bug 6 — Alembic migration fail-fast.

The old behaviour silently called ``create_tables()`` when Alembic
exited non-zero, which masked partial-migration corruption: the half-
applied DDL stayed in place, ``Base.metadata.create_all`` no-op'd over
existing tables, and the app came up on a schema that matched neither
the ORM model nor any Alembic revision. The fix is pure fail-fast —
any Alembic failure or missing ``alembic.ini`` raises ``RuntimeError``
before the app accepts traffic.

These tests monkeypatch ``subprocess.run`` and ``Path.exists`` to
exercise the three failure modes of ``_run_alembic_migrations`` without
actually running a migration or a DB. The happy path is covered
end-to-end by the shell spec (``scripts/ci_run_shell_spec.sh``) — this
file covers the failure paths that the shell spec can't reach from a
clean Postgres.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from dossier_engine import app as app_module


class _FakeCompleted:
    """Mimic the slice of ``subprocess.CompletedProcess`` that
    ``_run_alembic_migrations`` reads. Using a real
    ``CompletedProcess`` also works but this is cheaper and keeps
    the test intent visible."""

    def __init__(self, returncode: int, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


class TestRunAlembicMigrations:

    def test_missing_alembic_ini_raises_runtime_error(self, monkeypatch):
        """If ``alembic.ini`` isn't present at the expected path, the
        deployment is broken and startup must abort. Previously this
        branch silently called ``create_tables()``; that fallback is
        gone. The raised message must mention the ini path so the
        operator can see immediately what's missing."""
        # Force the exists() check to return False without touching
        # the real filesystem. Patch the Path.exists method used by
        # the helper.
        from pathlib import Path
        monkeypatch.setattr(Path, "exists", lambda self: False)

        with pytest.raises(RuntimeError) as exc:
            app_module._run_alembic_migrations("postgresql://x/y")

        assert "alembic.ini" in str(exc.value)
        assert "migration infrastructure" in str(exc.value)

    def test_nonzero_exit_raises_runtime_error(self, monkeypatch):
        """A non-zero exit from ``alembic upgrade head`` indicates the
        migration process ran but failed — possibly after applying
        some DDL. The old code swallowed this and called
        ``create_tables()`` as a fallback, which left a half-migrated
        schema in place. The fix raises instead; pin it here."""
        fake_result = _FakeCompleted(returncode=1, stderr="migration broke")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_result)

        with pytest.raises(RuntimeError) as exc:
            app_module._run_alembic_migrations("postgresql://x/y")

        assert "rc=1" in str(exc.value)
        assert "partial schema" in str(exc.value).lower() or \
               "partial" in str(exc.value).lower()

    def test_nonzero_exit_logs_stderr_at_error_level(
        self, monkeypatch, caplog,
    ):
        """Before raising, the helper logs Alembic's stderr at ERROR
        level so operators have the full traceback in the app log
        regardless of how the RuntimeError propagates upstream. This
        is the difference between "Alembic broke, good luck" and
        "Alembic broke, here's what it said"."""
        fake_result = _FakeCompleted(
            returncode=2,
            stderr=(
                "sqlalchemy.exc.OperationalError: (asyncpg.InvalidSchemaName) "
                "schema 'tenant_x' does not exist"
            ),
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_result)

        with caplog.at_level(logging.ERROR, logger="dossier.app"):
            with pytest.raises(RuntimeError):
                app_module._run_alembic_migrations("postgresql://x/y")

        # At least one ERROR record from dossier.app mentioning the
        # rc and the stderr contents.
        matches = [
            r for r in caplog.records
            if r.name == "dossier.app" and r.levelno == logging.ERROR
        ]
        assert matches, "no ERROR log from dossier.app"
        joined = " ".join(r.getMessage() for r in matches)
        assert "rc=2" in joined
        assert "InvalidSchemaName" in joined

    def test_zero_exit_logs_success_without_raising(
        self, monkeypatch, caplog,
    ):
        """Happy path: rc=0 means migrations ran cleanly; helper logs
        at INFO and returns. Pin this so the fail-fast logic can't
        regress into also raising on success (e.g. from a future
        refactor that misplaces the ``if result.returncode != 0``
        guard)."""
        fake_result = _FakeCompleted(returncode=0, stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_result)

        with caplog.at_level(logging.INFO, logger="dossier.app"):
            # Does not raise.
            app_module._run_alembic_migrations("postgresql://x/y")

        info_messages = [
            r.getMessage() for r in caplog.records
            if r.name == "dossier.app" and r.levelno == logging.INFO
        ]
        assert any("Alembic migrations applied successfully" in m
                   for m in info_messages)

    def test_subprocess_run_invoked_with_expected_args(self, monkeypatch):
        """The helper's subprocess invocation contract: it must run
        ``python3 -m alembic upgrade head`` with
        ``capture_output=True`` and pass ``DOSSIER_DB_URL`` in the
        environment. ``env.py`` reads that variable to construct the
        async engine; a missing or misnamed var silently falls back
        to the module's default connection string, which is *not*
        what operators think they're migrating."""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)

        app_module._run_alembic_migrations("postgresql+asyncpg://u:p@h/db")

        assert captured["cmd"] == [
            "python3", "-m", "alembic", "upgrade", "head",
        ]
        assert captured["kwargs"].get("capture_output") is True
        assert captured["kwargs"].get("text") is True
        env = captured["kwargs"].get("env", {})
        assert env.get("DOSSIER_DB_URL") == "postgresql+asyncpg://u:p@h/db"
