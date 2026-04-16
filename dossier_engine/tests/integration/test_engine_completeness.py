"""
Integration tests for the last engine/worker branches:

* `_auto_resolve_for_system_caller` — the system-caller second
  pass of `resolve_used`, which fills in `auto_resolve: latest`
  slots from trigger scope / anchor / singleton fallback.
* `_auto_resolve_used` — the side-effect variant (same general
  shape but with a different fall-through order and no anchor).
* `_persist_se_generated` — side-effect handler-generated
  persistence, including schema_version resolution.
* `_process_cross_dossier` — worker branch for cross-dossier
  task dispatch, which is structurally similar to
  `_process_scheduled_activity` but routes through a second
  plugin via the registry.

These four functions are all that's left for complete branch
coverage of the engine's logic surface. After this file, the
only untested code is the I/O layer (routes, file_service HTTP
handlers, worker CLI entry points).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.context import HandlerResult
from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.pipeline.side_effects import (
    _auto_resolve_used, _persist_se_generated,
)
from dossier_engine.engine.pipeline.used import (
    _auto_resolve_for_system_caller, _parse_local_trigger_id,
)
from dossier_engine.engine.state import ActivityState, Caller
from dossier_engine.entities import SYSTEM_ACTION_DEF, SystemNote, TaskEntity
from dossier_engine.plugin import Plugin
from dossier_engine.worker import _process_cross_dossier


D1 = UUID("11111111-1111-1111-1111-111111111111")
D2 = UUID("22222222-2222-2222-2222-222222222222")


def _user() -> User:
    return User(id="system", type="systeem", name="Systeem", roles=[], properties={})


class _SystemCallerPlugin:
    """Minimal plugin stub for `_auto_resolve_for_system_caller`
    tests. Needs `is_singleton` (for the singleton fallback) and
    nothing else — the phase doesn't call find_activity_def."""
    def __init__(self, singletons: set[str] | None = None):
        self._singletons = singletons or set()
        self.entity_models = {}

    def is_singleton(self, entity_type: str) -> bool:
        return entity_type in self._singletons

    def cardinality_of(self, entity_type: str) -> str:
        return "single" if entity_type in self._singletons else "multi"


async def _bootstrap(repo: Repository, dossier_id: UUID = D1) -> UUID:
    await repo.create_dossier(dossier_id, "test")
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


async def _seed_entity(
    repo: Repository,
    generated_by: UUID,
    entity_type: str,
    *,
    dossier_id: UUID = D1,
    content: dict | None = None,
    entity_id: UUID | None = None,
):
    eid = entity_id or uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=dossier_id,
        type=entity_type, generated_by=generated_by,
        content=content or {}, attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid


async def _seed_trigger_activity_with_used(
    repo: Repository, used_version_ids: list[UUID],
) -> UUID:
    """Create an activity row and link it to used versions."""
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="trigger",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    for vid in used_version_ids:
        await repo.create_used(act_id, vid)
    await repo.session.flush()
    return act_id


def _state(
    repo: Repository,
    *,
    plugin,
    activity_def: dict,
    caller: Caller = Caller.SYSTEM,
    informed_by: str | None = None,
    anchor_entity_id: UUID | None = None,
    anchor_type: str | None = None,
) -> ActivityState:
    s = ActivityState(
        plugin=plugin,
        activity_def=activity_def,
        repo=repo,
        dossier_id=D1,
        activity_id=uuid4(),
        user=_user(),
        role="systeem",
        used_items=[],
        generated_items=[],
        relation_items=[],
        caller=caller,
        informed_by=informed_by,
    )
    s.anchor_entity_id = anchor_entity_id
    s.anchor_type = anchor_type
    return s


# --------------------------------------------------------------------
# _parse_local_trigger_id
# --------------------------------------------------------------------


