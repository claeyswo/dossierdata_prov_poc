"""Unit tests for the ``valideer_exception`` validator.

The validator enforces two invariants for system:exception:

1. At most one logical system:exception per (activity) in the dossier,
   across all time. Fresh grants for activities that already have
   an exception are rejected; revisions of the existing entity are
   allowed.
2. The ``activity`` field is immutable across revisions — an
   exception for ``oe:A`` can't morph into one for ``oe:B``.

Plus two supporting shape checks: the submitted status must be
``active`` (retract / consume have their own paths), and the
``activity`` field must name a declared workflow activity.

These tests exercise the validator in isolation — no database, no
pipeline. The only method we need to fake on ActivityContext is
``get_typed`` and ``get_used_row`` (for the submitted entity's
entity_id), plus ``repo.get_entities_by_type_latest`` for the
history walk. A plain SimpleNamespace carrying closures suffices.

Integration tests in the engine repo exercise the full pipeline
(grantException → validator → persist → subsequent grant gets 422).
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from dossier_engine.engine.errors import ActivityError
from dossier_engine.entities import Exception_, ExceptionStatus
from dossier_engine.builtins.exceptions import valideer_exception


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRepo:
    """Minimal repo. Only one method is called by the validator."""
    def __init__(self, existing: list[SimpleNamespace] | None = None):
        self._existing = existing or []

    async def get_entities_by_type_latest(self, dossier_id, entity_type):
        # Filter by type so a future test that seeds mixed types
        # behaves correctly. The validator only asks for
        # "system:exception", but explicit-matching makes the fake
        # safer against drift.
        return [e for e in self._existing if e.type == entity_type]


class _FakePlugin:
    """Only ``workflow.activities[*].name`` is consulted."""
    def __init__(self, activity_names: list[str]):
        self.workflow = {
            "activities": [{"name": n} for n in activity_names],
        }


class _FakeContext:
    """Fake ActivityContext matching ``valideer_exception``'s surface:
    ``get_used_row`` (to read ``.entity_id`` + ``.content`` of the
    pending submission), ``repo``, ``dossier_id``, ``_plugin``.

    We pass the submitted content as a **raw dict**, not as a Pydantic
    ``Exception_`` instance. Matches the engine's post-
    ``process_generated`` shape: ``_PendingEntity.content`` is the
    literal client-submitted dict; the engine validates it against
    the Pydantic model (raises on bad shape) but does not replace it.
    Validators that care about what was asserted (vs what Pydantic
    defaults would fill in) must read the raw dict.

    A ``submitted_content`` of ``None`` means "no system:exception in
    the submitted generated block" — the validator's no-op path."""

    def __init__(
        self,
        submitted_content: dict | None,
        entity_id,
        plugin: _FakePlugin,
        repo: _FakeRepo,
    ):
        self._submitted_content = submitted_content
        self._entity_id = entity_id
        self._plugin = plugin
        self.repo = repo
        self.dossier_id = uuid4()

    def get_used_row(self, entity_type: str):
        if entity_type == "system:exception" and self._submitted_content is not None:
            return SimpleNamespace(
                entity_id=self._entity_id,
                content=dict(self._submitted_content),  # defensive copy
            )
        return None

    # ``get_typed`` is kept for API parity with the real
    # ActivityContext, but the rewritten validator doesn't call it.
    def get_typed(self, entity_type: str):
        return None


def _existing(entity_id, activity: str, *, content_extra: dict | None = None):
    """Build a fake existing system:exception row with minimum fields."""
    content = {"activity": activity, "reason": "legacy", "status": "active"}
    if content_extra:
        content.update(content_extra)
    return SimpleNamespace(
        type="system:exception",
        entity_id=entity_id,
        content=content,
    )


def _content(
    *,
    activity: str,
    reason: str = "legal extension",
    status: str = "active",
    granted_until: str | None = None,
) -> dict:
    """Build a raw submitted-content dict for system:exception. Defaults
    to the happy-path shape: status=active, reason set. Tests that
    exercise the 'missing status' / 'wrong status' branches build
    their own dicts explicitly so the intent is obvious at the call
    site."""
    c: dict = {"activity": activity, "reason": reason, "status": status}
    if granted_until is not None:
        c["granted_until"] = granted_until
    return c


_WORKFLOW_ACTS = [
    "oe:trekAanvraagIn",
    "oe:neemBeslissing",
    "oe:grantException",
]


# ---------------------------------------------------------------------------
# No-submission fast path
# ---------------------------------------------------------------------------


class TestNoSubmission:
    """If the submitted generated block contains no system:exception, the
    validator is a no-op. It doesn't fire from activities that don't
    generate exceptions; but if something routes it to such an
    activity accidentally, we don't want to crash."""

    async def test_no_submission_returns_true(self):
        ctx = _FakeContext(
            submitted_content=None,
            entity_id=None,
            plugin=_FakePlugin(_WORKFLOW_ACTS),
            repo=_FakeRepo(),
        )
        result = await valideer_exception(ctx)
        assert result is True


