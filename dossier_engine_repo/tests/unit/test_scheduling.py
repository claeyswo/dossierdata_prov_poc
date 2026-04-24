"""
Tests for `resolve_scheduled_for` — the parser that turns task YAML
``scheduled_for`` values into ISO 8601 datetime strings for storage.

Covers five kinds of inputs:

1. Relative offsets with required sign — ``+20d``, ``+2h``, ``+45m``,
   ``+3w``, and their negative equivalents (``-7d``). Resolved
   against a caller-supplied ``now``.
2. Absolute ISO 8601 — returned as-is when already timezone-aware,
   normalized to UTC when naive.
3. Dict form ``{from_entity, field}`` — reads an ISO datetime from
   an entity's ``content`` via dot-path, same idiom as authorization
   and finalization.
4. Dict form with ``offset`` — reads the entity field and shifts it
   by a signed relative offset. Covers "7 days before permit expiry"
   and similar.
5. Malformed values — raise ValueError so typos fail at activity
   execution rather than silently scheduling for "now".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from dossier_engine.engine.scheduling import resolve_scheduled_for


NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------
# Entity stubs for the dict form
# --------------------------------------------------------------------
# We don't use the real EntityRow / _PendingEntity here — the
# resolver only touches ``.content`` via getattr. A tiny dataclass
# keeps the test setup obvious and avoids dragging the DB layer into
# what should be a pure parser test.
# --------------------------------------------------------------------

@dataclass
class _FakeEntity:
    content: Any


def _entities(**by_type: Any) -> dict[str, _FakeEntity]:
    """Build a resolved_entities-shaped dict. Each kwarg becomes an
    entity with the given content. Example:
        _entities(oe_aanvraag={"registered_at": "2026-05-01T12:00:00Z"})
    Use underscores in kwarg names; we translate to colons for the
    entity type so callers don't fight Python's identifier rules."""
    return {
        k.replace("_", ":", 1): _FakeEntity(content=v)
        for k, v in by_type.items()
    }


# --------------------------------------------------------------------
# Form 1: signed relative offsets
# --------------------------------------------------------------------


class TestRelativeOffsets:
    """``+Nd`` / ``+Nh`` / ``+Nm`` / ``+Nw`` and their negative
    counterparts produce ``now + delta``."""

    def test_days(self):
        result = resolve_scheduled_for("+20d", NOW)
        assert result == (NOW + timedelta(days=20)).isoformat()

    def test_hours(self):
        result = resolve_scheduled_for("+2h", NOW)
        assert result == (NOW + timedelta(hours=2)).isoformat()

    def test_minutes(self):
        result = resolve_scheduled_for("+45m", NOW)
        assert result == (NOW + timedelta(minutes=45)).isoformat()

    def test_weeks(self):
        result = resolve_scheduled_for("+3w", NOW)
        assert result == (NOW + timedelta(weeks=3)).isoformat()

    def test_zero_offset_is_valid(self):
        """``+0d`` is weird but well-defined — resolves to now."""
        result = resolve_scheduled_for("+0d", NOW)
        assert result == NOW.isoformat()

    def test_negative_days(self):
        """Negative offsets are legal (covers 'fire 7d before X' via
        the dict+offset form, and callers that want a deliberately
        past-dated task which the worker will pick up immediately)."""
        result = resolve_scheduled_for("-7d", NOW)
        assert result == (NOW - timedelta(days=7)).isoformat()

    def test_negative_hours(self):
        result = resolve_scheduled_for("-3h", NOW)
        assert result == (NOW - timedelta(hours=3)).isoformat()


# --------------------------------------------------------------------
# Form 2: absolute ISO 8601
# --------------------------------------------------------------------


class TestAbsoluteISO:
    """Absolute ISO 8601 values pass through, optionally normalized."""

    def test_iso_with_z_suffix_passes_through(self):
        """Worker handles the Z suffix at dispatch time; we just
        preserve the author's format for readability."""
        result = resolve_scheduled_for("2026-05-01T12:00:00Z", NOW)
        assert result == "2026-05-01T12:00:00Z"

    def test_iso_with_utc_offset_passes_through(self):
        result = resolve_scheduled_for("2026-05-01T12:00:00+00:00", NOW)
        assert result == "2026-05-01T12:00:00+00:00"

    def test_iso_with_nonutc_offset_passes_through(self):
        """Non-UTC offsets (CET, etc.) also preserved — the worker
        normalizes at dispatch."""
        result = resolve_scheduled_for("2026-05-01T14:00:00+02:00", NOW)
        assert result == "2026-05-01T14:00:00+02:00"

    def test_naive_iso_normalized_to_utc(self):
        """A naive datetime is treated as UTC and normalized to
        include the offset so downstream string comparisons don't
        surprise anyone."""
        result = resolve_scheduled_for("2026-05-01T12:00:00", NOW)
        assert result == "2026-05-01T12:00:00+00:00"


