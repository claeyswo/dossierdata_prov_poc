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


# --------------------------------------------------------------------
# Deadline rules (not_after / not_before)
# --------------------------------------------------------------------


class _StubPlugin:
    """Minimal plugin stub for deadline tests. Only `is_singleton`
    is touched — the singleton-enforcement in `resolve_deadline`
    calls it, and `lookup_singleton` (which the same function
    invokes) calls it again. Nothing else."""
    def __init__(self, singletons: set[str] | None = None):
        self._singletons = singletons or set()

    def is_singleton(self, entity_type: str) -> bool:
        return entity_type in self._singletons


async def _seed_entity(
    repo: Repository,
    activity_id: UUID,
    entity_type: str,
    content: dict,
) -> UUID:
    """Seed one entity and return its entity_id. Used by deadline
    tests that exercise the dict-form resolver's DB lookup path."""
    eid = uuid4()
    await repo.create_entity(
        version_id=uuid4(), entity_id=eid, dossier_id=D1,
        type=entity_type, generated_by=activity_id,
        content=content, attributed_to="system",
    )
    await repo.session.flush()
    return eid


class TestDeadlineRules:
    """Tests for `forbidden.not_after` and `requirements.not_before`
    in `validate_workflow_rules`.

    These rules accept the same three forms as scheduled_for's dict
    grammar (no relative-offset-from-now): absolute ISO 8601,
    `{from_entity, field}`, `{from_entity, field, offset}`. When a
    plugin isn't supplied, the deadline checks are skipped entirely
    — every test here passes one explicitly.

    Time source: the function accepts a `now` kwarg; tests pass a
    fixed instant so pass/fail boundaries are deterministic. In
    production, preconditions supplies `state.now` and eligibility
    lets it default to `datetime.now(UTC)`.
    """

    # --- not_after, absolute ISO ---------------------------------

    async def test_not_after_future_iso_passes(self, repo):
        """Deadline is in the future relative to `now` → activity
        still allowed."""
        await _bootstrap_dossier(repo)
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {"not_after": "2026-12-31T23:59:59Z"},
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin(),
            now=now,
        )
        assert valid is True
        assert err is None

    async def test_not_after_past_iso_fails(self, repo):
        """Deadline has passed → activity rejected with a deadline-
        specific error message that names the resolved ISO time so
        the user can see which deadline it was."""
        await _bootstrap_dossier(repo)
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {"not_after": "2026-01-01T00:00:00Z"},
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin(),
            now=now,
        )
        assert valid is False
        assert "deadline has passed" in err
        assert "2026-01-01" in err

    async def test_not_after_exact_boundary_fails(self, repo):
        """At the exact instant the deadline hits, the activity is
        no longer allowed. We use `now >= not_after` (inclusive) so
        '23:59:59' means 'last allowed second' — past that it's
        rejected. Lock this in so nobody accidentally flips to `>`
        without a conscious decision."""
        await _bootstrap_dossier(repo)
        boundary = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {"not_after": "2026-04-24T12:00:00Z"},
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin(),
            now=boundary,
        )
        assert valid is False
        assert "deadline has passed" in err

    # --- not_after, entity-field dict ---------------------------

    async def test_not_after_from_entity_field_passes(self, repo):
        """Singleton has a datetime field in the future. Rule reads
        it via DB lookup and the deadline hasn't passed."""
        boot = await _bootstrap_dossier(repo)
        await _seed_entity(repo, boot, "oe:permit", {
            "expires_at": "2026-12-31T00:00:00Z",
        })
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {
                    "not_after": {
                        "from_entity": "oe:permit",
                        "field": "expires_at",
                    },
                },
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin({"oe:permit"}),
            now=now,
        )
        assert valid is True
        assert err is None

    async def test_not_after_from_entity_field_fails(self, repo):
        boot = await _bootstrap_dossier(repo)
        await _seed_entity(repo, boot, "oe:permit", {
            "expires_at": "2026-01-01T00:00:00Z",  # already past
        })
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {
                    "not_after": {
                        "from_entity": "oe:permit",
                        "field": "expires_at",
                    },
                },
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin({"oe:permit"}),
            now=now,
        )
        assert valid is False
        assert "deadline has passed" in err

    async def test_not_after_with_negative_offset(self, repo):
        """The killer reminder case — 'fire 7 days before permit
        expires'. Sibling feature to scheduled_for's dict+offset.
        Sanity-check that the offset subtracts correctly: expiry
        is Dec 31, offset -7d, so the effective deadline is Dec 24.
        At Dec 20 (before the effective deadline) → passes."""
        boot = await _bootstrap_dossier(repo)
        await _seed_entity(repo, boot, "oe:permit", {
            "expires_at": "2026-12-31T00:00:00Z",
        })
        now = datetime(2026, 12, 20, 12, 0, tzinfo=timezone.utc)
        valid, _ = await validate_workflow_rules(
            activity_def={
                "forbidden": {
                    "not_after": {
                        "from_entity": "oe:permit",
                        "field": "expires_at",
                        "offset": "-7d",
                    },
                },
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin({"oe:permit"}),
            now=now,
        )
        assert valid is True
        # And at Dec 28 (after the effective deadline of Dec 24) → fails.
        now = datetime(2026, 12, 28, 0, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {
                    "not_after": {
                        "from_entity": "oe:permit",
                        "field": "expires_at",
                        "offset": "-7d",
                    },
                },
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin({"oe:permit"}),
            now=now,
        )
        assert valid is False
        # The resolved effective deadline (2026-12-24) should be in
        # the error message.
        assert "2026-12-24" in err

    async def test_not_after_singleton_missing_treats_rule_as_inactive(self, repo):
        """When the deadline rule references a singleton that
        doesn't exist in the dossier yet, the resolver returns None
        and the rule is treated as 'no deadline applies'. The
        activity passes despite the declared rule. Plugins that
        want 'activity only allowed once anchor exists' compose
        `not_after` with `requirements.entities`."""
        await _bootstrap_dossier(repo)
        # NOTE: we do NOT seed oe:permit.
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {
                    "not_after": {
                        "from_entity": "oe:permit",
                        "field": "expires_at",
                    },
                },
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin({"oe:permit"}),
            now=now,
        )
        assert valid is True
        assert err is None

    # --- not_before -------------------------------------------------

    async def test_not_before_future_iso_fails(self, repo):
        """Activity can't run yet — the earliest-allowed time is
        in the future."""
        await _bootstrap_dossier(repo)
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"not_before": "2026-05-01T00:00:00Z"},
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin(),
            now=now,
        )
        assert valid is False
        assert "not yet available" in err
        assert "2026-05-01" in err

    async def test_not_before_past_iso_passes(self, repo):
        await _bootstrap_dossier(repo)
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"not_before": "2026-01-01T00:00:00Z"},
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin(),
            now=now,
        )
        assert valid is True
        assert err is None

    async def test_not_before_exact_boundary_passes(self, repo):
        """Opposite boundary from not_after — the earliest allowed
        second is the one that matches exactly. `now < not_before`
        is strict, so at the boundary the activity becomes legal."""
        await _bootstrap_dossier(repo)
        boundary = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, _ = await validate_workflow_rules(
            activity_def={
                "requirements": {"not_before": "2026-04-24T12:00:00Z"},
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin(),
            now=boundary,
        )
        assert valid is True

    async def test_not_before_from_entity_field_offset(self, repo):
        """'Activity legal starting 30 days after aanvraag was
        registered.' At day 15 → not yet. At day 45 → yes."""
        boot = await _bootstrap_dossier(repo)
        await _seed_entity(repo, boot, "oe:aanvraag", {
            "registered_at": "2026-04-01T00:00:00Z",
        })
        # Day 15 — too early.
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {
                    "not_before": {
                        "from_entity": "oe:aanvraag",
                        "field": "registered_at",
                        "offset": "+30d",
                    },
                },
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin({"oe:aanvraag"}),
            now=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        )
        assert valid is False
        assert "not yet available" in err
        # Day 45 — allowed.
        valid, _ = await validate_workflow_rules(
            activity_def={
                "requirements": {
                    "not_before": {
                        "from_entity": "oe:aanvraag",
                        "field": "registered_at",
                        "offset": "+30d",
                    },
                },
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin({"oe:aanvraag"}),
            now=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        )
        assert valid is True

    # --- combined ---------------------------------------------------

    async def test_both_rules_both_pass(self, repo):
        await _bootstrap_dossier(repo)
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"not_before": "2026-01-01T00:00:00Z"},
                "forbidden": {"not_after": "2026-12-31T00:00:00Z"},
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin(),
            now=now,
        )
        assert valid is True

    async def test_not_before_checked_before_not_after(self, repo):
        """When both declared and both fail, not_before is evaluated
        first and its error surfaces. Locks in the evaluation order
        so a future refactor doesn't silently change which rule wins
        in the error message."""
        await _bootstrap_dossier(repo)
        now = datetime(2030, 4, 24, 12, 0, tzinfo=timezone.utc)
        valid, err = await validate_workflow_rules(
            activity_def={
                "requirements": {"not_before": "2035-01-01T00:00:00Z"},
                "forbidden": {"not_after": "2025-01-01T00:00:00Z"},
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            plugin=_StubPlugin(),
            now=now,
        )
        assert valid is False
        assert "not yet available" in err

    # --- plumbing edge cases ----------------------------------------

    async def test_skipped_when_plugin_not_supplied(self, repo):
        """When no plugin is passed, deadline checks are skipped
        entirely — even a clearly-failing rule doesn't fire. This
        lets narrow unit tests of the non-deadline branches keep
        calling the function without plumbing a plugin through.
        Every production caller passes one."""
        await _bootstrap_dossier(repo)
        valid, err = await validate_workflow_rules(
            activity_def={
                "forbidden": {"not_after": "2020-01-01T00:00:00Z"},
            },
            repo=repo, dossier_id=D1,
            known_activity_types=set(),
            # plugin=None (default)
        )
        assert valid is True
        assert err is None

    async def test_malformed_deadline_raises(self, repo):
        """Runtime malformation (a rule that snuck past the plugin
        validator somehow) raises ValueError out of
        validate_workflow_rules. Caller wraps as 500. We assert
        the raise, not the return, because the contract is 'plugin-
        author bug, not user error'."""
        await _bootstrap_dossier(repo)
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="not_after"):
            await validate_workflow_rules(
                activity_def={
                    "forbidden": {"not_after": "not a date at all"},
                },
                repo=repo, dossier_id=D1,
                known_activity_types=set(),
                plugin=_StubPlugin(),
                now=now,
            )

    async def test_relative_offset_as_top_level_rejected(self, repo):
        """'+20d' has no meaning at deadline-check time — there's no
        fixed anchor for 'now'. Resolver raises loudly."""
        await _bootstrap_dossier(repo)
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="relative offsets are not supported"):
            await validate_workflow_rules(
                activity_def={
                    "forbidden": {"not_after": "+20d"},
                },
                repo=repo, dossier_id=D1,
                known_activity_types=set(),
                plugin=_StubPlugin(),
                now=now,
            )
