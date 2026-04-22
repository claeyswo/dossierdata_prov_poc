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
from dossier_engine.engine.state import (
    ActivityState, Caller, DomainRelationEntry,
)


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
        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:x", "kind": "process_control"},
            ],
        )
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
            workflow_relations=[
                {"type": "oe:permitted", "kind": "process_control"},
            ],
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
            workflow_relations=[
                {"type": "oe:references", "kind": "process_control"},
            ],
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
            workflow_relations=[
                {"type": "oe:references", "kind": "process_control"},
            ],
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
            workflow_relations=[
                {"type": "oe:references", "kind": "process_control"},
            ],
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
        `oe:references` with an activity-level `validator:` string
        (Style 2). A validator is registered by name in the plugin's
        `relation_validators` dict. The phase should call the
        validator with the entries list.

        Bug 78 (Round 26): the dict key is the validator's NAME
        (``validate_references``), not the relation type name —
        Style-3 type-name lookups were removed. The load-time
        validator enforces this at plugin load (the validator key
        can't collide with any declared relation type name)."""
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")

        called = []
        async def validator(**kwargs):
            called.append(kwargs)

        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:references", "kind": "process_control"},
            ],
            relation_validators={"validate_references": validator},
        )
        ref = f"oe:aanvraag/{eid}@{vid}"
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "relations": [
                    {"type": "oe:references",
                     "validator": "validate_references"},
                ],
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
        opt-in contract. Post-Bug-78, the validator is registered
        by name (Style 2); there's no Style-3 fallback that could
        accidentally fire it on a non-opt-in activity."""
        boot = await _bootstrap_dossier(repo)
        eid, vid = await _seed_entity(repo, boot, "oe:aanvraag")

        called = []
        async def validator(**kwargs):
            called.append(kwargs)

        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:references", "kind": "process_control"},
            ],
            relation_validators={"validate_references": validator},
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
        truth). Not deciding here — just locking in today's answer.

        Bug 78 (Round 26) note: this test bypasses load-time
        validation (tests construct `_RelationPlugin` directly,
        not via `create_app`), so the "activity opts into a type
        not declared at workflow level" shape still works at
        runtime. At plugin load the new validator WOULD reject
        this — `validate_relation_declarations` requires the
        activity's relation type to resolve to a workflow-level
        declaration. So this test pins runtime behaviour that
        would be prevented from existing in the first place, which
        is a slightly weaker pin than before. Kept as-is to
        preserve the behaviour documentation; a future
        refactor that fixes the union-vs-intersection question
        should also revisit this."""
        await _bootstrap_dossier(repo)

        called = []
        async def validator(**kwargs):
            called.append(kwargs)

        plugin = _RelationPlugin(
            workflow_relations=[],  # workflow permits nothing
            relation_validators={"validate_references": validator},
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "relations": [
                    {"type": "oe:references",
                     "validator": "validate_references"},
                ],
            },
            relation_items=[],  # no entries sent
        )

        # No exception. The validator fires with an empty entries
        # list (dispatch happens even without any items sent —
        # the validator decides whether absence-of-entries is OK).
        await process_relations(state)
        assert len(called) == 1
        assert called[0]["entries"] == []


# --------------------------------------------------------------------
# process_relations — remove operations (Bug 1/2 regression coverage)
# --------------------------------------------------------------------
#
# Before the fix at relations.py:442-446, the dispatch loop used
# ``r["relation_type"]`` to filter remove-entries by rel_type. But
# ``validated_remove_relations`` holds ``DomainRelationEntry`` frozen
# dataclasses, not dicts — so the subscript raised ``TypeError:
# 'DomainRelationEntry' object is not subscriptable`` the moment any
# activity submitted a non-empty ``remove_relations`` block with an
# activity-level ``relations:`` opt-in declared.
#
# The feature (remove-operations, as used by ``bewerkRelaties``) was
# dead on arrival: no test exercised this code path, the persistence
# reader at persistence.py:208-213 already used attribute access
# (so only the validator dispatcher was broken), and the guidebook
# didn't document per-operation validators. These tests pin the fix
# down and document the shape the validator receives.


