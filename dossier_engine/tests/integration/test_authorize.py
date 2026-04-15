"""
Integration tests for `authorize_activity` in
`engine.pipeline.authorization`.

The engine's access model has three `access` tiers:

* `everyone` — no auth needed, always passes.
* `authenticated` — any real user passes, anonymous fails.
* `roles` — the user must satisfy at least one entry in the
  activity's `roles` list.

Under `access: roles`, each list entry is one of three shapes:

1. **Direct**: `{role: "behandelaar"}` — user must have that exact
   string in their `roles` list. Plain equality check.

2. **Scoped**: `{role: "gemeente-toevoeger", scope: {from_entity:
   "oe:aanvraag", field: "content.gemeente"}}` — resolves to
   `f"{base_role}:{field_value}"` at runtime, then checks
   membership. Used for "per-municipality editor" style roles
   where the set of valid role strings depends on dossier data.

3. **Entity-derived**: `{from_entity: "oe:aanvraag", field:
   "content.aanvrager.rrn"}` — the field VALUE is the role string.
   Used for dossier ownership: the entity's owner identifier
   itself is the role, so every natuurlijk_persoon user
   automatically has exactly the set of "own-dossier" roles they
   should.

The function returns `(True, None)` on success and `(False, msg)`
on failure. We test both branches of each shape.

Why integration (DB-backed) not unit: the scoped and
entity-derived shapes call `lookup_singleton(plugin, repo,
dossier_id, entity_type)`, which runs a real query against the
entities table. We could stub the repo with a SimpleNamespace but
stubbing `get_entities_by_type_latest` + the Plugin's
`is_singleton` accurately is at least as much setup as seeding a
real entity. So we use the fixture for everything and keep the
tests uniform.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.engine.pipeline.authorization import authorize_activity


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _user(*roles: str) -> User:
    """Build a User with the given roles list. Other fields are
    filled with placeholder values the authorize path never reads."""
    return User(
        id="u1",
        type="natuurlijk_persoon",
        name="Test User",
        roles=list(roles),
        properties={},
    )


class _StubPlugin:
    """Minimal Plugin stub. `authorize_activity` never touches the
    plugin for direct-role checks. For scoped and entity-derived
    checks, it calls `lookup_singleton(plugin, repo, dossier, type)`,
    which in turn calls `plugin.is_singleton(type)` — so we only
    need to expose that one method."""
    def __init__(self, singletons: set[str]):
        self._singletons = singletons

    def is_singleton(self, entity_type: str) -> bool:
        return entity_type in self._singletons


async def _bootstrap_dossier(repo: Repository) -> UUID:
    """Create D1 and one bootstrap systemAction so subsequent
    entity seeding has something to point at as generated_by."""
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


async def _seed_aanvraag(
    repo: Repository,
    bootstrap_activity_id: UUID,
    content: dict,
) -> UUID:
    """Seed one oe:aanvraag singleton entity with the given content.
    Returns the version_id."""
    eid = uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type="oe:aanvraag", generated_by=bootstrap_activity_id,
        content=content, attributed_to="system",
    )
    await repo.session.flush()
    return vid


# --------------------------------------------------------------------
# access type branches (no DB needed for most, but we already have
# the fixture so uniform setup)
# --------------------------------------------------------------------


class TestAccessTypes:

    async def test_everyone_allows_no_user(self, repo):
        """access: everyone allows any caller, including a None
        user (unauthenticated)."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={"authorization": {"access": "everyone"}},
            user=None,
            repo=repo,
            dossier_id=None,
        )
        assert ok is True
        assert err is None

    async def test_authenticated_allows_real_user(self, repo):
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={"authorization": {"access": "authenticated"}},
            user=_user(),
            repo=repo,
            dossier_id=None,
        )
        assert ok is True
        assert err is None

    async def test_authenticated_rejects_no_user(self, repo):
        """No user, `access: authenticated` → 401-style failure."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={"authorization": {"access": "authenticated"}},
            user=None,
            repo=repo,
            dossier_id=None,
        )
        assert ok is False
        assert "Authentication required" in err

    async def test_no_authorization_block_defaults_to_authenticated(
        self, repo,
    ):
        """Activity has no `authorization` field at all → default
        is `authenticated`. Any logged-in user passes."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={},
            user=_user(),
            repo=repo,
            dossier_id=None,
        )
        assert ok is True

    async def test_roles_empty_list_allows(self, repo):
        """`access: roles` with an empty `roles` list is a
        degenerate case — the function treats it as "no role
        constraints" and passes. Debatable semantics, but locking
        in the current behavior so a future refactor that tightens
        this has to do so consciously."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={
                "authorization": {"access": "roles", "roles": []},
            },
            user=_user(),
            repo=repo,
            dossier_id=None,
        )
        assert ok is True

    async def test_unknown_access_type_rejected(self, repo):
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={
                "authorization": {"access": "bogus"},
            },
            user=_user(),
            repo=repo,
            dossier_id=None,
        )
        assert ok is False
        assert "Unknown access type" in err


# --------------------------------------------------------------------
# Direct role branches
# --------------------------------------------------------------------


class TestDirectRoles:

    async def test_user_has_required_role_allowed(self, repo):
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{"role": "behandelaar"}],
                },
            },
            user=_user("behandelaar"),
            repo=repo,
            dossier_id=None,
        )
        assert ok is True

    async def test_user_missing_required_role_denied(self, repo):
        """User doesn't have the required role. Error message
        includes the role name so clients can show 'you need
        behandelaar to do this'."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{"role": "behandelaar"}],
                },
            },
            user=_user("aanvrager"),
            repo=repo,
            dossier_id=None,
        )
        assert ok is False
        assert "behandelaar" in err

    async def test_multiple_entries_first_match_wins(self, repo):
        """Three role entries. User has the second one. The
        function tries each in turn and returns on the first
        match — no need to check the third."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [
                        {"role": "admin"},
                        {"role": "behandelaar"},
                        {"role": "aanvrager"},
                    ],
                },
            },
            user=_user("behandelaar"),
            repo=repo,
            dossier_id=None,
        )
        assert ok is True

    async def test_all_entries_fail_aggregates_errors(self, repo):
        """User has none of the required roles. Error message
        includes every rejected check, not just the last one —
        helps diagnosing complex policies."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [
                        {"role": "admin"},
                        {"role": "behandelaar"},
                    ],
                },
            },
            user=_user("nobody"),
            repo=repo,
            dossier_id=None,
        )
        assert ok is False
        assert "admin" in err
        assert "behandelaar" in err

    async def test_malformed_role_entry_contributes_error(self, repo):
        """A role entry that's not a dict with 'role' or
        'from_entity' is collected as an error. The function
        continues past it to try other entries — this lets a
        plugin ship with mixed valid/invalid entries and still
        work for the valid cases."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [
                        "a bare string, not a dict",
                        {"role": "behandelaar"},
                    ],
                },
            },
            user=_user("behandelaar"),
            repo=repo,
            dossier_id=None,
        )
        # Second entry is valid and user has the role → allowed.
        assert ok is True


# --------------------------------------------------------------------
# Scoped role branches (DB-backed)
# --------------------------------------------------------------------


class TestScopedRoles:

    async def test_scoped_role_resolved_and_matched(self, repo):
        """Activity requires `gemeente-toevoeger:<gemeente>`, where
        `gemeente` is resolved from the dossier's aanvraag entity.
        User has `gemeente-toevoeger:brugge`, aanvraag.content.gemeente
        is "brugge" → composed role matches, allowed."""
        boot = await _bootstrap_dossier(repo)
        await _seed_aanvraag(repo, boot, {"gemeente": "brugge"})
        plugin = _StubPlugin({"oe:aanvraag"})

        ok, err = await authorize_activity(
            plugin=plugin,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{
                        "role": "gemeente-toevoeger",
                        "scope": {
                            "from_entity": "oe:aanvraag",
                            "field": "content.gemeente",
                        },
                    }],
                },
            },
            user=_user("gemeente-toevoeger:brugge"),
            repo=repo,
            dossier_id=D1,
        )
        assert ok is True

    async def test_scoped_role_resolved_but_user_does_not_have_it(
        self, repo,
    ):
        """The composed role resolves to `gemeente-toevoeger:brugge`
        but the user only has `gemeente-toevoeger:gent`. Denied —
        wrong scope even though the base role is right."""
        boot = await _bootstrap_dossier(repo)
        await _seed_aanvraag(repo, boot, {"gemeente": "brugge"})
        plugin = _StubPlugin({"oe:aanvraag"})

        ok, err = await authorize_activity(
            plugin=plugin,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{
                        "role": "gemeente-toevoeger",
                        "scope": {
                            "from_entity": "oe:aanvraag",
                            "field": "content.gemeente",
                        },
                    }],
                },
            },
            user=_user("gemeente-toevoeger:gent"),
            repo=repo,
            dossier_id=D1,
        )
        assert ok is False
        assert "gemeente-toevoeger:brugge" in err

    async def test_scoped_role_entity_missing_rejected(self, repo):
        """Scope references an entity type that doesn't exist in
        the dossier yet. The resolution fails, the error is
        collected, and since it's the only role entry, the
        overall result is deny."""
        await _bootstrap_dossier(repo)
        plugin = _StubPlugin({"oe:aanvraag"})

        ok, err = await authorize_activity(
            plugin=plugin,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{
                        "role": "gemeente-toevoeger",
                        "scope": {
                            "from_entity": "oe:aanvraag",
                            "field": "content.gemeente",
                        },
                    }],
                },
            },
            user=_user("gemeente-toevoeger:brugge"),
            repo=repo,
            dossier_id=D1,
        )
        assert ok is False
        assert "not found" in err

    async def test_scoped_role_without_dossier_falls_back_to_unscoped(
        self, repo,
    ):
        """When `dossier_id` is None (e.g. the activity is the
        dossier-creation bootstrap), scope resolution can't run —
        there's no dossier to look up entities in. The code path
        skips the scope block and checks the user against the
        BASE role unchanged. Usually that's a deny, but if the
        user happens to have the base role alone they're allowed.
        Locking in the current behavior."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{
                        "role": "gemeente-toevoeger",
                        "scope": {
                            "from_entity": "oe:aanvraag",
                            "field": "content.gemeente",
                        },
                    }],
                },
            },
            user=_user("gemeente-toevoeger"),  # base role, unscoped
            repo=repo,
            dossier_id=None,
        )
        assert ok is True


