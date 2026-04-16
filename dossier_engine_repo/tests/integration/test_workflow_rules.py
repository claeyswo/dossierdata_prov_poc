"""
Integration tests for `validate_workflow_rules` in
`engine.pipeline.authorization`.

This is the structural-preconditions checker that runs before
every activity. It reads the activity's `requirements` and
`forbidden` blocks from the activity_def and verifies:

* Required activities have already been completed
* Required entity types already exist
* The dossier's current status is in the required set
* The dossier is NOT in a forbidden status
* No forbidden activity has already been completed

Failure produces a `(False, reason)` tuple which
`check_workflow_rules` (the pipeline wrapper) turns into a 409.

The function has a nice testability property: it accepts
`known_status` and `known_activity_types` kwargs, which let the
caller skip the repo queries when the caller already has the
values. That same hook lets tests run with fully-deterministic
inputs — no need to seed real activities and entities for every
branch, just pass the pre-computed sets directly.

We do seed real entities for the `entity_type_exists` branch
because that one doesn't have a `known_` kwarg shortcut.

Branches covered:

* `empty_activity_def_passes` — nothing required, nothing
  forbidden, passes trivially.
* `required_activity_completed_passes`
* `required_activity_missing_fails`
* `required_entity_type_exists_passes` — the DB-backed branch,
  seeds a real entity and checks `entity_type_exists`.
* `required_entity_type_missing_fails`
* `required_status_matches_passes`
* `required_status_does_not_match_fails`
* `forbidden_activity_completed_fails`
* `forbidden_status_matches_fails`
* `multiple_requirements_all_satisfied_passes`
* `first_failing_requirement_short_circuits` — the function
  returns on the FIRST failing check, not "collect all failures
  and report". Test locks in the short-circuit order.

The `known_*` kwargs let us avoid seeding per test — each test
passes in exactly the activity types or status value it cares
about. This makes each test one-or-two-lines of setup and one
assertion, which is what pure-branch tests should look like.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.pipeline.authorization import validate_workflow_rules


D1 = UUID("11111111-1111-1111-1111-111111111111")


async def _bootstrap_dossier(repo: Repository) -> UUID:
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


class TestValidateWorkflowRules:

    async def test_empty_activity_def_passes(self, repo):
        """No `requirements`, no `forbidden`. The function should
        return (True, None) immediately — no constraints, nothing
        to violate."""
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={},
            repo=repo,
            dossier_id=D1,
            known_status=None,
            known_activity_types=set(),
        )
        assert valid is True
        assert err is None

    async def test_required_activity_completed_passes(self, repo):
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"activities": ["dienAanvraagIn"]},
            },
            repo=repo,
            dossier_id=D1,
            known_activity_types={"dienAanvraagIn", "systemAction"},
        )
        assert valid is True
        assert err is None

    async def test_required_activity_missing_fails(self, repo):
        """Activity requires `dienAanvraagIn` but the dossier has
        only run `systemAction`. Fails with a specific reason."""
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"activities": ["dienAanvraagIn"]},
            },
            repo=repo,
            dossier_id=D1,
            known_activity_types={"systemAction"},
        )
        assert valid is False
        assert "dienAanvraagIn" in err
        assert "not completed" in err

    async def test_required_entity_type_exists_passes(self, repo):
        """The DB-backed branch. Seed an `oe:aanvraag` entity, then
        declare `requirements.entities = ['oe:aanvraag']`. The
        function calls `repo.entity_type_exists` and finds it."""
        boot = await _bootstrap_dossier(repo)
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=boot,
            content={"status": "draft"}, attributed_to="system",
        )
        await repo.session.flush()

        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"entities": ["oe:aanvraag"]},
            },
            repo=repo,
            dossier_id=D1,
            known_activity_types=set(),
        )
        assert valid is True
        assert err is None

    async def test_required_entity_type_missing_fails(self, repo):
        """No `oe:aanvraag` in the dossier, activity requires one.
        Fails with the 'Required entity type ... does not exist'
        reason."""
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"entities": ["oe:aanvraag"]},
            },
            repo=repo,
            dossier_id=D1,
            known_activity_types=set(),
        )
        assert valid is False
        assert "oe:aanvraag" in err
        assert "does not exist" in err

    async def test_required_status_matches_passes(self, repo):
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"statuses": ["draft", "submitted"]},
            },
            repo=repo,
            dossier_id=D1,
            known_status="submitted",
            known_activity_types=set(),
        )
        assert valid is True
        assert err is None

    async def test_required_status_does_not_match_fails(self, repo):
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"statuses": ["approved"]},
            },
            repo=repo,
            dossier_id=D1,
            known_status="draft",
            known_activity_types=set(),
        )
        assert valid is False
        assert "draft" in err  # current status appears in the message
        assert "approved" in err  # required status too

    async def test_forbidden_activity_completed_fails(self, repo):
        """An activity type that's on the forbidden list has
        already run in this dossier. Fails."""
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {"activities": ["trekAanvraagIn"]},
            },
            repo=repo,
            dossier_id=D1,
            known_activity_types={"trekAanvraagIn", "dienAanvraagIn"},
        )
        assert valid is False
        assert "trekAanvraagIn" in err
        assert "already completed" in err

    async def test_forbidden_status_matches_fails(self, repo):
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {"statuses": ["withdrawn"]},
            },
            repo=repo,
            dossier_id=D1,
            known_status="withdrawn",
            known_activity_types=set(),
        )
        assert valid is False
        assert "withdrawn" in err

    async def test_multiple_requirements_all_satisfied_passes(self, repo):
        """Required activity + required status + required entity
        type all satisfied simultaneously. Passes."""
        boot = await _bootstrap_dossier(repo)
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=boot,
            content={}, attributed_to="system",
        )
        await repo.session.flush()

        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {
                    "activities": ["dienAanvraagIn"],
                    "entities": ["oe:aanvraag"],
                    "statuses": ["submitted"],
                },
            },
            repo=repo,
            dossier_id=D1,
            known_status="submitted",
            known_activity_types={"dienAanvraagIn"},
        )
        assert valid is True
        assert err is None

    async def test_first_failing_requirement_short_circuits(self, repo):
        """When multiple requirements fail, the function returns
        on the first one it checks. The order in the source is:
        required activities → required entities → required
        statuses → forbidden activities → forbidden statuses.
        So if we set up a case where BOTH the required activity
        is missing AND the forbidden status matches, we should
        see the required-activity error (which is checked first)."""
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"activities": ["dienAanvraagIn"]},
                "forbidden": {"statuses": ["withdrawn"]},
            },
            repo=repo,
            dossier_id=D1,
            known_status="withdrawn",
            known_activity_types=set(),  # dienAanvraagIn missing
        )
        assert valid is False
        # First check is required activities — that's what should
        # be in the error.
        assert "dienAanvraagIn" in err
        assert "not completed" in err
