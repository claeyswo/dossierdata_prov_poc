"""
End-to-end integration tests for the exception lifecycle:
grant → bypass → consume (auto) → retract.

Uses ``execute_activity`` directly rather than HTTP, because the
engine-level wiring we're testing — auto-injection of consumeException
as a side-effect, the state flag flow, consume's auto-resolve from the
trigger's used scope — is entirely within the pipeline. HTTP-level
tests would add coverage of routing and serialization without telling
us more about the exception mechanism itself.

Mini-workflow built inline: one ``blockedActivity`` whose
``requirements.activities`` names an activity that never completes, so
workflow rules always fail without an exception. Running it with a
seeded active ``system:exception`` exercises the full bypass + consume
chain. Running it without one gives us the baseline 409.

What's NOT here:
* Validator edge-cases for system:exception shape — covered by
  ``dossier_toelatingen_repo/tests/unit/test_valideer_exception.py``.
* The check_exceptions phase in isolation — covered by
  ``test_check_exceptions.py``.
* HTTP-layer grant/retract — covered by the shell-spec D10 scenario.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine import execute_activity
from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.state import Caller
from dossier_engine.plugin import Plugin


D1 = UUID("11111111-1111-1111-1111-111111111111")


# --------------------------------------------------------------------
# Mini workflow
# --------------------------------------------------------------------
#
# One blocked user activity + the three exception activities, all
# declared inline. Activity names are qualified with oe: to match the
# engine's post-normalization convention.


_BOOTSTRAP_DEF = {
    "name": "oe:bootstrap",
    "label": "Bootstrap",
    "can_create_dossier": True,
    "client_callable": True,
    "default_role": "systeem",
    "allowed_roles": ["systeem"],
    "authorization": {"access": "authenticated"},
    "used": [],
    "generates": [],
    "status": "initial",
    "validators": [],
    "side_effects": [],
    "tasks": [],
}


# Activity whose rules will always fail in our tests (the required
# activity name will never complete).
_BLOCKED_DEF = {
    "name": "oe:blockedActivity",
    "label": "Blocked",
    "can_create_dossier": False,
    "client_callable": True,
    "default_role": "oe:beheerder",
    "allowed_roles": ["oe:beheerder"],
    "authorization": {"access": "authenticated"},
    "requirements": {"activities": ["oe:neverCompleted"]},
    "used": [],
    "generates": [],
    "status": "blocked_ran",
    "validators": [],
    "side_effects": [],
    "tasks": [],
}


def _build_plugin() -> Plugin:
    """Build the test plugin with the engine's built-in exception
    activities registered via the same helper ``app.py`` calls in
    production — ``register_exception_activities_on_plugin``. That
    helper reads ``workflow["exceptions"]`` and appends the three
    engine-provided activity defs (grant / retract / consume),
    registers the ``system:exception`` entity type, and wires the
    engine-provided callables into the plugin registries. One code
    path, shared by tests and production.

    The bootstrap + blocked activities are test-local; only the
    exception-lifecycle pieces come from the engine."""
    from dossier_engine.builtins.exceptions import (
        register_exception_activities_on_plugin,
    )
    from dossier_engine.plugin.normalize import _normalize_plugin_activity_names

    workflow = {
        "name": "xwf",
        "default_activity_name_prefix": "oe",
        # Only the test-local activities declared here. The engine's
        # three exception activities get appended by the helper.
        "activities": [_BOOTSTRAP_DEF, _BLOCKED_DEF],
        # No entity_types — the helper adds system:exception; the
        # other types the test uses (none, as it happens) would go
        # here.
        "entity_types": [],
        # Opt in to the exception mechanism. Uses the same role as
        # the rest of this test's activities (``beheerder``) so the
        # ``_user()`` helper authorizes for grant / retract without
        # needing a second role in its list.
        "exceptions": {
            "grant_allowed_roles": ["beheerder"],
            "retract_allowed_roles": ["beheerder"],
        },
    }

    plugin = Plugin(
        name="xwf",
        workflow=workflow,
        entity_models={},
        handlers={},
        validators={},
        relation_validators={},
        field_validators={},
        status_resolvers={},
        task_builders={},
        task_handlers={},
        side_effect_conditions={},
    )

    # Apply the engine's overlay — same code path as production.
    register_exception_activities_on_plugin(plugin)

    # Normalization mirrors what PluginRegistry.register does when
    # the plugin is registered the production way. Bare activity
    # names (the helper's appended ones carry bare "grantException"
    # etc.) get qualified to the default prefix, same as plugin-
    # declared activities.
    _normalize_plugin_activity_names(plugin)
    return plugin


def _user() -> User:
    return User(
        id="admin", type="persoon", name="Admin",
        roles=["beheerder"], properties={},
    )


async def _bootstrap(repo: Repository, plugin: Plugin) -> UUID:
    """Run oe:bootstrap to create the dossier and establish history
    so subsequent activities don't hit the first-activity skip."""
    activity_id = uuid4()
    await execute_activity(
        plugin=plugin, activity_def=_BOOTSTRAP_DEF, repo=repo,
        dossier_id=D1, activity_id=activity_id, user=_user(),
        role="systeem", used_items=[], generated_items=[],
        workflow_name="xwf",
    )
    return activity_id


