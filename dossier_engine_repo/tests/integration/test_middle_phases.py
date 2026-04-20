"""
Integration tests for the pipeline middle phases:

* `run_custom_validators` — plugin-declared validator dispatch
* `validate_tombstone` — the tombstone built-in's shape rules
* `process_relations` — relation parsing + validator dispatch

These three phases all run in the middle of the pipeline, after
`process_generated` populates `state.generated` but before the
persistence phase writes anything. They're covered together here
because they share a similar test shape: hand-built state with
a stub plugin, call the phase, assert on state mutations or
exceptions raised.

The tests use `SimpleNamespace` for stub rows and plugins rather
than the real dataclasses — each phase reads only a narrow slice
of its inputs, and stubs let the tests be short and focused.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.pipeline.relations import process_relations
from dossier_engine.engine.pipeline.tombstone import validate_tombstone
from dossier_engine.engine.pipeline.validators import run_custom_validators
from dossier_engine.engine.state import ActivityState, Caller


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _user() -> User:
    return User(id="u1", type="systeem", name="Test", roles=[], properties={})


def _state(
    repo: Repository,
    *,
    activity_def: dict | None = None,
    plugin=None,
    used_rows_by_ref: dict | None = None,
    generated: list | None = None,
    relation_items: list | None = None,
    resolved_entities: dict | None = None,
) -> ActivityState:
    s = ActivityState(
        plugin=plugin,
        activity_def=activity_def or {"name": "testActivity"},
        repo=repo,
        dossier_id=D1,
        activity_id=uuid4(),
        user=_user(),
        role="",
        used_items=[],
        generated_items=[],
        relation_items=relation_items or [],
        caller=Caller.CLIENT,
    )
    if used_rows_by_ref is not None:
        s.used_rows_by_ref = used_rows_by_ref
    if generated is not None:
        s.generated = generated
    if resolved_entities is not None:
        s.resolved_entities = resolved_entities
    return s


# --------------------------------------------------------------------
# run_custom_validators
# --------------------------------------------------------------------


class _ValidatorPlugin:
    """Stub plugin exposing a `validators` dict mapping name →
    callable. `entity_models` is an empty dict because the tests
    don't exercise typed-entity access through the context."""
    def __init__(self, validators: dict):
        self.validators = validators
        self.entity_models = {}


class TestRunCustomValidators:

    async def test_no_validators_declared_noop(self, repo):
        """Activity has no `validators` block. The phase walks an
        empty list and returns."""
        plugin = _ValidatorPlugin({})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
        })
        await run_custom_validators(state)
        # no exception

    async def test_validator_not_in_plugin_silently_skipped(self, repo):
        """Activity declares a validator named `missing_one` but
        the plugin doesn't have it. The phase silently skips —
        this is the current behavior and it's kind of lenient.
        Locking it in so a future refactor that decides to
        strict-fail here has to make the choice consciously."""
        plugin = _ValidatorPlugin({})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "validators": [{"name": "missing_one"}],
        })
        await run_custom_validators(state)  # no exception

    async def test_validator_returning_none_accepted(self, repo):
        """A validator that returns None is treated as "passed".
        Most validators in practice just run checks and return
        nothing."""
        called = []
        async def vfn(ctx):
            called.append(True)
            return None

        plugin = _ValidatorPlugin({"my_check": vfn})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "validators": [{"name": "my_check"}],
        })
        await run_custom_validators(state)
        assert called == [True]

    async def test_validator_returning_truthy_accepted(self, repo):
        """A truthy return is also accepted (the docstring says
        'None or a truthy value → accepted')."""
        async def vfn(ctx):
            return True

        plugin = _ValidatorPlugin({"my_check": vfn})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "validators": [{"name": "my_check"}],
        })
        await run_custom_validators(state)  # no exception

    async def test_validator_returning_falsy_raises_409(self, repo):
        """A falsy-but-not-None return (False, 0, empty string)
        is treated as 'rejected'. The engine wraps the rejection
        in a generic 409 with the validator's name."""
        async def vfn(ctx):
            return False

        plugin = _ValidatorPlugin({"my_check": vfn})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "validators": [{"name": "my_check"}],
        })
        with pytest.raises(ActivityError) as exc:
            await run_custom_validators(state)
        assert exc.value.status_code == 409
        assert "my_check" in str(exc.value)

    async def test_validator_raising_activity_error_propagates(self, repo):
        """A validator that wants custom status + payload raises
        ActivityError directly. The phase lets it propagate with
        the validator's own message."""
        async def vfn(ctx):
            raise ActivityError(422, "custom reason", payload={"k": "v"})

        plugin = _ValidatorPlugin({"my_check": vfn})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "validators": [{"name": "my_check"}],
        })
        with pytest.raises(ActivityError) as exc:
            await run_custom_validators(state)
        assert exc.value.status_code == 422
        assert "custom reason" in str(exc.value)
        assert exc.value.payload == {"k": "v"}


