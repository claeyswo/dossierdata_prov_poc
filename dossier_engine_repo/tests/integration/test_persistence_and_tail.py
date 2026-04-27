"""
Integration tests for the tail of the pipeline:

* `create_activity_row` — writes ActivityRow + Association
* `persist_outputs` — the bulk write phase
* `determine_status` — resolves the activity's status contribution
* `finalize_dossier` — caches state on the dossier row
* `build_full_response` — formats the response dict
* `derive_status` — walks activity history to find current status
* `compute_eligible_activities` — evaluates workflow rules across
  the activity list
* `filter_by_user_auth` — filters eligibility by user authorization

These are all the phases that run after generated/relations/handler
and produce the final observable effects of an activity. They're
covered here in one file because they're mostly straightforward
pass-through writes + formatters with a small number of branches
each — splitting them into separate files would be noise.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.context import HandlerResult
from dossier_engine.engine.pipeline._helpers.eligibility import (
    compute_eligible_activities, filter_by_user_auth,
    derive_allowed_activities,
)
from dossier_engine.engine.pipeline.finalization import (
    determine_status, finalize_dossier, build_full_response,
    run_pre_commit_hooks,
)
from dossier_engine.engine.pipeline.persistence import (
    create_activity_row, persist_outputs,
)
from dossier_engine.engine.pipeline._helpers.status import derive_status
from dossier_engine.engine.state import (
    ActivityState, Caller, UsedRef, ValidatedRelation,
)


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _user(user_id: str = "u1", *roles: str) -> User:
    return User(
        id=user_id, type="systeem", name="Test",
        roles=list(roles), properties={},
    )


def _state(
    repo: Repository,
    *,
    plugin=None,
    activity_id: UUID | None = None,
    activity_def: dict | None = None,
    user: User | None = None,
    role: str = "oe:system",
    **kwargs,
) -> ActivityState:
    s = ActivityState(
        plugin=plugin,
        activity_def=activity_def or {"name": "testActivity"},
        repo=repo,
        dossier_id=D1,
        activity_id=activity_id or uuid4(),
        user=user or _user(),
        role=role,
        used_items=[],
        generated_items=[],
        relation_items=[],
        caller=Caller.CLIENT,
    )
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


# --------------------------------------------------------------------
# create_activity_row
# --------------------------------------------------------------------


class TestCreateActivityRow:

    async def test_creates_activity_and_association(self, repo):
        """Baseline: one call produces one activity row, one
        association row, and ensures the user's agent row
        exists. All three are persisted and state.activity_row
        carries the activity row object."""
        await repo.create_dossier(D1, "toelatingen")

        activity_id = uuid4()
        user = _user("alice")
        state = _state(
            repo,
            activity_id=activity_id,
            activity_def={"name": "dienAanvraagIn"},
            user=user,
            role="oe:aanvrager",
            now=datetime.now(timezone.utc),
        )

        await create_activity_row(state)
        await repo.session.flush()

        # Activity row
        row = await repo.get_activity(activity_id)
        assert row is not None
        assert row.type == "dienAanvraagIn"
        assert row.dossier_id == D1
        assert state.activity_row is row

        # Agent row
        agents_result = await repo.session.execute(
            text("SELECT id FROM agents WHERE id = 'alice'")
        )
        assert agents_result.scalar() == "alice"

        # Association row
        assoc_result = await repo.session.execute(
            text(
                "SELECT agent_id, role FROM associations "
                "WHERE activity_id = :aid"
            ),
            {"aid": activity_id},
        )
        row = assoc_result.fetchone()
        assert row is not None
        assert row[0] == "alice"
        assert row[1] == "oe:aanvrager"

    async def test_informed_by_propagated(self, repo):
        """When `state.informed_by` is set, it gets persisted on
        the activity row. This is the PROV `wasInformedBy` edge
        linking a task-execution activity back to the activity
        that originally scheduled it."""
        await repo.create_dossier(D1, "toelatingen")

        activity_id = uuid4()
        state = _state(
            repo,
            activity_id=activity_id,
            activity_def={"name": "executeTask"},
            informed_by="some-trigger-activity",
            now=datetime.now(timezone.utc),
        )

        await create_activity_row(state)
        await repo.session.flush()

        row = await repo.get_activity(activity_id)
        assert row.informed_by == "some-trigger-activity"


# --------------------------------------------------------------------
# persist_outputs
# --------------------------------------------------------------------


async def _bootstrap_with_activity(repo: Repository) -> tuple[UUID, UUID]:
    """Create dossier + one activity row + agent. Returns
    (dossier_id, activity_id)."""
    await repo.create_dossier(D1, "toelatingen")
    await repo.ensure_agent("u1", "systeem", "Test", {})
    activity_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=activity_id, dossier_id=D1, type="testActivity",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=activity_id, agent_id="u1",
        agent_name="Test", agent_type="systeem", role="test",
    ))
    await repo.session.flush()
    return D1, activity_id


class TestPersistOutputs:

    async def test_generated_entity_persisted_and_in_response(self, repo):
        """One local generated entity in state.generated. After
        persist_outputs: the entity row exists in the DB with the
        right fields, and state.generated_response contains one
        item with ref, type, content."""
        _, activity_id = await _bootstrap_with_activity(repo)
        eid = uuid4()
        vid = uuid4()
        state = _state(
            repo, activity_id=activity_id,
            generated=[{
                "version_id": vid,
                "entity_id": eid,
                "type": "oe:aanvraag",
                "content": {"titel": "test"},
                "derived_from": None,
                "ref": f"oe:aanvraag/{eid}@{vid}",
                "schema_version": None,
            }],
        )

        await persist_outputs(state)
        await repo.session.flush()

        row = await repo.get_entity(vid)
        assert row is not None
        assert row.type == "oe:aanvraag"
        assert row.entity_id == eid
        assert row.content == {"titel": "test"}
        assert row.generated_by == activity_id
        assert row.attributed_to == "u1"

        assert len(state.generated_response) == 1
        item = state.generated_response[0]
        assert item["type"] == "oe:aanvraag"
        assert item["content"] == {"titel": "test"}
        assert "schemaVersion" not in item  # no schema version set

    async def test_schema_version_propagated_to_row_and_response(self, repo):
        """When the generated item carries `schema_version`, both
        the DB row and the response manifest reflect it. The
        response uses the camelCase key `schemaVersion`."""
        _, activity_id = await _bootstrap_with_activity(repo)
        eid = uuid4()
        vid = uuid4()
        state = _state(
            repo, activity_id=activity_id,
            generated=[{
                "version_id": vid,
                "entity_id": eid,
                "type": "oe:aanvraag",
                "content": {"titel": "test"},
                "derived_from": None,
                "ref": f"oe:aanvraag/{eid}@{vid}",
                "schema_version": "v2",
            }],
        )

        await persist_outputs(state)
        await repo.session.flush()

        row = await repo.get_entity(vid)
        assert row.schema_version == "v2"

        assert state.generated_response[0]["schemaVersion"] == "v2"

    async def test_external_uri_persisted_as_external_row(self, repo):
        """An entry in state.generated_externals gets a
        deterministic entity_id + fresh version_id, persisted
        as a type=external row, and added to the response."""
        _, activity_id = await _bootstrap_with_activity(repo)
        state = _state(
            repo, activity_id=activity_id,
            generated_externals=["https://example.org/foo"],
        )

        await persist_outputs(state)
        await repo.session.flush()

        # Find the external row
        result = await repo.session.execute(
            text("SELECT type, content FROM entities WHERE type = 'external'"),
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == "external"
        assert row[1] == {"uri": "https://example.org/foo"}

        # Response
        assert len(state.generated_response) == 1
        assert state.generated_response[0] == {
            "entity": "https://example.org/foo",
            "type": "external",
            "content": {"uri": "https://example.org/foo"},
        }

    async def test_used_links_persisted(self, repo):
        """For every `used_refs` entry with a `version_id`, a
        `used` table row linking the activity to the entity version
        is written."""
        _, activity_id = await _bootstrap_with_activity(repo)

        # Seed an entity to reference
        target_vid = uuid4()
        await repo.create_entity(
            version_id=target_vid, entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=activity_id,
            content={}, attributed_to="u1",
        )
        await repo.session.flush()

        state = _state(
            repo, activity_id=activity_id,
            used_refs=[UsedRef(
                entity=f"oe:aanvraag/xxx@{target_vid}",
                version_id=target_vid,
                type="oe:aanvraag",
            )],
        )

        await persist_outputs(state)
        await repo.session.flush()

        result = await repo.session.execute(
            text(
                "SELECT COUNT(*) FROM used "
                "WHERE activity_id = :aid AND entity_id = :eid"
            ),
            {"aid": activity_id, "eid": target_vid},
        )
        assert result.scalar() == 1

    async def test_relations_persisted(self, repo):
        """Each entry in `state.validated_relations` becomes a
        `activity_relations` row."""
        _, activity_id = await _bootstrap_with_activity(repo)
        target_vid = uuid4()
        await repo.create_entity(
            version_id=target_vid, entity_id=uuid4(), dossier_id=D1,
            type="oe:aanvraag", generated_by=activity_id,
            content={}, attributed_to="u1",
        )
        await repo.session.flush()

        state = _state(
            repo, activity_id=activity_id,
            validated_relations=[ValidatedRelation(
                version_id=target_vid,
                relation_type="oe:neemtAkteVan",
                ref=f"oe:aanvraag/xxx@{target_vid}",
            )],
        )

        await persist_outputs(state)
        await repo.session.flush()

        result = await repo.session.execute(
            text(
                "SELECT COUNT(*) FROM activity_relations "
                "WHERE activity_id = :aid AND relation_type = :t"
            ),
            {"aid": activity_id, "t": "oe:neemtAkteVan"},
        )
        assert result.scalar() == 1

    async def test_tombstone_nulls_referenced_versions(self, repo):
        """When state.tombstone_version_ids is populated, the
        referenced entity versions have their content nulled and
        their tombstoned_by stamped with the activity_id. The row
        itself is preserved — only the content blob is gone."""
        _, activity_id = await _bootstrap_with_activity(repo)
        target_eid = uuid4()
        target_vid = uuid4()
        await repo.create_entity(
            version_id=target_vid, entity_id=target_eid, dossier_id=D1,
            type="oe:aanvraag", generated_by=activity_id,
            content={"titel": "to be redacted"}, attributed_to="u1",
        )
        await repo.session.flush()

        state = _state(
            repo, activity_id=activity_id,
            tombstone_version_ids=[target_vid],
        )

        await persist_outputs(state)
        await repo.session.flush()

        row = await repo.get_entity(target_vid)
        assert row.content is None
        assert row.tombstoned_by == activity_id
        # Row still exists, so the PROV skeleton is intact.
        assert row.type == "oe:aanvraag"


# --------------------------------------------------------------------
# determine_status
# --------------------------------------------------------------------


class TestDetermineStatus:

    async def test_literal_status_stamped(self, repo):
        activity_row = SimpleNamespace(computed_status=None)
        state = _state(
            repo,
            activity_def={"name": "test", "status": "ingediend"},
            activity_row=activity_row,
        )

        determine_status(state)

        assert activity_row.computed_status == "ingediend"
        assert state.final_status == "ingediend"

    async def test_no_status_no_handler_leaves_row_none(self, repo):
        """Activity has no status in YAML and no HandlerResult.
        The row's computed_status stays None (no stamp)."""
        activity_row = SimpleNamespace(computed_status=None)
        state = _state(
            repo,
            activity_def={"name": "test"},  # no status
            activity_row=activity_row,
            handler_result=None,
        )

        determine_status(state)

        assert activity_row.computed_status is None

    async def test_handler_result_status_used_when_yaml_none(self, repo):
        """YAML has no status, handler returned HandlerResult
        with status → that value wins."""
        activity_row = SimpleNamespace(computed_status=None)
        handler_result = HandlerResult(status="from_handler")
        state = _state(
            repo,
            activity_def={"name": "test"},
            activity_row=activity_row,
            handler_result=handler_result,
        )

        determine_status(state)

        assert activity_row.computed_status == "from_handler"

    async def test_yaml_literal_wins_over_handler(self, repo):
        """When YAML has a literal status AND the handler returns
        a HandlerResult, the YAML value wins. The handler is the
        fallback, not the override."""
        activity_row = SimpleNamespace(computed_status=None)
        handler_result = HandlerResult(status="from_handler")
        state = _state(
            repo,
            activity_def={"name": "test", "status": "from_yaml"},
            activity_row=activity_row,
            handler_result=handler_result,
        )

        determine_status(state)

        assert activity_row.computed_status == "from_yaml"

    async def test_mapped_status_from_generated_entity(self, repo):
        """YAML's `status:` is a dict with `from_entity`, `field`,
        and `mapping`. The phase reads the field from the matching
        generated entity's content and looks up its value in the
        mapping."""
        activity_row = SimpleNamespace(computed_status=None)
        eid = uuid4()
        state = _state(
            repo,
            activity_def={
                "name": "neemBeslissing",
                "status": {
                    "from_entity": "oe:beslissing",
                    "field": "content.beslissing",
                    "mapping": {
                        "goedgekeurd": "toelating_verleend",
                        "afgewezen": "aanvraag_afgewezen",
                    },
                },
            },
            activity_row=activity_row,
            generated=[{
                "type": "oe:beslissing",
                "entity_id": eid,
                "content": {"beslissing": "goedgekeurd"},
            }],
        )

        determine_status(state)

        assert activity_row.computed_status == "toelating_verleend"

    async def test_mapped_status_value_not_in_mapping_leaves_row_none(
        self, repo,
    ):
        """The entity's field has a value that's not in the
        mapping → no status stamp. This is the 'degenerate YAML
        config' path — a mapping that doesn't cover the value
        produced by the handler."""
        activity_row = SimpleNamespace(computed_status=None)
        state = _state(
            repo,
            activity_def={
                "name": "test",
                "status": {
                    "from_entity": "oe:beslissing",
                    "field": "content.beslissing",
                    "mapping": {"goedgekeurd": "ok"},
                },
            },
            activity_row=activity_row,
            generated=[{
                "type": "oe:beslissing",
                "entity_id": uuid4(),
                "content": {"beslissing": "onbekend"},  # not in mapping
            }],
        )

        determine_status(state)

        assert activity_row.computed_status is None


