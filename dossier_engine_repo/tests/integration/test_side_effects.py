"""
Integration tests for `execute_side_effects` and `_condition_met`
in `engine.pipeline.side_effects`.

Side effects are the recursive child-activity mechanism: an
activity's YAML declares a `side_effects:` list, and after the
main activity persists, the engine walks that list and runs each
entry as a system-caller activity. Each can have its own
side effects, and the chain is depth-limited to prevent runaway.

Three concerns worth pinning down:

1. **Shape of execution.** Does the phase create the activity row,
   invoke the handler, and persist returned entities correctly?
2. **Conditional gating.** The `condition: {entity_type, field,
   value}` block should block execution when the condition
   entity exists but its field doesn't match, AND when the
   entity doesn't exist at all.
3. **Recursion + depth limit.** A side effect with its own
   `side_effects:` should recurse, and depth=max_depth should
   short-circuit cleanly.

The API suite's full workflow runs exercise the happy path but
only obliquely — without targeted tests, the conditional gating
and depth-limit branches aren't reached. These tests cover them
deliberately.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.context import HandlerResult
from dossier_engine.engine.pipeline.side_effects import (
    execute_side_effects, _condition_met,
)


D1 = UUID("11111111-1111-1111-1111-111111111111")


class _SidePlugin:
    """Stub plugin for side_effects tests. Carries a dict of
    activity definitions (looked up via find_activity_def), a
    dict of handlers, a set of singleton types, and a workflow
    dict for any phase that might poke at it."""
    def __init__(
        self,
        activity_defs: dict | None = None,
        handlers: dict | None = None,
        singletons: set[str] | None = None,
    ):
        self._defs = activity_defs or {}
        self.handlers = handlers or {}
        self._singletons = singletons or set()
        self.entity_models = {}
        self.validators = {}
        self.task_handlers = {}
        self.relation_validators = {}
        self.workflow = {"activities": list(self._defs.values()),
                         "relations": []}

    def find_activity_def(self, name: str):
        return self._defs.get(name)

    def is_singleton(self, entity_type: str) -> bool:
        return entity_type in self._singletons

    def cardinality_of(self, entity_type: str) -> str:
        return "singleton" if entity_type in self._singletons else "multi"

    def resolve_schema(self, entity_type, schema_version):
        return None


async def _bootstrap(repo: Repository) -> UUID:
    """Create D1 and a trigger activity. Returns the trigger's id."""
    await repo.create_dossier(D1, "toelatingen")
    await repo.ensure_agent("system", "systeem", "Systeem", {})
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
    await repo.session.flush()
    return act_id


async def _seed_generated(
    repo: Repository,
    by_activity: UUID,
    entity_type: str,
    content: dict,
    *,
    entity_id: UUID | None = None,
) -> UUID:
    """Seed one entity attributed to `by_activity`. Returns the
    version id."""
    eid = entity_id or uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type=entity_type, generated_by=by_activity,
        content=content, attributed_to="system",
    )
    await repo.session.flush()
    return vid


# --------------------------------------------------------------------
# execute_side_effects
# --------------------------------------------------------------------