# --------------------------------------------------------------------
# Form 3: empty/None
# --------------------------------------------------------------------


class TestEmptyAndNone:
    """Missing values produce None — the task is immediately due."""

    def test_none_returns_none(self):
        assert resolve_scheduled_for(None, NOW) is None

    def test_empty_string_returns_none(self):
        assert resolve_scheduled_for("", NOW) is None

    def test_whitespace_only_returns_none(self):
        assert resolve_scheduled_for("   ", NOW) is None


# --------------------------------------------------------------------
# Form 4: entity field reference (with and without offset)
# --------------------------------------------------------------------


class TestEntityFieldReference:
    """Dict form reads an ISO datetime from an entity's content."""

    def test_plain_field_reference(self):
        """``{from_entity, field}`` with no offset resolves to the
        field value as-is (normalized to include UTC offset)."""
        entities = _entities(
            oe_aanvraag={"registered_at": "2026-05-01T12:00:00Z"},
        )
        result = resolve_scheduled_for(
            {"from_entity": "oe:aanvraag", "field": "registered_at"},
            NOW, entities,
        )
        # Normalized to +00:00 form — we re-emit via `.isoformat()`.
        assert result == "2026-05-01T12:00:00+00:00"

    def test_field_reference_with_content_prefix(self):
        """Leading ``content.`` in the field path is stripped — the
        resolver is always invoked with the content dict already
        in hand. Same behavior as _resolve_field in authorization."""
        entities = _entities(
            oe_aanvraag={"registered_at": "2026-05-01T12:00:00Z"},
        )
        result = resolve_scheduled_for(
            {"from_entity": "oe:aanvraag", "field": "content.registered_at"},
            NOW, entities,
        )
        assert result == "2026-05-01T12:00:00+00:00"

    def test_nested_field_path(self):
        """Dot-notation walks into nested dicts, matching how
        authorization's scope fields work."""
        entities = _entities(
            oe_aanvraag={"meta": {"deadlines": {"permit_expires": "2026-06-01T00:00:00Z"}}},
        )
        result = resolve_scheduled_for(
            {"from_entity": "oe:aanvraag", "field": "meta.deadlines.permit_expires"},
            NOW, entities,
        )
        assert result == "2026-06-01T00:00:00+00:00"

    def test_date_only_field_value(self):
        """Date-only values (``2026-05-01``) are treated as midnight UTC."""
        entities = _entities(
            oe_aanvraag={"registered_on": "2026-05-01"},
        )
        result = resolve_scheduled_for(
            {"from_entity": "oe:aanvraag", "field": "registered_on"},
            NOW, entities,
        )
        assert result == "2026-05-01T00:00:00+00:00"

    def test_datetime_object_field_value(self):
        """When a handler writes a Python datetime directly into
        content (before any serialization), we accept it. Belt-and-
        suspenders for the code paths that do in-memory construction."""
        deadline = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        entities = _entities(
            oe_aanvraag={"deadline": deadline},
        )
        result = resolve_scheduled_for(
            {"from_entity": "oe:aanvraag", "field": "deadline"},
            NOW, entities,
        )
        assert result == deadline.isoformat()

    def test_naive_datetime_object_treated_as_utc(self):
        naive = datetime(2026, 6, 15, 9, 30)
        entities = _entities(
            oe_aanvraag={"deadline": naive},
        )
        result = resolve_scheduled_for(
            {"from_entity": "oe:aanvraag", "field": "deadline"},
            NOW, entities,
        )
        assert result == naive.replace(tzinfo=timezone.utc).isoformat()

    def test_field_plus_positive_offset(self):
        """The killer use case: ``7d after registration``."""
        entities = _entities(
            oe_aanvraag={"registered_at": "2026-05-01T12:00:00Z"},
        )
        result = resolve_scheduled_for(
            {
                "from_entity": "oe:aanvraag",
                "field": "registered_at",
                "offset": "+7d",
            },
            NOW, entities,
        )
        expected = (datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
                    + timedelta(days=7)).isoformat()
        assert result == expected

    def test_field_plus_negative_offset(self):
        """The other killer use case: ``7d before permit expiry``."""
        entities = _entities(
            oe_aanvraag={"expires_at": "2026-08-01T00:00:00Z"},
        )
        result = resolve_scheduled_for(
            {
                "from_entity": "oe:aanvraag",
                "field": "expires_at",
                "offset": "-7d",
            },
            NOW, entities,
        )
        expected = (datetime(2026, 8, 1, tzinfo=timezone.utc)
                    - timedelta(days=7)).isoformat()
        assert result == expected