async def _seed_exception(
    repo: Repository,
    *,
    status: str = "active",
    activity: str = "oe:blockedActivity",
) -> tuple[UUID, UUID]:
    """Seed an system:exception directly via repo (bypassing grantException
    so the plugin validator, which isn't loaded in this mini-plugin,
    isn't in the way). Returns (entity_id, version_id)."""
    # Need a generated_by activity — find the bootstrap one.
    activities = await repo.get_activities_for_dossier(D1)
    generated_by = activities[0].id if activities else None
    eid = uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type="system:exception", generated_by=generated_by,
        content={
            "activity": activity, "status": status,
            "reason": "test",
        },
        attributed_to="admin",
    )
    await repo.session.flush()
    return eid, vid


# --------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------


class TestBaseline:
    """Without an exception, blocked activity stays blocked."""

    async def test_blocked_without_exception_409(self, repo):
        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        with pytest.raises(ActivityError) as exc:
            await execute_activity(
                plugin=plugin, activity_def=_BLOCKED_DEF, repo=repo,
                dossier_id=D1, activity_id=uuid4(), user=_user(),
                role="oe:beheerder", used_items=[], generated_items=[],
                workflow_name="xwf",
            )
        assert exc.value.status_code == 409


class TestBypassAndConsume:
    """The main end-to-end flow — grant an exception, run the blocked
    activity, verify the bypass AND the auto-consume happened."""

    async def test_blocked_activity_runs_and_exception_is_consumed(self, repo):
        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        eid, vid = await _seed_exception(repo)

        # Run the blocked activity — should succeed via bypass.
        await execute_activity(
            plugin=plugin, activity_def=_BLOCKED_DEF, repo=repo,
            dossier_id=D1, activity_id=uuid4(), user=_user(),
            role="oe:beheerder", used_items=[], generated_items=[],
            workflow_name="xwf",
        )

        # Now walk history. There should be a consumeException
        # activity in the dossier.
        activities = await repo.get_activities_for_dossier(D1)
        types = [a.type for a in activities]
        assert "oe:consumeException" in types, (
            f"Expected consumeException in {types}"
        )
        assert "oe:blockedActivity" in types

        # And the exception's latest version should be status=consumed.
        latest = await repo.get_entities_by_type_latest(
            D1, "system:exception",
        )
        assert len(latest) == 1, (
            f"Expected one latest exception, got {len(latest)}"
        )
        assert latest[0].entity_id == eid  # same logical entity
        assert latest[0].id != vid  # but a new version
        assert latest[0].content["status"] == "consumed"
        # Activity / reason preserved — audit trail intact.
        assert latest[0].content["activity"] == "oe:blockedActivity"
        assert latest[0].content["reason"] == "test"

    async def test_consumed_exception_does_not_bypass_again(self, repo):
        """After the exception is consumed, trying the blocked
        activity again gets 409 — single-use semantics enforced."""
        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        await _seed_exception(repo)

        # First run succeeds.
        await execute_activity(
            plugin=plugin, activity_def=_BLOCKED_DEF, repo=repo,
            dossier_id=D1, activity_id=uuid4(), user=_user(),
            role="oe:beheerder", used_items=[], generated_items=[],
            workflow_name="xwf",
        )

        # Second run should fail — exception is now consumed.
        with pytest.raises(ActivityError) as exc:
            await execute_activity(
                plugin=plugin, activity_def=_BLOCKED_DEF, repo=repo,
                dossier_id=D1, activity_id=uuid4(), user=_user(),
                role="oe:beheerder", used_items=[], generated_items=[],
                workflow_name="xwf",
            )
        assert exc.value.status_code == 409


class TestRetract:
    """retractException — admin cancels a granted (still-active)
    exception. The exception's latest version flips to cancelled;
    the activity it would have authorized remains blocked."""

    async def test_retract_cancels_exception(self, repo):
        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        eid, vid = await _seed_exception(repo)

        ex_ref = f"system:exception/{eid}@{vid}"
        await execute_activity(
            plugin=plugin, activity_def=plugin.find_activity_def("retractException"), repo=repo,
            dossier_id=D1, activity_id=uuid4(), user=_user(),
            role="beheerder",
            used_items=[{"entity": ex_ref}],
            generated_items=[],
            workflow_name="xwf",
        )

        latest = await repo.get_entities_by_type_latest(
            D1, "system:exception",
        )
        assert len(latest) == 1
        assert latest[0].entity_id == eid
        assert latest[0].content["status"] == "cancelled"

    async def test_retracted_exception_does_not_bypass(self, repo):
        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        eid, vid = await _seed_exception(repo)

        # Retract it.
        ex_ref = f"system:exception/{eid}@{vid}"
        await execute_activity(
            plugin=plugin, activity_def=plugin.find_activity_def("retractException"), repo=repo,
            dossier_id=D1, activity_id=uuid4(), user=_user(),
            role="beheerder",
            used_items=[{"entity": ex_ref}],
            generated_items=[],
            workflow_name="xwf",
        )

        # Blocked activity should now 409 — no active exception.
        with pytest.raises(ActivityError) as exc:
            await execute_activity(
                plugin=plugin, activity_def=_BLOCKED_DEF, repo=repo,
                dossier_id=D1, activity_id=uuid4(), user=_user(),
                role="oe:beheerder", used_items=[], generated_items=[],
                workflow_name="xwf",
            )
        assert exc.value.status_code == 409