class TestParseLocalTriggerId:

    def test_none_returns_none(self):
        assert _parse_local_trigger_id(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_local_trigger_id("") is None

    def test_valid_uuid_string_parses(self):
        uid = uuid4()
        assert _parse_local_trigger_id(str(uid)) == uid

    def test_cross_dossier_uri_returns_none(self):
        """`informed_by` can carry a cross-dossier IRI like
        `https://data.vlaanderen.be/id/dossier/{id}/activiteiten/{id}`.
        That's not a local reference and should return None so the
        phase skips trigger-scope resolution for cross-dossier chains."""
        uri = "https://data.vlaanderen.be/id/dossier/11111111-1111-1111-1111-111111111111/activiteiten/22222222-2222-2222-2222-222222222222"
        assert _parse_local_trigger_id(uri) is None

    def test_garbage_string_returns_none(self):
        assert _parse_local_trigger_id("not-a-uuid") is None


# --------------------------------------------------------------------
# _auto_resolve_for_system_caller
# --------------------------------------------------------------------


class TestAutoResolveForSystemCaller:

    async def test_no_auto_resolve_slots_noop(self, repo):
        """Activity def has `used` entries but none declare
        `auto_resolve: latest`. Phase walks the list without
        populating anything."""
        await _bootstrap(repo)
        plugin = _SystemCallerPlugin()
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "used": [{"type": "oe:aanvraag"}],  # no auto_resolve
            },
        )

        await _auto_resolve_for_system_caller(state)
        assert state.resolved_entities == {}
        assert state.used_refs == []

    async def test_external_slots_skipped(self, repo):
        """Used entries flagged `external` are skipped — side
        effects and system callers don't take external inputs."""
        await _bootstrap(repo)
        plugin = _SystemCallerPlugin()
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "used": [{
                    "type": "oe:aanvraag",
                    "auto_resolve": "latest",
                    "external": True,
                }],
            },
        )

        await _auto_resolve_for_system_caller(state)
        assert state.resolved_entities == {}

    async def test_already_resolved_type_skipped(self, repo):
        """If `resolved_entities` already contains the type
        (client supplied it explicitly in the first pass), the
        auto-resolve loop skips it — explicit > auto."""
        boot = await _bootstrap(repo)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")
        existing_row = await repo.get_entity(vid)

        plugin = _SystemCallerPlugin()
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "used": [{"type": "oe:aanvraag", "auto_resolve": "latest"}],
            },
        )
        # Pre-populate as if the explicit pass had resolved it.
        state.resolved_entities["oe:aanvraag"] = existing_row

        await _auto_resolve_for_system_caller(state)

        # Still just the one, no duplicate in used_refs
        assert state.resolved_entities["oe:aanvraag"] is existing_row
        assert state.used_refs == []  # no auto-resolve entry added

    async def test_trigger_scope_resolves_from_generated(self, repo):
        """`informed_by` points at a local activity that
        generated a matching entity. Phase finds it via trigger
        scope and records it."""
        boot = await _bootstrap(repo)
        # Trigger activity generates an oe:aanvraag
        trigger_act = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=trigger_act, dossier_id=D1, type="trigger",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=trigger_act, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        target_eid, target_vid = await _seed_entity(
            repo, trigger_act, "oe:aanvraag",
            content={"source": "trigger"},
        )

        plugin = _SystemCallerPlugin()
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "used": [{"type": "oe:aanvraag", "auto_resolve": "latest"}],
            },
            informed_by=str(trigger_act),
        )

        await _auto_resolve_for_system_caller(state)

        assert "oe:aanvraag" in state.resolved_entities
        assert state.resolved_entities["oe:aanvraag"].id == target_vid
        assert len(state.used_refs) == 1
        assert state.used_refs[0]["auto_resolved"] is True
        assert state.used_refs[0]["type"] == "oe:aanvraag"

    async def test_anchor_fallback_when_trigger_scope_empty(self, repo):
        """Trigger scope doesn't have the type, but an anchor of
        matching type was supplied at task scheduling time.
        Phase falls back to the anchor."""
        boot = await _bootstrap(repo)
        # Seed an anchored entity (outside the trigger's scope)
        eid, vid = await _seed_entity(
            repo, boot, "oe:aanvraag", content={"v": 1},
        )
        # Create an unrelated trigger activity with no matching entity
        trigger_act = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=trigger_act, dossier_id=D1, type="unrelated",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=trigger_act, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        plugin = _SystemCallerPlugin()  # not singleton
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "used": [{"type": "oe:aanvraag", "auto_resolve": "latest"}],
            },
            informed_by=str(trigger_act),
            anchor_entity_id=eid,
            anchor_type="oe:aanvraag",
        )

        await _auto_resolve_for_system_caller(state)

        assert state.resolved_entities["oe:aanvraag"].id == vid
        assert state.used_refs[0]["auto_resolved"] is True

    async def test_singleton_fallback_when_nothing_else_matches(self, repo):
        """Trigger scope empty AND no anchor. But the type is a
        singleton and an instance exists in the dossier → fall
        back to singleton lookup."""
        boot = await _bootstrap(repo)
        eid, vid = await _seed_entity(
            repo, boot, "oe:dossier_access", content={"level": "owner"},
        )

        plugin = _SystemCallerPlugin(singletons={"oe:dossier_access"})
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "used": [{"type": "oe:dossier_access", "auto_resolve": "latest"}],
            },
            # No informed_by, no anchor
        )

        await _auto_resolve_for_system_caller(state)

        assert "oe:dossier_access" in state.resolved_entities
        assert state.resolved_entities["oe:dossier_access"].id == vid

    async def test_multi_cardinality_not_found_silently_skipped(self, repo):
        """Multi-cardinality type, not in trigger scope, no
        anchor, singleton fallback N/A. Phase silently skips —
        the activity runs without it in resolved_entities, and
        downstream phases that need it will raise their own
        error."""
        await _bootstrap(repo)
        plugin = _SystemCallerPlugin()  # nothing is singleton
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "used": [{"type": "oe:missing", "auto_resolve": "latest"}],
            },
        )

        await _auto_resolve_for_system_caller(state)

        assert state.resolved_entities == {}
        assert state.used_refs == []

    async def test_anchor_wrong_type_ignored(self, repo):
        """Anchor is supplied but its type doesn't match what
        we're looking for. The anchor fallback is scoped to
        matching type only; this call falls through to
        singleton lookup (and then silently skips)."""
        await _bootstrap(repo)
        plugin = _SystemCallerPlugin()
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "used": [{"type": "oe:aanvraag", "auto_resolve": "latest"}],
            },
            anchor_entity_id=uuid4(),
            anchor_type="oe:beslissing",  # mismatched
        )

        await _auto_resolve_for_system_caller(state)
        assert state.resolved_entities == {}


