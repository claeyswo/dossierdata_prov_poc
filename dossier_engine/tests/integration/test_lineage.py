"""
Integration tests for `lineage.find_related_entity` — the
activity-graph walker that finds related entities by walking
backwards through the PROV graph.

Branches:
* Start entity IS the target type → return itself (trivial)
* Start entity has no generated_by → return None (root/external)
* Target found at first hop (in the generating activity's scope)
* Target found after two hops (through used entity's generator)
* Ambiguous result (two distinct entity_ids of target type at
  one activity) → return None
* Max hops exhausted → return None
* Target not found anywhere → return None
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.lineage import find_related_entity


D1 = UUID("11111111-1111-1111-1111-111111111111")


async def _bootstrap(repo: Repository) -> UUID:
    await repo.create_dossier(D1, "test")
    await repo.ensure_agent("system", "systeem", "Systeem", {})
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="systemAction",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


async def _make_activity(repo, act_type="act", informed_by=None):
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type=act_type,
        started_at=now, ended_at=now,
        informed_by=informed_by,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


async def _make_entity(repo, gen_by, etype, eid=None):
    eid = eid or uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type=etype, generated_by=gen_by,
        content={}, attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid


class TestFindRelatedEntity:

    async def test_start_entity_is_target_type(self, repo):
        """Trivial case: the start entity is already the target
        type. Returns itself without walking."""
        boot = await _bootstrap(repo)
        eid, vid = await _make_entity(repo, boot, "oe:aanvraag")
        row = await repo.get_entity(vid)

        result = await find_related_entity(
            repo, D1, row, "oe:aanvraag",
        )
        assert result is not None
        assert result.id == vid

    async def test_start_entity_no_generated_by_returns_none(self, repo):
        """Start entity has no generating activity (external or
        root). Can't walk — returns None."""
        boot = await _bootstrap(repo)
        eid = uuid4()
        vid = uuid4()
        await repo.create_entity(
            version_id=vid, entity_id=eid, dossier_id=D1,
            type="external", generated_by=None,  # no generator
            content={"uri": "https://example.org"},
            attributed_to="system",
        )
        await repo.session.flush()
        row = await repo.get_entity(vid)

        result = await find_related_entity(
            repo, D1, row, "oe:aanvraag",
        )
        assert result is None

    async def test_target_found_at_first_hop(self, repo):
        """Activity A generates both an aanvraag and a
        beslissing. Start from beslissing, find aanvraag at
        the same activity (one hop)."""
        boot = await _bootstrap(repo)
        act_a = await _make_activity(repo, "makeStuff")
        aanvraag_eid, _ = await _make_entity(repo, act_a, "oe:aanvraag")
        _, beslissing_vid = await _make_entity(repo, act_a, "oe:beslissing")

        start_row = await repo.get_entity(beslissing_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )
        assert result is not None
        assert result.entity_id == aanvraag_eid

    async def test_target_found_via_used_entity_generator(self, repo):
        """Two activities:
        * A generates aanvraag
        * B uses aanvraag, generates beslissing

        Start from beslissing, target is aanvraag. The walker:
        1. Visits B (beslissing's generator) — checks generated+used.
           Finds aanvraag in used. Returns it."""
        boot = await _bootstrap(repo)
        act_a = await _make_activity(repo, "createAanvraag")
        aanvraag_eid, aanvraag_vid = await _make_entity(
            repo, act_a, "oe:aanvraag",
        )

        act_b = await _make_activity(repo, "makeBeslissing")
        await repo.create_used(act_b, aanvraag_vid)
        _, beslissing_vid = await _make_entity(
            repo, act_b, "oe:beslissing",
        )
        await repo.session.flush()

        start_row = await repo.get_entity(beslissing_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )
        assert result is not None
        assert result.entity_id == aanvraag_eid

    async def test_target_found_two_hops_away(self, repo):
        """Three activities:
        * A generates aanvraag
        * B uses aanvraag, generates dossier_access
        * C uses dossier_access, generates nota

        Start from nota, target is aanvraag. The walker needs
        to go through C → dossier_access → B → aanvraag (two
        hops). The walker checks C's scope first (finds
        dossier_access, not aanvraag), then walks to
        dossier_access's generator (B), then finds aanvraag in
        B's used."""
        boot = await _bootstrap(repo)

        act_a = await _make_activity(repo, "a")
        aanvraag_eid, aanvraag_vid = await _make_entity(
            repo, act_a, "oe:aanvraag",
        )

        act_b = await _make_activity(repo, "b")
        await repo.create_used(act_b, aanvraag_vid)
        _, access_vid = await _make_entity(repo, act_b, "oe:access")
        await repo.session.flush()

        act_c = await _make_activity(repo, "c")
        await repo.create_used(act_c, access_vid)
        _, nota_vid = await _make_entity(repo, act_c, "system:note")
        await repo.session.flush()

        start_row = await repo.get_entity(nota_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )
        assert result is not None
        assert result.entity_id == aanvraag_eid

    async def test_ambiguous_returns_none(self, repo):
        """Activity A generates TWO distinct aanvraag entities
        and one beslissing. Start from beslissing, target is
        aanvraag. Two distinct entity_ids → ambiguous → None."""
        boot = await _bootstrap(repo)
        act_a = await _make_activity(repo, "a")
        await _make_entity(repo, act_a, "oe:aanvraag")
        await _make_entity(repo, act_a, "oe:aanvraag")
        _, beslissing_vid = await _make_entity(repo, act_a, "oe:beslissing")

        start_row = await repo.get_entity(beslissing_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )
        assert result is None

    async def test_max_hops_exhausted_returns_none(self, repo):
        """Build a chain deeper than max_hops and verify the
        walker gives up."""
        boot = await _bootstrap(repo)
        # Chain: boot → entity → act1 → entity → act2 → ...
        prev_act = boot
        for i in range(5):
            _, vid = await _make_entity(repo, prev_act, f"type_{i}")
            prev_act = await _make_activity(repo, f"act_{i}")
            await repo.create_used(prev_act, vid)
            await repo.session.flush()

        # Final entity in the chain
        _, start_vid = await _make_entity(repo, prev_act, "oe:end")
        # The aanvraag is at the root (boot), 5+ hops away
        await _make_entity(repo, boot, "oe:aanvraag")

        start_row = await repo.get_entity(start_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag", max_hops=2,
        )
        # With max_hops=2, the walker can't reach the root
        assert result is None

    async def test_no_match_anywhere_returns_none(self, repo):
        """Target type doesn't exist in the graph at all.
        Walker exhausts the frontier and returns None."""
        boot = await _bootstrap(repo)
        act_a = await _make_activity(repo, "a")
        _, vid = await _make_entity(repo, act_a, "oe:beslissing")

        start_row = await repo.get_entity(vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:nonexistent",
        )
        assert result is None