class TestExecuteSideEffects:

    async def test_empty_list_noop(self, repo):
        """No side effects declared → the phase returns
        immediately without even touching the agents table.
        This is the common case (most activities don't have
        side effects) so the no-op path needs to be fast."""
        trigger = await _bootstrap(repo)
        plugin = _SidePlugin()

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[],
        )

        # No new activity rows beyond the trigger itself.
        result = await repo.session.execute(
            text("SELECT COUNT(*) FROM activities WHERE dossier_id = :d"),
            {"d": D1},
        )
        assert result.scalar() == 1  # just the trigger

    async def test_depth_limit_short_circuits(self, repo):
        """When depth >= max_depth, the function returns early.
        This is the runaway-chain guard. Calling with depth=10
        and max_depth=10 should do nothing even if side_effects
        is non-empty."""
        trigger = await _bootstrap(repo)
        plugin = _SidePlugin()

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[{"activity": "runMe"}],
            depth=10, max_depth=10,
        )

        # No new activity rows written — depth gate fired.
        result = await repo.session.execute(
            text("SELECT COUNT(*) FROM activities WHERE dossier_id = :d"),
            {"d": D1},
        )
        assert result.scalar() == 1

    async def test_activity_def_not_in_plugin_skipped(self, repo):
        """Side effect names an activity the plugin doesn't know.
        Silently skipped — same leniency pattern as other
        lookup-returns-None paths. Allows workflow YAML to
        reference activities that haven't been loaded yet."""
        trigger = await _bootstrap(repo)
        plugin = _SidePlugin()  # no activity defs

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[{"activity": "notInPlugin"}],
        )

        # No side-effect activity row created.
        result = await repo.session.execute(
            text(
                "SELECT COUNT(*) FROM activities "
                "WHERE dossier_id = :d AND type = 'notInPlugin'"
            ),
            {"d": D1},
        )
        assert result.scalar() == 0

    async def test_handler_missing_skipped(self, repo):
        """Activity def exists but has no handler (or handler
        name isn't registered). Silently skipped — side effects
        must compute via handler, no handler means no work to do."""
        trigger = await _bootstrap(repo)
        plugin = _SidePlugin(
            activity_defs={"runMe": {"name": "runMe"}},  # no handler key
        )

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[{"activity": "runMe"}],
        )

        result = await repo.session.execute(
            text(
                "SELECT COUNT(*) FROM activities "
                "WHERE dossier_id = :d AND type = 'runMe'"
            ),
            {"d": D1},
        )
        assert result.scalar() == 0

    async def test_side_effect_without_activity_field_skipped(self, repo):
        """A side effect entry without `activity` is a no-op —
        the phase skips it rather than raising. Defensive for
        partially-written workflow YAML."""
        trigger = await _bootstrap(repo)
        plugin = _SidePlugin()

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[{}],  # no activity field
        )

        result = await repo.session.execute(
            text("SELECT COUNT(*) FROM activities WHERE dossier_id = :d"),
            {"d": D1},
        )
        assert result.scalar() == 1  # just trigger

    async def test_happy_path_creates_activity_row_and_runs_handler(
        self, repo,
    ):
        """The main happy path: side effect with a real activity
        def, real handler, no condition. After the call:
        * An activity row exists for the side effect, with
          informed_by pointing at the trigger.
        * The system association exists.
        * The handler was invoked."""
        trigger = await _bootstrap(repo)

        called = []
        async def handler(ctx, client_content):
            called.append(True)
            return None

        plugin = _SidePlugin(
            activity_defs={
                "runMe": {"name": "runMe", "handler": "h"},
            },
            handlers={"h": handler},
        )

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[{"activity": "runMe"}],
        )
        await repo.session.flush()

        assert called == [True]

        # Side effect activity row exists with informed_by = trigger
        result = await repo.session.execute(
            text(
                "SELECT informed_by FROM activities "
                "WHERE dossier_id = :d AND type = 'runMe'"
            ),
            {"d": D1},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == str(trigger)

    async def test_handler_result_status_stamped(self, repo):
        """When the handler returns HandlerResult.status, it
        gets stamped on the side-effect activity row's
        computed_status. This is how side effects contribute to
        dossier status transitions."""
        trigger = await _bootstrap(repo)

        async def handler(ctx, client_content):
            return HandlerResult(status="ingediend")

        plugin = _SidePlugin(
            activity_defs={
                "runMe": {"name": "runMe", "handler": "h"},
            },
            handlers={"h": handler},
        )

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[{"activity": "runMe"}],
        )
        await repo.session.flush()

        result = await repo.session.execute(
            text(
                "SELECT computed_status FROM activities "
                "WHERE dossier_id = :d AND type = 'runMe'"
            ),
            {"d": D1},
        )
        assert result.scalar() == "ingediend"

    async def test_recursive_nested_side_effects(self, repo):
        """A side effect whose activity_def has its own
        `side_effects:` block triggers a recursive call. After
        the top-level call returns, both the outer and inner
        side-effect activities exist in the DB."""
        trigger = await _bootstrap(repo)

        calls = []
        async def outer_h(ctx, client_content):
            calls.append("outer")
        async def inner_h(ctx, client_content):
            calls.append("inner")

        plugin = _SidePlugin(
            activity_defs={
                "outer": {
                    "name": "outer",
                    "handler": "outer_h",
                    "side_effects": [{"activity": "inner"}],
                },
                "inner": {"name": "inner", "handler": "inner_h"},
            },
            handlers={"outer_h": outer_h, "inner_h": inner_h},
        )

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[{"activity": "outer"}],
        )
        await repo.session.flush()

        assert calls == ["outer", "inner"]

        # Both activity rows landed in the DB
        result = await repo.session.execute(
            text(
                "SELECT type FROM activities WHERE dossier_id = :d "
                "ORDER BY started_at"
            ),
            {"d": D1},
        )
        types = [r[0] for r in result.fetchall()]
        assert types == ["trigger", "outer", "inner"]