class TestBug78KindDispatch:
    """Bug 78 (Round 26): runtime dispatch in ``_parse_relations`` is
    driven by the workflow-level ``kind:`` declaration, not by the
    request item's shape. Shape-vs-kind mismatch produces a 422 with
    an informative error naming the declared kind and the received
    shape.

    Before the fix, dispatch guessed kind from the request shape
    (``has entity:`` → process-control; ``has from+to:`` → domain).
    That made the ``kind:`` YAML field decorative — plugin authors
    could declare ``kind: domain`` and a client could silently get
    process-control dispatch by sending an ``entity:`` field.

    Paranoia-check these: revert the kind-dispatch + shape-check in
    ``_parse_relations`` to the old shape-guessing behaviour, rerun,
    both shape-mismatch tests should go red (the old code would
    happily dispatch by shape). The 422 messages should identify the
    declared kind + request shape specifically so operators can fix
    the misaligned request."""

    async def test_domain_kind_rejects_entity_shape(self, repo):
        """Workflow declares ``oe:betreft`` as domain. Client sends
        ``{entity: ...}`` (process-control shape). Must 422 with a
        message naming the declared kind and pointing to the right
        fields (``from:`` + ``to:``)."""
        await _bootstrap_dossier(repo)
        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:betreft", "kind": "domain"},
            ],
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},
            relation_items=[{
                "type": "oe:betreft",
                "entity": f"oe:aanvraag/{uuid4()}@{uuid4()}",
            }],
        )
        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        msg = str(exc.value)
        # Message identifies the declared kind and the right shape.
        assert "kind: domain" in msg
        assert "from:" in msg and "to:" in msg
        assert "oe:betreft" in msg

    async def test_process_control_kind_rejects_domain_shape(self, repo):
        """Workflow declares ``oe:neemtAkteVan`` as process_control.
        Client sends ``{from:, to:}`` (domain shape). Must 422."""
        await _bootstrap_dossier(repo)
        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:neemtAkteVan", "kind": "process_control"},
            ],
        )
        from_uri = "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1"
        to_uri = "https://id.erfgoed.net/erfgoedobjecten/60001"
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},
            relation_items=[{
                "type": "oe:neemtAkteVan",
                "from": from_uri, "to": to_uri,
            }],
        )
        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        msg = str(exc.value)
        assert "kind: process_control" in msg
        assert "entity:" in msg
        assert "oe:neemtAkteVan" in msg

    async def test_remove_rejected_on_process_control(self, repo):
        """Defense-in-depth check in ``_parse_remove_relations``:
        remove operations are domain-only. The load-time validator
        forbids ``operations: [remove]`` on process_control activity
        declarations, but this runtime guard catches the case where
        a caller bypasses load-time validation (test fixtures, or a
        future regression that loosens the load-time rule)."""
        await _bootstrap_dossier(repo)
        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:foo", "kind": "process_control"},
            ],
        )
        from_uri = "https://id.erfgoed.net/dossiers/d1/entities/oe:x/a/b"
        to_uri = "https://id.erfgoed.net/anything"

        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "test",
                "relations": [
                    # Bypass load-time — declare remove on a
                    # process_control type at activity level.
                    {"type": "oe:foo", "operations": ["remove"]},
                ],
            },
            relation_items=[],
        )
        state.remove_relation_items = [{
            "type": "oe:foo", "from": from_uri, "to": to_uri,
        }]

        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        msg = str(exc.value)
        assert "process_control" in msg
        assert "remove" in msg

    async def test_domain_kind_happy_path_with_from_to(self, repo):
        """Positive control: a domain-declared type with the right
        shape dispatches correctly. Without this, the negative tests
        above could pass because nothing routes correctly. Sanity
        check that Bug 78's rewire didn't break the happy path."""
        await _bootstrap_dossier(repo)
        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:betreft", "kind": "domain"},
            ],
        )
        from_uri = "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1"
        to_uri = "https://id.erfgoed.net/erfgoedobjecten/12345"
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test"},
            relation_items=[{
                "type": "oe:betreft",
                "from": from_uri, "to": to_uri,
            }],
        )
        await process_relations(state)
        assert len(state.validated_domain_relations) == 1
        assert state.validated_domain_relations[0].relation_type == "oe:betreft"


