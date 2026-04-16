"""
Integration tests for the pre-execution phases in
`engine.pipeline.preconditions`:

* `check_idempotency` — replay detection
* `ensure_dossier` — creation-or-lookup gate
* `resolve_role` — PROV role resolution

The `authorize` and `check_workflow_rules` phases are thin
delegating wrappers; their underlying logic is covered by
`test_authorize.py` and `test_workflow_rules.py` respectively.

`check_idempotency` has one branch we don't cover here: the
"exists, same dossier, same type → build replay response"
happy path. Building the replay response calls into
`build_replay_response` which touches `derive_status`,
`derive_allowed_activities`, and the plugin registry — an
integration shape that's already exercised by the API suite's
idempotency retry flow (`test_requests.sh` retries the same PUT
with the same activity_id to verify the idempotent-replay
contract). The branches we cover here are the error ones that
happen BEFORE the replay response build, since those are the
ones clients can trip on accidentally (reusing an activity_id
across different dossiers or types).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.pipeline.preconditions import (
    check_idempotency, ensure_dossier, resolve_role,
)
from dossier_engine.engine.state import ActivityState, Caller


D1 = UUID("11111111-1111-1111-1111-111111111111")
D2 = UUID("22222222-2222-2222-2222-222222222222")


def _user() -> User:
    return User(id="u1", type="systeem", name="Test", roles=[], properties={})


def _state(
    repo: Repository,
    *,
    activity_id: UUID | None = None,
    dossier_id: UUID = D1,
    activity_def: dict | None = None,
    workflow_name: str | None = None,
    role: str = "",
) -> ActivityState:
    """Build a minimal ActivityState for preconditions tests.
    Different phases read different subsets, so most fields are
    passed as defaults and overridden per test."""
    return ActivityState(
        plugin=None,
        activity_def=activity_def or {"name": "testActivity"},
        repo=repo,
        dossier_id=dossier_id,
        activity_id=activity_id or uuid4(),
        user=_user(),
        role=role,
        used_items=[],
        generated_items=[],
        relation_items=[],
        caller=Caller.CLIENT,
        workflow_name=workflow_name,
    )


# --------------------------------------------------------------------
# check_idempotency
# --------------------------------------------------------------------


class TestCheckIdempotency:

    async def test_activity_does_not_exist_returns_none(self, repo):
        """Fresh activity_id, no prior row. Returns None and the
        pipeline continues."""
        await repo.create_dossier(D1, "toelatingen")
        state = _state(repo)

        result = await check_idempotency(state)
        assert result is None

    async def test_existing_activity_different_dossier_raises_409(self, repo):
        """Same activity_id exists in dossier D2 but the current
        request is for D1. This is a client-side bug (reused id
        by mistake across dossiers) and gets a 409 'different
        dossier'."""
        await repo.create_dossier(D1, "toelatingen")
        await repo.create_dossier(D2, "toelatingen")

        # Seed the activity under D2
        activity_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=activity_id, dossier_id=D2, type="testActivity",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=activity_id, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        # Current request is for D1
        state = _state(repo, activity_id=activity_id, dossier_id=D1)

        with pytest.raises(ActivityError) as exc:
            await check_idempotency(state)
        assert exc.value.status_code == 409
        assert "different dossier" in str(exc.value)

    async def test_existing_activity_different_type_raises_409(self, repo):
        """Same activity_id and dossier but different type. Also
        a client-side reuse bug. The existing row is type
        'testActivity', current request claims 'otherActivity'."""
        await repo.create_dossier(D1, "toelatingen")

        activity_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=activity_id, dossier_id=D1, type="testActivity",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=activity_id, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.session.flush()

        state = _state(
            repo, activity_id=activity_id,
            activity_def={"name": "otherActivity"},
        )

        with pytest.raises(ActivityError) as exc:
            await check_idempotency(state)
        assert exc.value.status_code == 409
        assert "different type" in str(exc.value)


# --------------------------------------------------------------------
# ensure_dossier
# --------------------------------------------------------------------