# --------------------------------------------------------------------
# derive_status
# --------------------------------------------------------------------


class TestDeriveStatus:

    async def test_empty_dossier_returns_nieuw(self, repo):
        """A fresh dossier with no activities has status
        'nieuw' (Dutch for 'new')."""
        await repo.create_dossier(D1, "toelatingen")
        status = await derive_status(repo, D1)
        assert status == "nieuw"

    async def test_activities_without_computed_status_return_nieuw(
        self, repo,
    ):
        """A dossier with activities that all have
        `computed_status=None` still resolves to 'nieuw'. Only
        status-stamping activities affect the derived status."""
        await repo.create_dossier(D1, "toelatingen")
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=uuid4(), dossier_id=D1, type="noop",
            started_at=now, ended_at=now, computed_status=None,
        )
        await repo.session.flush()

        status = await derive_status(repo, D1)
        assert status == "nieuw"

    async def test_latest_computed_status_wins(self, repo):
        """The function walks newest-first and returns the first
        non-null computed_status. So if three activities have
        statuses A, B, C in chronological order, the result is C."""
        await repo.create_dossier(D1, "toelatingen")
        base = datetime.now(timezone.utc)
        for i, status in enumerate(["ingediend", "onvolledig", "goedgekeurd"]):
            await repo.create_activity(
                activity_id=uuid4(), dossier_id=D1, type=f"step_{i}",
                started_at=base.replace(microsecond=i * 100),
                ended_at=base.replace(microsecond=i * 100),
                computed_status=status,
            )
        await repo.session.flush()

        status = await derive_status(repo, D1)
        assert status == "goedgekeurd"

    async def test_skips_null_to_find_earlier_non_null(self, repo):
        """If the newest activity has null computed_status (e.g.
        it was a side effect that didn't advance status), the
        walk continues to find the earlier non-null one."""
        await repo.create_dossier(D1, "toelatingen")
        base = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=uuid4(), dossier_id=D1, type="step_1",
            started_at=base.replace(microsecond=100),
            ended_at=base.replace(microsecond=100),
            computed_status="ingediend",
        )
        await repo.create_activity(
            activity_id=uuid4(), dossier_id=D1, type="step_2",
            started_at=base.replace(microsecond=200),
            ended_at=base.replace(microsecond=200),
            computed_status=None,  # didn't stamp
        )
        await repo.session.flush()

        status = await derive_status(repo, D1)
        assert status == "ingediend"