# --------------------------------------------------------------------
# Dict-form error paths
# --------------------------------------------------------------------


class TestEntityFieldReferenceErrors:
    """The dict form has several failure modes; each produces a
    distinct, actionable error message."""

    def test_entity_not_in_resolved(self):
        """Activity didn't declare the referenced entity in its
        ``used`` or ``generated`` block."""
        entities = _entities()  # empty
        with pytest.raises(ValueError, match="couldn't be resolved"):
            resolve_scheduled_for(
                {"from_entity": "oe:aanvraag", "field": "registered_at"},
                NOW, entities,
            )

    def test_field_missing(self):
        entities = _entities(oe_aanvraag={"other_field": "value"})
        with pytest.raises(ValueError, match="null or missing"):
            resolve_scheduled_for(
                {"from_entity": "oe:aanvraag", "field": "registered_at"},
                NOW, entities,
            )

    def test_field_null(self):
        entities = _entities(oe_aanvraag={"registered_at": None})
        with pytest.raises(ValueError, match="null or missing"):
            resolve_scheduled_for(
                {"from_entity": "oe:aanvraag", "field": "registered_at"},
                NOW, entities,
            )

    def test_field_is_int(self):
        """Plain Unix timestamps aren't accepted — the DSL is ISO
        only. Force handlers to do any timestamp conversion."""
        entities = _entities(oe_aanvraag={"registered_at": 1714564800})
        with pytest.raises(ValueError, match="expected an ISO 8601 string"):
            resolve_scheduled_for(
                {"from_entity": "oe:aanvraag", "field": "registered_at"},
                NOW, entities,
            )

    def test_field_is_unparseable_string(self):
        entities = _entities(oe_aanvraag={"registered_at": "last tuesday"})
        with pytest.raises(ValueError, match="expected an ISO 8601"):
            resolve_scheduled_for(
                {"from_entity": "oe:aanvraag", "field": "registered_at"},
                NOW, entities,
            )

    def test_missing_from_entity_key(self):
        entities = _entities(oe_aanvraag={"registered_at": "2026-05-01T12:00:00Z"})
        with pytest.raises(ValueError, match="from_entity.*field"):
            resolve_scheduled_for(
                {"field": "registered_at"},
                NOW, entities,
            )

    def test_missing_field_key(self):
        entities = _entities(oe_aanvraag={"registered_at": "2026-05-01T12:00:00Z"})
        with pytest.raises(ValueError, match="from_entity.*field"):
            resolve_scheduled_for(
                {"from_entity": "oe:aanvraag"},
                NOW, entities,
            )

    def test_bad_offset_in_dict_form(self):
        """The offset string inside the dict form goes through the
        same parser as the top-level string form, so errors match."""
        entities = _entities(oe_aanvraag={"registered_at": "2026-05-01T12:00:00Z"})
        with pytest.raises(ValueError, match="Invalid offset"):
            resolve_scheduled_for(
                {
                    "from_entity": "oe:aanvraag",
                    "field": "registered_at",
                    "offset": "20d",  # missing sign
                },
                NOW, entities,
            )

    def test_dict_form_without_resolved_entities_errors_loudly(self):
        """If a caller passes the dict form but forgot to plumb
        state.resolved_entities through, we'd prefer a clear error
        over a silent None-dereference. Engine code always supplies
        it; this branch protects against future refactors."""
        with pytest.raises(ValueError, match="requires resolved_entities"):
            resolve_scheduled_for(
                {"from_entity": "oe:aanvraag", "field": "registered_at"},
                NOW,
                None,
            )


