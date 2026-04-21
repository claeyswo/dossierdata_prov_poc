"""
Tests for pure helper functions inside `dossier_engine.worker`.

These helpers were extracted during sub-steps 2, 6, and 7 of the
worker hardening arc precisely because they were hard to test
through the full worker loop. Now they're standalone functions
that take primitives and return primitives (or datetimes) — the
exact shape pytest likes.

What's covered here:

* `_parse_scheduled_for` — the ISO-8601 tolerance layer. Must
  handle `Z`-suffixed strings, `+00:00`-suffixed strings, naive
  strings (treated as UTC), None, and garbage. This is the fix
  for sub-step 2 (string-compare bug on scheduled_for).

* `_compute_next_attempt_at` — exponential backoff with jitter.
  The math is `base * 2**(attempt-1) * (1 + uniform(-0.1, 0.1))`,
  so tests pin the jitter via monkeypatching `random.uniform` to
  make the math deterministic, then assert exact durations.

* `_is_task_due` — the "which tasks are claimable" Python-side
  filter. Critical for correctness because `find_due_tasks` and
  `_claim_one_due_task` both delegate to it. Must respect BOTH
  `scheduled_for` and `next_attempt_at` when either is set.

The helpers operate on `EntityRow` but only read the `.content`
attribute, so we use a `SimpleNamespace` stub — no need to drag
a database row shape into a pure-Python test.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dossier_engine.worker import (
    _parse_scheduled_for,
    _compute_next_attempt_at,
    _is_task_due,
)


UTC = timezone.utc


# --------------------------------------------------------------------
# _parse_scheduled_for
# --------------------------------------------------------------------

class TestParseScheduledFor:
    """The engine writes `scheduled_for` as an ISO 8601 string, but
    the exact form depends on who produced it — `datetime.isoformat()`
    gives `+00:00` suffixes, some test inputs use `Z`, and legacy
    rows may be naive. This function normalizes all three."""

    def test_none_returns_none(self):
        assert _parse_scheduled_for(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_scheduled_for("") is None

    def test_whitespace_only_returns_none(self):
        # The `if not value` short-circuit catches empty strings
        # but `"   "` is truthy in Python. The function strips
        # before parsing, so after stripping it becomes "" which
        # fromisoformat rejects, returning None via the except branch.
        assert _parse_scheduled_for("   ") is None

    def test_z_suffix(self):
        result = _parse_scheduled_for("2026-05-01T12:30:00Z")
        assert result == datetime(2026, 5, 1, 12, 30, 0, tzinfo=UTC)

    def test_offset_suffix(self):
        result = _parse_scheduled_for("2026-05-01T12:30:00+00:00")
        assert result == datetime(2026, 5, 1, 12, 30, 0, tzinfo=UTC)

    def test_non_utc_offset(self):
        """A non-UTC offset should be preserved as-is — the comparison
        in `_is_task_due` handles aware datetimes uniformly regardless
        of the specific tz, so we don't need to normalize to UTC here."""
        result = _parse_scheduled_for("2026-05-01T14:30:00+02:00")
        assert result is not None
        # Same instant, expressed as UTC:
        assert result.astimezone(UTC) == datetime(2026, 5, 1, 12, 30, 0, tzinfo=UTC)

    def test_naive_assumed_utc(self):
        """A naive string gets UTC attached. This is a conservative
        choice — old rows in the database predate the tz fix and
        happen to all be written in UTC anyway, so treating naive
        as UTC is correct for the historical data."""
        result = _parse_scheduled_for("2026-05-01T12:30:00")
        assert result == datetime(2026, 5, 1, 12, 30, 0, tzinfo=UTC)

    def test_with_microseconds(self):
        result = _parse_scheduled_for("2026-05-01T12:30:00.123456Z")
        assert result == datetime(2026, 5, 1, 12, 30, 0, 123456, tzinfo=UTC)

    def test_garbage_returns_datetime_max(self, caplog):
        """Bug 12: malformed (non-empty) strings used to collapse to
        ``None``, which ``_is_task_due`` treated as "immediately due" —
        a task scheduled for next week would fire right now. Now we
        return ``datetime.max`` (aware UTC) so the due-check's
        ``> now`` comparison defers the task indefinitely, and we log
        loudly so the corruption is visible."""
        import logging
        with caplog.at_level(logging.ERROR, logger="dossier.worker"):
            result = _parse_scheduled_for("not a date")
        assert result == datetime.max.replace(tzinfo=UTC)
        assert result.tzinfo is UTC
        # One error record per malformed value.
        assert len(caplog.records) == 1
        assert "not a date" in caplog.records[0].getMessage()

    def test_multiple_garbage_forms_all_defer(self, caplog):
        """Each malformed shape logs and defers — none collapse to
        None (the old bug)."""
        import logging
        with caplog.at_level(logging.ERROR, logger="dossier.worker"):
            for bad in ("not a date", "2026-13-45", "12:30:00"):
                result = _parse_scheduled_for(bad)
                assert result == datetime.max.replace(tzinfo=UTC), (
                    f"{bad!r} should defer, not collapse to None"
                )

    def test_empty_and_none_still_return_none(self):
        """The None/empty branch is distinct — those mean "no
        scheduling constraint was set" (the common case for tasks
        that should fire on the next poll), and must keep returning
        ``None`` so the caller treats them as due-now. Regression
        guard against conflating the two cases."""
        assert _parse_scheduled_for(None) is None
        assert _parse_scheduled_for("") is None
        assert _parse_scheduled_for("   ") is None

    def test_roundtrip_with_isoformat(self):
        """datetime.isoformat() on an aware UTC datetime produces a
        `+00:00` string, and parsing that back should give the same
        datetime. This is the "engine wrote it, engine reads it"
        case and must be lossless."""
        original = datetime(2026, 7, 15, 9, 45, 30, tzinfo=UTC)
        parsed = _parse_scheduled_for(original.isoformat())
        assert parsed == original