# --------------------------------------------------------------------
# compute_eligible_activities + filter_by_user_auth
# --------------------------------------------------------------------


class _EligibilityPlugin:
    """Stub plugin for eligibility tests. workflow dict carries
    the activities list. `_resolve_field` isn't needed here.

    `singletons` lets tests exercise the dict-form deadline path —
    `resolve_deadline` calls `plugin.is_singleton` before the DB
    lookup, so tests that declare ``not_after: {from_entity, field}``
    must whitelist that type here or the resolver raises."""
    def __init__(
        self, activities: list[dict],
        singletons: set[str] | None = None,
    ):
        self.workflow = {"activities": activities, "relations": []}
        self.entity_models = {}
        self.validators = {}
        self.handlers = {}
        self._singletons = singletons or set()

    def is_singleton(self, entity_type):
        return entity_type in self._singletons

    def cardinality_of(self, entity_type):
        """Only reached from lookup_singleton's error path if
        is_singleton returns False. Satisfies its contract."""
        return "single" if entity_type in self._singletons else "multiple"


class TestComputeEligibleActivities:

    async def test_all_activities_without_rules_are_eligible(self, repo):
        """Workflow with three activities, no rules. All three
        come back as eligible (structurally unrestricted)."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([
            {"name": "a"}, {"name": "b"}, {"name": "c"},
        ])

        result = await compute_eligible_activities(plugin, repo, D1)
        assert {e["name"] for e in result} == {"a", "b", "c"}
        # No exceptions in play → no entry should carry the field.
        assert all("exempted_by_exception" not in e for e in result)

    async def test_non_client_callable_excluded(self, repo):
        """Activities marked `client_callable: false` are
        excluded from the eligibility list — they exist in the
        workflow but aren't meant to be called directly. System
        actions and side-effect triggers fall in this category."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([
            {"name": "a"},
            {"name": "internal", "client_callable": False},
            {"name": "b"},
        ])

        result = await compute_eligible_activities(plugin, repo, D1)
        names = {e["name"] for e in result}
        assert names == {"a", "b"}
        assert "internal" not in names

    async def test_failing_workflow_rules_excludes(self, repo):
        """Activities whose structural preconditions fail don't
        show up in the result. Activity 'b' requires 'a' to have
        run; in an empty dossier 'a' is eligible but 'b' is not."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([
            {"name": "a"},
            {"name": "b", "requirements": {"activities": ["a"]}},
        ])

        result = await compute_eligible_activities(plugin, repo, D1)
        names = {e["name"] for e in result}
        assert "a" in names
        assert "b" not in names


class TestFilterByUserAuth:

    async def test_authorized_activities_returned_with_label(self, repo):
        """Activities the user is authorized for come back as
        {type, label} dicts. Default label falls back to the
        activity name when none is set."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([
            {"name": "a", "authorization": {"access": "authenticated"}},
            {"name": "b", "label": "B Label",
             "authorization": {"access": "authenticated"}},
        ])

        result = await filter_by_user_auth(
            plugin, [{"name": "a"}, {"name": "b"}], _user(), repo, D1,
        )

        assert result == [
            {"type": "a", "label": "a"},
            {"type": "b", "label": "B Label"},
        ]

    async def test_unauthorized_activities_filtered_out(self, repo):
        """User lacks the required role for 'a'. It's dropped
        from the list."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([
            {
                "name": "a",
                "authorization": {
                    "access": "roles",
                    "roles": [{"role": "admin"}],
                },
            },
            {"name": "b", "authorization": {"access": "authenticated"}},
        ])

        result = await filter_by_user_auth(
            plugin, [{"name": "a"}, {"name": "b"}], _user(), repo, D1,
        )

        assert [a["type"] for a in result] == ["b"]

    async def test_unknown_activity_name_silently_skipped(self, repo):
        """If an activity name in the eligible list doesn't exist
        in the workflow (stale cache scenario), it's silently
        skipped rather than raising."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([
            {"name": "a", "authorization": {"access": "authenticated"}},
        ])

        result = await filter_by_user_auth(
            plugin, [{"name": "a"}, {"name": "ghost"}], _user(), repo, D1,
        )

        assert [a["type"] for a in result] == ["a"]

    # --- deadline fields in the response (Pass B) ----------------
    #
    # These lock in the flat-shape contract: `not_before` /
    # `not_after` appear on an entry ONLY when (a) the activity_def
    # declares the rule AND (b) the deadline resolves successfully.
    # Missing singletons → field absent. Neither declared → entry
    # shape is plain {type, label} (covered by the earlier tests).

    async def test_not_after_iso_form_appears_in_response(self, repo):
        """Activity declares `forbidden.not_after: "<ISO>"`. The
        response entry includes `not_after` with the same ISO
        string (normalized)."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([{
            "name": "renew",
            "authorization": {"access": "authenticated"},
            "forbidden": {"not_after": "2026-12-31T23:59:59Z"},
        }])

        result = await filter_by_user_auth(
            plugin, [{"name": "renew"}], _user(), repo, D1,
        )

        assert len(result) == 1
        entry = result[0]
        assert entry["type"] == "renew"
        assert entry["not_after"] == "2026-12-31T23:59:59+00:00"
        # Confirm we didn't sneak in anything else (not_before absent).
        assert "not_before" not in entry

    async def test_not_before_iso_form_appears_in_response(self, repo):
        """Symmetric check for `requirements.not_before`. Fields
        are flat (per design choice in Q5); `not_before` is a
        sibling of `type`/`label`, not nested under a `deadlines`
        key."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([{
            "name": "earlyAction",
            "authorization": {"access": "authenticated"},
            "requirements": {"not_before": "2026-01-01T00:00:00Z"},
        }])

        result = await filter_by_user_auth(
            plugin, [{"name": "earlyAction"}], _user(), repo, D1,
        )

        assert len(result) == 1
        entry = result[0]
        assert entry["not_before"] == "2026-01-01T00:00:00+00:00"
        assert "not_after" not in entry

    async def test_both_rules_both_fields_present(self, repo):
        """An activity with both rules gets both fields in the
        response. Frontends can show a 'window open from X to Y'
        UI hint."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([{
            "name": "windowedAction",
            "authorization": {"access": "authenticated"},
            "requirements": {"not_before": "2026-01-01T00:00:00Z"},
            "forbidden": {"not_after": "2026-12-31T23:59:59Z"},
        }])

        result = await filter_by_user_auth(
            plugin, [{"name": "windowedAction"}], _user(), repo, D1,
        )

        assert len(result) == 1
        entry = result[0]
        assert "not_before" in entry
        assert "not_after" in entry

    async def test_no_rules_no_fields(self, repo):
        """No deadline declared → response entry is the old
        `{type, label}` shape exactly. Locks in that the new fields
        don't leak in as null / None when absent from the YAML —
        absent from the dict entirely."""
        await repo.create_dossier(D1, "toelatingen")
        plugin = _EligibilityPlugin([{
            "name": "plain",
            "authorization": {"access": "authenticated"},
        }])

        result = await filter_by_user_auth(
            plugin, [{"name": "plain"}], _user(), repo, D1,
        )

        assert result == [{"type": "plain", "label": "plain"}]

    async def test_dict_form_not_after_resolves_from_singleton(self, repo):
        """Dict-form `not_after` hits the DB via `lookup_singleton`.
        Seed a singleton, declare the rule, confirm the response
        carries the resolved ISO string."""
        from dossier_engine.db.models import AssociationRow
        await repo.create_dossier(D1, "toelatingen")
        # Bootstrap activity so seeded entity has a valid generated_by.
        act_id = uuid4()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=act_id, dossier_id=D1, type="systemAction",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=act_id, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(), dossier_id=D1,
            type="oe:permit", generated_by=act_id,
            content={"expires_at": "2026-12-31T00:00:00Z"},
            attributed_to="system",
        )
        await repo.session.flush()

        plugin = _EligibilityPlugin(
            [{
                "name": "renew",
                "authorization": {"access": "authenticated"},
                "forbidden": {
                    "not_after": {
                        "from_entity": "oe:permit",
                        "field": "expires_at",
                    },
                },
            }],
            singletons={"oe:permit"},
        )

        result = await filter_by_user_auth(
            plugin, [{"name": "renew"}], _user(), repo, D1,
        )

        assert len(result) == 1
        assert result[0]["not_after"] == "2026-12-31T00:00:00+00:00"

    async def test_dict_form_singleton_missing_field_absent(self, repo):
        """The 'rule inactive' path. Dict-form rule points at a
        singleton type that isn't in the dossier yet. Resolver
        returns None; the response entry has no `not_after` field.
        Matches the validator semantics: when the anchor doesn't
        exist, the deadline rule has no meaning, so nothing to
        display to the user."""
        await repo.create_dossier(D1, "toelatingen")
        # NOTE: no oe:permit seeded.
        plugin = _EligibilityPlugin(
            [{
                "name": "renew",
                "authorization": {"access": "authenticated"},
                "forbidden": {
                    "not_after": {
                        "from_entity": "oe:permit",
                        "field": "expires_at",
                    },
                },
            }],
            singletons={"oe:permit"},
        )

        result = await filter_by_user_auth(
            plugin, [{"name": "renew"}], _user(), repo, D1,
        )

        # Activity is still eligible (rule treated as inactive when
        # anchor missing, per Pass A semantics). But no not_after
        # in the response because there's nothing to display.
        assert len(result) == 1
        assert "not_after" not in result[0]


# --------------------------------------------------------------------
# finalize_dossier
# --------------------------------------------------------------------


class TestFinalizeDossier:

    async def test_skip_cache_uses_computed_status_directly(self, repo):
        """Bulk path: state.skip_cache=True bypasses the cache
        write, post-activity hook, and eligibility computation.
        state.current_status comes from the activity_row's
        computed_status."""
        await repo.create_dossier(D1, "toelatingen")
        activity_row = SimpleNamespace(computed_status="bulked")
        state = _state(
            repo,
            plugin=_EligibilityPlugin([]),
            skip_cache=True,
            activity_row=activity_row,
            final_status="bulked",
        )

        await finalize_dossier(state)

        assert state.current_status == "bulked"
        assert state.allowed_activities == []

    async def test_skip_cache_fallback_to_unknown(self, repo):
        """Bulk path with no computed status anywhere → current
        status falls back to 'unknown' rather than None."""
        await repo.create_dossier(D1, "toelatingen")
        activity_row = SimpleNamespace(computed_status=None)
        state = _state(
            repo,
            plugin=_EligibilityPlugin([]),
            skip_cache=True,
            activity_row=activity_row,
            final_status=None,
        )

        await finalize_dossier(state)

        assert state.current_status == "unknown"

    async def test_full_path_writes_cache_and_computes_allowed(self, repo):
        """Non-skip path: derives status from history, caches it
        on the dossier row, computes the user-filtered allowed
        activities list, and writes it back to the state."""
        await repo.create_dossier(D1, "toelatingen")
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=uuid4(), dossier_id=D1, type="dienAanvraagIn",
            started_at=now, ended_at=now, computed_status="ingediend",
        )
        await repo.session.flush()

        plugin = _EligibilityPlugin([
            {
                "name": "bewerkAanvraag",
                "authorization": {"access": "authenticated"},
            },
        ])
        plugin.post_activity_hook = None  # explicit no-hook

        activity_row = SimpleNamespace(computed_status="ingediend")
        state = _state(
            repo,
            plugin=plugin,
            skip_cache=False,
            activity_row=activity_row,
            activity_def={"name": "dienAanvraagIn"},
            final_status="ingediend",
        )

        await finalize_dossier(state)
        await repo.session.flush()

        assert state.current_status == "ingediend"
        assert any(a["type"] == "bewerkAanvraag" for a in state.allowed_activities)

        # Cache was written back to the dossier row
        dossier = await repo.get_dossier(D1)
        assert dossier.cached_status == "ingediend"
        assert "bewerkAanvraag" in dossier.eligible_activities

    async def test_post_activity_hook_called_when_present(self, repo):
        """When plugin.post_activity_hook is defined, it's called
        with the current entities map, dossier_id, activity_type,
        and status. Hook exceptions are swallowed (warnings
        logged) so a hook failure doesn't fail the request."""
        await repo.create_dossier(D1, "toelatingen")

        called = []
        async def hook(**kwargs):
            called.append(kwargs)

        plugin = _EligibilityPlugin([])
        plugin.post_activity_hook = hook

        activity_row = SimpleNamespace(computed_status="ingediend")
        state = _state(
            repo,
            plugin=plugin,
            skip_cache=False,
            activity_row=activity_row,
            activity_def={"name": "test"},
            final_status="ingediend",
        )

        await finalize_dossier(state)

        assert len(called) == 1
        assert called[0]["dossier_id"] == D1
        assert called[0]["activity_type"] == "test"

    async def test_post_activity_hook_exception_swallowed(self, repo):
        """Hook raises → warning logged, finalize continues to
        cache the results and return normally. The request
        shouldn't fail because an indexer hook had a glitch."""
        await repo.create_dossier(D1, "toelatingen")

        async def hook(**kwargs):
            raise RuntimeError("hook broke")

        plugin = _EligibilityPlugin([])
        plugin.post_activity_hook = hook

        activity_row = SimpleNamespace(computed_status="ok")
        state = _state(
            repo, plugin=plugin, skip_cache=False,
            activity_row=activity_row,
            activity_def={"name": "test"},
            final_status="ok",
        )

        # No exception raised by finalize itself.
        await finalize_dossier(state)