# --------------------------------------------------------------------
# String-form error paths
# --------------------------------------------------------------------


class TestStringFormMalformed:
    """String inputs that don't match any of the two string forms."""

    def test_bare_duration_without_sign_rejected(self):
        """``20d`` without the sign prefix isn't accepted — the sign
        is the disambiguating marker, now mandatory."""
        with pytest.raises(ValueError, match="Invalid scheduled_for"):
            resolve_scheduled_for("20d", NOW)

    def test_unknown_unit_rejected(self):
        """``+20y`` (years) and ``+20s`` (seconds) aren't in the
        grammar. If you need them, extend _UNIT_KWARGS first."""
        with pytest.raises(ValueError):
            resolve_scheduled_for("+20y", NOW)
        with pytest.raises(ValueError):
            resolve_scheduled_for("+20s", NOW)

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            resolve_scheduled_for("tomorrow", NOW)

    def test_error_message_mentions_all_forms(self):
        """The error message should guide the author toward every
        valid form so they don't have to read source to fix their
        typo. Covers the three forms we support."""
        with pytest.raises(ValueError) as exc:
            resolve_scheduled_for("invalid", NOW)
        msg = str(exc.value)
        assert "+20d" in msg
        assert "-7d" in msg
        assert "ISO 8601" in msg
        assert "from_entity" in msg

    def test_wrong_type_rejected(self):
        """List, int, etc. — not string and not dict → loud error."""
        with pytest.raises(ValueError, match="must be a string or a dict"):
            resolve_scheduled_for(["+20d"], NOW)
        with pytest.raises(ValueError, match="must be a string or a dict"):
            resolve_scheduled_for(42, NOW)


# --------------------------------------------------------------------
# resolve_deadline (for workflow rules: not_after / not_before)
# --------------------------------------------------------------------
#
# These tests exercise the deadline resolver in isolation — no DB,
# no plugin-load machinery, just a stubbed singleton lookup. The
# grammar is intentionally narrower than scheduled_for: no relative-
# offset-from-now, and the entity form must point at a declared
# singleton. Integration tests in test_workflow_rules.py exercise
# the full stack (real repo, real validate_workflow_rules).
# --------------------------------------------------------------------

from dossier_engine.engine.scheduling import resolve_deadline


class _FakePlugin:
    """Minimal plugin stub. `resolve_deadline` calls `is_singleton`
    once on the referenced type. If is_singleton returns True the
    code path proceeds to `lookup_singleton`, which calls
    `is_singleton` again (yes, twice) and then `repo.get_singleton_entity`."""

    def __init__(self, singletons: set[str]):
        self._singletons = singletons

    def is_singleton(self, entity_type: str) -> bool:
        return entity_type in self._singletons

    def cardinality_of(self, entity_type: str) -> str:
        """Only hit from `lookup_singleton`'s error path when
        is_singleton returns False. Not exercised by passing tests,
        but needed for completeness so the tests that expect
        singleton-rejection produce the right error shape."""
        return "single" if entity_type in self._singletons else "multiple"


class _FakeRepo:
    """Minimal repo stub. `resolve_deadline` only hits
    `get_singleton_entity(dossier_id, entity_type)` via
    `lookup_singleton`. The test sets up one mapping; missing
    entities yield None (which is how the real repo behaves for a
    type that isn't in the dossier)."""

    def __init__(self, entities: dict[str, Any] | None = None):
        self._entities = entities or {}

    async def get_singleton_entity(self, dossier_id: Any, entity_type: str):
        return self._entities.get(entity_type)


_DOSSIER_ID = "d0000000-0000-0000-0000-000000000001"