# --------------------------------------------------------------------
# validate_tombstone
# --------------------------------------------------------------------


def _used_row(entity_id: UUID, entity_type: str, version_id: UUID | None = None):
    """Stub used-row with only the attributes validate_tombstone
    reads: entity_id, type, id."""
    return SimpleNamespace(
        entity_id=entity_id,
        type=entity_type,
        id=version_id or uuid4(),
    )


class TestValidateTombstone:

    async def test_non_tombstone_activity_is_noop(self, repo):
        """Any activity whose name isn't 'tombstone' falls through
        immediately — this is the default path for every request."""
        state = _state(repo, activity_def={"name": "dienAanvraagIn"})
        await validate_tombstone(state)
        # tombstone_version_ids not set — phase didn't fire.
        assert state.tombstone_version_ids == []

    async def test_tombstone_empty_used_raises_422(self, repo):
        state = _state(
            repo,
            activity_def={"name": "tombstone"},
            used_rows_by_ref={},
        )
        with pytest.raises(ActivityError) as exc:
            await validate_tombstone(state)
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "tombstone_no_used_entities"

    async def test_tombstone_two_different_entities_raises_422(self, repo):
        """Tombstone may only target ONE logical entity. Two
        different entity_ids in `used` → rejected."""
        state = _state(
            repo,
            activity_def={"name": "tombstone"},
            used_rows_by_ref={
                "ref_a": _used_row(uuid4(), "oe:aanvraag"),
                "ref_b": _used_row(uuid4(), "oe:aanvraag"),
            },
        )
        with pytest.raises(ActivityError) as exc:
            await validate_tombstone(state)
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "tombstone_multi_entity"

    async def test_tombstone_two_different_types_raises_422(self, repo):
        """Same error path as two entity_ids but triggered by
        having two DIFFERENT types in the used set for a single
        entity_id (a shape that shouldn't exist in practice but
        the guard exists for defense in depth)."""
        shared_eid = uuid4()
        state = _state(
            repo,
            activity_def={"name": "tombstone"},
            used_rows_by_ref={
                "ref_a": _used_row(shared_eid, "oe:aanvraag"),
                "ref_b": _used_row(shared_eid, "oe:beslissing"),
            },
        )
        with pytest.raises(ActivityError) as exc:
            await validate_tombstone(state)
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "tombstone_multi_entity"

    async def test_tombstone_missing_replacement_raises_422(self, repo):
        """One entity in `used`, but no matching replacement in
        `generated`. The replacement is the placeholder that
        takes over the lineage — without it the logical entity
        has no latest version."""
        eid = uuid4()
        state = _state(
            repo,
            activity_def={"name": "tombstone"},
            used_rows_by_ref={
                "ref_a": _used_row(eid, "oe:aanvraag"),
            },
            generated=[
                {"type": "system:note", "entity_id": uuid4(), "content": {}},
            ],
        )
        with pytest.raises(ActivityError) as exc:
            await validate_tombstone(state)
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "tombstone_replacement_count"
        assert exc.value.payload["got"] == 0

    async def test_tombstone_missing_reason_note_raises_422(self, repo):
        """Replacement is present but no system:note. Every
        tombstone must carry the redaction reason for auditing."""
        eid = uuid4()
        state = _state(
            repo,
            activity_def={"name": "tombstone"},
            used_rows_by_ref={
                "ref_a": _used_row(eid, "oe:aanvraag"),
            },
            generated=[
                {"type": "oe:aanvraag", "entity_id": eid, "content": {}},
            ],
        )
        with pytest.raises(ActivityError) as exc:
            await validate_tombstone(state)
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "tombstone_missing_reason_note"

    async def test_tombstone_unexpected_generated_entry_raises_422(
        self, repo,
    ):
        """Generated contains the replacement, a note, AND
        something else (a different entity type). 422 with the
        unexpected types listed."""
        eid = uuid4()
        state = _state(
            repo,
            activity_def={"name": "tombstone"},
            used_rows_by_ref={
                "ref_a": _used_row(eid, "oe:aanvraag"),
            },
            generated=[
                {"type": "oe:aanvraag", "entity_id": eid, "content": {}},
                {"type": "system:note", "entity_id": uuid4(), "content": {}},
                {"type": "oe:beslissing", "entity_id": uuid4(), "content": {}},
            ],
        )
        with pytest.raises(ActivityError) as exc:
            await validate_tombstone(state)
        assert exc.value.status_code == 422
        assert exc.value.payload["error"] == "tombstone_unexpected_generated"
        assert "oe:beslissing" in exc.value.payload["unexpected_types"]

    async def test_tombstone_valid_captures_version_ids(self, repo):
        """Happy path: one entity in used, one matching
        replacement, one system:note. Phase populates
        `state.tombstone_version_ids` with the used rows' ids —
        the persistence phase will null out the content of those
        specific versions after the replacement has been written."""
        eid = uuid4()
        vid1 = uuid4()
        vid2 = uuid4()
        state = _state(
            repo,
            activity_def={"name": "tombstone"},
            used_rows_by_ref={
                "ref_a": _used_row(eid, "oe:aanvraag", vid1),
                "ref_b": _used_row(eid, "oe:aanvraag", vid2),
            },
            generated=[
                {"type": "oe:aanvraag", "entity_id": eid, "content": {}},
                {"type": "system:note", "entity_id": uuid4(), "content": {}},
            ],
        )
        await validate_tombstone(state)
        assert set(state.tombstone_version_ids) == {vid1, vid2}