# --------------------------------------------------------------------
# run_pre_commit_hooks
# --------------------------------------------------------------------


class TestRunPreCommitHooks:
    """Pre-commit hooks are the strict counterpart of post_activity_hook.
    Unlike post_activity_hook, whose exceptions are logged and swallowed,
    pre-commit hooks propagate exceptions so the engine can roll back
    the transaction. Use them for mandatory validation/side effects."""

    async def test_no_hooks_is_noop(self, repo):
        """Plugin with no pre_commit_hooks → phase returns silently.
        This is the default and must stay a pure no-op so nothing
        regresses for existing plugins."""
        plugin = _EligibilityPlugin([])
        # Default Plugin would have pre_commit_hooks=[]; the stub
        # doesn't declare it, which also must be tolerated (getattr
        # with default).
        state = _state(repo, plugin=plugin)

        await run_pre_commit_hooks(state)  # must not raise

    async def test_empty_list_is_noop(self, repo):
        """Explicit empty list also behaves as no-op."""
        plugin = _EligibilityPlugin([])
        plugin.pre_commit_hooks = []
        state = _state(repo, plugin=plugin)

        await run_pre_commit_hooks(state)

    async def test_single_hook_invoked_with_kwargs(self, repo):
        """A declared hook is called once, receiving the expected
        keyword arguments (repo, dossier_id, plugin, activity_def,
        generated_items, used_rows, user)."""
        await repo.create_dossier(D1, "toelatingen")

        calls = []
        async def hook(**kwargs):
            calls.append(kwargs)

        plugin = _EligibilityPlugin([])
        plugin.pre_commit_hooks = [hook]
        state = _state(
            repo,
            plugin=plugin,
            activity_def={"name": "test"},
        )

        await run_pre_commit_hooks(state)

        assert len(calls) == 1
        kw = calls[0]
        assert kw["repo"] is repo
        assert kw["dossier_id"] == D1
        assert kw["plugin"] is plugin
        assert kw["activity_def"] == {"name": "test"}
        # generated_items / used_rows / user keys are present
        assert "generated_items" in kw
        assert "used_rows" in kw
        assert "user" in kw

    async def test_hook_exception_propagates(self, repo):
        """Unlike post_activity_hook, pre-commit exceptions escape —
        this is the whole point. The caller (execute_activity) is
        inside a transaction; letting the exception bubble up is
        what rolls back the activity."""
        await repo.create_dossier(D1, "toelatingen")

        async def hook(**kwargs):
            raise RuntimeError("veto")

        plugin = _EligibilityPlugin([])
        plugin.pre_commit_hooks = [hook]
        state = _state(repo, plugin=plugin)

        with pytest.raises(RuntimeError, match="veto"):
            await run_pre_commit_hooks(state)

    async def test_hooks_run_in_declaration_order(self, repo):
        """Multiple hooks run in the order they're declared.
        Important for hooks that logically depend on each other."""
        await repo.create_dossier(D1, "toelatingen")

        order = []
        async def hook_a(**kwargs): order.append("a")
        async def hook_b(**kwargs): order.append("b")
        async def hook_c(**kwargs): order.append("c")

        plugin = _EligibilityPlugin([])
        plugin.pre_commit_hooks = [hook_a, hook_b, hook_c]
        state = _state(repo, plugin=plugin)

        await run_pre_commit_hooks(state)

        assert order == ["a", "b", "c"]

    async def test_first_raise_stops_subsequent_hooks(self, repo):
        """When a hook raises, later hooks in the chain DON'T run.
        This mirrors a normal Python function — there's no 'all
        hooks must get a chance' semantics. First veto wins."""
        await repo.create_dossier(D1, "toelatingen")

        ran = []
        async def hook_a(**kwargs): ran.append("a")
        async def hook_b(**kwargs):
            ran.append("b")
            raise RuntimeError("veto")
        async def hook_c(**kwargs): ran.append("c")

        plugin = _EligibilityPlugin([])
        plugin.pre_commit_hooks = [hook_a, hook_b, hook_c]
        state = _state(repo, plugin=plugin)

        with pytest.raises(RuntimeError):
            await run_pre_commit_hooks(state)

        assert ran == ["a", "b"], f"hook_c must not run after hook_b raised, got {ran}"


