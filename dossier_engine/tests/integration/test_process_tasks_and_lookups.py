"""
Integration tests for `process_tasks` and its helpers plus the
entity lookup functions they depend on:

* `process_tasks` — the top-level task scheduling phase
* `_fire_and_forget` — inline task handler invocation
* `_resolve_anchor` — anchor entity_id resolution for scheduled tasks
* `resolve_from_trigger` / `resolve_from_prefetched` — the entity
  lookup used by anchor auto-fill and side-effect resolution
* `lookup_singleton` — the cardinality-enforcing singleton lookup

`_schedule_recorded_task` isn't tested directly because it's a
thin wrapper that composes `_resolve_anchor` + `_supersede_matching`
+ `repo.create_entity`. Both the resolve and supersede halves are
covered in detail elsewhere (`test_task_supersede.py` for supersede,
the anchor classes below for resolve), and the persistence half is
just a `create_entity` call. The effective coverage of the wrapper
comes from the `process_tasks` end-to-end tests that verify a task
gets written with the right content.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.context import HandlerResult
from dossier_engine.engine.errors import ActivityError, CardinalityError
from dossier_engine.engine.lookups import (
    lookup_singleton, resolve_from_trigger, resolve_from_prefetched,
)
from dossier_engine.engine.pipeline.tasks import (
    process_tasks, _fire_and_forget, _resolve_anchor,
)
from dossier_engine.engine.state import ActivityState, Caller
from dossier_engine.auth import User


D1 = UUID("11111111-1111-1111-1111-111111111111")
ANCHOR_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ANCHOR_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _user() -> User:
    return User(id="u1", type="systeem", name="Test", roles=[], properties={})


class _TaskPlugin:
    """Stub plugin exposing `task_handlers`, `entity_models`, and
    `is_singleton` + `cardinality_of` for lookup tests."""
    def __init__(
        self,
        task_handlers: dict | None = None,
        singletons: set[str] | None = None,
    ):
        self.task_handlers = task_handlers or {}
        self.entity_models = {}
        self._singletons = singletons or set()

    def is_singleton(self, entity_type: str) -> bool:
        return entity_type in self._singletons

    def cardinality_of(self, entity_type: str) -> str:
        return "singleton" if entity_type in self._singletons else "multi"


async def _bootstrap(repo: Repository) -> UUID:
    """Create D1 and one bootstrap activity. Returns activity_id."""
    await repo.create_dossier(D1, "toelatingen")
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
    repo: Repository,
    generated_by: UUID,
    entity_type: str,
    *,
    entity_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    """Seed one entity. Returns (entity_id, version_id)."""
    eid = entity_id or uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type=entity_type, generated_by=generated_by,
        content={}, attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid


async def _seed_activity_with_used(
    repo: Repository,
    activity_type: str,
    used_version_ids: list[UUID],
) -> UUID:
    """Create an activity row and link it to `used_version_ids`
    via the `used` table. Returns the activity_id."""
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type=activity_type,
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
    plugin=None,
    activity_id: UUID | None = None,
    activity_def: dict | None = None,
    handler_result=None,
    resolved_entities: dict | None = None,
) -> ActivityState:
    s = ActivityState(
        plugin=plugin or _TaskPlugin(),
        activity_def=activity_def or {"name": "testActivity"},
        repo=repo,
        dossier_id=D1,
        activity_id=activity_id or uuid4(),
        user=_user(),
        role="",
        used_items=[],
        generated_items=[],
        relation_items=[],
        caller=Caller.CLIENT,
    )
    if handler_result is not None:
        s.handler_result = handler_result
    if resolved_entities is not None:
        s.resolved_entities = resolved_entities
    return s


async def _persist_triggering_activity(
    repo: Repository, activity_type: str = "testActivity",
) -> UUID:
    """Persist a real activity row so its id can be used as
    state.activity_id — without this, FK constraints on the task
    persistence layer (generated_by) fail."""
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type=activity_type,
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


# --------------------------------------------------------------------
# _fire_and_forget
# --------------------------------------------------------------------


class TestFireAndForget:

    async def test_no_function_noop(self, repo):
        """task_def without a `function` key → no-op, no
        crash. The task is malformed but fire-and-forget is
        lenient by design."""
        plugin = _TaskPlugin()
        state = _state(repo, plugin=plugin)
        await _fire_and_forget(state, {})  # no exception

    async def test_function_not_registered_noop(self, repo):
        """task_def names a function but the plugin doesn't have
        it. Silently skipped — same leniency pattern as
        run_custom_validators."""
        plugin = _TaskPlugin()
        state = _state(repo, plugin=plugin)
        await _fire_and_forget(state, {"function": "missing"})

    async def test_function_called_with_context(self, repo):
        """Happy path: registered function is called with an
        ActivityContext carrying the state's resolved_entities
        and plugin. Context exposes entities via `get_raw` /
        `get_typed` rather than a public attribute, so we verify
        the mapping by round-tripping through get_raw rather than
        accessing the private field."""
        called = []
        async def fn(ctx):
            called.append(ctx)

        plugin = _TaskPlugin(task_handlers={"notify": fn})
        state = _state(
            repo, plugin=plugin,
            resolved_entities={"oe:x": "some_row"},
        )

        await _fire_and_forget(state, {"function": "notify"})
        assert len(called) == 1
        ctx = called[0]
        assert ctx.dossier_id == D1
        # Round-trip through the public API
        assert ctx.get_used_entity("oe:x") == "some_row"

    async def test_exception_swallowed(self, repo):
        """The name is literal: errors are silently swallowed.
        A failing notification handler shouldn't bring down the
        whole activity."""
        async def fn(ctx):
            raise RuntimeError("nope")

        plugin = _TaskPlugin(task_handlers={"notify": fn})
        state = _state(repo, plugin=plugin)

        # No exception bubbles out.
        await _fire_and_forget(state, {"function": "notify"})


# --------------------------------------------------------------------
# _resolve_anchor
# --------------------------------------------------------------------


class TestResolveAnchor:

    async def test_handler_override_passes_through(self, repo):
        """When task_def carries `anchor_entity_id` directly (e.g.
        from a handler that computed it), that value is used
        verbatim without any DB lookup."""
        state = _state(repo)
        task_def = {
            "anchor_type": "oe:aanvraag",
            "anchor_entity_id": str(ANCHOR_A),
        }
        result = await _resolve_anchor(state, task_def)
        assert result == ANCHOR_A

    async def test_no_anchor_type_returns_none(self, repo):
        """task_def has neither `anchor_type` nor explicit
        `anchor_entity_id` → the task is global-scope, anchor is
        None, no lookup attempted."""
        state = _state(repo)
        result = await _resolve_anchor(state, {})
        assert result is None

    async def test_auto_resolve_from_trigger_generated(self, repo):
        """task_def declares `anchor_type: oe:aanvraag`. The
        triggering activity generated one oe:aanvraag entity.
        The resolver finds it and returns its entity_id."""
        await _bootstrap(repo)
        # Create a triggering activity that generated an oe:aanvraag
        trigger_act = await _persist_triggering_activity(repo, "dienAanvraagIn")
        target_eid, _ = await _seed_entity(
            repo, trigger_act, "oe:aanvraag",
        )

        state = _state(
            repo,
            activity_id=trigger_act,
            activity_def={"name": "dienAanvraagIn"},
        )
        task_def = {"anchor_type": "oe:aanvraag"}

        result = await _resolve_anchor(state, task_def)
        assert result == target_eid

    async def test_auto_resolve_raises_500_when_no_entity(self, repo):
        """task_def demands an anchor but the triggering activity
        didn't touch any entity of that type. 500 — workflow
        misconfiguration."""
        await _bootstrap(repo)
        trigger_act = await _persist_triggering_activity(repo, "someActivity")

        state = _state(
            repo,
            activity_id=trigger_act,
            activity_def={"name": "someActivity"},
        )
        task_def = {
            "anchor_type": "oe:aanvraag",
            "target_activity": "herinnerAanvrager",
        }

        with pytest.raises(ActivityError) as exc:
            await _resolve_anchor(state, task_def)
        assert exc.value.status_code == 500
        assert "Cannot resolve anchor" in str(exc.value)
        assert "oe:aanvraag" in str(exc.value)


# --------------------------------------------------------------------
# process_tasks top-level
# --------------------------------------------------------------------


class TestProcessTasks:

    async def test_no_tasks_noop(self, repo):
        plugin = _TaskPlugin()
        state = _state(repo, plugin=plugin, activity_def={"name": "test"})
        await process_tasks(state)
        # No task rows written.
        rows = await repo.get_entities_by_type(D1, "system:task")
        assert rows == []

    async def test_yaml_task_persisted(self, repo):
        """A YAML-declared scheduled_activity task with no anchor
        gets written as a system:task entity with the right
        content."""
        await _bootstrap(repo)
        trigger_act = await _persist_triggering_activity(repo)

        plugin = _TaskPlugin()
        state = _state(
            repo, plugin=plugin,
            activity_id=trigger_act,
            activity_def={
                "name": "test",
                "tasks": [{
                    "kind": "scheduled_activity",
                    "target_activity": "checkLater",
                    "scheduled_for": "2030-01-01T00:00:00Z",
                }],
            },
        )

        await process_tasks(state)
        await repo.session.flush()

        rows = await repo.get_entities_by_type(D1, "system:task")
        assert len(rows) == 1
        content = rows[0].content
        assert content["kind"] == "scheduled_activity"
        assert content["target_activity"] == "checkLater"
        assert content["status"] == "scheduled"

    async def test_handler_tasks_merged_with_yaml(self, repo):
        """The phase walks `activity_def['tasks']` then
        `handler_result.tasks`. Both get persisted."""
        await _bootstrap(repo)
        trigger_act = await _persist_triggering_activity(repo)

        plugin = _TaskPlugin()
        handler_result = HandlerResult(tasks=[
            {
                "kind": "scheduled_activity",
                "target_activity": "fromHandler",
            },
        ])
        state = _state(
            repo, plugin=plugin,
            activity_id=trigger_act,
            activity_def={
                "name": "test",
                "tasks": [{
                    "kind": "scheduled_activity",
                    "target_activity": "fromYaml",
                }],
            },
            handler_result=handler_result,
        )

        await process_tasks(state)
        await repo.session.flush()

        rows = await repo.get_entities_by_type(D1, "system:task")
        assert len(rows) == 2
        targets = sorted(r.content["target_activity"] for r in rows)
        assert targets == ["fromHandler", "fromYaml"]

    async def test_fire_and_forget_kind_routed_to_handler(self, repo):
        """A task with `kind: fire_and_forget` calls
        `_fire_and_forget` instead of persisting. No task row
        ends up in the DB."""
        await _bootstrap(repo)
        trigger_act = await _persist_triggering_activity(repo)

        called = []
        async def fn(ctx):
            called.append(True)

        plugin = _TaskPlugin(task_handlers={"notify": fn})
        state = _state(
            repo, plugin=plugin,
            activity_id=trigger_act,
            activity_def={
                "name": "test",
                "tasks": [{
                    "kind": "fire_and_forget",
                    "function": "notify",
                }],
            },
        )

        await process_tasks(state)
        await repo.session.flush()

        # fire_and_forget ran...
        assert called == [True]
        # ...but nothing persisted.
        rows = await repo.get_entities_by_type(D1, "system:task")
        assert rows == []

    async def test_kind_defaults_to_recorded(self, repo):
        """When a task_def has no explicit `kind` field, it
        defaults to `recorded` — which goes down the
        `_schedule_recorded_task` path and gets persisted."""
        await _bootstrap(repo)
        trigger_act = await _persist_triggering_activity(repo)

        plugin = _TaskPlugin()
        state = _state(
            repo, plugin=plugin,
            activity_id=trigger_act,
            activity_def={
                "name": "test",
                "tasks": [{
                    # no kind
                    "target_activity": "something",
                }],
            },
        )

        await process_tasks(state)
        await repo.session.flush()

        rows = await repo.get_entities_by_type(D1, "system:task")
        assert len(rows) == 1
        assert rows[0].content["kind"] == "recorded"


# --------------------------------------------------------------------
# lookup_singleton
# --------------------------------------------------------------------


class TestLookupSingleton:

    async def test_singleton_type_found_returned(self, repo):
        await _bootstrap(repo)
        boot = await _persist_triggering_activity(repo)
        target_eid, target_vid = await _seed_entity(
            repo, boot, "oe:dossier_access",
        )
        plugin = _TaskPlugin(singletons={"oe:dossier_access"})

        result = await lookup_singleton(
            plugin, repo, D1, "oe:dossier_access",
        )
        assert result is not None
        assert result.entity_id == target_eid
        assert result.id == target_vid

    async def test_singleton_type_missing_returns_none(self, repo):
        """Singleton type is registered but no instance exists in
        the dossier. Returns None (not an error)."""
        await _bootstrap(repo)
        plugin = _TaskPlugin(singletons={"oe:dossier_access"})

        result = await lookup_singleton(
            plugin, repo, D1, "oe:dossier_access",
        )
        assert result is None

    async def test_multi_cardinality_type_raises_cardinality_error(
        self, repo,
    ):
        """Calling lookup_singleton on a type the plugin has NOT
        declared as singleton raises `CardinalityError`. This
        enforces the invariant at the API boundary — callers that
        really want "the latest instance of multi-cardinality type
        X" must use a different helper."""
        plugin = _TaskPlugin(singletons=set())  # nothing is singleton

        with pytest.raises(CardinalityError) as exc:
            await lookup_singleton(plugin, repo, D1, "oe:bijlage")
        assert "non-singleton" in str(exc.value)
        assert "oe:bijlage" in str(exc.value)


