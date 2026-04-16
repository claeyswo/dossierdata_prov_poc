"""
Alembic environment configuration for the dossier_engine schema.

Supports both online (connected) and offline (SQL script) migration
modes. Uses asyncpg via SQLAlchemy's async engine, matching the
runtime engine configuration.

DB URL resolution order:
1. DOSSIER_DB_URL environment variable (for CI, Docker, etc.)
2. alembic.ini `sqlalchemy.url` (for local development)

The `target_metadata` points at our declarative Base so autogenerate
can diff the models against the database and produce migrations
automatically.
"""
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

# Import the declarative Base that carries all our table definitions.
# This is the single source of truth for the schema.
from dossier_engine.db.models import Base

# Alembic Config object — provides access to alembic.ini values.
config = context.config

# Set up Python logging from alembic.ini's [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata autogenerate compares against.
target_metadata = Base.metadata


def get_url() -> str:
    """Resolve the database URL.

    Environment variable wins over alembic.ini so deployments can
    override without editing files."""
    return os.environ.get(
        "DOSSIER_DB_URL",
        config.get_main_option("sqlalchemy.url"),
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout
    without connecting to the database.

    Useful for generating migration scripts that a DBA reviews
    and applies manually."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """Configure the migration context with a live connection
    and run all pending migrations."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine, connect, and run migrations.

    Uses NullPool because migration commands are short-lived
    processes — no point maintaining a connection pool."""
    connectable = create_async_engine(
        get_url(),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connected to a live
    database via asyncpg."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
