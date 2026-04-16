"""
Tests for the dossier_toelatingen plugin:

* Entity model validation (Aanvrager kbo/rrn mutual exclusivity)
* Relation validator: validate_neemt_akte_van (staleness detection)
* Handlers: handle_beslissing (status branching + task scheduling)

These are the three pieces of the concrete plugin that carry
real logic beyond simple field copying. The remaining handlers
(set_dossier_access, set_verantwoordelijke_organisatie, etc.)
are exercised by the E2E suite and are mostly lookup→return
with minimal branching.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.context import ActivityContext, HandlerResult
from dossier_engine.engine.errors import ActivityError


D1 = UUID("11111111-1111-1111-1111-111111111111")


# ----------------------------------------------------------------
# Entity model tests (pure unit — no DB)
# ----------------------------------------------------------------


class TestAanvragerModel:

    def test_valid_with_rrn(self):
        from dossier_toelatingen.entities import Aanvrager
        a = Aanvrager(rrn="12345678901")
        assert a.rrn == "12345678901"
        assert a.kbo is None

    def test_valid_with_kbo(self):
        from dossier_toelatingen.entities import Aanvrager
        a = Aanvrager(kbo="0123456789")
        assert a.kbo == "0123456789"
        assert a.rrn is None

    def test_neither_kbo_nor_rrn_rejected(self):
        from dossier_toelatingen.entities import Aanvrager
        with pytest.raises(ValueError, match="either.*kbo.*rrn"):
            Aanvrager()

    def test_both_kbo_and_rrn_rejected(self):
        from dossier_toelatingen.entities import Aanvrager
        with pytest.raises(ValueError, match="not both"):
            Aanvrager(kbo="0123456789", rrn="12345678901")


class TestBeslissingUitkomst:

    def test_valid_values(self):
        from dossier_toelatingen.entities import BeslissingUitkomst
        assert BeslissingUitkomst("goedgekeurd") == "goedgekeurd"
        assert BeslissingUitkomst("afgekeurd") == "afgekeurd"
        assert BeslissingUitkomst("onvolledig") == "onvolledig"

    def test_invalid_value_rejected(self):
        from dossier_toelatingen.entities import BeslissingUitkomst
        with pytest.raises(ValueError):
            BeslissingUitkomst("unknown")


# ----------------------------------------------------------------
# Relation validator: validate_neemt_akte_van
# ----------------------------------------------------------------


async def _bootstrap(repo: Repository) -> UUID:
    await repo.create_dossier(D1, "toelatingen")
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


async def _seed_entity(
    repo, generated_by, entity_type,
    entity_id=None, content=None,
):
    eid = entity_id or uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type=entity_type, generated_by=generated_by,
        content=content or {}, attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid


class _StubPlugin:
    def __init__(self, entity_models=None):
        self.entity_models = entity_models or {}
    def is_singleton(self, t):
        return False
    def resolve_schema(self, entity_type, schema_version):
        return self.entity_models.get(entity_type)


class TestValidateNeemtAkteVan:

    async def test_all_used_refs_are_latest_no_relations_needed(self, repo):
        """Every used ref points at the latest version of its
        entity. No staleness → validator passes without any
        oe:neemtAkteVan entries. This is the common case (most
        requests use current versions)."""
        from dossier_toelatingen.relation_validators import validate_neemt_akte_van

        boot = await _bootstrap(repo)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")
        row = await repo.get_entity(vid)
        ref = f"oe:aanvraag/{eid}@{vid}"

        await validate_neemt_akte_van(
            plugin=_StubPlugin(), repo=repo, dossier_id=D1,
            activity_def={"name": "test"},
            entries=[],  # no acknowledgements needed
            used_rows_by_ref={ref: row},
            generated_items=[],
        )
        # No exception — passes.

    async def test_stale_ref_without_acknowledgement_raises_409(self, repo):
        """Used ref points at v1 but v2 exists. No
        oe:neemtAkteVan entry covering v2 → 409
        stale_used_reference with the structured payload."""
        from dossier_toelatingen.relation_validators import validate_neemt_akte_van

        boot = await _bootstrap(repo)
        eid = uuid4()
        _, vid_v1 = await _seed_entity(
            repo, boot, "oe:aanvraag", entity_id=eid, content={"v": 1},
        )
        await asyncio.sleep(0.002)
        _, vid_v2 = await _seed_entity(
            repo, boot, "oe:aanvraag", entity_id=eid, content={"v": 2},
        )

        v1_row = await repo.get_entity(vid_v1)
        ref = f"oe:aanvraag/{eid}@{vid_v1}"

        with pytest.raises(ActivityError) as exc:
            await validate_neemt_akte_van(
                plugin=_StubPlugin(), repo=repo, dossier_id=D1,
                activity_def={"name": "test"},
                entries=[],
                used_rows_by_ref={ref: v1_row},
                generated_items=[],
            )
        assert exc.value.status_code == 409
        assert exc.value.payload["error"] == "stale_used_reference"
        stale = exc.value.payload["stale"]
        assert len(stale) == 1
        assert stale[0]["declared_version"] == str(vid_v1)
        assert stale[0]["latest_version"] == str(vid_v2)

    async def test_stale_ref_with_acknowledgement_passes(self, repo):
        """Used ref points at v1, v2 exists, but the client sent
        an oe:neemtAkteVan entry covering v2. The validator
        accepts — the client explicitly acknowledged the newer
        version and chose to proceed with the older one."""
        from dossier_toelatingen.relation_validators import validate_neemt_akte_van

        boot = await _bootstrap(repo)
        eid = uuid4()
        _, vid_v1 = await _seed_entity(
            repo, boot, "oe:aanvraag", entity_id=eid, content={"v": 1},
        )
        await asyncio.sleep(0.002)
        _, vid_v2 = await _seed_entity(
            repo, boot, "oe:aanvraag", entity_id=eid, content={"v": 2},
        )

        v1_row = await repo.get_entity(vid_v1)
        v2_row = await repo.get_entity(vid_v2)
        ref_v1 = f"oe:aanvraag/{eid}@{vid_v1}"
        ref_v2 = f"oe:aanvraag/{eid}@{vid_v2}"

        await validate_neemt_akte_van(
            plugin=_StubPlugin(), repo=repo, dossier_id=D1,
            activity_def={"name": "test"},
            entries=[{
                "ref": ref_v2,
                "entity_row": v2_row,
                "raw": {"type": "oe:neemtAkteVan", "entity": ref_v2},
            }],
            used_rows_by_ref={ref_v1: v1_row},
            generated_items=[],
        )
        # No exception — the acknowledgement covers the stale ref.

    async def test_unrelated_acknowledgement_raises_422(self, repo):
        """Client sends an oe:neemtAkteVan entry for an entity
        version that's NOT an intervening version of any stale
        used reference. 422 unrelated_acknowledgement — the
        client acknowledged something irrelevant, which is a
        bug."""
        from dossier_toelatingen.relation_validators import validate_neemt_akte_van

        boot = await _bootstrap(repo)
        # Create one fresh entity (used ref is latest, not stale)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")
        row = await repo.get_entity(vid)
        ref = f"oe:aanvraag/{eid}@{vid}"

        # Create a completely unrelated entity to acknowledge
        _, unrelated_vid = await _seed_entity(
            repo, boot, "oe:beslissing",
        )
        unrelated_row = await repo.get_entity(unrelated_vid)
        unrelated_ref = f"oe:beslissing/{uuid4()}@{unrelated_vid}"

        with pytest.raises(ActivityError) as exc:
            await validate_neemt_akte_van(
                plugin=_StubPlugin(), repo=repo, dossier_id=D1,
                activity_def={"name": "test"},
                entries=[{
                    "ref": unrelated_ref,
                    "entity_row": unrelated_row,
                    "raw": {},
                }],
                used_rows_by_ref={ref: row},
                generated_items=[],
            )
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "unrelated_acknowledgement"

    async def test_multiple_intervening_versions_all_must_be_covered(
        self, repo,
    ):
        """Used ref is v1, v2 and v3 exist. Client acknowledges
        v2 only. v3 is not covered → 409. The validator requires
        ALL intervening versions to be acknowledged, not just
        the latest one."""
        from dossier_toelatingen.relation_validators import validate_neemt_akte_van

        boot = await _bootstrap(repo)
        eid = uuid4()
        _, vid_v1 = await _seed_entity(
            repo, boot, "oe:aanvraag", entity_id=eid, content={"v": 1},
        )
        await asyncio.sleep(0.002)
        _, vid_v2 = await _seed_entity(
            repo, boot, "oe:aanvraag", entity_id=eid, content={"v": 2},
        )
        await asyncio.sleep(0.002)
        _, vid_v3 = await _seed_entity(
            repo, boot, "oe:aanvraag", entity_id=eid, content={"v": 3},
        )

        v1_row = await repo.get_entity(vid_v1)
        v2_row = await repo.get_entity(vid_v2)
        ref_v1 = f"oe:aanvraag/{eid}@{vid_v1}"
        ref_v2 = f"oe:aanvraag/{eid}@{vid_v2}"

        with pytest.raises(ActivityError) as exc:
            await validate_neemt_akte_van(
                plugin=_StubPlugin(), repo=repo, dossier_id=D1,
                activity_def={"name": "test"},
                entries=[{
                    "ref": ref_v2,
                    "entity_row": v2_row,
                    "raw": {},
                }],
                used_rows_by_ref={ref_v1: v1_row},
                generated_items=[],
            )
        assert exc.value.status_code == 409
        # v3 is missing from the acknowledgements
        stale = exc.value.payload["stale"]
        assert len(stale) == 1


# ----------------------------------------------------------------
# Handler: handle_beslissing
# ----------------------------------------------------------------


class TestHandleBeslissing:

    def _ctx(self, repo, used_entities):
        """Build an ActivityContext with the given used entities
        resolved. Expects dicts mapping type → row/model."""
        from dossier_toelatingen.entities import (
            Aanvraag, Beslissing, Handtekening,
        )
        models = {
            "oe:aanvraag": Aanvraag,
            "oe:beslissing": Beslissing,
            "oe:handtekening": Handtekening,
        }
        return ActivityContext(
            repo=repo,
            dossier_id=D1,
            used_entities=used_entities,
            entity_models=models,
            plugin=_StubPlugin(entity_models=models),
        )

    def _handtekening_row(self, getekend: bool):
        return SimpleNamespace(
            content={"getekend": getekend},
            schema_version=None,
        )

    def _beslissing_row(self, uitkomst: str):
        return SimpleNamespace(
            content={
                "beslissing": uitkomst,
                "datum": "2025-01-01",
                "object": "https://obj/1",
                "brief": "file-123",
            },
            schema_version=None,
            type="oe:beslissing",
            generated_by=None,  # lineage walker needs this
            entity_id=uuid4(),
            id=uuid4(),
        )

    async def test_no_handtekening_returns_te_tekenen(self, repo):
        """No handtekening entity → status is
        `beslissing_te_tekenen`."""
        from dossier_toelatingen.handlers import handle_beslissing
        ctx = self._ctx(repo, {})
        result = await handle_beslissing(ctx, None)
        assert isinstance(result, HandlerResult)
        assert result.status == "beslissing_te_tekenen"

    async def test_handtekening_not_signed_returns_klaar(self, repo):
        """Handtekening exists but getekend=False → status
        `klaar_voor_behandeling`."""
        from dossier_toelatingen.handlers import handle_beslissing
        ctx = self._ctx(repo, {
            "oe:handtekening": self._handtekening_row(False),
        })
        result = await handle_beslissing(ctx, None)
        assert result.status == "klaar_voor_behandeling"

    async def test_goedgekeurd_returns_toelating_verleend(self, repo):
        from dossier_toelatingen.handlers import handle_beslissing
        ctx = self._ctx(repo, {
            "oe:handtekening": self._handtekening_row(True),
            "oe:beslissing": self._beslissing_row("goedgekeurd"),
        })
        result = await handle_beslissing(ctx, None)
        assert result.status == "toelating_verleend"

    async def test_afgekeurd_returns_toelating_geweigerd(self, repo):
        from dossier_toelatingen.handlers import handle_beslissing
        ctx = self._ctx(repo, {
            "oe:handtekening": self._handtekening_row(True),
            "oe:beslissing": self._beslissing_row("afgekeurd"),
        })
        result = await handle_beslissing(ctx, None)
        assert result.status == "toelating_geweigerd"

    async def test_onvolledig_schedules_trekAanvraagIn_task(self, repo):
        """Beslissing=onvolledig → status `aanvraag_onvolledig`
        AND a `trekAanvraagIn` scheduled task in the returned
        HandlerResult.tasks list."""
        from dossier_toelatingen.handlers import handle_beslissing
        ctx = self._ctx(repo, {
            "oe:handtekening": self._handtekening_row(True),
            "oe:beslissing": self._beslissing_row("onvolledig"),
        })
        result = await handle_beslissing(ctx, None)
        assert result.status == "aanvraag_onvolledig"
        assert len(result.tasks) == 1
        task = result.tasks[0]
        assert task["kind"] == "scheduled_activity"
        assert task["target_activity"] == "trekAanvraagIn"
        assert task["cancel_if_activities"] == ["vervolledigAanvraag"]
        assert task["anchor_type"] == "oe:aanvraag"

    async def test_signed_no_beslissing_returns_ondertekend(self, repo):
        """Handtekening=signed but no beslissing entity exists →
        `beslissing_ondertekend` (the signing happened before
        the decision was recorded)."""
        from dossier_toelatingen.handlers import handle_beslissing
        ctx = self._ctx(repo, {
            "oe:handtekening": self._handtekening_row(True),
        })
        result = await handle_beslissing(ctx, None)
        assert result.status == "beslissing_ondertekend"


# ----------------------------------------------------------------
# Handler: duid_behandelaar_aan
# ----------------------------------------------------------------


class TestDuidBehandelaarAan:

    async def test_oe_org_assigns_specific_behandelaar(self, repo):
        """When verantwoordelijke_organisatie is the central OE
        office, a specific behandelaar is assigned."""
        from dossier_toelatingen.handlers import duid_behandelaar_aan
        from dossier_toelatingen.entities import VerantwoordelijkeOrganisatie

        ctx = ActivityContext(
            repo=repo, dossier_id=D1,
            used_entities={
                "oe:verantwoordelijke_organisatie": SimpleNamespace(
                    content={"uri": "https://id.erfgoed.net/organisaties/oe"},
                    schema_version=None,
                ),
            },
            entity_models={
                "oe:verantwoordelijke_organisatie": VerantwoordelijkeOrganisatie,
            },
            plugin=_StubPlugin(entity_models={
                "oe:verantwoordelijke_organisatie": VerantwoordelijkeOrganisatie,
            }),
        )
        result = await duid_behandelaar_aan(ctx, None)
        assert "benjamma" in result.generated[0]["content"]["uri"]
        assert result.status == "klaar_voor_behandeling"

    async def test_other_org_assigns_org_uri_as_behandelaar(self, repo):
        """Non-OE org → the org's own URI becomes the
        behandelaar URI."""
        from dossier_toelatingen.handlers import duid_behandelaar_aan
        from dossier_toelatingen.entities import VerantwoordelijkeOrganisatie

        ctx = ActivityContext(
            repo=repo, dossier_id=D1,
            used_entities={
                "oe:verantwoordelijke_organisatie": SimpleNamespace(
                    content={"uri": "https://id.erfgoed.net/organisaties/brugge"},
                    schema_version=None,
                ),
            },
            entity_models={
                "oe:verantwoordelijke_organisatie": VerantwoordelijkeOrganisatie,
            },
            plugin=_StubPlugin(entity_models={
                "oe:verantwoordelijke_organisatie": VerantwoordelijkeOrganisatie,
            }),
        )
        result = await duid_behandelaar_aan(ctx, None)
        assert result.generated[0]["content"]["uri"] == "https://id.erfgoed.net/organisaties/brugge"

    async def test_no_org_assigns_onbekend(self, repo):
        """No verantwoordelijke_organisatie → fallback to
        'onbekend'."""
        from dossier_toelatingen.handlers import duid_behandelaar_aan

        ctx = ActivityContext(
            repo=repo, dossier_id=D1,
            used_entities={},
            entity_models={},
            plugin=_StubPlugin(),
        )
        result = await duid_behandelaar_aan(ctx, None)
        assert "onbekend" in result.generated[0]["content"]["uri"]