class TestEnsureDossier:

    async def test_existing_dossier_loaded_into_state(self, repo):
        """Dossier already in the database, the phase just loads
        it into `state.dossier`."""
        await repo.create_dossier(D1, "toelatingen")
        state = _state(repo)

        await ensure_dossier(state)

        assert state.dossier is not None
        assert state.dossier.id == D1
        assert state.dossier.workflow == "toelatingen"

    async def test_missing_dossier_can_create_with_workflow_name(self, repo):
        """Dossier doesn't exist, activity has
        `can_create_dossier: true`, `workflow_name` provided →
        creates the dossier. This is the 'submit' bootstrap path
        at the start of every workflow."""
        state = _state(
            repo,
            activity_def={
                "name": "dienAanvraagIn",
                "can_create_dossier": True,
            },
            workflow_name="toelatingen",
        )

        await ensure_dossier(state)
        await repo.session.flush()

        assert state.dossier is not None
        assert state.dossier.id == D1
        # Round-trip through the DB to confirm it was persisted.
        fetched = await repo.get_dossier(D1)
        assert fetched is not None
        assert fetched.workflow == "toelatingen"

    async def test_missing_dossier_cannot_create_raises_404(self, repo):
        """Dossier doesn't exist AND the activity isn't allowed
        to create one. 404 — this prevents clients from running
        non-bootstrap activities against non-existent dossiers."""
        state = _state(
            repo,
            activity_def={"name": "bewerkAanvraag"},  # no can_create_dossier
            workflow_name="toelatingen",
        )

        with pytest.raises(ActivityError) as exc:
            await ensure_dossier(state)
        assert exc.value.status_code == 404
        assert "not found" in str(exc.value).lower()

    async def test_missing_dossier_can_create_but_no_workflow_name_raises_400(
        self, repo,
    ):
        """The activity is allowed to create a dossier but the
        client didn't specify which workflow the new dossier
        belongs to. 400 — the client has to pick a workflow,
        the engine can't guess."""
        state = _state(
            repo,
            activity_def={
                "name": "dienAanvraagIn",
                "can_create_dossier": True,
            },
            workflow_name=None,  # missing
        )

        with pytest.raises(ActivityError) as exc:
            await ensure_dossier(state)
        assert exc.value.status_code == 400
        assert "workflow" in str(exc.value).lower()


# --------------------------------------------------------------------
# resolve_role (sync function, no DB reads — pure unit)
# --------------------------------------------------------------------


class TestResolveRole:

    async def test_client_supplied_role_in_allowed_kept(self, repo):
        """Client supplied `oe:behandelaar` and it's in the
        activity's allowed list. Kept as-is."""
        state = _state(
            repo,
            activity_def={
                "allowed_roles": ["oe:behandelaar", "oe:aanvrager"],
            },
            role="oe:behandelaar",
        )
        resolve_role(state)
        assert state.role == "oe:behandelaar"

    async def test_client_supplied_role_not_in_allowed_raises_422(self, repo):
        """Client supplied a role that isn't allowed for this
        activity. 422 with the allowed list in the message so
        the client can pick a valid one."""
        state = _state(
            repo,
            activity_def={
                "allowed_roles": ["oe:behandelaar"],
            },
            role="oe:admin",
        )
        with pytest.raises(ActivityError) as exc:
            resolve_role(state)
        assert exc.value.status_code == 422
        assert "oe:admin" in str(exc.value)
        assert "oe:behandelaar" in str(exc.value)

    async def test_no_role_supplied_uses_default_role(self, repo):
        """Client didn't specify a role, activity has a
        `default_role` declared → use that."""
        state = _state(
            repo,
            activity_def={
                "default_role": "oe:behandelaar",
                "allowed_roles": ["oe:behandelaar", "oe:aanvrager"],
            },
            role="",
        )
        resolve_role(state)
        assert state.role == "oe:behandelaar"

    async def test_no_role_no_default_uses_first_allowed(self, repo):
        """Client didn't specify, no `default_role` either, but
        `allowed_roles` is populated → first entry wins."""
        state = _state(
            repo,
            activity_def={
                "allowed_roles": ["oe:aanvrager", "oe:behandelaar"],
            },
            role="",
        )
        resolve_role(state)
        assert state.role == "oe:aanvrager"

    async def test_no_role_no_default_no_allowed_falls_back_to_participant(
        self, repo,
    ):
        """Activity has no role config at all. Falls back to
        generic `participant`. This is the catch-all case for
        activities that don't care about functional roles."""
        state = _state(
            repo,
            activity_def={},
            role="",
        )
        resolve_role(state)
        assert state.role == "participant"

    async def test_no_role_with_default_and_allowed_prefers_default(
        self, repo,
    ):
        """When BOTH `default_role` and `allowed_roles` are
        present, `default_role` wins. This is the common case
        and it's nice to have it locked in."""
        state = _state(
            repo,
            activity_def={
                "default_role": "oe:behandelaar",
                "allowed_roles": ["oe:aanvrager", "oe:behandelaar"],
            },
            role="",
        )
        resolve_role(state)
        assert state.role == "oe:behandelaar"