class TestProcessRemoveRelations:

    async def test_remove_relation_populates_state(self, repo):
        """Happy path: activity opts into ``[add, remove]``, client
        sends a ``remove_relations`` item with valid from/to refs.
        The phase should stage a ``DomainRelationEntry`` under
        ``state.validated_remove_relations`` without raising."""
        await _bootstrap_dossier(repo)

        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:betreft", "kind": "domain"},
            ],
        )
        # Use full URIs as from/to so expand_ref passes them through
        # unchanged — this keeps the assertion independent of the
        # base-URI configuration.
        from_uri = "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1"
        to_uri = "https://id.erfgoed.net/erfgoedobjecten/60001"

        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "bewerkRelaties",
                "relations": [
                    {
                        "type": "oe:betreft",
                        "operations": ["add", "remove"],
                    },
                ],
            },
            relation_items=[],
        )
        state.remove_relation_items = [{
            "type": "oe:betreft",
            "from": from_uri,
            "to": to_uri,
        }]

        await process_relations(state)

        assert len(state.validated_remove_relations) == 1
        entry = state.validated_remove_relations[0]
        assert isinstance(entry, DomainRelationEntry)
        assert entry.relation_type == "oe:betreft"
        assert entry.from_ref == from_uri
        assert entry.to_ref == to_uri

    async def test_remove_validator_dispatched_with_remove_entries(
        self, repo,
    ):
        """The Bug 1/2 regression test.

        Activity opts into ``[add, remove]`` for ``oe:betreft`` and
        declares a per-operation ``remove`` validator. The client
        sends a ``remove_relations`` item. The phase must:

        * Filter ``validated_remove_relations`` by rel_type (this is
          the line that used to raise ``TypeError`` on dict subscript
          of a frozen dataclass).
        * Resolve the ``remove`` validator via Style 1 of
          ``_resolve_validator`` (per-operation dict).
        * Call the validator with ``entries=[<DomainRelationEntry>]``.

        If this test ever raises ``TypeError``, the Bug 1/2 fix has
        regressed and every ``bewerkRelaties`` call with a non-empty
        ``remove_relations`` block in production will 500."""
        await _bootstrap_dossier(repo)

        called = []
        async def remove_validator(**kwargs):
            called.append(kwargs)

        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:betreft", "kind": "domain"},
            ],
            relation_validators={
                "validate_betreft_removable": remove_validator,
            },
        )
        from_uri = "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1"
        to_uri = "https://id.erfgoed.net/erfgoedobjecten/60001"

        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "bewerkRelaties",
                "relations": [
                    {
                        "type": "oe:betreft",
                        "operations": ["add", "remove"],
                        "validators": {
                            "remove": "validate_betreft_removable",
                        },
                    },
                ],
            },
            relation_items=[],
        )
        state.remove_relation_items = [{
            "type": "oe:betreft",
            "from": from_uri,
            "to": to_uri,
        }]

        # Must not raise TypeError — this is the Bug 1/2 regression.
        await process_relations(state)

        assert len(called) == 1
        entries = called[0]["entries"]
        assert len(entries) == 1
        # Validator receives the frozen dataclass directly, same shape
        # as `validated_remove_relations`. This is the shape plugin
        # authors will code their `remove` validators against.
        assert isinstance(entries[0], DomainRelationEntry)
        assert entries[0].relation_type == "oe:betreft"
        assert entries[0].from_ref == from_uri
        assert entries[0].to_ref == to_uri

    async def test_remove_entries_filtered_by_relation_type(self, repo):
        """Two remove-entries with different relation types. The
        dispatch filters ``validated_remove_relations`` per rel_type
        so each type's remove validator receives only its own
        entries.

        This is the list comprehension at relations.py:443-446 —
        the exact line that used to crash. Filtering two types
        simultaneously exercises both iterations of the dispatch
        loop and confirms the filter's equality check works on the
        dataclass's ``relation_type`` attribute.

        Uses per-operation validators (Style 1 of ``_resolve_validator``)
        so each remove validator is exclusive to its rel_type — this
        is the declaration style the per-operation feature was
        designed for."""
        await _bootstrap_dossier(repo)

        betreft_remove_calls = []
        valt_onder_remove_calls = []
        async def betreft_remove_validator(**kwargs):
            betreft_remove_calls.append(kwargs)
        async def valt_onder_remove_validator(**kwargs):
            valt_onder_remove_calls.append(kwargs)

        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:betreft", "kind": "domain"},
                {"type": "oe:valtOnder", "kind": "domain"},
            ],
            relation_validators={
                "validate_betreft_remove": betreft_remove_validator,
                "validate_valt_onder_remove": valt_onder_remove_validator,
            },
        )
        from_uri = "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1"

        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "bewerkRelaties",
                "relations": [
                    {
                        "type": "oe:betreft",
                        "operations": ["add", "remove"],
                        "validators": {
                            "remove": "validate_betreft_remove",
                        },
                    },
                    {
                        "type": "oe:valtOnder",
                        "operations": ["add", "remove"],
                        "validators": {
                            "remove": "validate_valt_onder_remove",
                        },
                    },
                ],
            },
            relation_items=[],
        )
        state.remove_relation_items = [
            {
                "type": "oe:betreft",
                "from": from_uri,
                "to": "https://id.erfgoed.net/erfgoedobjecten/60001",
            },
            {
                "type": "oe:valtOnder",
                "from": from_uri,
                "to": "https://id.erfgoed.net/dossiers/d2/",
            },
        ]

        await process_relations(state)

        # Each rel_type's remove validator fires exactly once, with
        # exactly its own entry — not the other type's.
        assert len(betreft_remove_calls) == 1
        assert len(valt_onder_remove_calls) == 1
        assert len(betreft_remove_calls[0]["entries"]) == 1
        assert betreft_remove_calls[0]["entries"][0].relation_type == "oe:betreft"
        assert len(valt_onder_remove_calls[0]["entries"]) == 1
        assert valt_onder_remove_calls[0]["entries"][0].relation_type == "oe:valtOnder"

    async def test_remove_without_operation_declared_rejected(self, repo):
        """Activity declares ``oe:betreft`` but with default
        ``operations`` (i.e. ``[add]`` only — see
        ``_allowed_operations`` at relations.py:74-82). A
        ``remove_relations`` item for that type must 422 with a
        message naming the allowed operations.

        Catching this at parse time (``_parse_remove_relations``)
        matters because the dispatch loop only sees the filtered
        types set — a typo in the workflow that omits ``remove``
        from ``operations`` should fail loudly, not silently skip."""
        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:betreft", "kind": "domain"},
            ],
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "bewerkRelaties",
                "relations": [
                    {
                        "type": "oe:betreft",
                        # operations omitted → defaults to {"add"}
                    },
                ],
            },
            relation_items=[],
        )
        state.remove_relation_items = [{
            "type": "oe:betreft",
            "from": "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1",
            "to": "https://id.erfgoed.net/erfgoedobjecten/60001",
        }]

        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        msg = str(exc.value).lower()
        assert "remov" in msg
        assert "oe:betreft" in str(exc.value)

    async def test_remove_missing_from_or_to_rejected(self, repo):
        """Remove items require both ``from`` and ``to`` —
        supersession targets a specific edge, not an endpoint.
        Missing either field is 422."""
        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:betreft", "kind": "domain"},
            ],
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "bewerkRelaties",
                "relations": [
                    {
                        "type": "oe:betreft",
                        "operations": ["add", "remove"],
                    },
                ],
            },
            relation_items=[],
        )
        state.remove_relation_items = [{
            "type": "oe:betreft",
            "from": "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1",
            # `to` omitted
        }]

        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        assert "'from' and 'to'" in str(exc.value)

    async def test_remove_missing_type_rejected(self, repo):
        """Remove items must carry ``type`` so the dispatch can
        resolve operation permissions and the per-operation
        validator. 422 with an explicit message."""
        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:betreft", "kind": "domain"},
            ],
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "bewerkRelaties",
                "relations": [
                    {
                        "type": "oe:betreft",
                        "operations": ["add", "remove"],
                    },
                ],
            },
            relation_items=[],
        )
        state.remove_relation_items = [{
            # `type` omitted
            "from": "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1",
            "to": "https://id.erfgoed.net/erfgoedobjecten/60001",
        }]

        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        assert "missing 'type'" in str(exc.value)

    async def test_remove_disallowed_relation_type_rejected(self, repo):
        """Remove item carries a type that the workflow doesn't
        permit at all (not in ``workflow.relations``, not opted
        into by the activity). 422, same gate as add-relations —
        the permission check is operation-agnostic."""
        plugin = _RelationPlugin(
            workflow_relations=[
                {"type": "oe:betreft", "kind": "domain"},
            ],
        )
        state = _state(
            repo, plugin=plugin,
            activity_def={
                "name": "bewerkRelaties",
                "relations": [
                    {
                        "type": "oe:betreft",
                        "operations": ["add", "remove"],
                    },
                ],
            },
            relation_items=[],
        )
        state.remove_relation_items = [{
            "type": "oe:forbidden",
            "from": "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1",
            "to": "https://id.erfgoed.net/erfgoedobjecten/60001",
        }]

        with pytest.raises(ActivityError) as exc:
            await process_relations(state)
        assert exc.value.status_code == 422
        assert "oe:forbidden" in str(exc.value)
        assert "does not allow" in str(exc.value)
