"""Database session management."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

_engine = None
_session_factory = None

T = TypeVar("T")


async def init_db(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 20,
    pool_recycle: int = 1800,
    pool_timeout: int = 30,
):
    """Initialize the async engine and session factory.

    Pool defaults are tuned for a medium-load deployment:
    - pool_size=10: baseline persistent connections
    - max_overflow=20: burst capacity up to 30 total connections
    - pool_recycle=1800: recycle connections after 30min to avoid
      Postgres idle_in_transaction_session_timeout issues
    - pool_timeout=30: seconds to wait for a connection from the
      pool before raising an error
    """
    global _engine, _session_factory
    _engine = create_async_engine(
        database_url,
        echo=False,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=pool_recycle,
        pool_timeout=pool_timeout,
    )
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def create_tables():
    from .models import Base
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session_factory() -> async_sessionmaker:
    return _session_factory


def _is_deadlock_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is Postgres' deadlock_detected (SQLSTATE 40P01).

    SQLAlchemy wraps asyncpg errors in ``DBAPIError`` — the original
    exception is reachable via ``__cause__`` (set by ``raise ... from
    exc``) or via ``exc.orig``. We check both because different
    driver/wrapper versions have surfaced it differently in the past.
    SQLSTATE is the stable identifier; matching on the string message
    is fragile across Postgres locales.
    """
    if not isinstance(exc, DBAPIError):
        return False
    for candidate in (getattr(exc, "orig", None), exc.__cause__):
        sqlstate = getattr(candidate, "sqlstate", None)
        if sqlstate == "40P01":
            return True
    return False


async def run_with_deadlock_retry(
    work: Callable[[AsyncSession], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_backoff_seconds: float = 0.05,
) -> T:
    """Run ``work`` inside a transaction; retry if Postgres reports
    a deadlock.

    Bug 74 fix — defence in depth. The primary fix is structural
    (worker acquires the dossier lock in the same order user
    activities do, see ``worker._execute_claimed_task``), so this
    wrapper should in practice never fire. It's here as a safety net
    for future lock-order inversions we didn't anticipate: a retry
    under deadlock with a fresh transaction is safe because Postgres
    has already rolled the loser's work back.

    Contract for ``work``:
    * Receives an ``AsyncSession`` with an **already-open**
      transaction (``session.begin()`` has been entered).
    * May execute any SQL it needs; must not open nested
      transactions via ``session.begin()`` itself.
    * Returns an arbitrary value, which this function relays to
      the caller.

    Retry policy: exponential backoff with jitter, bounded by
    ``max_attempts``. The first retry waits ~base_backoff_seconds,
    the second ~2×base, the third ~4×base — same shape the worker
    uses for scheduled-task failures. Jitter is ±25% to avoid the
    thundering-herd effect where two deadlocked transactions both
    retry at the same instant and deadlock again.

    Only ``deadlock_detected`` (SQLSTATE 40P01) triggers a retry.
    Other DB errors, HTTP errors, and application errors bubble
    out unchanged — they're not racy and retrying them would mask
    real bugs.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        session_factory = get_session_factory()
        try:
            async with session_factory() as session:
                async with session.begin():
                    return await work(session)
        except Exception as exc:
            if not _is_deadlock_error(exc):
                raise
            last_exc = exc
            if attempt == max_attempts:
                logger.error(
                    "Deadlock on attempt %d/%d; giving up",
                    attempt, max_attempts,
                )
                raise
            backoff = base_backoff_seconds * (2 ** (attempt - 1))
            jittered = backoff * (1 + random.uniform(-0.25, 0.25))
            logger.warning(
                "Deadlock detected on attempt %d/%d; retrying in %.3fs",
                attempt, max_attempts, jittered,
            )
            await asyncio.sleep(jittered)

    # Unreachable — the loop either returns, raises on the last attempt,
    # or raises on a non-deadlock exception. Kept to satisfy type
    # checkers that can't prove the loop exits.
    assert last_exc is not None
    raise last_exc

