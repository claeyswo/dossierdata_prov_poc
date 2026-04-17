"""
Integration tests for domain relation repository methods.
"""

from __future__ import annotations

import pytest
from uuid import uuid4, UUID
from datetime import datetime, timezone

from dossier_engine.db.models import DomainRelationRow, DossierRow, ActivityRow


# =====================================================================
# Helpers
# =====================================================================

D1 = UUID("d1000000-0000-0000-0000-000000000001")


async def _ensure_dossier(repo, dossier_id=D1):
    """Create the dossier row if it doesn't exist."""
    existing = await repo.get_dossier(dossier_id)
    if not existing:
        await repo.create_dossier(dossier_id, "test_workflow")


async def _create_activity(repo, dossier_id=D1) -> UUID:
    """Create a minimal activity row and return its id."""
    act_id = uuid4()
    row = ActivityRow(
        id=act_id,
        dossier_id=dossier_id,
        type="testActivity",
        started_at=datetime.now(timezone.utc),
    )
    repo.session.add(row)
    await repo.session.flush()
    return act_id


# =====================================================================
# Tests
# =====================================================================

class TestDomainRelationRepository:
    """Tests for create / supersede / get on domain_relations."""

    async def test_create_and_get(self, repo):
        """Create a domain relation, then retrieve it as active."""
        await _ensure_dossier(repo)
        a1 = await _create_activity(repo)

        await repo.create_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:aanvraag/e1@v1",
            to_ref="https://id.erfgoed.net/erfgoedobjecten/10001",
            created_by_activity_id=a1,
        )
        await repo.session.flush()

        rels = await repo.get_active_domain_relations(D1)
        assert len(rels) == 1
        r = rels[0]
        assert r.relation_type == "oe:betreft"
        assert r.from_ref == "oe:aanvraag/e1@v1"
        assert r.to_ref == "https://id.erfgoed.net/erfgoedobjecten/10001"
        assert r.created_by_activity_id == a1
        assert r.superseded_at is None

    async def test_create_is_idempotent(self, repo):
        """Creating the same (type, from, to) twice returns the
        existing row — no duplicate."""
        await _ensure_dossier(repo)
        a1 = await _create_activity(repo)
        a2 = await _create_activity(repo)

        r1 = await repo.create_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:aanvraag/e1@v1",
            to_ref="https://example.com/obj/1",
            created_by_activity_id=a1,
        )
        await repo.session.flush()
        r2 = await repo.create_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:aanvraag/e1@v1",
            to_ref="https://example.com/obj/1",
            created_by_activity_id=a2,
        )
        await repo.session.flush()
        assert r1.id == r2.id

        rels = await repo.get_active_domain_relations(D1)
        assert len(rels) == 1

    async def test_supersede(self, repo):
        """Supersede marks the relation with the removing activity
        and timestamp. It no longer appears in active relations."""
        await _ensure_dossier(repo)
        a1 = await _create_activity(repo)
        a2 = await _create_activity(repo)

        await repo.create_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:aanvraag/e1@v1",
            to_ref="https://example.com/obj/1",
            created_by_activity_id=a1,
        )
        await repo.session.flush()

        found = await repo.supersede_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:aanvraag/e1@v1",
            to_ref="https://example.com/obj/1",
            superseded_by_activity_id=a2,
        )
        assert found is True
        await repo.session.flush()

        active = await repo.get_active_domain_relations(D1)
        assert len(active) == 0

    async def test_supersede_nonexistent_is_noop(self, repo):
        """Removing a relation that doesn't exist returns False."""
        await _ensure_dossier(repo)
        a2 = await _create_activity(repo)

        found = await repo.supersede_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:doesnt/exist@v1",
            to_ref="https://example.com/nope",
            superseded_by_activity_id=a2,
        )
        assert found is False

    async def test_multiple_relations(self, repo):
        """Multiple distinct relations on the same dossier coexist."""
        await _ensure_dossier(repo)
        a1 = await _create_activity(repo)

        await repo.create_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:aanvraag/e1@v1",
            to_ref="https://example.com/obj/1",
            created_by_activity_id=a1,
        )
        await repo.create_domain_relation(
            dossier_id=D1,
            relation_type="oe:gerelateerd_aan",
            from_ref="dossier:d1",
            to_ref="dossier:d2",
            created_by_activity_id=a1,
        )
        await repo.session.flush()

        rels = await repo.get_active_domain_relations(D1)
        assert len(rels) == 2
        types = {r.relation_type for r in rels}
        assert types == {"oe:betreft", "oe:gerelateerd_aan"}

    async def test_supersede_only_affects_target(self, repo):
        """Superseding one relation leaves others untouched."""
        await _ensure_dossier(repo)
        a1 = await _create_activity(repo)
        a2 = await _create_activity(repo)

        await repo.create_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:aanvraag/e1@v1",
            to_ref="https://example.com/obj/1",
            created_by_activity_id=a1,
        )
        await repo.create_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:aanvraag/e1@v1",
            to_ref="https://example.com/obj/2",
            created_by_activity_id=a1,
        )
        await repo.session.flush()

        await repo.supersede_domain_relation(
            dossier_id=D1,
            relation_type="oe:betreft",
            from_ref="oe:aanvraag/e1@v1",
            to_ref="https://example.com/obj/1",
            superseded_by_activity_id=a2,
        )
        await repo.session.flush()

        active = await repo.get_active_domain_relations(D1)
        assert len(active) == 1
        assert active[0].to_ref == "https://example.com/obj/2"
