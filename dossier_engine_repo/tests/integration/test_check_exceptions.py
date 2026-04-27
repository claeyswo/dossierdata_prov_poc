"""
Integration tests for the ``check_exceptions`` phase.

Validates the bypass mechanism end-to-end: given a workflow-rules
failure and a seeded ``system:exception`` entity, the phase sets
``state.exempted_by_exception`` and appends the exception to
``state.used_refs`` so ``check_workflow_rules`` skips and the
rest of the pipeline sees the exception in the PROV graph.

What's NOT tested here:

* The consume side-effect firing after persistence — that's
  Pass C's concern, tested in a separate file.
* The full HTTP → pipeline flow for grantException itself —
  covered by the Pydantic validator unit tests plus the shell-
  spec D10 scenario.
* The validator's one-per-activity rejection — that's unit-
  tested over in ``dossier_toelatingen_repo`` since the
  validator lives there.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.pipeline.exceptions import check_exceptions
from dossier_engine.engine.state import ActivityState, Caller


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _user() -> User:
    return User(id="u1", type="systeem", name="Test", roles=[], properties={})


class _StubPlugin:
    """Minimal plugin stub. Deadline rules require ``is_singleton``
    (deadlines feature), but exception-matching itself doesn't
    call anything on the plugin."""
    def __init__(self, singletons: set[str] | None = None):
        self._singletons = singletons or set()

    def is_singleton(self, entity_type: str) -> bool:
        return entity_type in self._singletons


def _state(
    repo: Repository,
    *,
    activity_def: dict,
    now: datetime | None = None,
    plugin=None,
) -> ActivityState:
    """Build a minimal ActivityState for check_exceptions tests."""
    return ActivityState(
        plugin=plugin or _StubPlugin(),
        activity_def=activity_def,
        repo=repo,
        dossier_id=D1,
        activity_id=uuid4(),
        user=_user(),
        role="",
        used_items=[],
        generated_items=[],
        relation_items=[],
        caller=Caller.CLIENT,
        workflow_name="toelatingen",
        now=now or datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc),
    )


async def _bootstrap_dossier(repo: Repository) -> UUID:
    """Seed a dossier with one completed bootstrap activity. Without
    this, ``check_exceptions`` hits its first-activity short-circuit
    and doesn't do anything interesting."""
    await repo.create_dossier(D1, "toelatingen")
    activity_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=activity_id, dossier_id=D1, type="bootstrap",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=activity_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return activity_id


async def _seed_exception(
    repo: Repository,
    activity_id: UUID,
    *,
    activity: str,
    status: str = "active",
    granted_until: str | None = None,
    entity_id: UUID | None = None,
):
    """Seed an ``system:exception`` entity. Returns its version_id
    (row id) which callers can compare against
    ``state.exempted_by_exception``."""
    eid = entity_id or uuid4()
    vid = uuid4()
    content = {
        "activity": activity,
        "status": status,
        "reason": "test",
    }
    if granted_until is not None:
        content["granted_until"] = granted_until
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type="system:exception", generated_by=activity_id,
        content=content, attributed_to="beheerder",
    )
    await repo.session.flush()
    return vid, eid


# --------------------------------------------------------------------
# Happy path — rules pass, phase is a no-op
# --------------------------------------------------------------------


class TestNoExceptionNeeded:
    """When workflow rules pass, the phase never looks at exceptions
    and never flags anything. This is the common case and has to
    stay fast."""

    async def test_rules_pass_no_bypass_flag(self, repo):
        """Activity with empty requirements/forbidden — rules trivially
        pass. The phase checks them and returns without even looking
        for an exception."""
        await _bootstrap_dossier(repo)
        state = _state(repo, activity_def={
            "name": "oe:someActivity",
            "requirements": {},
            "forbidden": {},
        })

        await check_exceptions(state)

        assert state.exempted_by_exception is None
        assert state.used_refs == []
        assert "system:exception" not in state.resolved_entities

    async def test_rules_pass_with_exception_seeded_still_no_bypass(self, repo):
        """Even with an active matching exception sitting in the
        dossier, if the rules pass, the exception is untouched.
        Granted exceptions stay on ice until there's actually
        something blocking. This is the 'bypass-or-nothing' rule."""
        boot = await _bootstrap_dossier(repo)
        await _seed_exception(repo, boot, activity="oe:someActivity")
        state = _state(repo, activity_def={
            "name": "oe:someActivity",
            "requirements": {},
            "forbidden": {},
        })

        await check_exceptions(state)

        assert state.exempted_by_exception is None
        # Exception is not injected into used refs — it wasn't consumed.
        assert state.used_refs == []


# --------------------------------------------------------------------
# Bypass path — rules would fail, matching exception takes effect
# --------------------------------------------------------------------