# --------------------------------------------------------------------
# _auto_resolve_used (side-effects variant)
# --------------------------------------------------------------------


class TestAutoResolveUsedSideEffects:

    async def test_empty_used_returns_empty(self, repo):
        plugin = _SystemCallerPlugin()
        se_activity_id = uuid4()
        resolved = await _auto_resolve_used(
            plugin=plugin, repo=repo, dossier_id=D1,
            se_def={"name": "se", "used": []},
            se_activity_id=se_activity_id,
            trigger_generated=[], trigger_used=[],
        )
        assert resolved == {}

    async def test_external_and_non_auto_resolve_skipped(self, repo):
        """Two kinds of entries that are skipped: `external: True`
        and anything without `auto_resolve: latest`. Neither
        contributes to the resolved dict."""
        plugin = _SystemCallerPlugin()
        resolved = await _auto_resolve_used(
            plugin=plugin, repo=repo, dossier_id=D1,
            se_def={
                "name": "se",
                "used": [
                    {"type": "oe:a", "external": True, "auto_resolve": "latest"},
                    {"type": "oe:b"},  # no auto_resolve
                ],
            },
            se_activity_id=uuid4(),
            trigger_generated=[], trigger_used=[],
        )
        assert resolved == {}

    async def test_resolves_from_trigger_generated_and_writes_used_link(
        self, repo,
    ):
        """Side effect declares `auto_resolve: latest` for
        oe:aanvraag. The trigger generated one. Phase finds it,
        adds to resolved dict, AND writes a `used` link row so
        the side-effect's PROV chain includes it."""
        boot = await _bootstrap(repo)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")
        target_row = await repo.get_entity(vid)

        # Create an SE activity row (the caller would do this)
        se_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=se_id, dossier_id=D1, type="se",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=se_id, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        plugin = _SystemCallerPlugin()
        resolved = await _auto_resolve_used(
            plugin=plugin, repo=repo, dossier_id=D1,
            se_def={
                "name": "se",
                "used": [{"type": "oe:aanvraag", "auto_resolve": "latest"}],
            },
            se_activity_id=se_id,
            trigger_generated=[target_row],
            trigger_used=[],
        )
        await repo.session.flush()

        assert "oe:aanvraag" in resolved
        assert resolved["oe:aanvraag"].id == vid

        # Used link row should exist
        used_ids = await repo.get_used_entity_ids_for_activity(se_id)
        assert vid in used_ids

    async def test_singleton_fallback_when_not_in_trigger_scope(self, repo):
        """Trigger didn't touch the type, but it's a singleton
        and exists elsewhere in the dossier. Falls back to
        singleton lookup."""
        boot = await _bootstrap(repo)
        eid, vid = await _seed_entity(
            repo, boot, "oe:dossier_access", content={"level": "owner"},
        )

        se_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=se_id, dossier_id=D1, type="se",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=se_id, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        plugin = _SystemCallerPlugin(singletons={"oe:dossier_access"})
        resolved = await _auto_resolve_used(
            plugin=plugin, repo=repo, dossier_id=D1,
            se_def={
                "name": "se",
                "used": [
                    {"type": "oe:dossier_access", "auto_resolve": "latest"},
                ],
            },
            se_activity_id=se_id,
            trigger_generated=[],
            trigger_used=[],
        )
        assert "oe:dossier_access" in resolved

    async def test_multi_cardinality_not_in_trigger_silently_skipped(
        self, repo,
    ):
        """Multi-cardinality + not in trigger scope → skipped,
        does NOT fall back to "latest of type" across dossier.
        This is deliberate: latest-of-type would be ambiguous
        when multiple instances exist."""
        boot = await _bootstrap(repo)
        # Seed two oe:bijlage (multi-cardinality)
        await _seed_entity(repo, boot, "oe:bijlage")
        await _seed_entity(repo, boot, "oe:bijlage")

        se_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=se_id, dossier_id=D1, type="se",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=se_id, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        plugin = _SystemCallerPlugin()  # not singleton
        resolved = await _auto_resolve_used(
            plugin=plugin, repo=repo, dossier_id=D1,
            se_def={
                "name": "se",
                "used": [{"type": "oe:bijlage", "auto_resolve": "latest"}],
            },
            se_activity_id=se_id,
            trigger_generated=[],
            trigger_used=[],
        )
        assert resolved == {}


