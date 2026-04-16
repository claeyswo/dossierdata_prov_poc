"""
Sentry integration for the dossier engine, scoped to the worker.

Goals (opinionated, see README "Worker → Sentry" for the rationale):

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
  module is a silent no-op. The worker runs normally.

Deployments wire Sentry by setting ``SENTRY_DSN`` in the worker's env.
We intentionally disable the ``LoggingIntegration``'s ``event_level``
so ordinary log records don't become Sentry events on their own —
only the explicit ``capture_*`` calls below produce events. Log
records still ride along as breadcrumbs for context.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID

_log = logging.getLogger("dossier.worker.sentry")

try:  # sentry_sdk is an optional dependency
    import sentry_sdk as _sentry_sdk  # type: ignore
    from sentry_sdk.integrations.logging import (  # type: ignore
        LoggingIntegration as _LoggingIntegration,
    )
    _SENTRY_AVAILABLE = True
except ImportError:
    _sentry_sdk = None  # type: ignore
    _LoggingIntegration = None  # type: ignore
    _SENTRY_AVAILABLE = False


_initialized = False


def init_sentry(dsn: str | None = None) -> bool:
    """Initialize the Sentry SDK for the worker.

    Reads ``SENTRY_DSN`` from the environment when ``dsn`` is None.
    Returns True if Sentry was initialized, False if skipped (either
    SDK not installed, or no DSN configured). Safe to call multiple
    times — subsequent calls are no-ops after the first init.

    The ``LoggingIntegration`` is configured with
    ``event_level=None``, so log records become breadcrumbs only, not
    standalone Sentry events. Events are produced explicitly by the
    ``capture_*`` helpers in this module, which gives us control over
    fingerprinting and grouping.
    """
    global _initialized
    if _initialized:
        return True

    if not _SENTRY_AVAILABLE:
        _log.debug("sentry_sdk not installed; Sentry integration disabled")
        return False

    effective_dsn = dsn or os.environ.get("SENTRY_DSN")
    if not effective_dsn:
        _log.debug("SENTRY_DSN not set; Sentry integration disabled")
        return False

    _sentry_sdk.init(
        dsn=effective_dsn,
        integrations=[
            _LoggingIntegration(
                level=logging.INFO,      # capture INFO+ as breadcrumbs
                event_level=None,        # but DON'T create events from log records
            ),
        ],
        # Release and environment can be set by the deployment via
        # SENTRY_RELEASE and SENTRY_ENVIRONMENT env vars (sentry_sdk
        # reads those automatically).
    )
    _initialized = True
    _log.info("Sentry initialized for worker")
    return True


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