class TestBypassOnMatch:
    """The main negative-becomes-positive flow. The activity's rules
    fail but an active exception authorizes it anyway."""

    async def test_bypass_flag_set_and_exception_in_used(self, repo):
        boot = await _bootstrap_dossier(repo)
        vid, eid = await _seed_exception(
            repo, boot, activity="oe:gatedActivity",
        )
        state = _state(repo, activity_def={
            "name": "oe:gatedActivity",
            # Unsatisfiable requirement — no ``missingAct`` has
            # completed in the dossier.
            "requirements": {"activities": ["oe:missingAct"]},
        })

        await check_exceptions(state)

        # Phase signalled bypass.
        assert state.exempted_by_exception == vid
        # And injected the exception into the activity's used set so
        # PROV records the usage. Without this edge, the
        # consumeException side-effect's auto-resolve couldn't find
        # the exception in trigger scope.
        assert "system:exception" in state.resolved_entities
        assert len(state.used_refs) == 1
        ref = state.used_refs[0]
        assert ref.type == "system:exception"
        assert ref.version_id == vid
        assert str(eid) in ref.entity
        # used_rows_by_ref keyed on the same ref string
        assert ref.entity in state.used_rows_by_ref

    async def test_bypass_respects_forbidden_rules_too(self, repo):
        """Same flow works when the blocking rule is ``forbidden``
        rather than ``requirements``. Confirming the bypass is
        blanket-over-workflow-rules, not just requirements."""
        boot = await _bootstrap_dossier(repo)
        # Seed an activity row of type "blockingAct" so
        # ``forbidden.activities: [blockingAct]`` fails.
        blocking_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=blocking_id, dossier_id=D1, type="oe:blockingAct",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=blocking_id, agent_id="u1",
            agent_name="User", agent_type="persoon", role="aanvrager",
        ))
        await repo.session.flush()

        vid, _ = await _seed_exception(
            repo, boot, activity="oe:shouldBypass",
        )
        state = _state(repo, activity_def={
            "name": "oe:shouldBypass",
            "forbidden": {"activities": ["oe:blockingAct"]},
        })

        await check_exceptions(state)

        assert state.exempted_by_exception == vid


# --------------------------------------------------------------------
# Filter: activity name mismatch
# --------------------------------------------------------------------


class TestActivityMismatch:
    """Exception for activity A doesn't bypass rules for activity B.
    The match is by exact activity-name equality (both sides are
    qualified after plugin load)."""

    async def test_exception_for_different_activity_does_not_bypass(self, repo):
        boot = await _bootstrap_dossier(repo)
        await _seed_exception(repo, boot, activity="oe:activityA")
        state = _state(repo, activity_def={
            "name": "oe:activityB",
            "requirements": {"activities": ["oe:missing"]},
        })

        await check_exceptions(state)

        assert state.exempted_by_exception is None


# --------------------------------------------------------------------
# Filter: status
# --------------------------------------------------------------------


class TestStatusFiltering:
    """Only status=active exceptions bypass. Consumed and cancelled
    exceptions remain in the dossier as audit history but don't
    grant any active authorization."""

    async def test_consumed_exception_does_not_bypass(self, repo):
        boot = await _bootstrap_dossier(repo)
        await _seed_exception(
            repo, boot, activity="oe:gated", status="consumed",
        )
        state = _state(repo, activity_def={
            "name": "oe:gated",
            "requirements": {"activities": ["oe:missing"]},
        })

        await check_exceptions(state)

        assert state.exempted_by_exception is None

    async def test_cancelled_exception_does_not_bypass(self, repo):
        boot = await _bootstrap_dossier(repo)
        await _seed_exception(
            repo, boot, activity="oe:gated", status="cancelled",
        )
        state = _state(repo, activity_def={
            "name": "oe:gated",
            "requirements": {"activities": ["oe:missing"]},
        })

        await check_exceptions(state)

        assert state.exempted_by_exception is None


# --------------------------------------------------------------------
# Filter: granted_until
# --------------------------------------------------------------------


class TestGrantedUntil:
    """The optional per-exception deadline."""

    async def test_future_granted_until_allows_bypass(self, repo):
        boot = await _bootstrap_dossier(repo)
        vid, _ = await _seed_exception(
            repo, boot, activity="oe:gated",
            granted_until="2026-12-31T00:00:00Z",
        )
        state = _state(repo, activity_def={
            "name": "oe:gated",
            "requirements": {"activities": ["oe:missing"]},
        })

        await check_exceptions(state)

        assert state.exempted_by_exception == vid

    async def test_past_granted_until_blocks_bypass(self, repo):
        """Expired exception — the deadline legally retired it even
        though status is still 'active'. Treated as not-a-match so
        the activity falls through to the normal 409."""
        boot = await _bootstrap_dossier(repo)
        await _seed_exception(
            repo, boot, activity="oe:gated",
            granted_until="2025-01-01T00:00:00Z",
        )
        state = _state(repo, activity_def={
            "name": "oe:gated",
            "requirements": {"activities": ["oe:missing"]},
        })

        await check_exceptions(state)

        assert state.exempted_by_exception is None

    async def test_boundary_instant_blocks_bypass(self, repo):
        """At exactly the granted_until instant, the exception is
        already expired. Uses ``>=`` for consistency with
        ``forbidden.not_after`` deadline semantics."""
        boot = await _bootstrap_dossier(repo)
        boundary = "2026-04-24T12:00:00Z"
        await _seed_exception(
            repo, boot, activity="oe:gated", granted_until=boundary,
        )
        state = _state(
            repo,
            activity_def={
                "name": "oe:gated",
                "requirements": {"activities": ["oe:missing"]},
            },
            now=datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc),
        )

        await check_exceptions(state)

        assert state.exempted_by_exception is None


# --------------------------------------------------------------------
# First-activity short-circuit
# --------------------------------------------------------------------


class TestFirstActivitySkip:
    """Matches the check_workflow_rules skip semantics — on the very
    first activity of a brand-new dossier, the exception lookup is
    useless (no exceptions could possibly exist) and would wastefully
    hit the DB."""

    async def test_bootstrap_first_activity_skips(self, repo):
        """No bootstrap activity seeded. ``can_create_dossier: True``
        and an empty activity list → skip, even if the rules would
        technically fail."""
        await repo.create_dossier(D1, "toelatingen")
        state = _state(repo, activity_def={
            "name": "oe:createDossier",
            "can_create_dossier": True,
            "requirements": {"activities": ["oe:missing"]},
        })

        await check_exceptions(state)

        assert state.exempted_by_exception is None
        # Key: used_refs also clean — we didn't even enter the
        # lookup path, so no side-effects on state.
        assert state.used_refs == []