# ---------------------------------------------------------------------------
# Status enforcement
# ---------------------------------------------------------------------------


class TestStatusMustBeActive:
    """grantException must submit status=active. The other two
    statuses (consumed, cancelled) belong to dedicated activities —
    the engine-wired consumeException and user-initiated
    retractException. A client posting status=cancelled via
    grantException would bypass retract's audit trail entirely, so
    we reject loud."""

    async def test_active_passes(self):
        content = _content(
            activity="oe:trekAanvraagIn",
            reason="Legal extension #4711",
            status="active",
        )
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS), repo=_FakeRepo(),
        )
        assert await valideer_exception(ctx) is True

    async def test_consumed_rejected(self):
        content = _content(
            activity="oe:trekAanvraagIn",
            reason="r",
            status="consumed",
        )
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS), repo=_FakeRepo(),
        )
        with pytest.raises(ActivityError) as exc:
            await valideer_exception(ctx)
        assert exc.value.status_code == 422
        assert "status='active'" in exc.value.detail

    async def test_cancelled_rejected(self):
        content = _content(
            activity="oe:trekAanvraagIn",
            reason="r",
            status="cancelled",
        )
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS), repo=_FakeRepo(),
        )
        with pytest.raises(ActivityError):
            await valideer_exception(ctx)

    async def test_missing_status_rejected(self):
        """The class of bug that motivated removing the
        ``= ExceptionStatus.active`` default from the Pydantic model.
        The engine's content-validation phase doesn't mutate submitted
        content — it only validates — so a missing status on the
        client's submission gets persisted as an absent field. A
        silent-default in the model would have invented an assertion
        the agent never made, weakening the PROV audit trail. The
        validator rejects at grant time so this never happens."""
        # Raw dict without status — not buildable via _content() since
        # _content() always writes it. Ship a bespoke dict instead.
        content = {"activity": "oe:trekAanvraagIn", "reason": "r"}
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS), repo=_FakeRepo(),
        )
        with pytest.raises(ActivityError) as exc:
            await valideer_exception(ctx)
        assert exc.value.status_code == 422
        assert "status='active'" in exc.value.detail
        # Error message should make the "no defaults injected" rule
        # explicit — future readers of the error should understand
        # why they can't skip the field.
        assert "no status field" in exc.value.detail.lower() or \
               "does not inject" in exc.value.detail.lower()

    async def test_missing_activity_rejected(self):
        """Same principle for the activity field — the Pydantic
        model's ``activity: str`` has no default, but if the client
        sent a dict without it, we'd persist it without activity.
        Validator catches this too."""
        content = {"reason": "r", "status": "active"}  # no activity
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS), repo=_FakeRepo(),
        )
        with pytest.raises(ActivityError) as exc:
            await valideer_exception(ctx)
        assert exc.value.status_code == 422
        assert "activity" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# Activity-name validity
# ---------------------------------------------------------------------------


class TestActivityMustBeDeclared:
    """An exception that names an activity not in the workflow is a
    typo — rejecting at grant time is vastly nicer than creating a
    silent never-matches exception that sits in the dossier forever.

    Also covers the bare-name → qualified-name normalization: the
    validator qualifies ``trekAanvraagIn`` to ``oe:trekAanvraagIn``
    using the default prefix before comparing to the declared set."""

    async def test_qualified_known_passes(self):
        content = _content(activity="oe:trekAanvraagIn", reason="r")
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS), repo=_FakeRepo(),
        )
        assert await valideer_exception(ctx) is True

    async def test_bare_known_passes_after_qualify(self):
        content = _content(activity="trekAanvraagIn", reason="r")
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS), repo=_FakeRepo(),
        )
        assert await valideer_exception(ctx) is True

    async def test_unknown_rejected(self):
        content = _content(activity="oe:thisDoesNotExist", reason="r")
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS), repo=_FakeRepo(),
        )
        with pytest.raises(ActivityError) as exc:
            await valideer_exception(ctx)
        assert exc.value.status_code == 422
        assert "unknown activity" in exc.value.detail
        assert "oe:thisDoesNotExist" in exc.value.detail


# ---------------------------------------------------------------------------
# Rule 1: one-per-activity
# ---------------------------------------------------------------------------