# --------------------------------------------------------------------
# _condition_met
# --------------------------------------------------------------------


class TestConditionMet:

    async def test_no_condition_returns_true(self, repo):
        plugin = _SidePlugin()
        result = await _condition_met(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_generated=[], trigger_used=[],
            condition=None,
        )
        assert result is True

    async def test_condition_entity_missing_returns_false(self, repo):
        """Condition declares an entity_type that isn't in the
        trigger's generated OR used scope, and isn't a singleton.
        The condition entity can't be found → condition not met."""
        await _bootstrap(repo)
        plugin = _SidePlugin()  # nothing is singleton
        result = await _condition_met(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_generated=[], trigger_used=[],
            condition={
                "entity_type": "oe:absent",
                "field": "type",
                "value": "x",
            },
        )
        assert result is False

    async def test_condition_field_matches_returns_true(self, repo):
        """Condition entity exists in the trigger's generated list
        and its field equals the expected value. Condition met."""
        trigger = await _bootstrap(repo)
        await _seed_generated(
            repo, trigger, "oe:aanvrager",
            {"type": "natuurlijk_persoon"},
        )

        # Fetch the row so we can pass it in trigger_generated
        rows = await repo.get_entities_by_type(D1, "oe:aanvrager")
        plugin = _SidePlugin()

        result = await _condition_met(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_generated=rows, trigger_used=[],
            condition={
                "entity_type": "oe:aanvrager",
                "field": "type",
                "value": "natuurlijk_persoon",
            },
        )
        assert result is True

    async def test_condition_field_mismatches_returns_false(self, repo):
        """Condition entity exists but its field doesn't match
        the expected value. This is the most common "skip this
        side effect for this user type" pattern."""
        trigger = await _bootstrap(repo)
        await _seed_generated(
            repo, trigger, "oe:aanvrager",
            {"type": "rechtspersoon"},
        )
        rows = await repo.get_entities_by_type(D1, "oe:aanvrager")
        plugin = _SidePlugin()

        result = await _condition_met(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_generated=rows, trigger_used=[],
            condition={
                "entity_type": "oe:aanvrager",
                "field": "type",
                "value": "natuurlijk_persoon",  # expected != stored
            },
        )
        assert result is False

    async def test_singleton_fallback_when_not_in_trigger_scope(self, repo):
        """Trigger didn't touch `oe:dossier_access`, but it's a
        singleton type AND exists in the dossier at large. The
        condition resolver falls back to `lookup_singleton` and
        finds it."""
        await _bootstrap(repo)
        # Seed a singleton outside the trigger's scope
        boot_act = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=boot_act, dossier_id=D1, type="bootstrap",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=boot_act, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await _seed_generated(
            repo, boot_act, "oe:dossier_access", {"level": "owner"},
        )

        plugin = _SidePlugin(singletons={"oe:dossier_access"})

        # Trigger lists are empty — fallback is required
        result = await _condition_met(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_generated=[], trigger_used=[],
            condition={
                "entity_type": "oe:dossier_access",
                "field": "level",
                "value": "owner",
            },
        )
        assert result is True
