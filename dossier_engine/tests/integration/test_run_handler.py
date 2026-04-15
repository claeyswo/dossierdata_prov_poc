"""
Integration tests for the handler dispatch pipeline:

* `run_handler` — invokes the plugin handler, stashes HandlerResult
* `_append_handler_generated` — normalizes handler-returned entities
* `resolve_handler_generated_identity` — the identity triple resolver

The three form a chain: `run_handler` calls the plugin handler,
`_append_handler_generated` walks each returned entity, and
`resolve_handler_generated_identity` decides how to fill in
missing `type` / `entity_id` / `derived_from` fields. Branches:

**`run_handler`:**
* no handler declared on the activity → no-op
* handler name declared but not registered in plugin → no-op
* handler returns None → handler_result None, no state mutation
* handler returns plain content (legacy shape) → handler_result
  stored, no generated mutation
* handler returns HandlerResult with `generated` items and the
  client didn't send any → items appended to state.generated
* handler returns HandlerResult with generated items BUT the
  client already sent some → handler items ignored (mutually
  exclusive contract)
* handler receives client_content from state.generated[0] when
  present

**`_append_handler_generated`:**
* external type with URI → routed to state.generated_externals
* identity-less regular entity → identity resolved and appended
* `resolve_handler_generated_identity` returns None → item
  silently dropped

**`resolve_handler_generated_identity`:**
* no type + no allowed_types → None
* no type + allowed_types[0] → defaulted to allowed_types[0]
* no content → None
* explicit entity_id (+optional derived_from) → pass-through
* singleton with existing row → revise: reuse entity_id, link
  derived_from to latest version
* singleton with no existing row → fresh entity_id, no derivation
* multi-cardinality → always fresh entity_id, no derivation

That's 17 branches across three functions. I'll write one
test per branch using stubs for the plugin and real DB fixture
for the singleton/multi paths that need entity lookups.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.context import HandlerResult
from dossier_engine.engine.pipeline._identity import (
    resolve_handler_generated_identity, ResolvedIdentity,
)
from dossier_engine.engine.pipeline.handlers import run_handler
from dossier_engine.engine.state import ActivityState, Caller


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _user() -> User:
    return User(id="u1", type="systeem", name="Test", roles=[], properties={})


async def _bootstrap(repo: Repository) -> UUID:
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


class _HandlerPlugin:
    """Stub plugin carrying a `handlers` dict + singleton
    registration. `run_handler` reads plugin.handlers; the
    identity resolver reads plugin.is_singleton."""
    def __init__(self, handlers: dict, singletons: set[str] | None = None):
        self.handlers = handlers
        self._singletons = singletons or set()
        self.entity_models = {}

    def is_singleton(self, entity_type: str) -> bool:
        return entity_type in self._singletons


def _state(
    repo: Repository,
    *,
    plugin,
    activity_def: dict | None = None,
    generated: list | None = None,
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
        relation_items=[],
        caller=Caller.CLIENT,
    )
    if generated is not None:
        s.generated = generated
    return s


# --------------------------------------------------------------------
# run_handler branches
# --------------------------------------------------------------------


class TestRunHandler:

    async def test_no_handler_declared_noop(self, repo):
        plugin = _HandlerPlugin({})
        state = _state(repo, plugin=plugin, activity_def={"name": "test"})
        await run_handler(state)
        assert state.handler_result is None

    async def test_handler_name_not_registered_noop(self, repo):
        """Activity declares `handler: foo` but the plugin has no
        `foo` in its handlers map. Silent no-op — matches the
        same leniency pattern as `run_custom_validators`."""
        plugin = _HandlerPlugin({})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "handler": "foo",
        })
        await run_handler(state)
        assert state.handler_result is None

    async def test_handler_returning_none_stored(self, repo):
        """Handler returns None → state.handler_result becomes
        None → downstream phases see 'handler didn't contribute'."""
        async def h(ctx, client_content):
            return None

        plugin = _HandlerPlugin({"h": h})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "handler": "h",
        })
        await run_handler(state)
        assert state.handler_result is None

    async def test_handler_returning_plain_dict_stored_but_not_appended(
        self, repo,
    ):
        """Legacy shape: handler returns a raw content dict. It
        becomes `state.handler_result` but state.generated is
        NOT mutated — the raw-dict shape is for single-content
        activities, not for appending entities."""
        async def h(ctx, client_content):
            return {"some": "content"}

        plugin = _HandlerPlugin({"h": h})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "handler": "h",
        })
        await run_handler(state)
        assert state.handler_result == {"some": "content"}
        assert state.generated == []

    async def test_handler_result_generated_appended_when_client_empty(
        self, repo,
    ):
        """Handler returns HandlerResult with one generated entity.
        Client didn't send any. The entity lands in
        state.generated."""
        await _bootstrap(repo)

        async def h(ctx, client_content):
            return HandlerResult(
                generated=[
                    {
                        "type": "oe:aanvraag",
                        "content": {"titel": "from handler"},
                    },
                ],
            )

        plugin = _HandlerPlugin(
            {"h": h},
            singletons={"oe:aanvraag"},
        )
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "handler": "h",
            "generates": ["oe:aanvraag"],
        })

        await run_handler(state)

        assert isinstance(state.handler_result, HandlerResult)
        assert len(state.generated) == 1
        assert state.generated[0]["type"] == "oe:aanvraag"
        assert state.generated[0]["content"] == {"titel": "from handler"}

    async def test_handler_result_generated_ignored_when_client_sent(
        self, repo,
    ):
        """Client already sent `generated` (1 item in state.generated).
        Handler also returns HandlerResult.generated. The handler's
        items are NOT appended — the two are mutually exclusive."""
        client_item = {
            "version_id": uuid4(),
            "entity_id": uuid4(),
            "type": "oe:aanvraag",
            "content": {"titel": "from client"},
            "derived_from": None,
            "ref": "oe:aanvraag/x@y",
        }

        async def h(ctx, client_content):
            return HandlerResult(
                generated=[
                    {"type": "oe:aanvraag", "content": {"titel": "handler"}},
                ],
            )

        plugin = _HandlerPlugin({"h": h}, singletons={"oe:aanvraag"})
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test", "handler": "h"},
            generated=[client_item],
        )

        await run_handler(state)

        # Still just the client's item — handler's generated not appended.
        assert len(state.generated) == 1
        assert state.generated[0]["content"] == {"titel": "from client"}

    async def test_handler_receives_client_content_when_generated_present(
        self, repo,
    ):
        """When state.generated already has items, the handler
        receives state.generated[0]['content'] as its
        `client_content` argument. This is how handlers like
        `set_dossier_access` read what the client sent."""
        received = []
        async def h(ctx, client_content):
            received.append(client_content)
            return None

        plugin = _HandlerPlugin({"h": h})
        state = _state(
            repo, plugin=plugin,
            activity_def={"name": "test", "handler": "h"},
            generated=[{"content": {"key": "value"}}],
        )

        await run_handler(state)

        assert received == [{"key": "value"}]

    async def test_handler_receives_none_client_content_when_empty(self, repo):
        """No client-sent generated → handler receives None for
        client_content. Handlers that don't care about client
        input ignore this argument."""
        received = []
        async def h(ctx, client_content):
            received.append(client_content)
            return None

        plugin = _HandlerPlugin({"h": h})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test", "handler": "h",
        })

        await run_handler(state)

        assert received == [None]


