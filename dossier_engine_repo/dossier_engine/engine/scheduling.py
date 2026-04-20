"""
Parsing for ``scheduled_for`` values on task declarations.

Two accepted forms:

* **Relative offset** — ``+20d``, ``+2h``, ``+45m``, ``+3w``.
  Resolves against the activity's ``now`` timestamp. Useful for
  YAML task declarations that want "20 days from when this
  activity runs". The ``+`` prefix is required and unambiguous
  (a bare ``20d`` would be confusing).

* **Absolute ISO 8601** — ``2026-05-01T12:00:00Z``,
  ``2026-05-01T12:00:00+00:00``. Useful when you genuinely know
  the wall-clock time (calibration dates, regulatory cutoffs).

For anything that depends on entity content — "30 days after the
aanvraag's registration date" — compute the deadline in a handler
and return it in ``HandlerResult.tasks[0]["scheduled_for"]`` as an
ISO string. YAML templating over entity fields is deliberately not
supported; handlers have full Python and real types.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


_OFFSET_PATTERN = re.compile(
    r"^\+(?P<value>\d+)(?P<unit>[mhdw])$"
)

_UNIT_KWARGS = {
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def resolve_scheduled_for(
    value: str | None, now: datetime,
) -> str | None:
    """Resolve a ``scheduled_for`` task-field value.

    Returns an ISO 8601 datetime string suitable for storage in the
    task entity's JSON content, or None if ``value`` is None / empty.

    * ``None`` / empty → None (task is immediately due).
    * Relative offset (``+20d``, ``+45m``) → ``(now + delta).isoformat()``.
    * ISO 8601 absolute → returned as-is (the worker parses it at
      dispatch time via ``_parse_scheduled_for``).

    Raises ValueError on a malformed value — a silent fallthrough
    would produce a task that's immediately due, which is rarely
    what the author intended.
    """
    if not value:
        return None

    s = value.strip()
    if not s:
        return None

    # Relative offset form.
    m = _OFFSET_PATTERN.match(s)
    if m:
        amount = int(m.group("value"))
        unit_key = _UNIT_KWARGS[m.group("unit")]
        delta = timedelta(**{unit_key: amount})
        return (now + delta).isoformat()

    # Absolute ISO 8601 form. Parse to validate, but return the
    # original string to preserve whatever timezone suffix the
    # author wrote (worker normalizes at parse time).
    iso = s
    if iso.endswith("Z"):
        iso_check = iso[:-1] + "+00:00"
    else:
        iso_check = iso
    try:
        dt = datetime.fromisoformat(iso_check)
    except ValueError as e:
        raise ValueError(
            f"Invalid scheduled_for value {value!r}: expected an ISO "
            f"8601 datetime (e.g. '2026-05-01T12:00:00Z') or a "
            f"relative offset (e.g. '+20d', '+2h', '+45m', '+3w')"
        ) from e

    # Naive datetimes are treated as UTC; return a normalized form
    # so downstream code doesn't have to handle naive again.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return value