# --------------------------------------------------------------------
# _compute_next_attempt_at
# --------------------------------------------------------------------

class TestComputeNextAttemptAt:
    """Exponential backoff with ±10% jitter. Jitter is sourced from
    `random.uniform(-0.1, 0.1)` — to get deterministic tests, we
    monkeypatch `random.uniform` on the worker module so the jitter
    is pinned for the duration of each test."""

    NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

    def _pin_jitter(self, monkeypatch, value: float):
        """Force `random.uniform(-0.1, 0.1)` to return `value` in the
        worker module's namespace."""
        import dossier_engine.worker as worker_mod
        monkeypatch.setattr(
            worker_mod.random, "uniform", lambda a, b: value
        )

    def test_first_attempt_with_zero_jitter(self, monkeypatch):
        """attempt_count=1, base=60, jitter=0 → delay = 60s.
        This is the baseline shape: first failure, retry in one base
        delay."""
        self._pin_jitter(monkeypatch, 0.0)
        result = _compute_next_attempt_at(1, 60, self.NOW)
        assert result == self.NOW + timedelta(seconds=60)

    def test_second_attempt_doubles(self, monkeypatch):
        """attempt_count=2, base=60 → delay = 60 * 2^1 = 120s."""
        self._pin_jitter(monkeypatch, 0.0)
        result = _compute_next_attempt_at(2, 60, self.NOW)
        assert result == self.NOW + timedelta(seconds=120)

    def test_third_attempt_quadruples(self, monkeypatch):
        """attempt_count=3, base=60 → delay = 60 * 2^2 = 240s."""
        self._pin_jitter(monkeypatch, 0.0)
        result = _compute_next_attempt_at(3, 60, self.NOW)
        assert result == self.NOW + timedelta(seconds=240)

    def test_max_positive_jitter(self, monkeypatch):
        """Jitter of +0.1 means the delay is 10% longer than the
        deterministic base. attempt_count=1, base=60 → 60 * 1.1 = 66s."""
        self._pin_jitter(monkeypatch, 0.1)
        result = _compute_next_attempt_at(1, 60, self.NOW)
        assert result == self.NOW + timedelta(seconds=66)

    def test_max_negative_jitter(self, monkeypatch):
        """Jitter of -0.1 means 10% shorter. 60 * 0.9 = 54s."""
        self._pin_jitter(monkeypatch, -0.1)
        result = _compute_next_attempt_at(1, 60, self.NOW)
        assert result == self.NOW + timedelta(seconds=54)

    def test_base_delay_zero(self, monkeypatch):
        """Zero base means zero delay regardless of attempt count —
        used for "retry immediately" workflows. The bug this caught
        (base_delay_seconds=0 clobbered by `x or 60`) is what made me
        write this test first."""
        self._pin_jitter(monkeypatch, 0.05)  # nonzero jitter still zeros out
        result = _compute_next_attempt_at(3, 0, self.NOW)
        assert result == self.NOW

    def test_custom_base_delay(self, monkeypatch):
        """Per-task override: base_delay=30 → attempt 2 gets 60s."""
        self._pin_jitter(monkeypatch, 0.0)
        result = _compute_next_attempt_at(2, 30, self.NOW)
        assert result == self.NOW + timedelta(seconds=60)

    def test_jitter_stays_within_bounds(self):
        """Without pinning, the real jitter should always produce a
        delay in [base * 0.9, base * 1.1]. Run enough iterations to
        catch any accidental widening of the bounds."""
        for _ in range(200):
            result = _compute_next_attempt_at(1, 100, self.NOW)
            delay = (result - self.NOW).total_seconds()
            assert 90.0 <= delay <= 110.0


# --------------------------------------------------------------------
# _is_task_due
# --------------------------------------------------------------------

def _task(content: dict | None):
    """Minimal EntityRow stub — `_is_task_due` only reads `.content`."""
    return SimpleNamespace(content=content)