# --------------------------------------------------------------------
# _persist_se_generated
# --------------------------------------------------------------------


class _PersistPlugin:
    """Plugin stub for _persist_se_generated. Provides
    is_singleton (for the identity resolver) and
    `resolve_schema` (for _resolve_schema_version). No real
    validation happens — we just need the schema lookup to
    return None so the side-effect path skips validation."""
    def __init__(self, singletons: set[str] | None = None):
        self._singletons = singletons or set()
        self.entity_models = {}

    def is_singleton(self, entity_type: str) -> bool:
        return entity_type in self._singletons

    def cardinality_of(self, entity_type: str) -> str:
        return "single" if entity_type in self._singletons else "multi"

    def resolve_schema(self, entity_type, schema_version):
        return None


class TestPersistSeGenerated:

    async def test_empty_list_noop(self, repo):
        await _bootstrap(repo)
        plugin = _PersistPlugin()
        await _persist_se_generated(
            plugin=plugin, repo=repo, dossier_id=D1,
            se_def={"name": "se", "generates": []},
            se_activity_id=uuid4(),
            handler_generated=[],
        )
        # No exception, no rows written.

    async def test_persists_fresh_multi_cardinality_entity(self, repo):
        """Handler returns one generated entity. No explicit
        entity_id, type is multi-cardinality → fresh entity_id
        is minted and the row is persisted with attributed_to=system."""
        await _bootstrap(repo)
        se_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=se_id, dossier_id=D1, type="se",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=se_id, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        plugin = _PersistPlugin()
        await _persist_se_generated(
            plugin=plugin, repo=repo, dossier_id=D1,
            se_def={"name": "se", "generates": ["oe:bijlage"]},
            se_activity_id=se_id,
            handler_generated=[
                {"type": "oe:bijlage", "content": {"name": "a.pdf"}},
            ],
        )
        await repo.session.flush()

        rows = await repo.get_entities_by_type(D1, "oe:bijlage")
        assert len(rows) == 1
        assert rows[0].content == {"name": "a.pdf"}
        assert rows[0].generated_by == se_id
        assert rows[0].attributed_to == "system"

    async def test_singleton_revise_links_derived_from(self, repo):
        """Existing singleton entity. Handler returns a new
        revision. Phase reuses the entity_id, sets derived_from
        to the parent version, and persists the new row."""
        boot = await _bootstrap(repo)
        existing_eid, existing_vid = await _seed_entity(
            repo, boot, "oe:dossier_access",
            content={"level": "viewer"},
        )

        se_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=se_id, dossier_id=D1, type="se",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=se_id, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        plugin = _PersistPlugin(singletons={"oe:dossier_access"})
        await _persist_se_generated(
            plugin=plugin, repo=repo, dossier_id=D1,
            se_def={"name": "se", "generates": ["oe:dossier_access"]},
            se_activity_id=se_id,
            handler_generated=[
                {
                    "type": "oe:dossier_access",
                    "content": {"level": "owner"},
                },
            ],
        )
        await repo.session.flush()

        versions = await repo.get_entity_versions(D1, existing_eid)
        assert len(versions) == 2
        new_version = versions[-1]
        assert new_version.derived_from == existing_vid
        assert new_version.content == {"level": "owner"}

    async def test_unresolvable_identity_silently_dropped(self, repo):
        """Handler returns an item with no type AND no
        `generates[0]` fallback → identity resolver returns None
        → item silently dropped. Matches the pattern in
        `_append_handler_generated`."""
        await _bootstrap(repo)
        se_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=se_id, dossier_id=D1, type="se",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=se_id, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        plugin = _PersistPlugin()
        await _persist_se_generated(
            plugin=plugin, repo=repo, dossier_id=D1,
            se_def={"name": "se", "generates": []},  # no fallback
            se_activity_id=se_id,
            handler_generated=[{"content": {"x": 1}}],  # no type
        )
        # No exception. No rows written.
        rows = await repo.get_all_latest_entities(D1)
        # Only the bootstrap — nothing new.
        assert len(rows) == 0


