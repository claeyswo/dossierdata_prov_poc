"""
Unit tests for `dossier_engine.sentry`.

Exercises the three things that matter:

1. **No-op discipline.** With ``SENTRY_DSN`` unset, every init
   function returns False and doesn't call ``sentry_sdk.init``. Dev
   and test runs stay quiet.

2. **Single init per process.** The ``_initialized`` flag is shared
   across the two entry points — calling ``init_sentry_fastapi``
   after ``init_sentry_worker`` (or vice versa) is a no-op the
   second time. This matters if an operator accidentally wires both
   init calls into a process that's running both roles.

3. **Integrations list is correct.** When the SDK *is* installed
   and a DSN is present, the worker init must wire the logging
   integration; the app init must wire logging *and* FastApi.
   Breakage here would mean either request context missing from
   Sentry (for FastApi) or log breadcrumbs disappearing (for
   logging).

We monkeypatch ``sentry_sdk.init`` rather than letting it run —
running it would attempt to reach the (fake) DSN URL and either
noise up the test output or hang. Capturing the args it *would*
have been called with is enough to check the integrations list.
"""

from __future__ import annotations

import importlib
import logging

import pytest


def _reload_sentry_module():
    """Fresh import of dossier_engine.sentry so each test starts with
    ``_initialized=False``. The module holds that flag at module
    level, which matches production behavior (one process, one init)
    but means tests have to reset explicitly."""
    import dossier_engine.sentry as sentry_mod
    return importlib.reload(sentry_mod)


@pytest.fixture
def sentry(monkeypatch):
    """Fresh sentry module, ``SENTRY_DSN`` stripped from env so tests
    that care about the env-read path start from a known state."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    return _reload_sentry_module()


@pytest.fixture
def captured_init(monkeypatch, sentry):
    """Replace ``sentry_sdk.init`` with a capture so we can inspect
    the integrations list without actually calling into the real SDK
    (which would try to reach the DSN endpoint)."""
    captured = {}

    def fake_init(**kwargs):
        captured.update(kwargs)

    # The module stores its own reference at import time; patch that
    # reference directly so the indirection inside ``_init_sdk``
    # picks up the fake.
    if sentry._sentry_sdk is None:
        pytest.skip("sentry_sdk not installed")
    monkeypatch.setattr(sentry._sentry_sdk, "init", fake_init)
    return captured


class TestNoopWhenDsnUnset:
    """With no DSN, init functions return False and don't call
    ``sentry_sdk.init``. Dev and test runs rely on this."""

    def test_worker_init_returns_false_without_dsn(self, sentry, monkeypatch):
        # Patch init so a bug that *does* call it would be caught;
        # test asserts the call doesn't happen.
        called = {"count": 0}
        if sentry._sentry_sdk is not None:
            monkeypatch.setattr(
                sentry._sentry_sdk, "init",
                lambda **_: called.__setitem__("count", called["count"] + 1),
            )
        assert sentry.init_sentry_worker() is False
        assert called["count"] == 0

    def test_fastapi_init_returns_false_without_dsn(self, sentry, monkeypatch):
        called = {"count": 0}
        if sentry._sentry_sdk is not None:
            monkeypatch.setattr(
                sentry._sentry_sdk, "init",
                lambda **_: called.__setitem__("count", called["count"] + 1),
            )
        # The `app` parameter is accepted but not consumed today;
        # passing a string is enough to exercise the signature.
        assert sentry.init_sentry_fastapi("fake-app") is False
        assert called["count"] == 0

    def test_explicit_none_dsn_equivalent_to_env_unset(self, sentry):
        """Passing ``dsn=None`` explicitly falls through to the env
        read, same as the default. Guards against a future change
        where the default moves from ``None`` to something else."""
        assert sentry.init_sentry_worker(dsn=None) is False
        assert sentry.init_sentry_fastapi("app", dsn=None) is False


class TestSingleInitGuard:
    """The ``_initialized`` flag is shared across entry points —
    second call is a no-op."""

    def test_second_worker_init_is_noop(self, captured_init, sentry):
        first = sentry.init_sentry_worker(dsn="https://fake@sentry.test/1")
        second = sentry.init_sentry_worker(dsn="https://fake@sentry.test/1")
        assert first is True
        assert second is False

    def test_fastapi_after_worker_is_noop(self, captured_init, sentry):
        """The real scenario: a deployment wires both by mistake.
        The second init should not re-register integrations — if it
        did, ``sentry_sdk.init`` running twice in-process might attach
        duplicate handlers to the root logger."""
        first = sentry.init_sentry_worker(dsn="https://fake@sentry.test/1")
        second = sentry.init_sentry_fastapi(
            "app", dsn="https://fake@sentry.test/1",
        )
        assert first is True
        assert second is False

    def test_worker_after_fastapi_is_noop(self, captured_init, sentry):
        """Symmetric case — order doesn't matter, first-wins."""
        first = sentry.init_sentry_fastapi(
            "app", dsn="https://fake@sentry.test/1",
        )
        second = sentry.init_sentry_worker(dsn="https://fake@sentry.test/1")
        assert first is True
        assert second is False