class TestIsTaskDue:
    """`_is_task_due` decides whether a task candidate returned by
    the SQL-level poll is actually claimable right now. It checks
    both `scheduled_for` (the original schedule time) and
    `next_attempt_at` (the retry delay) — both must be <= now for
    the task to be due."""

    NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    PAST = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    FUTURE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)

    def test_empty_content_not_due(self):
        """A task row without content is structurally invalid — the
        phase returns not-due with a sentinel sort key so the caller
        can skip it without crashing on the missing .content."""
        is_due, _ = _is_task_due(_task(None), self.NOW)
        assert is_due is False

    def test_no_scheduled_for_no_retry_is_due(self):
        """A freshly-scheduled task with no times set at all is
        treated as immediately due."""
        is_due, _ = _is_task_due(_task({"status": "scheduled"}), self.NOW)
        assert is_due is True

    def test_scheduled_for_in_past_is_due(self):
        is_due, sort_key = _is_task_due(
            _task({"scheduled_for": self.PAST.isoformat()}), self.NOW,
        )
        assert is_due is True
        assert sort_key == self.PAST

    def test_scheduled_for_in_future_not_due(self):
        is_due, sort_key = _is_task_due(
            _task({"scheduled_for": self.FUTURE.isoformat()}), self.NOW,
        )
        assert is_due is False
        assert sort_key == self.FUTURE

    def test_scheduled_for_equals_now_is_due(self):
        """Boundary: `scheduled_for == now` means due. The code uses
        `> now` for the not-due check, which correctly treats `==`
        as due."""
        is_due, _ = _is_task_due(
            _task({"scheduled_for": self.NOW.isoformat()}), self.NOW,
        )
        assert is_due is True

    def test_next_attempt_in_future_blocks_claim(self):
        """Even if `scheduled_for` is ancient, a retry delay that
        hasn't elapsed yet means the task isn't claimable. This is
        the whole point of the retry delay field — it MUST gate
        claim, not just be observational."""
        is_due, _ = _is_task_due(
            _task({
                "scheduled_for": self.PAST.isoformat(),
                "next_attempt_at": self.FUTURE.isoformat(),
            }),
            self.NOW,
        )
        assert is_due is False

    def test_next_attempt_in_past_allows_claim(self):
        """Retry delay has elapsed — task is claimable. `scheduled_for`
        is also in the past, so both gates pass."""
        is_due, sort_key = _is_task_due(
            _task({
                "scheduled_for": self.PAST.isoformat(),
                "next_attempt_at": self.PAST.isoformat(),
            }),
            self.NOW,
        )
        assert is_due is True
        # The sort key should be next_attempt_at (retries sort ahead
        # of original-schedule ordering so they drain promptly).
        assert sort_key == self.PAST

    def test_sort_key_prefers_next_attempt_over_scheduled_for(self):
        """When both are set and past, the sort key is
        `next_attempt_at` — the retry-drain-priority rule."""
        nat_past = datetime(2026, 4, 15, tzinfo=UTC)
        sf_older = datetime(2026, 4, 1, tzinfo=UTC)
        is_due, sort_key = _is_task_due(
            _task({
                "scheduled_for": sf_older.isoformat(),
                "next_attempt_at": nat_past.isoformat(),
            }),
            self.NOW,
        )
        assert is_due is True
        assert sort_key == nat_past  # NOT sf_older

    def test_null_next_attempt_at_ignored(self):
        """A task that was never retried has `next_attempt_at: null`
        on freshly-requeued versions. The null must not accidentally
        mark the task as not-due."""
        is_due, _ = _is_task_due(
            _task({
                "scheduled_for": self.PAST.isoformat(),
                "next_attempt_at": None,
            }),
            self.NOW,
        )
        assert is_due is True

    def test_malformed_scheduled_for_defers_task(self, caplog):
        """Bug 12 end-to-end. A task row with a non-parseable
        ``scheduled_for`` must NOT fire. Before the fix the malformed
        string collapsed to None, the `is not None` guard short-
        circuited, and the task was classified as due-now. After the
        fix, ``_parse_scheduled_for`` returns ``datetime.max``, the
        ``> now`` comparison holds, and the task defers indefinitely.
        An error log surfaces the corruption so ops notices."""
        import logging
        with caplog.at_level(logging.ERROR, logger="dossier.worker"):
            is_due, sort_key = _is_task_due(
                _task({"scheduled_for": "definitely-not-an-iso-string"}),
                self.NOW,
            )
        assert is_due is False, (
            "Malformed scheduled_for must defer, not fire. This is "
            "the whole point of Bug 12's fix."
        )
        assert sort_key == datetime.max.replace(tzinfo=UTC)
        assert any(
            "definitely-not-an-iso-string" in r.getMessage()
            for r in caplog.records
        )

    def test_malformed_next_attempt_at_defers_task(self, caplog):
        """Same guarantee on the retry-delay field. A malformed
        ``next_attempt_at`` (e.g. from a legacy pre-isoformat write)
        must defer, not advance the retry."""
        import logging
        with caplog.at_level(logging.ERROR, logger="dossier.worker"):
            is_due, _ = _is_task_due(
                _task({
                    "scheduled_for": self.PAST.isoformat(),
                    "next_attempt_at": "garbage",
                }),
                self.NOW,
            )
        assert is_due is False
