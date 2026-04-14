"""Database session management."""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

_engine = None
_session_factory = None


async def init_db(database_url: str):
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def create_tables():
    from .models import Base
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session_factory() -> async_sessionmaker:
    return _session_factory