# --------------------------------------------------------------------
# _process_cross_dossier (worker branch)
# --------------------------------------------------------------------


def _make_cross_plugin(
    task_handlers: dict | None = None,
    extra_activities: list[dict] | None = None,
) -> Plugin:
    """Build a real Plugin with systemAction + optional extras.
    Same helper shape as test_worker_orchestration.py."""
    activities = [SYSTEM_ACTION_DEF]
    if extra_activities:
        activities.extend(extra_activities)
    return Plugin(
        name="test",
        workflow={
            "activities": activities,
            "entity_types": [
                {"type": "system:task", "cardinality": "multiple"},
                {"type": "system:note", "cardinality": "multiple"},
            ],
            "relations": [],
        },
        entity_models={
            "system:task": TaskEntity,
            "system:note": SystemNote,
        },
        task_handlers=task_handlers or {},
    )


async def _seed_cross_task(
    repo: Repository,
    generated_by: UUID,
    target_dossier: UUID,
    target_activity: str,
    fn_name: str = "compute_target",
    **overrides,
):
    """Seed a cross_dossier_activity task."""
    eid = uuid4()
    vid = uuid4()
    content = {
        "kind": "cross_dossier_activity",
        "function": fn_name,
        "target_activity": target_activity,
        "status": "scheduled",
        "result_activity_id": str(uuid4()),
        "attempt_count": 0,
        "max_attempts": 3,
        "base_delay_seconds": 60,
    }
    content.update(overrides)
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type="system:task", generated_by=generated_by,
        content=content, attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid, await repo.get_entity(vid)


