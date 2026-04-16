"""
Fixture smoke test — proves the DB-backed fixtures actually work.

This file exists to catch fixture-wiring bugs before they get
obscured by test-specific complexity. If `test_fixture_smoke.py`
fails, every integration test will fail in confusing ways; fix the
fixture first, then the downstream tests should just work.

The three things this file verifies:

1. The session-scoped fixture can connect to the test DB and the
   tables exist after `create_tables` runs.
2. The function-scoped fixture yields a usable session on which
   queries succeed and writes commit.
3. Truncation between tests actually works — we insert a row in
   one test and the next test sees an empty table.
"""
from __future__ import annotations

from sqlalchemy import text


async def test_db_reachable(db_session):
    """The function-scoped fixture gives us a real session that can
    run a trivial query."""
    result = await db_session.execute(text("SELECT 1 AS x"))
    assert result.scalar() == 1


async def test_schema_exists(db_session):
    """All seven engine tables are present in the test database
    after `create_tables` has run. If this fails, either
    `create_tables()` isn't being called or it's being called
    against the wrong DB."""
    result = await db_session.execute(
        text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN (
                'dossiers', 'activities', 'associations', 'entities',
                'used', 'activity_relations', 'agents'
              )
            ORDER BY table_name
        """)
    )
    names = [row[0] for row in result.fetchall()]
    assert names == [
        "activities", "activity_relations", "agents", "associations",
        "dossiers", "entities", "used",
    ]


async def test_insert_visible_within_same_session(db_session):
    """A write inside the session is readable inside the same
    session before commit. This is the baseline for all
    write-and-verify tests that will follow."""
    await db_session.execute(
        text("INSERT INTO dossiers (id, workflow) "
             "VALUES ('00000000-0000-0000-0000-000000000001', 'toelatingen')")
    )
    result = await db_session.execute(
        text("SELECT workflow FROM dossiers WHERE id = '00000000-0000-0000-0000-000000000001'")
    )
    assert result.scalar() == "toelatingen"


async def test_truncation_between_tests(db_session):
    """Runs after `test_insert_visible_within_same_session`. If the
    truncate fixture works, this test sees an empty dossiers table
    even though the previous test inserted a row. The test order
    matters — pytest runs tests in file order by default, and both
    tests live in this same module."""
    result = await db_session.execute(text("SELECT COUNT(*) FROM dossiers"))
    assert result.scalar() == 0