class TestOnePerActivity:
    """The core invariant. A dossier has at most one logical
    system:exception per activity, ever. Subsequent grants for the same
    activity MUST revise the existing entity — a fresh grant (new
    entity_id) is rejected."""

    async def test_fresh_grant_for_new_activity_passes(self):
        """No existing exceptions; a brand-new entity for a new
        activity goes through."""
        content = _content(activity="oe:trekAanvraagIn", reason="r")
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS),
            repo=_FakeRepo(),  # no existing
        )
        assert await valideer_exception(ctx) is True

    async def test_fresh_grant_for_different_activity_allowed(self):
        """An existing exception for oe:A doesn't block a fresh
        grant for oe:B — the uniqueness rule is per-activity."""
        content = _content(activity="oe:neemBeslissing", reason="r")
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS),
            repo=_FakeRepo([
                _existing(uuid4(), "oe:trekAanvraagIn"),
            ]),
        )
        assert await valideer_exception(ctx) is True

    async def test_fresh_grant_for_same_activity_rejected(self):
        """The main negative. Fresh entity_id, existing entity for
        the same activity → 422 with a pointer to revise instead."""
        existing_id = uuid4()
        content = _content(activity="oe:trekAanvraagIn", reason="r")
        ctx = _FakeContext(
            submitted_content=content,
            entity_id=uuid4(),  # DIFFERENT from existing_id
            plugin=_FakePlugin(_WORKFLOW_ACTS),
            repo=_FakeRepo([
                _existing(existing_id, "oe:trekAanvraagIn"),
            ]),
        )
        with pytest.raises(ActivityError) as exc:
            await valideer_exception(ctx)
        assert exc.value.status_code == 422
        assert "already exists" in exc.value.detail
        assert "Revise" in exc.value.detail
        assert str(existing_id) in exc.value.detail

    async def test_rejection_survives_across_statuses(self):
        """Even if the existing exception is currently ``consumed``
        or ``cancelled``, you still revise the same entity. We
        don't filter by status — the rule is purely identity-based.
        The history lives on one logical entity per activity."""
        existing_id = uuid4()
        content = _content(activity="oe:trekAanvraagIn", reason="r")
        ctx = _FakeContext(
            submitted_content=content, entity_id=uuid4(),
            plugin=_FakePlugin(_WORKFLOW_ACTS),
            repo=_FakeRepo([
                _existing(
                    existing_id, "oe:trekAanvraagIn",
                    content_extra={"status": "cancelled"},
                ),
            ]),
        )
        with pytest.raises(ActivityError) as exc:
            await valideer_exception(ctx)
        assert "already exists" in exc.value.detail


# ---------------------------------------------------------------------------
# Rule 2: revision allowed, activity immutable
# ---------------------------------------------------------------------------


class TestRevision:
    """When the submitted entity_id matches an existing entity_id
    in the dossier, this is a revision, not a fresh grant. Revisions
    bypass the one-per-activity rule (they ARE the existing one).
    The activity field, however, must match the parent's."""

    async def test_revision_same_activity_passes(self):
        """Classic re-grant: previously-active exception got
        consumed; admin grants a fresh 'round'. Same entity_id,
        same activity, status back to active. This is the core
        per-activity-timeline pattern."""
        shared_id = uuid4()
        content = _content(activity="oe:trekAanvraagIn", reason="r2")
        ctx = _FakeContext(
            submitted_content=content,
            entity_id=shared_id,
            plugin=_FakePlugin(_WORKFLOW_ACTS),
            repo=_FakeRepo([
                _existing(
                    shared_id, "oe:trekAanvraagIn",
                    content_extra={"status": "consumed"},
                ),
            ]),
        )
        assert await valideer_exception(ctx) is True

    async def test_revision_changes_activity_rejected(self):
        """The integrity of the per-activity history depends on
        the activity being immutable. Exception for oe:A can't
        morph into exception for oe:B — history gets weird."""
        shared_id = uuid4()
        content = _content(activity="oe:neemBeslissing", reason="r")
        ctx = _FakeContext(
            submitted_content=content,
            entity_id=shared_id,
            plugin=_FakePlugin(_WORKFLOW_ACTS),
            repo=_FakeRepo([
                _existing(shared_id, "oe:trekAanvraagIn"),
            ]),
        )
        with pytest.raises(ActivityError) as exc:
            await valideer_exception(ctx)
        assert exc.value.status_code == 422
        assert "Cannot change the activity" in exc.value.detail
        assert "oe:trekAanvraagIn" in exc.value.detail
        assert "oe:neemBeslissing" in exc.value.detail

    async def test_revision_ignores_other_activities_exceptions(self):
        """When we're revising exception-for-oe:A, the existence of
        another latest exception for oe:B with a different entity_id
        doesn't matter. Revision is scoped to matching entity_id."""
        shared_id = uuid4()
        content = _content(activity="oe:trekAanvraagIn", reason="r")
        ctx = _FakeContext(
            submitted_content=content,
            entity_id=shared_id,
            plugin=_FakePlugin(_WORKFLOW_ACTS),
            repo=_FakeRepo([
                _existing(shared_id, "oe:trekAanvraagIn"),
                _existing(uuid4(), "oe:neemBeslissing"),
            ]),
        )
        assert await valideer_exception(ctx) is True