# --------------------------------------------------------------------
# _append_handler_generated external routing + identity-none drops
# (These are verified via run_handler since the helper is private.)
# --------------------------------------------------------------------


class TestAppendHandlerGenerated:

    async def test_external_type_with_uri_routed(self, repo):
        """A handler returns a generated entry with `type: external`
        and a `uri` in content. The entry goes to
        `state.generated_externals`, not `state.generated`."""
        async def h(ctx, client_content):
            return HandlerResult(
                generated=[
                    {
                        "type": "external",
                        "content": {"uri": "https://example.org/foo"},
                    },
                ],
            )

        plugin = _HandlerPlugin({"h": h})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test", "handler": "h",
        })

        await run_handler(state)

        assert state.generated_externals == ["https://example.org/foo"]
        assert state.generated == []

    async def test_unresolvable_entity_silently_dropped(self, repo):
        """A generated entry with no type AND no allowed_types[0]
        fallback → identity resolver returns None → entry is
        silently dropped. This is defensive behavior for handlers
        that return partially-specified items; documented current
        behavior."""
        async def h(ctx, client_content):
            return HandlerResult(
                generated=[
                    # no type, and activity has no `generates`
                    {"content": {"x": 1}},
                ],
            )

        plugin = _HandlerPlugin({"h": h})
        state = _state(repo, plugin=plugin, activity_def={
            "name": "test",
            "handler": "h",
            # no generates → no fallback type
        })

        await run_handler(state)

        assert state.generated == []  # dropped


# --------------------------------------------------------------------
# resolve_handler_generated_identity — direct tests of every branch
# --------------------------------------------------------------------


async def _seed_entity(repo, bootstrap_id, entity_type, content=None):
    """Seed one entity and return the (entity_id, version_id)."""
    eid = uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type=entity_type, generated_by=bootstrap_id,
        content=content or {}, attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid


class TestResolveHandlerGeneratedIdentity:

    async def test_no_type_no_fallback_returns_none(self, repo):
        """Item has no type and allowed_types is empty. The
        identity resolver can't guess → None."""
        plugin = _HandlerPlugin({})
        result = await resolve_handler_generated_identity(
            plugin=plugin, repo=repo, dossier_id=D1,
            gen_item={"content": {"x": 1}},
            allowed_types=[],
        )
        assert result is None

    async def test_no_type_defaults_to_allowed_types_first(self, repo):
        """Item has no type but allowed_types[0] exists → that
        becomes the type. For a multi-cardinality type this also
        produces a fresh entity_id with no derivation."""
        plugin = _HandlerPlugin({})  # no singletons
        result = await resolve_handler_generated_identity(
            plugin=plugin, repo=repo, dossier_id=D1,
            gen_item={"content": {"x": 1}},
            allowed_types=["oe:aanvraag"],
        )
        assert result is not None
        assert result.gen_type == "oe:aanvraag"
        assert result.derived_from_id is None

    async def test_no_content_returns_none(self, repo):
        """Item has a type but no content → None. Content-less
        entities aren't persistable."""
        plugin = _HandlerPlugin({})
        result = await resolve_handler_generated_identity(
            plugin=plugin, repo=repo, dossier_id=D1,
            gen_item={"type": "oe:aanvraag"},  # no content
            allowed_types=["oe:aanvraag"],
        )
        assert result is None

    async def test_explicit_entity_id_passes_through(self, repo):
        """Handler supplies `entity_id` explicitly. The resolver
        uses it as-is, doesn't look up singletons."""
        plugin = _HandlerPlugin({}, singletons={"oe:aanvraag"})
        explicit_eid = uuid4()
        explicit_parent = uuid4()
        result = await resolve_handler_generated_identity(
            plugin=plugin, repo=repo, dossier_id=D1,
            gen_item={
                "type": "oe:aanvraag",
                "content": {"x": 1},
                "entity_id": str(explicit_eid),
                "derived_from": str(explicit_parent),
            },
            allowed_types=["oe:aanvraag"],
        )
        assert result.entity_id == explicit_eid
        assert result.derived_from_id == explicit_parent

    async def test_explicit_entity_id_without_derived_from(self, repo):
        """Explicit entity_id, no derived_from → fresh-entity
        semantics with handler-chosen id."""
        plugin = _HandlerPlugin({})
        explicit_eid = uuid4()
        result = await resolve_handler_generated_identity(
            plugin=plugin, repo=repo, dossier_id=D1,
            gen_item={
                "type": "oe:aanvraag",
                "content": {"x": 1},
                "entity_id": str(explicit_eid),
            },
            allowed_types=[],
        )
        assert result.entity_id == explicit_eid
        assert result.derived_from_id is None

    async def test_singleton_existing_reuses_entity_id_and_links_derivation(
        self, repo,
    ):
        """Type is a singleton, an instance already exists.
        The new entity reuses the existing entity_id and points
        derived_from at the latest version — this is what turns
        'handler returns new content' into 'revise the existing
        instance'."""
        boot = await _bootstrap(repo)
        existing_eid, existing_vid = await _seed_entity(
            repo, boot, "oe:dossier_access", {"v": 1},
        )
        plugin = _HandlerPlugin({}, singletons={"oe:dossier_access"})

        result = await resolve_handler_generated_identity(
            plugin=plugin, repo=repo, dossier_id=D1,
            gen_item={
                "type": "oe:dossier_access",
                "content": {"v": 2},
            },
            allowed_types=["oe:dossier_access"],
        )

        assert result.entity_id == existing_eid
        assert result.derived_from_id == existing_vid

    async def test_singleton_no_existing_mints_fresh(self, repo):
        """Singleton type with no existing instance → fresh
        entity_id, no derivation. This is the first-revision
        case for a singleton that didn't exist yet."""
        await _bootstrap(repo)
        plugin = _HandlerPlugin({}, singletons={"oe:dossier_access"})

        result = await resolve_handler_generated_identity(
            plugin=plugin, repo=repo, dossier_id=D1,
            gen_item={
                "type": "oe:dossier_access",
                "content": {"v": 1},
            },
            allowed_types=["oe:dossier_access"],
        )

        assert result.derived_from_id is None
        # entity_id is a valid UUID, freshly minted
        assert isinstance(result.entity_id, UUID)

    async def test_multi_cardinality_always_fresh(self, repo):
        """Type is not a singleton → every handler call creates
        a new logical entity. Even if other instances exist, no
        derivation links — they're independent."""
        boot = await _bootstrap(repo)
        # Seed one pre-existing; the resolver should ignore it.
        await _seed_entity(repo, boot, "oe:bijlage", {"name": "a"})
        plugin = _HandlerPlugin({})  # no singletons — bijlage is multi

        result = await resolve_handler_generated_identity(
            plugin=plugin, repo=repo, dossier_id=D1,
            gen_item={
                "type": "oe:bijlage",
                "content": {"name": "b"},
            },
            allowed_types=["oe:bijlage"],
        )

        assert result.derived_from_id is None
        assert isinstance(result.entity_id, UUID)