class TestResolveDeadlineIsoForm:
    """String form: absolute ISO 8601, no relative offsets."""

    async def test_iso_with_z_suffix_returns_aware_datetime(self):
        plugin = _FakePlugin(singletons=set())
        repo = _FakeRepo()
        result = await resolve_deadline(
            "2026-12-31T23:59:59Z",
            plugin, repo, _DOSSIER_ID,
            rule_name="not_after",
        )
        # Returns a datetime, not a string — callers compare with
        # `now` and don't want to re-parse.
        assert isinstance(result, datetime)
        assert result == datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    async def test_iso_with_offset_suffix(self):
        plugin = _FakePlugin(singletons=set())
        repo = _FakeRepo()
        result = await resolve_deadline(
            "2026-05-01T12:00:00+02:00",
            plugin, repo, _DOSSIER_ID,
            rule_name="not_before",
        )
        # Parsed as CET → 10:00 UTC.
        assert result == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)

    async def test_none_returns_none(self):
        plugin = _FakePlugin(singletons=set())
        repo = _FakeRepo()
        assert await resolve_deadline(
            None, plugin, repo, _DOSSIER_ID, rule_name="not_after",
        ) is None

    async def test_empty_string_returns_none(self):
        plugin = _FakePlugin(singletons=set())
        repo = _FakeRepo()
        assert await resolve_deadline(
            "", plugin, repo, _DOSSIER_ID, rule_name="not_after",
        ) is None

    async def test_relative_offset_string_rejected(self):
        """The grammar intentionally excludes `+20d` from deadlines.
        'Relative to now' has no fixed meaning at check time — the
        deadline would slide each time the check ran. Error message
        explains the reasoning."""
        plugin = _FakePlugin(singletons=set())
        repo = _FakeRepo()
        with pytest.raises(ValueError, match="relative offsets are not supported"):
            await resolve_deadline(
                "+20d", plugin, repo, _DOSSIER_ID, rule_name="not_after",
            )
        with pytest.raises(ValueError, match="relative offsets are not supported"):
            await resolve_deadline(
                "-7d", plugin, repo, _DOSSIER_ID, rule_name="not_before",
            )

    async def test_garbage_string_rejected(self):
        plugin = _FakePlugin(singletons=set())
        repo = _FakeRepo()
        with pytest.raises(ValueError, match="Invalid not_after"):
            await resolve_deadline(
                "last tuesday", plugin, repo, _DOSSIER_ID,
                rule_name="not_after",
            )

    async def test_rule_name_flows_into_error_message(self):
        """The `rule_name` parameter determines the prefix in error
        messages — callers pass 'not_after' or 'not_before' so the
        user sees which rule failed."""
        plugin = _FakePlugin(singletons=set())
        repo = _FakeRepo()
        with pytest.raises(ValueError) as exc:
            await resolve_deadline(
                "garbage", plugin, repo, _DOSSIER_ID,
                rule_name="not_before",
            )
        assert "not_before" in str(exc.value)


class TestResolveDeadlineDictForm:
    """Entity field reference: the value comes from a singleton
    in the dossier."""

    async def test_plain_field_reference(self):
        plugin = _FakePlugin(singletons={"oe:permit"})
        repo = _FakeRepo({
            "oe:permit": _FakeEntity(
                content={"expires_at": "2026-12-31T23:59:59Z"},
            ),
        })
        result = await resolve_deadline(
            {"from_entity": "oe:permit", "field": "expires_at"},
            plugin, repo, _DOSSIER_ID,
            rule_name="not_after",
        )
        assert result == datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    async def test_nested_field_path(self):
        plugin = _FakePlugin(singletons={"oe:permit"})
        repo = _FakeRepo({
            "oe:permit": _FakeEntity(
                content={"meta": {"deadline": "2026-12-31T00:00:00Z"}},
            ),
        })
        result = await resolve_deadline(
            {"from_entity": "oe:permit", "field": "meta.deadline"},
            plugin, repo, _DOSSIER_ID,
            rule_name="not_after",
        )
        assert result == datetime(2026, 12, 31, tzinfo=timezone.utc)

    async def test_field_plus_positive_offset(self):
        plugin = _FakePlugin(singletons={"oe:aanvraag"})
        repo = _FakeRepo({
            "oe:aanvraag": _FakeEntity(
                content={"registered_at": "2026-04-01T00:00:00Z"},
            ),
        })
        result = await resolve_deadline(
            {
                "from_entity": "oe:aanvraag",
                "field": "registered_at",
                "offset": "+30d",
            },
            plugin, repo, _DOSSIER_ID,
            rule_name="not_before",
        )
        assert result == datetime(2026, 5, 1, tzinfo=timezone.utc)

    async def test_field_plus_negative_offset(self):
        """The reminder pattern: 7 days before permit expiry."""
        plugin = _FakePlugin(singletons={"oe:permit"})
        repo = _FakeRepo({
            "oe:permit": _FakeEntity(
                content={"expires_at": "2026-12-31T00:00:00Z"},
            ),
        })
        result = await resolve_deadline(
            {
                "from_entity": "oe:permit",
                "field": "expires_at",
                "offset": "-7d",
            },
            plugin, repo, _DOSSIER_ID,
            rule_name="not_after",
        )
        assert result == datetime(2026, 12, 24, tzinfo=timezone.utc)

    async def test_singleton_missing_returns_none(self):
        """Singleton isn't in the dossier yet → resolver returns
        None, validator treats the rule as inactive. Documented
        behaviour — lets plugins compose the deadline with
        requirements.entities to gate on anchor existence."""
        plugin = _FakePlugin(singletons={"oe:permit"})
        repo = _FakeRepo({})  # empty — oe:permit not in dossier
        result = await resolve_deadline(
            {"from_entity": "oe:permit", "field": "expires_at"},
            plugin, repo, _DOSSIER_ID,
            rule_name="not_after",
        )
        assert result is None