# --------------------------------------------------------------------
# process_relations
# --------------------------------------------------------------------


class _RelationPlugin:
    """Stub plugin with a workflow-level allowed relations set
    and an optional relation_validators map for dispatch tests."""
    def __init__(
        self,
        workflow_relations: list[dict],
        relation_validators: dict | None = None,
    ):
        self.workflow = {"relations": workflow_relations}
        self.relation_validators = relation_validators or {}
        self.entity_models = {}


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


async def _seed_entity(repo, bootstrap, type_):
    eid = uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type=type_, generated_by=bootstrap,
        content={}, attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid


class TestProcessRelations:

    async def test_no_relation_items_noop(self, repo):
        plugin = _RelationPlugin(workflow_relations=[])
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},
            relation_items=[],
        )
        await process_relations(state)
        assert state.validated_relations == []

    async def test_missing_type_field_rejected(self, repo):
        plugin = _RelationPlugin(workflow_relations=[{"type": "oe:x"}])
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},
            relation_items=[{"entity": "oe:aanvraag/abc@def"}],  # no type
        )
        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        assert "missing 'type'" in str(exc.value)

    async def test_disallowed_relation_type_rejected(self, repo):
        """Workflow allows `oe:permitted`, activity tries to use
        `oe:forbidden`. 422 with the allowed set in the message."""
        plugin = _RelationPlugin(
            workflow_relations=[{"type": "oe:permitted"}],
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},
            relation_items=[{
                "type": "oe:forbidden",
                "entity": f"oe:aanvraag/{uuid4()}@{uuid4()}",
            }],
        )
        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        assert "oe:forbidden" in str(exc.value)
        assert "oe:permitted" in str(exc.value)

    async def test_external_uri_in_relation_rejected(self, repo):
        """Relations cannot reference external URIs — the
        semantic they express depends on dossier-internal lineage.
        The permission check fires first (it has to, because
        external strings would otherwise slip through the shape
        check), so we use a workflow that allows the relation
        type."""
        plugin = _RelationPlugin(
            workflow_relations=[{"type": "oe:references"}],
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},
            relation_items=[{
                "type": "oe:references",
                "entity": "https://example.org/foo",
            }],
        )
        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        assert "external" in str(exc.value).lower()

    async def test_unknown_entity_ref_rejected(self, repo):
        """Relation points at a version UUID that doesn't exist
        in the dossier. 422 — we can't create an edge to nothing."""
        await _bootstrap_dossier(repo)
        plugin = _RelationPlugin(
            workflow_relations=[{"type": "oe:references"}],
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},
            relation_items=[{
                "type": "oe:references",
                "entity": f"oe:aanvraag/{uuid4()}@{uuid4()}",
            }],
        )
        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        assert "not found" in str(exc.value).lower()

    async def test_valid_relation_populates_state(self, repo):
        """Happy path: relation type is allowed, entity exists in
        the same dossier. Phase populates both
        `state.validated_relations` and `state.relations_by_type`."""
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")
        plugin = _RelationPlugin(
            workflow_relations=[{"type": "oe:references"}],
        )
        ref = f"oe:aanvraag/{eid}@{vid}"
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},
            relation_items=[{"type": "oe:references", "entity": ref}],
        )

        await process_relations(state)

        assert len(state.validated_relations) == 1
        assert state.validated_relations[0].version_id == vid
        assert state.validated_relations[0].relation_type == "oe:references"
        assert "oe:references" in state.relations_by_type

    async def test_activity_opt_in_triggers_validator(self, repo):
        """Activity's own `relations:` block opts into
        `oe:references`. A validator is registered. The phase
        should call the validator with the entries list."""
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")

        called = []
        async def validator(**kwargs):
            called.append(kwargs)

        plugin = _RelationPlugin(
            workflow_relations=[{"type": "oe:references"}],
            relation_validators={"oe:references": validator},
        )
        ref = f"oe:aanvraag/{eid}@{vid}"
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "relations": [{"type": "oe:references"}],  # opt-in
            },
            relation_items=[{"type": "oe:references", "entity": ref}],
        )

        await process_relations(state)

        assert len(called) == 1
        assert len(called[0]["entries"]) == 1

    async def test_workflow_allows_but_activity_does_not_opt_in_no_dispatch(
        self, repo,
    ):
        """Workflow-level allows `oe:references` so the client
        may send it. The activity does NOT opt into it (no
        `relations:` block on the activity itself). The validator
        is registered but must NOT be called — this is the
        opt-in contract."""
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")

        called = []
        async def validator(**kwargs):
            called.append(kwargs)

        plugin = _RelationPlugin(
            workflow_relations=[{"type": "oe:references"}],
            relation_validators={"oe:references": validator},
        )
        ref = f"oe:aanvraag/{eid}@{vid}"
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},  # no relations block
            relation_items=[{"type": "oe:references", "entity": ref}],
        )

        await process_relations(state)

        # Validated and stored, but validator NOT fired.
        assert len(state.validated_relations) == 1
        assert called == []

    async def test_activity_opt_in_always_flows_into_allowed_set(self, repo):
        """DOCUMENTED CURRENT BEHAVIOR: the 500 'not in workflow'
        branch in `_dispatch_validators` is unreachable in the
        current code. Here's why:

        `allowed_relation_types_for_activity` computes the
        permission set as `workflow_types | activity_types` (a
        UNION). So any type the activity opts into is
        automatically added to `allowed`. Then in
        `_dispatch_validators`, the check `if rel_type not in
        allowed` can never be true for activity-opted-in types,
        because we just put them there.

        The 500 branch is belt-and-braces — it would fire if
        someone later refactored the union to be an intersection
        or dropped the `| activity` term. This test pins down the
        current behavior so that refactor becomes visible: an
        activity can opt into any type the workflow doesn't list,
        and the phase silently accepts the opt-in.

        This is either a feature (activities can add their own
        validators without workflow coordination) or a bug
        (workflow-wide permission is supposed to be the source of
        truth). Not deciding here — just locking in today's
        answer."""
        await _bootstrap_dossier(repo)

        called = []
        async def validator(**kwargs):
            called.append(kwargs)

        plugin = _RelationPlugin(
            workflow_relations=[],  # workflow permits nothing
            relation_validators={"oe:references": validator},
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "relations": [{"type": "oe:references"}],
            },
            relation_items=[],  # no entries sent
        )

        # No exception. The validator fires with an empty entries
        # list (dispatch happens even without any items sent —
        # the validator decides whether absence-of-entries is OK).
        await process_relations(state)
        assert len(called) == 1
        assert called[0]["entries"] == []
