"""
Integration tests for `Repository` methods that aren't yet
covered at the direct-call level.

Most Repository methods are exercised transitively by the
pipeline phase tests, but a few have their own interesting
branches (sorting, filtering, cache behavior) that a phase test
can't reliably pin down in isolation. This file covers them
directly.

Methods covered:

* `get_entity_versions` — returns all versions of a logical
  entity in creation order. Distinct from `get_entities_by_type`
  (type-scoped) and `get_latest_entity_by_id` (latest-only).
* `get_all_latest_entities` — one row per distinct entity_id,
  each being the newest version. Used by the post-activity hook
  and the replay-response builder.
* `get_entities_by_type_latest` — latest per logical entity,
  scoped to one type. For singleton types returns at most one,
  for multi-cardinality one per distinct entity_id.
* `entity_type_exists` — boolean existence check used by
  `validate_workflow_rules` for required-entity-type rules.
* `get_singleton_entity` — latest entity of a type, ordered by
  created_at desc. Does NOT enforce cardinality (that's the
  engine layer's job via `lookup_singleton`).
* `create_used` / `get_used_entity_ids_for_activity` — used-link
  round-trip.
* `get_entities_generated_by_activity` — entities attributed to
  an activity via `generated_by`.
* `get_used_entities_for_activity` — entities in the activity's
  used block (join through the `used` table).
* `create_relation` / `get_relations_for_activity` — relation
  round-trip.
* `ensure_agent` idempotency — re-ensuring an existing agent
  doesn't produce a duplicate.
* Repository cache behavior — `_dossier_cache` and
  `_activities_cache` populated after writes.

These are all short fast tests against the shared DB fixture.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from dossier_engine.db.models import (
    Repository, AssociationRow, RelationRow,
)


D1 = UUID("11111111-1111-1111-1111-111111111111")
D2 = UUID("22222222-2222-2222-2222-222222222222")


async def _setup_dossier_and_activity(repo: Repository, dossier_id: UUID = D1):
    """Create the dossier + one bootstrap activity + association."""
    await repo.create_dossier(dossier_id, "toelatingen")
    await repo.ensure_agent("system", "systeem", "Systeem", {})
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=dossier_id, type="systemAction",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


# --------------------------------------------------------------------
# get_entity_versions
# --------------------------------------------------------------------


class TestGetEntityVersions:

    async def test_single_version_returned(self, repo):
        boot = await _setup_dossier_and_activity(repo)
        eid = uuid4()
        vid = uuid4()
        await repo.create_entity(
            version_id=vid, entity_id=eid, dossier_id=D1,
            type="oe:aanvraag", generated_by=boot,
            content={}, attributed_to="system",
        )
        await repo.session.flush()

        versions = await repo.get_entity_versions(D1, eid)
        assert len(versions) == 1
        assert versions[0].id == vid

    async def test_multiple_versions_in_creation_order(self, repo):
        """Seeds 3 versions with 1ms sleeps between them, then
        asserts the returned list is in creation order (oldest
        first). Locks in the ASC ordering."""
        boot = await _setup_dossier_and_activity(repo)
        eid = uuid4()
        vids: list[UUID] = []
        for i in range(3):
            vid = uuid4()
            await repo.create_entity(
                version_id=vid, entity_id=eid, dossier_id=D1,
                type="oe:aanvraag", generated_by=boot,
                content={"i": i}, attributed_to="system",
            )
            await repo.session.flush()
            vids.append(vid)
            await asyncio.sleep(0.002)

        versions = await repo.get_entity_versions(D1, eid)
        assert [v.id for v in versions] == vids
        assert [v.content["i"] for v in versions] == [0, 1, 2]

    async def test_unknown_entity_id_returns_empty(self, repo):
        await _setup_dossier_and_activity(repo)
        versions = await repo.get_entity_versions(D1, uuid4())
        assert versions == []


# --------------------------------------------------------------------
# get_all_latest_entities
# --------------------------------------------------------------------


class TestGetAllLatestEntities:

    async def test_one_row_per_logical_entity(self, repo):
        """Three distinct logical entities, each with two
        versions. Result: three rows, each being the latest
        version of its logical entity."""
        boot = await _setup_dossier_and_activity(repo)
        latest_ids: set[UUID] = set()
        for name in ("a", "b", "c"):
            eid = uuid4()
            v1 = uuid4()
            await repo.create_entity(
                version_id=v1, entity_id=eid, dossier_id=D1,
                type="oe:aanvraag", generated_by=boot,
                content={"n": name, "v": 1}, attributed_to="system",
            )
            await repo.session.flush()
            await asyncio.sleep(0.002)
            v2 = uuid4()
            await repo.create_entity(
                version_id=v2, entity_id=eid, dossier_id=D1,
                type="oe:aanvraag", generated_by=boot,
                content={"n": name, "v": 2}, attributed_to="system",
            )
            await repo.session.flush()
            await asyncio.sleep(0.002)
            latest_ids.add(v2)

        result = await repo.get_all_latest_entities(D1)
        # Each returned row is the v2 version (content v=2)
        assert len(result) == 3
        assert {r.id for r in result} == latest_ids
        assert all(r.content["v"] == 2 for r in result)

    async def test_other_dossier_not_included(self, repo):
        """Entities in D2 must not appear in the D1 result."""
        boot1 = await _setup_dossier_and_activity(repo, D1)
        boot2 = await _setup_dossier_and_activity(repo, D2)
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=boot1,
            content={}, attributed_to="system",
        )
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D2,
            type="oe:aanvraag", generated_by=boot2,
            content={}, attributed_to="system",
        )
        await repo.session.flush()

        result_d1 = await repo.get_all_latest_entities(D1)
        assert len(result_d1) == 1
        assert result_d1[0].dossier_id == D1

    async def test_empty_dossier_returns_empty(self, repo):
        await _setup_dossier_and_activity(repo)
        result = await repo.get_all_latest_entities(D1)
        assert result == []


# --------------------------------------------------------------------
# get_entities_by_type_latest
# --------------------------------------------------------------------


class TestGetEntitiesByTypeLatest:

    async def test_latest_version_per_logical_entity(self, repo):
        """Two logical entities of type oe:bijlage, each with
        two versions. Result: two rows, each the v2 version."""
        boot = await _setup_dossier_and_activity(repo)
        for label in ("a", "b"):
            eid = uuid4()
            await repo.create_entity(
                version_id=uuid4(), entity_id=eid, dossier_id=D1,
                type="oe:bijlage", generated_by=boot,
                content={"label": label, "v": 1}, attributed_to="system",
            )
            await repo.session.flush()
            await asyncio.sleep(0.002)
            await repo.create_entity(
                version_id=uuid4(), entity_id=eid, dossier_id=D1,
                type="oe:bijlage", generated_by=boot,
                content={"label": label, "v": 2}, attributed_to="system",
            )
            await repo.session.flush()
            await asyncio.sleep(0.002)

        result = await repo.get_entities_by_type_latest(D1, "oe:bijlage")
        assert len(result) == 2
        assert all(r.content["v"] == 2 for r in result)

    async def test_type_scope_excludes_other_types(self, repo):
        """Seeded oe:aanvraag and oe:bijlage. A type-scoped call
        only returns the matching type."""
        boot = await _setup_dossier_and_activity(repo)
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=boot,
            content={}, attributed_to="system",
        )
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
            type="oe:bijlage", generated_by=boot,
            content={}, attributed_to="system",
        )
        await repo.session.flush()

        result = await repo.get_entities_by_type_latest(D1, "oe:aanvraag")
        assert len(result) == 1
        assert result[0].type == "oe:aanvraag"

    async def test_empty_type_returns_empty(self, repo):
        await _setup_dossier_and_activity(repo)
        result = await repo.get_entities_by_type_latest(D1, "oe:absent")
        assert result == []


# --------------------------------------------------------------------
# entity_type_exists
# --------------------------------------------------------------------


class TestEntityTypeExists:

    async def test_type_present_returns_true(self, repo):
        boot = await _setup_dossier_and_activity(repo)
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=boot,
            content={}, attributed_to="system",
        )
        await repo.session.flush()
        assert await repo.entity_type_exists(D1, "oe:aanvraag") is True

    async def test_type_absent_returns_false(self, repo):
        await _setup_dossier_and_activity(repo)
        assert await repo.entity_type_exists(D1, "oe:absent") is False

    async def test_type_in_other_dossier_returns_false(self, repo):
        """Existence is dossier-scoped. Same type in D2 doesn't
        count for D1."""
        boot2 = await _setup_dossier_and_activity(repo, D2)
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D2,
            type="oe:aanvraag", generated_by=boot2,
            content={}, attributed_to="system",
        )
        await _setup_dossier_and_activity(repo, D1)
        await repo.session.flush()
        assert await repo.entity_type_exists(D1, "oe:aanvraag") is False


# --------------------------------------------------------------------
# get_singleton_entity
# --------------------------------------------------------------------


class TestGetSingletonEntity:

    async def test_returns_latest_when_multiple_versions(self, repo):
        """The method orders by `created_at DESC LIMIT 1`, so
        when there are multiple rows of the same type it returns
        the most recent. This is cardinality-unaware — the
        engine layer is responsible for ensuring only singleton
        types are looked up this way."""
        boot = await _setup_dossier_and_activity(repo)
        eid = uuid4()
        await repo.create_entity(
            version_id=uuid4(), entity_id=eid, dossier_id=D1,
            type="oe:dossier_access", generated_by=boot,
            content={"v": 1}, attributed_to="system",
        )
        await repo.session.flush()
        await asyncio.sleep(0.002)
        latest_vid = uuid4()
        await repo.create_entity(
            version_id=latest_vid, entity_id=eid, dossier_id=D1,
            type="oe:dossier_access", generated_by=boot,
            content={"v": 2}, attributed_to="system",
        )
        await repo.session.flush()

        result = await repo.get_singleton_entity(D1, "oe:dossier_access")
        assert result is not None
        assert result.id == latest_vid
        assert result.content["v"] == 2

    async def test_returns_none_when_type_absent(self, repo):
        await _setup_dossier_and_activity(repo)
        assert await repo.get_singleton_entity(D1, "oe:absent") is None


# --------------------------------------------------------------------
# create_used + get_used_entity_ids_for_activity
# --------------------------------------------------------------------


class TestUsedLinks:

    async def test_used_round_trip(self, repo):
        """Create used link, fetch via
        get_used_entity_ids_for_activity, confirm the version id
        is in the returned set."""
        boot = await _setup_dossier_and_activity(repo)
        target_vid = uuid4()
        await repo.create_entity(
            version_id=target_vid, entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=boot,
            content={}, attributed_to="system",
        )
        await repo.session.flush()

        await repo.create_used(boot, target_vid)
        await repo.session.flush()

        ids = await repo.get_used_entity_ids_for_activity(boot)
        assert target_vid in ids

    async def test_get_used_entities_returns_full_rows(self, repo):
        """`get_used_entities_for_activity` (different from
        `get_used_entity_ids_for_activity`) returns the full
        EntityRow objects for the used links."""
        boot = await _setup_dossier_and_activity(repo)
        eid = uuid4()
        vid = uuid4()
        await repo.create_entity(
            version_id=vid, entity_id=eid, dossier_id=D1,
            type="oe:aanvraag", generated_by=boot,
            content={"note": "used-via-link"}, attributed_to="system",
        )
        await repo.session.flush()
        await repo.create_used(boot, vid)
        await repo.session.flush()

        rows = await repo.get_used_entities_for_activity(boot)
        assert len(rows) == 1
        assert rows[0].id == vid
        assert rows[0].content["note"] == "used-via-link"


# --------------------------------------------------------------------
# get_entities_generated_by_activity
# --------------------------------------------------------------------


class TestGetEntitiesGeneratedByActivity:

    async def test_returns_only_entities_attributed_to_this_activity(
        self, repo,
    ):
        """Seed one activity that generates two entities, and a
        second activity that generates one. Query on activity A →
        only its two entities."""
        boot_a = await _setup_dossier_and_activity(repo)
        # Second activity
        boot_b = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=boot_b, dossier_id=D1, type="otherActivity",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=boot_b, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        # A generates 2
        for _ in range(2):
            await repo.create_entity(
                version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
                type="oe:aanvraag", generated_by=boot_a,
                content={}, attributed_to="system",
            )
        # B generates 1
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=boot_b,
            content={}, attributed_to="system",
        )
        await repo.session.flush()

        result = await repo.get_entities_generated_by_activity(boot_a)
        assert len(result) == 2


# --------------------------------------------------------------------
# create_relation + get_relations_for_activity
# --------------------------------------------------------------------


class TestRelations:

    async def test_relation_round_trip(self, repo):
        boot = await _setup_dossier_and_activity(repo)
        target_vid = uuid4()
        await repo.create_entity(
            version_id=target_vid, entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=boot,
            content={}, attributed_to="system",
        )
        await repo.session.flush()

        await repo.create_relation(
            activity_id=boot,
            entity_version_id=target_vid,
            relation_type="oe:neemtAkteVan",
        )
        await repo.session.flush()

        rels = await repo.get_relations_for_activity(boot)
        assert len(rels) == 1
        assert rels[0].relation_type == "oe:neemtAkteVan"
        assert rels[0].entity_id == target_vid


# --------------------------------------------------------------------
# ensure_agent idempotency
# --------------------------------------------------------------------


class TestEnsureAgent:

    async def test_first_call_creates(self, repo):
        await repo.ensure_agent("alice", "natuurlijk_persoon", "Alice", {})
        await repo.session.flush()

        result = await repo.session.execute(
            text("SELECT id, name FROM agents WHERE id = 'alice'")
        )
        row = result.fetchone()
        assert row is not None
        assert row[1] == "Alice"

    async def test_repeated_calls_do_not_duplicate(self, repo):
        """Three successive calls to ensure_agent with the same
        id. Only one row should exist. This is the idempotency
        guarantee — the persistence phase calls ensure_agent
        on every activity, and nothing should blow up if the
        user already exists."""
        for _ in range(3):
            await repo.ensure_agent("bob", "systeem", "Bob", {})
        await repo.session.flush()

        result = await repo.session.execute(
            text("SELECT COUNT(*) FROM agents WHERE id = 'bob'")
        )
        assert result.scalar() == 1


# --------------------------------------------------------------------
# Repository caching
# --------------------------------------------------------------------


class TestRepositoryCache:

    async def test_dossier_cache_populated_on_create(self, repo):
        """After create_dossier, a subsequent get_dossier for the
        same id returns the same row object from the cache
        without issuing a SELECT. We can't easily observe "no
        SQL emitted" but we can observe "same object identity"."""
        created = await repo.create_dossier(D1, "toelatingen")
        fetched = await repo.get_dossier(D1)
        assert fetched is created

    async def test_activities_cache_populated_on_create(self, repo):
        """Same shape for activities: after create_activity, the
        activity is visible via get_activities_for_dossier
        without re-querying."""
        await _setup_dossier_and_activity(repo)
        activities = await repo.get_activities_for_dossier(D1)
        assert len(activities) == 1

    async def test_activities_cache_appends_on_create(self, repo):
        """get_activities_for_dossier populates the cache; a
        subsequent create_activity appends to the cached list
        so a second call sees both rows without re-querying."""
        await repo.create_dossier(D1, "toelatingen")
        # First call: populates cache (empty list)
        first = await repo.get_activities_for_dossier(D1)
        assert first == []

        # Create an activity — cache should be updated in place
        act_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=act_id, dossier_id=D1, type="test",
            started_at=now, ended_at=now,
        )

        # Second call: should see the activity without re-querying
        second = await repo.get_activities_for_dossier(D1)
        assert len(second) == 1
        assert second[0].id == act_id