# --------------------------------------------------------------------
# resolve_from_trigger / resolve_from_prefetched
# --------------------------------------------------------------------


class TestResolveFromTrigger:

    async def test_generated_single_match_returned(self, repo):
        """Trigger generated one entity of the requested type.
        Returned."""
        await _bootstrap(repo)
        trigger = await _persist_triggering_activity(repo)
        target_eid, _ = await _seed_entity(repo, trigger, "oe:aanvraag")

        result = await resolve_from_trigger(
            repo, trigger, D1, "oe:aanvraag",
        )
        assert result is not None
        assert result.entity_id == target_eid

    async def test_generated_multiple_ids_returns_none(self, repo):
        """Trigger generated TWO distinct entities of the type.
        The resolver can't decide which one the caller wants → None.
        Caller is expected to raise."""
        await _bootstrap(repo)
        trigger = await _persist_triggering_activity(repo)
        await _seed_entity(repo, trigger, "oe:bijlage")
        await _seed_entity(repo, trigger, "oe:bijlage")

        result = await resolve_from_trigger(
            repo, trigger, D1, "oe:bijlage",
        )
        assert result is None

    async def test_falls_back_to_used_when_not_generated(self, repo):
        """Trigger didn't generate the type but did USE one
        entity of it. The resolver walks the second level and
        finds it. Returns the latest version (via
        get_latest_entity_by_id), not the version the trigger
        actually consumed — that's by design, since anchor
        resolution wants the current state, not the historical."""
        await _bootstrap(repo)
        # Pre-seed an entity
        boot = await _persist_triggering_activity(repo, "bootstrap")
        target_eid, target_vid = await _seed_entity(
            repo, boot, "oe:aanvraag",
        )
        # Trigger USES it (doesn't generate)
        trigger = await _seed_activity_with_used(
            repo, "readerActivity", [target_vid],
        )

        result = await resolve_from_trigger(
            repo, trigger, D1, "oe:aanvraag",
        )
        assert result is not None
        assert result.entity_id == target_eid

    async def test_no_match_anywhere_returns_none(self, repo):
        """Trigger neither generated nor used an entity of the
        type. None."""
        await _bootstrap(repo)
        trigger = await _persist_triggering_activity(repo)

        result = await resolve_from_trigger(
            repo, trigger, D1, "oe:nonexistent",
        )
        assert result is None

    async def test_prefetched_same_logic_no_queries(self, repo):
        """`resolve_from_prefetched` takes the generated/used
        lists directly. Same resolution logic; useful when
        resolving several types from the same trigger."""
        await _bootstrap(repo)
        boot = await _persist_triggering_activity(repo)
        target_eid, target_vid = await _seed_entity(repo, boot, "oe:aanvraag")
        target_row = await repo.get_entity(target_vid)

        result = await resolve_from_prefetched(
            repo, D1,
            trigger_generated=[target_row],
            trigger_used=[],
            entity_type="oe:aanvraag",
        )
        assert result is not None
        assert result.entity_id == target_eid

    async def test_prefetched_multiple_used_same_entity_latest_returned(
        self, repo,
    ):
        """Edge case: trigger used TWO versions of the SAME
        logical entity. The resolver sees one distinct entity_id
        and returns the latest version via get_latest_entity_by_id."""
        await _bootstrap(repo)
        boot = await _persist_triggering_activity(repo)
        shared_eid = uuid4()
        _, vid1 = await _seed_entity(
            repo, boot, "oe:aanvraag", entity_id=shared_eid,
        )
        _, vid2 = await _seed_entity(
            repo, boot, "oe:aanvraag", entity_id=shared_eid,
        )
        row1 = await repo.get_entity(vid1)
        row2 = await repo.get_entity(vid2)

        result = await resolve_from_prefetched(
            repo, D1,
            trigger_generated=[],
            trigger_used=[row1, row2],
            entity_type="oe:aanvraag",
        )
        # Returns the current latest (vid2), not vid1
        assert result is not None
        assert result.id == vid2
