"""
Tests for `resolve_scheduled_for` — the parser that turns task YAML
`scheduled_for` values into ISO 8601 datetime strings for storage.

Covers three kinds of inputs:

1. Relative offsets (``+20d``, ``+2h``, ``+45m``, ``+3w``) — resolved
   against a caller-supplied ``now``. These are the common YAML case.
2. Absolute ISO 8601 — returned as-is (or normalized to UTC if naive).
3. Malformed values — raise ValueError so typos fail at activity
   execution rather than silently scheduling for "now".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dossier_engine.engine.scheduling import resolve_scheduled_for


NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


class TestRelativeOffsets:
    """``+Nd`` / ``+Nh`` / ``+Nm`` / ``+Nw`` produce `now + delta`."""

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

    def test_naive_iso_normalized_to_utc(self):
        """A naive datetime is treated as UTC and normalized to
        include the offset so downstream string comparisons don't
        surprise anyone."""
        result = resolve_scheduled_for("2026-05-01T12:00:00", NOW)
        assert result == "2026-05-01T12:00:00+00:00"


class TestEmptyAndNone:
    """Missing values produce None — the task is immediately due."""

    def test_none_returns_none(self):
        assert resolve_scheduled_for(None, NOW) is None

    def test_empty_string_returns_none(self):
        assert resolve_scheduled_for("", NOW) is None

    def test_whitespace_only_returns_none(self):
        assert resolve_scheduled_for("   ", NOW) is None


class TestMalformed:
    """Bad input raises ValueError — never silently interpreted as 'now'."""

    def test_bare_duration_without_plus_rejected(self):
        """``20d`` without the ``+`` prefix isn't accepted — the
        prefix is the disambiguating marker between offset and ISO."""
        with pytest.raises(ValueError, match="Invalid scheduled_for"):
            resolve_scheduled_for("20d", NOW)

    def test_negative_offset_rejected(self):
        """Scheduling a task in the past makes no sense."""
        with pytest.raises(ValueError):
            resolve_scheduled_for("-5d", NOW)

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

    def test_error_message_mentions_both_forms(self):
        """The error message should guide the author toward either
        valid form so they don't have to read source to fix their
        typo."""
        with pytest.raises(ValueError, match=r"\+20d.*ISO 8601|ISO 8601.*\+20d"):
            resolve_scheduled_for("invalid", NOW)