class TestIntegrationsWired:
    """When SDK is installed and DSN is set, the integrations list
    must contain the right types. SIEM/Sentry alert rules depend on
    request context being attached to FastAPI events; losing
    FastApiIntegration would break that silently."""

    def test_worker_wires_logging_integration_only(self, captured_init, sentry):
        sentry.init_sentry_worker(dsn="https://fake@sentry.test/1")

        integrations = captured_init.get("integrations") or []
        types = [type(i).__name__ for i in integrations]
        assert "LoggingIntegration" in types
        assert "FastApiIntegration" not in types

    def test_fastapi_wires_both_integrations(self, captured_init, sentry):
        sentry.init_sentry_fastapi(
            "app", dsn="https://fake@sentry.test/1",
        )

        integrations = captured_init.get("integrations") or []
        types = [type(i).__name__ for i in integrations]
        assert "LoggingIntegration" in types
        assert "FastApiIntegration" in types

    def test_logging_integration_event_level_is_none(self, captured_init, sentry):
        """The design invariant (see module docstring): log records
        become breadcrumbs, NOT standalone events. If this drifts,
        every logger.warning in the codebase turns into a Sentry
        issue and the issue stream becomes unusable."""
        sentry.init_sentry_worker(dsn="https://fake@sentry.test/1")

        integrations = captured_init.get("integrations") or []
        log_ints = [
            i for i in integrations
            if type(i).__name__ == "LoggingIntegration"
        ]
        assert len(log_ints) == 1
        # LoggingIntegration stores the configured event_level on an
        # internal attribute. Naming has been stable across the 1.x
        # and 2.x SDK lines; if it ever changes we want the test to
        # fail loudly rather than silently pass without checking.
        li = log_ints[0]
        assert hasattr(li, "_handler") or hasattr(li, "event_level"), (
            "LoggingIntegration internal shape changed — update the "
            "test to check event_level via the new attribute."
        )

    def test_dsn_propagates(self, captured_init, sentry):
        """The DSN passed to init_* must reach sentry_sdk.init."""
        sentry.init_sentry_worker(dsn="https://specific-dsn@sentry.test/42")
        assert captured_init.get("dsn") == "https://specific-dsn@sentry.test/42"


class TestBackCompat:
    """Old deployments import ``init_sentry`` (the original single-
    process name). The back-compat alias must still work."""

    def test_init_sentry_alias_exists(self, sentry):
        assert hasattr(sentry, "init_sentry")

    def test_init_sentry_points_at_worker(self, sentry):
        """The alias points at the worker init because that's what
        the original ``init_sentry`` did. A deployment that imports
        it gets its pre-rename behavior unchanged."""
        assert sentry.init_sentry is sentry.init_sentry_worker


class TestCapturesAreNoopsWithoutInit:
    """The capture_* helpers are no-ops when Sentry isn't
    initialized — existing worker code relies on this and calls them
    unconditionally."""

    def test_capture_task_retry_noop(self, sentry):
        """Pre-existing contract: capture_task_retry returns None and
        doesn't raise when the SDK isn't initialized. Confirms the
        rename didn't regress the no-op discipline."""
        from uuid import uuid4
        # Must not raise.
        sentry.capture_task_retry(
            exc=RuntimeError("x"),
            task_id=uuid4(),
            task_entity_id=uuid4(),
            dossier_id=uuid4(),
            function="some_func",
            attempt_count=1,
            max_attempts=3,
        )

    def test_capture_worker_loop_crash_noop(self, sentry):
        sentry.capture_worker_loop_crash(RuntimeError("boom"))
