"""Database session management."""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

_engine = None
_session_factory = None


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