class TestEligibilityViaException:
    """``compute_eligible_activities`` and ``filter_by_user_auth`` must
    surface activities that are runnable thanks to a granted exception.
    Without this, the frontend would hide exception-eligible activities
    from the user — making the exception functionally invisible.

    These tests exercise the read-side (``GET /dossiers/{id}``) flow
    rather than ``execute_activity``: we want to verify that asking
    "what can be run?" returns the right answer, separate from the
    bypass-and-execute flow tested in ``TestBypassAndConsume``.
    """

    async def test_blocked_activity_appears_with_exempted_field_when_exception_active(
        self, repo,
    ):
        from dossier_engine.engine.pipeline._helpers.eligibility import (
            compute_eligible_activities,
        )

        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        _eid, vid = await _seed_exception(repo)

        eligible = await compute_eligible_activities(plugin, repo, D1)
        names = {e["name"] for e in eligible}
        assert "oe:blockedActivity" in names, (
            "expected blocked activity to be eligible via exception"
        )

        entry = next(
            e for e in eligible if e["name"] == "oe:blockedActivity"
        )
        assert entry.get("exempted_by_exception") == str(vid), (
            "entry should carry the matching exception's version_id"
        )

    async def test_other_activities_have_no_exempted_field(self, repo):
        """Sanity check: only the actually-bypassed activity carries
        the field. Bootstrap is normally eligible (no rules failure),
        so it should be plain."""
        from dossier_engine.engine.pipeline._helpers.eligibility import (
            compute_eligible_activities,
        )

        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        await _seed_exception(repo)

        eligible = await compute_eligible_activities(plugin, repo, D1)
        for entry in eligible:
            if entry["name"] == "oe:blockedActivity":
                assert "exempted_by_exception" in entry
            else:
                assert "exempted_by_exception" not in entry, (
                    f"unexpected exemption on {entry['name']}: {entry}"
                )

    async def test_blocked_activity_absent_without_exception(self, repo):
        """Mirror of the active-exception case: when no exception is
        granted, the blocked activity should NOT appear in the eligible
        list. Pins down the existing behavior so the exception path
        doesn't accidentally make non-exempted blocked activities
        eligible too."""
        from dossier_engine.engine.pipeline._helpers.eligibility import (
            compute_eligible_activities,
        )

        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        # Deliberately do NOT seed an exception.

        eligible = await compute_eligible_activities(plugin, repo, D1)
        names = {e["name"] for e in eligible}
        assert "oe:blockedActivity" not in names

    async def test_consumed_exception_does_not_grant_eligibility(self, repo):
        """A consumed exception (single-use already burned) must not
        re-grant eligibility. ``find_active_exception_for_activity``
        filters by ``status == 'active'``; the same predicate the
        bypass phase uses, so eligibility and execution stay
        consistent."""
        from dossier_engine.engine.pipeline._helpers.eligibility import (
            compute_eligible_activities,
        )

        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        await _seed_exception(repo, status="consumed")

        eligible = await compute_eligible_activities(plugin, repo, D1)
        names = {e["name"] for e in eligible}
        assert "oe:blockedActivity" not in names

    async def test_cancelled_exception_does_not_grant_eligibility(self, repo):
        from dossier_engine.engine.pipeline._helpers.eligibility import (
            compute_eligible_activities,
        )

        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        await _seed_exception(repo, status="cancelled")

        eligible = await compute_eligible_activities(plugin, repo, D1)
        names = {e["name"] for e in eligible}
        assert "oe:blockedActivity" not in names

    async def test_filter_passes_exempted_field_through(self, repo):
        """``filter_by_user_auth`` must propagate the
        ``exempted_by_exception`` field from input entries to output
        entries — that's what makes it visible to the response."""
        from dossier_engine.engine.pipeline._helpers.eligibility import (
            compute_eligible_activities, filter_by_user_auth,
        )

        plugin = _build_plugin()
        await _bootstrap(repo, plugin)
        _eid, vid = await _seed_exception(repo)

        eligible = await compute_eligible_activities(plugin, repo, D1)
        allowed = await filter_by_user_auth(
            plugin, eligible, _user(), repo, D1,
        )

        entry = next(
            (a for a in allowed if a["type"] == "oe:blockedActivity"), None,
        )
        assert entry is not None
        assert entry.get("exempted_by_exception") == str(vid)