# --------------------------------------------------------------------
# Entity-derived role branches (DB-backed)
# --------------------------------------------------------------------


class TestEntityDerivedRoles:

    async def test_entity_derived_value_matches_user_role(self, repo):
        """The aanvraag.content.aanvrager.rrn IS the role string.
        User has that exact string as a role → allowed. This is
        the ownership model: a natuurlijk_persoon user
        automatically has exactly one role per dossier they own
        (their own RRN)."""
        boot = await _bootstrap_dossier(repo)
        await _seed_aanvraag(repo, boot, {
            "aanvrager": {"rrn": "85010112345"},
        })
        plugin = _StubPlugin({"oe:aanvraag"})

        ok, err = await authorize_activity(
            plugin=plugin,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{
                        "from_entity": "oe:aanvraag",
                        "field": "content.aanvrager.rrn",
                    }],
                },
            },
            user=_user("85010112345"),
            repo=repo,
            dossier_id=D1,
        )
        assert ok is True

    async def test_entity_derived_value_does_not_match_user_role(
        self, repo,
    ):
        """Dossier owner is RRN 85010112345, user has a different
        RRN. Denied — the user isn't the owner of this dossier."""
        boot = await _bootstrap_dossier(repo)
        await _seed_aanvraag(repo, boot, {
            "aanvrager": {"rrn": "85010112345"},
        })
        plugin = _StubPlugin({"oe:aanvraag"})

        ok, err = await authorize_activity(
            plugin=plugin,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{
                        "from_entity": "oe:aanvraag",
                        "field": "content.aanvrager.rrn",
                    }],
                },
            },
            user=_user("99999999999"),
            repo=repo,
            dossier_id=D1,
        )
        assert ok is False

    async def test_entity_derived_requires_dossier(self, repo):
        """Entity-derived checks need an existing dossier — the
        role string is a field of a real entity, and there's
        nothing to read without a dossier. A bootstrap activity
        (dossier_id=None) can't satisfy this kind of check."""
        ok, err = await authorize_activity(
            plugin=None,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{
                        "from_entity": "oe:aanvraag",
                        "field": "content.aanvrager.rrn",
                    }],
                },
            },
            user=_user("85010112345"),
            repo=repo,
            dossier_id=None,
        )
        assert ok is False
        assert "requires existing dossier" in err

    async def test_entity_derived_missing_field_rejected(self, repo):
        """Entity exists but the field path resolves to None
        (e.g. the aanvrager dict doesn't have an rrn key).
        Collected as an error and the role entry is rejected."""
        boot = await _bootstrap_dossier(repo)
        await _seed_aanvraag(repo, boot, {
            "aanvrager": {"other_field": "x"},  # no rrn
        })
        plugin = _StubPlugin({"oe:aanvraag"})

        ok, err = await authorize_activity(
            plugin=plugin,
            activity_def={
                "authorization": {
                    "access": "roles",
                    "roles": [{
                        "from_entity": "oe:aanvraag",
                        "field": "content.aanvrager.rrn",
                    }],
                },
            },
            user=_user("85010112345"),
            repo=repo,
            dossier_id=D1,
        )
        assert ok is False
        assert "is null" in err
