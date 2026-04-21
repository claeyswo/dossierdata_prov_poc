"""
Sentry integration for the dossier platform.

Two entry points — one per process kind:

* ``init_sentry_worker()`` — used by ``dossier_engine.worker``. Enables
  the ``LoggingIntegration`` so log records ride along as breadcrumbs
  (without becoming standalone events — see ``event_level=None`` below).

* ``init_sentry_fastapi(app)`` — used by ``dossier_engine.create_app``.
  Enables the ``FastApiIntegration`` in addition to ``LoggingIntegration``,
  so every unhandled exception in a request handler is captured with full
  request context (URL, method, status code, trace ID, sanitized
  headers). Works with the existing ``emit_audit`` / ``emit_dossier_audit``
  stream — Sentry sees code-level exceptions; the audit log sees
  authorization and lifecycle events.

Both entry points share the same underlying ``sentry_sdk.init`` call
via ``_init_sdk``. The ``_initialized`` guard means calling either
twice — or both in the same process — is a no-op after the first
success. The SDK itself also guards against double-init, but we check
ourselves too so we can report "already initialized" explicitly rather
than letting the SDK silently ignore the second call.

Design goals (opinionated, see README "Worker → Sentry" for rationale):

* **Retries don't flood Sentry.** All retry failures of a given task
  function collapse into a single issue fingerprinted by the function
  name. The event count inside that issue reflects how often the retry
  path fires — useful signal, not noise.

* **Dead-letters are per-task issues.** When a task escalates to
  ``dead_letter``, each dead-lettered task gets its own Sentry issue,
  fingerprinted by the task's logical entity id. Operators resolve
  them one by one (investigate, fix root cause, requeue).

* **Worker crashes are a single issue.** If the poll loop itself
  throws, we emit exactly one fatal event under a stable fingerprint.

* **No-op if Sentry isn't configured.** If ``sentry_sdk`` isn't
  installed or the DSN env var isn't set, every function in this
  module is a silent no-op. The worker and app run normally.

* **Log records are breadcrumbs, not events.** We configure
  ``LoggingIntegration(event_level=None)`` so ordinary log records
  don't become Sentry events on their own — only the explicit
  ``capture_*`` helpers (for the worker) and FastAPI's built-in
  request-error capture (for the app) produce events. This keeps
  fingerprinting discipline: breadcrumbs give context, explicit
  captures define issues.

Deployments wire Sentry by setting ``SENTRY_DSN`` in the process env.
``SENTRY_RELEASE`` and ``SENTRY_ENVIRONMENT`` are read automatically by
the SDK.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID

_log = logging.getLogger("dossier.sentry")

try:  # sentry_sdk is an optional dependency
    import sentry_sdk as _sentry_sdk  # type: ignore
    from sentry_sdk.integrations.logging import (  # type: ignore
        LoggingIntegration as _LoggingIntegration,
    )
    # FastApiIntegration is only imported if the SDK itself is
    # available; if the SDK isn't installed, the whole block fails
    # and everything becomes a no-op. The FastApi integration itself
    # has been in sentry_sdk since 1.14 (2023) so no version guard.
    from sentry_sdk.integrations.fastapi import (  # type: ignore
        FastApiIntegration as _FastApiIntegration,
    )
    _SENTRY_AVAILABLE = True
except ImportError:
    _sentry_sdk = None  # type: ignore
    _LoggingIntegration = None  # type: ignore
    _FastApiIntegration = None  # type: ignore
    _SENTRY_AVAILABLE = False


_initialized = False


def _init_sdk(
    dsn: str | None,
    *,
    process_kind: str,
    extra_integrations: list | None = None,
) -> bool:
    """Shared SDK initialization used by both entry points.

    Builds the integrations list with ``LoggingIntegration`` always
    present (and ``event_level=None`` so log records become
    breadcrumbs, not standalone events), plus whatever the caller
    passed in ``extra_integrations`` (typically ``FastApiIntegration``
    for the app).

    Returns True on success, False if the SDK isn't installed, no DSN
    was provided, or we were already initialized in this process.
    Caller-facing ``init_sentry_worker`` / ``init_sentry_fastapi``
    translate the return value into log lines.
    """
    global _initialized
    if _initialized:
        # Already set up — second init request is a no-op. The SDK
        # would tolerate a double-init too, but reporting "already"
        # lets the caller distinguish "we just wired it up" from
        # "someone else did."
        _log.debug(
            "Sentry already initialized; ignoring %s re-init",
            process_kind,
        )
        return False

    if not _SENTRY_AVAILABLE:
        _log.debug(
            "sentry_sdk not installed; Sentry integration disabled (%s)",
            process_kind,
        )
        return False

    effective_dsn = dsn or os.environ.get("SENTRY_DSN")
    if not effective_dsn:
        _log.debug(
            "SENTRY_DSN not set; Sentry integration disabled (%s)",
            process_kind,
        )
        return False

    integrations = [
        _LoggingIntegration(
            level=logging.INFO,      # capture INFO+ as breadcrumbs
            event_level=None,        # but DON'T create events from log records
        ),
    ]
    if extra_integrations:
        integrations.extend(extra_integrations)

    _sentry_sdk.init(
        dsn=effective_dsn,
        integrations=integrations,
        # Release and environment can be set by the deployment via
        # SENTRY_RELEASE and SENTRY_ENVIRONMENT env vars (sentry_sdk
        # reads those automatically).
    )
    _initialized = True
    _log.info("Sentry initialized for %s", process_kind)
    return True


def init_sentry_worker(dsn: str | None = None) -> bool:
    """Initialize the Sentry SDK for the worker process.

    Reads ``SENTRY_DSN`` from the environment when ``dsn`` is None.
    Returns True if Sentry was initialized, False if skipped (SDK not
    installed, no DSN configured, or already initialized).

    Enables ``LoggingIntegration`` so log records become breadcrumbs
    (with ``event_level=None`` so they don't become events on their
    own). Events are produced explicitly by the ``capture_task_retry``
    / ``capture_task_dead_letter`` / ``capture_worker_loop_crash``
    helpers below, which control fingerprinting.
    """
    return _init_sdk(dsn, process_kind="worker")


def init_sentry_fastapi(app, dsn: str | None = None) -> bool:
    """Initialize the Sentry SDK for the FastAPI app process.

    Reads ``SENTRY_DSN`` from the environment when ``dsn`` is None.
    Returns True if Sentry was initialized, False if skipped (SDK not
    installed, no DSN configured, or already initialized).

    Adds ``FastApiIntegration`` on top of ``LoggingIntegration``, so
    every unhandled exception raised from a request handler is
    captured with the request URL, method, status code, and (SDK-
    sanitized) headers. The FastAPI integration hooks via the ASGI
    middleware stack, so explicit middleware registration is not
    needed — the SDK instruments FastAPI as soon as ``init`` is
    called.

    The ``app`` parameter is accepted for future-proofing and symmetry
    with integrations like the Starlette or Django ones that want an
    app handle; today FastApiIntegration doesn't take one. Kept in the
    signature so callers don't need to change when the SDK version
    changes the integration API.
    """
    # The explicit ``app`` handle isn't consumed by the current SDK;
    # suppressing the ``unused`` warning without an underscore-rename
    # so the call-site signature stays stable.
    del app  # noqa: F841
    extra = [_FastApiIntegration()] if _SENTRY_AVAILABLE else None
    return _init_sdk(dsn, process_kind="fastapi", extra_integrations=extra)


# Back-compat alias. The original single-process module exposed
# ``init_sentry``; any deployment script or external tooling that
# imports that name keeps working. Points at the worker init because
# that's what the old name actually did.
init_sentry = init_sentry_worker


def _noop_context() -> Iterator[None]:
    yield


@contextmanager
def _scope_or_noop() -> Iterator[Any]:
    """Yield a Sentry scope if SDK is active, otherwise a no-op."""
    if _SENTRY_AVAILABLE and _initialized:
        with _sentry_sdk.push_scope() as scope:
            yield scope
    else:
        yield None


def capture_task_retry(
    *,
    exc: BaseException,
    task_id: UUID,
    task_entity_id: UUID,
    dossier_id: UUID,
    function: str | None,
    attempt_count: int,
    max_attempts: int,
) -> None:
    """Emit a WARNING-level Sentry event for a single retry failure.

    Fingerprint: ``["worker.task.retry", <function>]`` — all retries
    of the same function collapse into one issue. Event count inside
    the issue reflects retry frequency; individual events carry the
    specific task id as a tag so operators can drill down.

    No-op if Sentry isn't initialized.
    """
    if not (_SENTRY_AVAILABLE and _initialized):
        return
    with _scope_or_noop() as scope:
        if scope is None:
            return
        scope.set_level("warning")
        scope.set_tag("task_id", str(task_id))
        scope.set_tag("task_entity_id", str(task_entity_id))
        scope.set_tag("dossier_id", str(dossier_id))
        scope.set_tag("task_function", function or "<unknown>")
        scope.set_tag("task_attempt", str(attempt_count))
        scope.set_tag("task_phase", "retry")
        scope.set_extra("max_attempts", max_attempts)
        scope.set_fingerprint([
            "worker.task.retry",
            function or "<unknown>",
        ])
        _sentry_sdk.capture_exception(exc)


def capture_task_dead_letter(
    *,
    exc: BaseException,
    task_id: UUID,
    task_entity_id: UUID,
    dossier_id: UUID,
    function: str | None,
    attempt_count: int,
    max_attempts: int,
) -> None:
    """Emit an ERROR-level Sentry event for a task that gave up.

    Fingerprint: ``["worker.task.dead_letter", <function>,
    <task_entity_id>]`` — each dead-lettered task is its own issue
    because operators need to resolve them individually (investigate,
    fix, requeue via ``--requeue-dead-letters``).

    No-op if Sentry isn't initialized.
    """
    if not (_SENTRY_AVAILABLE and _initialized):
        return
    with _scope_or_noop() as scope:
        if scope is None:
            return
        scope.set_level("error")
        scope.set_tag("task_id", str(task_id))
        scope.set_tag("task_entity_id", str(task_entity_id))
        scope.set_tag("dossier_id", str(dossier_id))
        scope.set_tag("task_function", function or "<unknown>")
        scope.set_tag("task_attempt", str(attempt_count))
        scope.set_tag("task_phase", "dead_letter")
        scope.set_extra("max_attempts", max_attempts)
        scope.set_fingerprint([
            "worker.task.dead_letter",
            function or "<unknown>",
            str(task_entity_id),
        ])
        _sentry_sdk.capture_exception(exc)


def capture_worker_loop_crash(exc: BaseException) -> None:
    """Emit a FATAL-level Sentry event for a worker loop crash.

    Fingerprint: ``["worker.loop.crash"]`` — all such events group
    into a single issue. This is the "the worker itself died"
    signal. Different from task failures, which are handled by the
    per-task capture functions above.

    No-op if Sentry isn't initialized.
    """
    if not (_SENTRY_AVAILABLE and _initialized):
        return
    with _scope_or_noop() as scope:
        if scope is None:
            return
        scope.set_level("fatal")
        scope.set_fingerprint(["worker.loop.crash"])
        _sentry_sdk.capture_exception(exc)