class TestResolveDeadlineErrors:
    """Malformed / semantically-invalid declarations."""

    async def test_non_singleton_rejected(self):
        """The workflow validator should catch this at load, but the
        resolver defends against test harnesses that bypass the
        validator (direct Plugin construction etc.)."""
        plugin = _FakePlugin(singletons=set())  # oe:permit NOT singleton
        repo = _FakeRepo()
        with pytest.raises(ValueError, match="not a singleton"):
            await resolve_deadline(
                {"from_entity": "oe:permit", "field": "expires_at"},
                plugin, repo, _DOSSIER_ID,
                rule_name="not_after",
            )

    async def test_missing_from_entity_key(self):
        plugin = _FakePlugin(singletons={"oe:permit"})
        repo = _FakeRepo()
        with pytest.raises(ValueError, match="'from_entity' and 'field'"):
            await resolve_deadline(
                {"field": "expires_at"},
                plugin, repo, _DOSSIER_ID,
                rule_name="not_after",
            )

    async def test_missing_field_key(self):
        plugin = _FakePlugin(singletons={"oe:permit"})
        repo = _FakeRepo()
        with pytest.raises(ValueError, match="'from_entity' and 'field'"):
            await resolve_deadline(
                {"from_entity": "oe:permit"},
                plugin, repo, _DOSSIER_ID,
                rule_name="not_after",
            )

    async def test_wrong_type_rejected(self):
        """Neither string nor dict — typical accidental integer/list."""
        plugin = _FakePlugin(singletons=set())
        repo = _FakeRepo()
        with pytest.raises(ValueError, match="must be a string or a dict"):
            await resolve_deadline(
                42, plugin, repo, _DOSSIER_ID, rule_name="not_after",
            )

    async def test_field_null_raises(self):
        """Singleton exists but the declared field is null → the
        author has a bug. Fail loud with the rule_name in the message."""
        plugin = _FakePlugin(singletons={"oe:permit"})
        repo = _FakeRepo({
            "oe:permit": _FakeEntity(content={"expires_at": None}),
        })
        with pytest.raises(ValueError) as exc:
            await resolve_deadline(
                {"from_entity": "oe:permit", "field": "expires_at"},
                plugin, repo, _DOSSIER_ID,
                rule_name="not_after",
            )
        assert "not_after" in str(exc.value)
        assert "null or missing" in str(exc.value)

    async def test_field_unparseable_raises(self):
        plugin = _FakePlugin(singletons={"oe:permit"})
        repo = _FakeRepo({
            "oe:permit": _FakeEntity(
                content={"expires_at": "not a real date"},
            ),
        })
        with pytest.raises(ValueError, match="expected an ISO 8601"):
            await resolve_deadline(
                {"from_entity": "oe:permit", "field": "expires_at"},
                plugin, repo, _DOSSIER_ID,
                rule_name="not_after",
            )

    async def test_bad_offset_rejected(self):
        plugin = _FakePlugin(singletons={"oe:permit"})
        repo = _FakeRepo({
            "oe:permit": _FakeEntity(
                content={"expires_at": "2026-12-31T00:00:00Z"},
            ),
        })
        with pytest.raises(ValueError, match="Invalid offset"):
            await resolve_deadline(
                {
                    "from_entity": "oe:permit",
                    "field": "expires_at",
                    "offset": "7d",  # missing sign
                },
                plugin, repo, _DOSSIER_ID,
                rule_name="not_after",
            )
