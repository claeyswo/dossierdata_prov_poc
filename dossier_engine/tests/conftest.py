"""
Shared pytest fixtures for the dossier_engine test suite.

This module has two layers of fixtures:

1. **Pure fixtures** — no I/O, no DB, no async. Used by unit tests
   in `tests/unit/`. The main one is `stub_state`, documented
   below.

2. **DB-backed fixtures** — set up a dedicated Postgres test
   database (`dossiers_test`), create the schema once per pytest
   session, truncate tables between tests. Used by integration
   tests in `tests/integration/`. The main ones are
   `db_session_factory` (session-scoped, creates the schema) and
   `db_session` (function-scoped, yields a fresh session with
   truncated tables).

Design notes for the pure fixtures:

The engine's pipeline phases are all functions that take an
`ActivityState` and mutate it. To test a phase in isolation, we need
to construct an `ActivityState` with just enough shape to satisfy the
fields the phase reads — and ideally *nothing more*, so that if the
phase accidentally reaches into a field it shouldn't be reading, the
test crashes loudly instead of happily receiving a convenient default.

That's why `stub_state` below is a thin factory with `None` for
everything the dataclass requires but isn't going to touch. Tests
override whichever fields their phase-under-test actually reads.

Keeping fixtures narrow also makes tests self-documenting: if a test
constructs a state with `activity_def={"built_in": True}` and nothing
else, a reader knows the phase under test reads `built_in` and probably
not much else. If the fixture instead pre-populated twenty fields with
plausible-looking values, every test would be an exercise in figuring
out which fields mattered.

Design notes for the DB fixtures:

The test DB is a SEPARATE database (`dossiers_test`) from the one
the dossier API uses (`dossiers`). This isolation is deliberate — it
means you can run pytest against a live system without stomping on
real data, and a developer debugging via `bash test_requests.sh` in
one terminal won't poison the pytest run in another.

We use "shared schema + truncate between tests" rather than
"fresh schema per test" or "fresh database per test" because it's
the fastest option: schema creation is a few hundred ms,
`TRUNCATE ... CASCADE` is sub-millisecond. The tradeoff is that
tests can't run in parallel against the same database — fine
for now because pytest defaults to sequential execution.

The engine's `init_db` / `get_session_factory` uses module-level
singletons for the async engine and sessionmaker. The session-scoped
`db_session_factory` fixture calls `init_db(TEST_DB_URL)` exactly
once, pointing those globals at the test database for the rest of
the pytest process. We don't restore them at teardown because there
are no production callers in the test process to restore them to —
when pytest exits, the process exits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from dossier_engine.engine.state import ActivityState, Caller
from dossier_engine.db import init_db, create_tables, get_session_factory
from dossier_engine.db.models import Repository


# Dedicated test database — created once out-of-band via:
#   su postgres -c "psql -c \"CREATE DATABASE dossiers_test OWNER dossier;\""
# Tests truncate all tables between runs to isolate state. Changing
# this URL to point at `dossiers` (the production DB) would let
# pytest destroy real data — don't do that.
TEST_DB_URL = "postgresql+asyncpg://dossier:dossier@127.0.0.1:5432/dossiers_test"


@pytest.fixture
def stub_state():
    """Factory that builds a minimal ActivityState.

    Returns a callable. Test code overrides whichever fields it
    actually cares about; everything else is either `None` (for
    phase-injected dependencies we won't be exercising) or an empty
    list / dict (for collections the phase-under-test may append to).

    Typical usage:

        def test_something(stub_state):
            state = stub_state(
                activity_def={"name": "foo", "built_in": False},
                used_refs=[{"entity": "oe:x/e1@v1"}],
                generated_items=[{"entity": "oe:y/e2@v2"}],
            )
            some_phase(state)
            assert state.used_refs == [...]

    Any field set to `None` by the factory that the phase-under-test
    actually touches will raise an AttributeError — which is exactly
    what we want, because it means the test wasn't set up to exercise
    that phase correctly. Fix the test by passing the field explicitly
    rather than by loosening the stub.
    """
    def _factory(**overrides: Any) -> ActivityState:
        defaults: dict[str, Any] = {
            # Required fields the dataclass won't let us omit. All
            # set to None or stub values — tests override the ones
            # they exercise.
            "plugin": None,
            "activity_def": {},
            "repo": None,
            "dossier_id": uuid4(),
            "activity_id": uuid4(),
            "user": None,
            "role": "",
            "used_items": [],
            "generated_items": [],
            "relation_items": [],
            # Optional fields with their dataclass defaults. Listed
            # explicitly here so tests that need to override them
            # (e.g. anchor_entity_id for anchor tests) can do so via
            # the factory kwargs.
            "workflow_name": None,
            "informed_by": None,
            "skip_cache": False,
            "caller": Caller.CLIENT,
            "anchor_entity_id": None,
            "anchor_type": None,
            "now": datetime.now(timezone.utc),
        }
        defaults.update(overrides)
        return ActivityState(**defaults)

    return _factory


# --------------------------------------------------------------------
# DB-backed fixtures (for tests in tests/integration/)
# --------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_session_factory():
    """Initialize the test database once per pytest session.

    Calls `init_db(TEST_DB_URL)` which installs the async engine and
    sessionmaker on the `dossier_engine.db.session` module-level
    globals. Runs `create_tables()` to make sure the schema exists.
    Yields the session factory. No teardown — the test process
    exits when pytest finishes and the globals die with it.

    Because this fixture is session-scoped, schema creation happens
    exactly once for the entire pytest run, not once per test.
    """
    await init_db(TEST_DB_URL)
    await create_tables()
    yield get_session_factory()


@pytest_asyncio.fixture
async def db_session(db_session_factory):
    """Yield a fresh session with truncated tables.

    Before the test runs, every table is truncated via one
    `TRUNCATE ... RESTART IDENTITY CASCADE` statement. That's a few
    hundred microseconds — fast enough to do per test, strong enough
    to guarantee tests don't leak state between each other.

    Tables must be listed explicitly (not discovered from metadata)
    because the order matters for readability and because if someone
    adds a new table without updating this list, the test suite
    surfaces it as a failure rather than silently leaking state.
    CASCADE handles the foreign-key dependencies automatically.
    """
    tables = [
        "used",
        "activity_relations",
        "associations",
        "agents",
        "entities",
        "activities",
        "dossiers",
    ]
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
            )
    async with db_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def repo(db_session):
    """Shortcut: yield a Repository wrapped around the fresh session.

    Most tests want the Repository, not the raw session — it's the
    API surface for every pipeline phase. This fixture lets tests
    say `async def test_x(repo):` instead of
    `async def test_x(db_session): repo = Repository(db_session)`.
    """
    return Repository(db_session)