class _DummyRegistry:
    def __init__(self, mapping: dict):
        self._mapping = mapping

    def get(self, name):
        return self._mapping.get(name)


class TestProcessCrossDossier:

    async def test_target_dossier_lookup_and_completion(self, repo):
        """Happy path for cross-dossier dispatch. The task
        function returns a target_dossier_id. The phase looks
        up the target dossier's plugin via the registry,
        finds the target activity, executes it in the target
        dossier, then completes the source task with a
        URN-style `result` URI."""
        source_boot = await _bootstrap(repo, D1)
        await _bootstrap(repo, D2)

        # Target activity: a minimal system-callable def
        target_def = {
            "name": "receiveCrossDossierCall",
            "label": "Receive",
            "can_create_dossier": False,
            "client_callable": True,
            "default_role": "systeem",
            "allowed_roles": ["systeem"],
            "authorization": {
                "access": "roles",
                "roles": [{"role": "systeemgebruiker"}],
            },
            "used": [],
            "generates": [],
            "status": None,
            "validators": [],
            "side_effects": [],
            "tasks": [],
        }

        # Source plugin: handles the source-side task function
        # Target plugin: has the target activity
        target_plugin = _make_cross_plugin(
            extra_activities=[target_def],
        )

        # The task function returns the target dossier id.
        from dossier_engine.engine.context import TaskResult

        async def compute(ctx):
            return TaskResult(target_dossier_id=str(D2))

        source_plugin = _make_cross_plugin(
            task_handlers={"compute": compute},
            extra_activities=[target_def],
        )

        # Task pointing at target activity
        eid, _, task_row = await _seed_cross_task(
            repo, source_boot,
            target_dossier=D2,
            target_activity="receiveCrossDossierCall",
            fn_name="compute",
        )

        registry = _DummyRegistry({"test": target_plugin})

        await _process_cross_dossier(
            repo, source_plugin, registry, D1, task_row,
        )
        await repo.session.flush()

        latest = await repo.get_latest_entity_by_id(D1, eid)
        assert latest.content["status"] == "completed"
        # Result URI points at the target dossier + activity
        assert latest.content["result"].startswith(f"https://data.vlaanderen.be/id/dossier/{D2}/")

    async def test_function_not_registered_raises(self, repo):
        """Task declares a function name the plugin doesn't
        know. _process_cross_dossier raises ValueError so the
        outer loop routes it through _record_failure."""
        source_boot = await _bootstrap(repo, D1)
        source_plugin = _make_cross_plugin()  # no task handlers
        registry = _DummyRegistry({})

        _, _, task_row = await _seed_cross_task(
            repo, source_boot,
            target_dossier=D2,
            target_activity="whatever",
            fn_name="missing",
        )

        with pytest.raises(ValueError) as exc:
            await _process_cross_dossier(
                repo, source_plugin, registry, D1, task_row,
            )
        assert "missing" in str(exc.value)

    async def test_target_activity_not_found_raises(self, repo):
        """The task function succeeds and returns a valid
        target_dossier_id, but the target plugin doesn't have
        the declared target_activity. ValueError."""
        await _bootstrap(repo, D1)
        await _bootstrap(repo, D2)
        source_boot = await repo.get_activities_for_dossier(D1)
        source_boot_id = source_boot[0].id

        from dossier_engine.engine.context import TaskResult

        async def compute(ctx):
            return TaskResult(target_dossier_id=str(D2))

        # Target plugin has NO extra activities besides systemAction
        target_plugin = _make_cross_plugin()
        source_plugin = _make_cross_plugin(task_handlers={"compute": compute})
        registry = _DummyRegistry({"test": target_plugin})

        _, _, task_row = await _seed_cross_task(
            repo, source_boot_id,
            target_dossier=D2,
            target_activity="missingInTarget",
            fn_name="compute",
        )

        with pytest.raises(ValueError) as exc:
            await _process_cross_dossier(
                repo, source_plugin, registry, D1, task_row,
            )
        assert "missingInTarget" in str(exc.value)