# --------------------------------------------------------------------
# build_full_response
# --------------------------------------------------------------------


class TestBuildFullResponse:

    async def test_response_shape(self, repo):
        """The response dict has the expected top-level keys:
        activity, used, generated, relations, dossier. Each
        carries the right projection of state."""
        await repo.create_dossier(D1, "toelatingen")
        dossier_row = await repo.get_dossier(D1)

        activity_id = uuid4()
        now = datetime.now(timezone.utc)
        user = _user("alice")
        state = _state(
            repo,
            activity_id=activity_id,
            activity_def={"name": "test"},
            user=user,
            role="oe:admin",
            now=now,
            dossier=dossier_row,
            used_refs=[
                UsedRef(
                    entity="oe:aanvraag/x@y",
                    version_id=uuid4(),
                    type="oe:aanvraag",
                ),
            ],
            generated_response=[{
                "entity": "oe:beslissing/a@b",
                "type": "oe:beslissing",
                "content": {"x": 1},
            }],
            validated_relations=[ValidatedRelation(
                version_id=uuid4(),
                relation_type="oe:references",
                ref="oe:thing/q@r",
            )],
            current_status="goedgekeurd",
            allowed_activities=[{"type": "foo", "label": "Foo"}],
        )

        response = build_full_response(state)

        assert response["activity"]["id"] == str(activity_id)
        assert response["activity"]["type"] == "test"
        assert response["activity"]["associatedWith"]["agent"] == "alice"
        assert response["activity"]["associatedWith"]["role"] == "oe:admin"

        assert response["used"] == [
            {"entity": "oe:aanvraag/x@y", "type": "oe:aanvraag"},
        ]
        assert response["generated"][0]["type"] == "oe:beslissing"
        assert response["relations"] == [
            {"entity": "oe:thing/q@r", "type": "oe:references"},
        ]
        assert response["dossier"]["id"] == str(D1)
        assert response["dossier"]["status"] == "goedgekeurd"
        assert response["dossier"]["allowedActivities"] == [
            {"type": "foo", "label": "Foo"},
        ]

    async def test_auto_resolved_used_ref_flagged(self, repo):
        """`used_refs` entries with `auto_resolved: True` carry
        an `autoResolved: true` field in the response. This is
        how clients see that the engine filled in a used ref
        they didn't supply explicitly."""
        await repo.create_dossier(D1, "toelatingen")
        state = _state(
            repo,
            activity_def={"name": "test"},
            now=datetime.now(timezone.utc),
            used_refs=[UsedRef(
                entity="oe:aanvraag/x@y",
                version_id=uuid4(),
                type="oe:aanvraag",
                auto_resolved=True,
            )],
            generated_response=[],
            validated_relations=[],
            current_status="nieuw",
            allowed_activities=[],
        )

        response = build_full_response(state)

        assert response["used"][0].get("autoResolved") is True

    async def test_workflow_fallback_to_state_when_dossier_missing(self, repo):
        """If `state.dossier` is None (the activity is creating
        a new dossier), the response's workflow field falls back
        to `state.workflow_name`."""
        await repo.create_dossier(D1, "toelatingen")
        state = _state(
            repo,
            activity_def={"name": "dienAanvraagIn"},
            now=datetime.now(timezone.utc),
            dossier=None,
            workflow_name="toelatingen",
            used_refs=[],
            generated_response=[],
            validated_relations=[],
            current_status="nieuw",
            allowed_activities=[],
        )

        response = build_full_response(state)

        assert response["dossier"]["workflow"] == "toelatingen"
