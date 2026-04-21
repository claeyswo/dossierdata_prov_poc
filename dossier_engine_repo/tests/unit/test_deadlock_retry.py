"""Tests for dossier_engine.db.session's deadlock-retry helper.

Covers the Bug 74 defence-in-depth layer: ``run_with_deadlock_retry``
wraps a unit of work in a transaction and retries if Postgres
reports a deadlock (SQLSTATE 40P01). Other exceptions bubble out
unchanged.

The primary Bug 74 fix is structural (worker takes the dossier
lock before any entity INSERTs, matching the user-side lock order),
so in production this wrapper should very rarely fire. These tests
pin the contract down so a future refactor can't silently break
the safety net.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError

from dossier_engine.db.session import (
    _is_deadlock_error, run_with_deadlock_retry,
)


def _make_deadlock_error() -> DBAPIError:
    """Construct a DBAPIError with sqlstate=40P01 on its orig.

    Mimics what asyncpg's DeadlockDetectedError looks like after
    SQLAlchemy wraps it. Real asyncpg errors inherit from a chain we
    don't want to rebuild in test fixtures; duck-typing sqlstate on
    a mock is sufficient because that's the only attribute the
    detector reads.

    Note: We construct DBAPIError directly rather than via
    ``DBAPIError.instance``, which downgrades the wrapping class
    to StatementError when the base exception isn't a real DBAPI
    base. Direct construction keeps the isinstance check honest."""
    orig = MagicMock()
    orig.sqlstate = "40P01"
    return DBAPIError("SELECT 1", None, orig)


def _make_non_deadlock_db_error(sqlstate: str = "42P01") -> DBAPIError:
    """DBAPIError that isn't a deadlock. 42P01 is undefined_table —
    a real-but-different postgres error."""
    orig = MagicMock()
    orig.sqlstate = sqlstate
    return DBAPIError("SELECT 1", None, orig)


class TestIsDeadlockError:

    def test_deadlock_detected_true(self):
        assert _is_deadlock_error(_make_deadlock_error()) is True

    def test_other_db_error_false(self):
        """Any DBAPIError with a sqlstate other than 40P01 must NOT
        be treated as a deadlock. Retrying those would mask real
        bugs (unique-constraint violation, undefined table, etc.)."""
        assert _is_deadlock_error(_make_non_deadlock_db_error()) is False

    def test_non_db_error_false(self):
        """Plain Python exceptions aren't deadlocks. The detector
        must not false-positive on generic errors that happen to
        flow through the wrapper."""
        assert _is_deadlock_error(ValueError("oops")) is False
        assert _is_deadlock_error(RuntimeError("nope")) is False

    def test_db_error_via_cause_chain(self):
        """SQLAlchemy can surface the sqlstate via __cause__ in some
        driver/wrapper combinations. The detector checks both
        ``.orig`` and ``__cause__`` to be robust across versions."""
        # Construct a DBAPIError where orig has no sqlstate but
        # __cause__ does. __cause__ must be a real exception for
        # Python to accept the assignment, so we subclass Exception
        # and attach the sqlstate attribute.
        orig_without_sqlstate = MagicMock(spec=[])  # no sqlstate attr
        err = DBAPIError("SELECT 1", None, orig_without_sqlstate)

        class FakeDriverError(Exception):
            sqlstate = "40P01"

        err.__cause__ = FakeDriverError("deadlock")
        assert _is_deadlock_error(err) is True


class TestRunWithDeadlockRetry:

    @pytest.fixture(autouse=True)
    def _patch_session_factory(self):
        """Replace the module-level session factory with a mock so
        tests don't touch a real DB. The wrapper is a
        control-flow-only concern — the DB interaction belongs in
        integration tests, not here."""
        # Build a session factory that returns a session mock
        # which supports `async with session:` and
        # `async with session.begin():`.
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=tx)
        tx.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=tx)

        factory = MagicMock(return_value=session)

        with patch(
            "dossier_engine.db.session.get_session_factory",
            return_value=factory,
        ):
            yield session

    async def test_succeeds_on_first_attempt(self):
        """If the work function returns cleanly, we don't retry.
        No ``time.sleep`` calls, the return value bubbles up
        directly."""
        work = AsyncMock(return_value="OK")
        result = await run_with_deadlock_retry(work)
        assert result == "OK"
        assert work.await_count == 1

    async def test_retries_on_deadlock_then_succeeds(self):
        """Deadlock on attempt 1, success on attempt 2. The second
        attempt should happen in a fresh transaction — we can tell
        because the work function sees a clean call history."""
        # First call raises a deadlock, second returns normally.
        work = AsyncMock(
            side_effect=[_make_deadlock_error(), "OK"],
        )
        # Patch out the sleep so the test runs instantly.
        with patch("dossier_engine.db.session.asyncio.sleep",
                   new=AsyncMock()):
            result = await run_with_deadlock_retry(
                work, max_attempts=3, base_backoff_seconds=0.001,
            )
        assert result == "OK"
        assert work.await_count == 2

    async def test_gives_up_after_max_attempts(self):
        """All ``max_attempts`` attempts deadlock — re-raise the
        last deadlock error. Caller decides whether to 500 or
        surface a nicer retry-later response."""
        work = AsyncMock(side_effect=_make_deadlock_error())
        with patch("dossier_engine.db.session.asyncio.sleep",
                   new=AsyncMock()):
            with pytest.raises(DBAPIError):
                await run_with_deadlock_retry(
                    work, max_attempts=3, base_backoff_seconds=0.001,
                )
        assert work.await_count == 3

    async def test_non_deadlock_error_not_retried(self):
        """A non-deadlock DBAPIError (say, a unique-constraint
        violation) raises on the first attempt without retrying.
        Retrying those would mask bugs and double-execute any
        side effects the work function already performed before
        hitting the constraint."""
        err = _make_non_deadlock_db_error()
        work = AsyncMock(side_effect=err)
        with pytest.raises(DBAPIError):
            await run_with_deadlock_retry(work, max_attempts=3)
        assert work.await_count == 1

    async def test_application_error_not_retried(self):
        """ActivityError, HTTPException, ValueError — all bubble
        out on attempt 1. The wrapper is strictly a deadlock
        safety net, not a generic retry mechanism."""
        work = AsyncMock(side_effect=ValueError("business rule"))
        with pytest.raises(ValueError, match="business rule"):
            await run_with_deadlock_retry(work, max_attempts=3)
        assert work.await_count == 1

    async def test_single_attempt_still_raises_on_deadlock(self):
        """``max_attempts=1`` means "try once, don't retry." The
        deadlock still propagates; the wrapper just doesn't sleep
        and doesn't retry. Useful for call sites that want the
        error visibility without the retry behaviour."""
        work = AsyncMock(side_effect=_make_deadlock_error())
        with pytest.raises(DBAPIError):
            await run_with_deadlock_retry(work, max_attempts=1)
        assert work.await_count == 1

    async def test_backoff_grows_exponentially(self):
        """First retry ~base, second ~2×base, third ~4×base.
        Check the sequence of sleep-call arguments to confirm the
        exponent is right. Jitter is ±25%, so we bracket-check."""
        work = AsyncMock(
            side_effect=[
                _make_deadlock_error(),
                _make_deadlock_error(),
                _make_deadlock_error(),
                "OK",
            ]
        )
        sleeper = AsyncMock()
        with patch("dossier_engine.db.session.asyncio.sleep", new=sleeper):
            await run_with_deadlock_retry(
                work, max_attempts=4, base_backoff_seconds=0.1,
            )
        # Three sleeps between four attempts.
        delays = [call.args[0] for call in sleeper.await_args_list]
        assert len(delays) == 3
        # With ±25% jitter: attempt 2 waits 0.075..0.125, attempt 3
        # waits 0.15..0.25, attempt 4 waits 0.3..0.5.
        assert 0.075 <= delays[0] <= 0.125
        assert 0.15 <= delays[1] <= 0.25
        assert 0.3 <= delays[2] <= 0.5
