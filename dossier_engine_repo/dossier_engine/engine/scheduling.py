"""
Parsing for ``scheduled_for`` values on task declarations.

Four accepted forms:

* **Relative offset** — ``+20d``, ``+2h``, ``+45m``, ``+3w``, or the
  negative equivalents (``-7d`` for "seven days ago / before").
  Resolves against the activity's ``now`` timestamp. The most common
  YAML case — "20 days from when this activity runs". Both ``+`` and
  ``-`` signs are accepted; the worker's ``scheduled_for <= now``
  check handles already-past times (a past-dated task fires
  immediately on the next worker poll).

* **Absolute ISO 8601** — ``2026-05-01T12:00:00Z``,
  ``2026-05-01T12:00:00+00:00``. Useful when you genuinely know the
  wall-clock time (calibration dates, regulatory cutoffs).

* **Entity field reference** — a dict ``{from_entity, field}`` that
  reads an ISO datetime (or date-only) string from an entity already
  resolved for this activity. The entity must be in
  ``state.resolved_entities``, i.e. in this activity's ``used`` or
  ``generated`` block. Dot-notation paths like ``content.expires_at``
  work the same way as ``from_entity`` does in authorization and
  finalization.

* **Entity field + offset** — the same dict plus an ``offset`` key
  containing a relative offset string (``+20d`` / ``-7d``). Resolves
  to the field value shifted by the offset. Use this for "7 days
  before the permit expires" (``{from_entity: ..., field: ...,
  offset: "-7d"}``).

The two dict forms use the same ``from_entity``/``field`` idiom plugin
authors already know from authorization scopes, finalization status
mappings, and side-effect conditions.

For schedules that depend on more than one entity or need Python-level
computation, build the ``scheduled_for`` inside a handler and return
it as a pre-formatted ISO string. The DSL covers the common cases;
handlers cover everything else.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


# ``[+-]`` makes the sign mandatory — a bare ``20d`` would be
# ambiguous with entity-field paths and we want all relative forms to
# carry their sign explicitly.
_OFFSET_PATTERN = re.compile(
    r"^(?P<sign>[+-])(?P<value>\d+)(?P<unit>[mhdw])$"
)

_UNIT_KWARGS = {
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def _parse_offset(offset_str: str) -> timedelta:
    """Parse ``+20d`` / ``-7d`` / ``+45m`` etc. into a timedelta.

    Raises ValueError with a grammar-reminder message on any input
    that doesn't match the offset pattern. Keeps the error single-
    sourced: every scheduled_for code path that involves an offset
    ends up here.
    """
    m = _OFFSET_PATTERN.match(offset_str)
    if not m:
        raise ValueError(
            f"Invalid offset {offset_str!r}: expected a signed "
            f"duration like '+20d', '-7d', '+2h', '+45m', '+3w' "
            f"(units: m=minutes, h=hours, d=days, w=weeks; sign "
            f"is required)"
        )
    amount = int(m.group("value"))
    unit_key = _UNIT_KWARGS[m.group("unit")]
    delta = timedelta(**{unit_key: amount})
    return delta if m.group("sign") == "+" else -delta


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 datetime or date-only string into an
    aware UTC datetime.

    Accepts:
      - ``2026-05-01T12:00:00Z``
      - ``2026-05-01T12:00:00+00:00``
      - ``2026-05-01T12:00:00`` (naive → treated as UTC)
      - ``2026-05-01`` (date-only → midnight UTC)

    Raises ValueError on anything else. Used both for the absolute
    form and for reading datetime fields off entities.
    """
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)  # lets ValueError propagate as-is
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_datetime_from_entity(
    resolved_entities: Mapping[str, Any],
    entity_type: str,
    field_path: str,
    *,
    context_name: str = "scheduled_for",
) -> datetime:
    """Resolve an entity field to an aware UTC datetime.

    `resolved_entities` is a dict of entity_type → entity-like object
    (a persisted row, a ``_PendingEntity``, or — for deadlines — a
    freshly-fetched singleton row). Each has a ``.content`` dict we
    walk with dot notation, matching how authorization / finalization
    read entity fields.

    Accepts the same field-value shapes as ``_parse_iso``, plus
    an already-parsed ``datetime`` (in case a handler stuffed one
    directly into ``content`` before Pydantic serialized it). Every
    other shape (ints, None, missing field, missing entity) raises
    ValueError with a context-specific message so the 500 the engine
    wraps this in points the plugin author at the actual problem.

    `context_name` is woven into every error message — defaults to
    ``"scheduled_for"`` because that's the historical caller, but
    deadline resolution passes ``"not_after"`` / ``"not_before"`` so
    the error points to the rule the author actually wrote.
    """
    entity = resolved_entities.get(entity_type)
    if entity is None:
        raise ValueError(
            f"{context_name} references entity type {entity_type!r} "
            f"but it couldn't be resolved for this check. For "
            f"scheduled_for, the entity must be in the activity's "
            f"'used' or 'generated' block. For not_after/not_before, "
            f"the entity must be a declared singleton and must exist "
            f"in the dossier."
        )

    # Import locally to avoid a module-level circular (authorization
    # imports from the engine package, engine's pipeline depends on
    # scheduling).
    from .pipeline.authorization import _resolve_field

    content = getattr(entity, "content", None)
    if content is None:
        raise ValueError(
            f"{context_name} entity {entity_type!r} has no content "
            f"to read field {field_path!r} from"
        )

    raw = _resolve_field(content, field_path)
    if raw is None:
        raise ValueError(
            f"{context_name} field {field_path!r} on {entity_type!r} "
            f"is null or missing; a datetime is required"
        )

    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    if isinstance(raw, str):
        try:
            return _parse_iso(raw)
        except ValueError as e:
            raise ValueError(
                f"{context_name} field {field_path!r} on {entity_type!r} "
                f"is {raw!r}; expected an ISO 8601 datetime or date "
                f"(e.g. '2026-05-01T12:00:00Z' or '2026-05-01')"
            ) from e

    raise ValueError(
        f"{context_name} field {field_path!r} on {entity_type!r} is "
        f"{type(raw).__name__}; expected an ISO 8601 string"
    )


def resolve_scheduled_for(
    value: str | dict | None,
    now: datetime,
    resolved_entities: Mapping[str, Any] | None = None,
) -> str | None:
    """Resolve a ``scheduled_for`` task-field value to an ISO 8601
    string (or None for "immediately due").

    Accepts four forms:

    1. ``None`` / empty string → None.
    2. String with a sign prefix (``+20d`` / ``-7d``) → relative to
       ``now``.
    3. String ISO 8601 datetime → passes through (naive normalized
       to UTC).
    4. Dict ``{from_entity, field}`` (optionally with ``offset``)
       → reads the datetime from the entity and optionally shifts.

    `resolved_entities` only needs to be supplied when the caller
    might pass the dict form. Unit tests for the string forms can
    omit it. The task-scheduling phase always supplies
    ``state.resolved_entities``.

    Raises ValueError on any malformed value — a silent fallthrough
    would produce a task that's immediately due, which is rarely
    what the author intended. Callers (``_schedule_recorded_task``)
    wrap this in a 500 ``ActivityError`` so YAML typos fail loudly
    at activity execution.
    """
    if value is None:
        return None

    # Dict form — entity field reference, optionally with offset.
    if isinstance(value, dict):
        if resolved_entities is None:
            # A dict arrived but the caller isn't plumbing entities
            # through — programming error on the engine side.
            raise ValueError(
                f"scheduled_for dict form {value!r} requires "
                f"resolved_entities, but none were supplied"
            )
        try:
            entity_type = value["from_entity"]
            field_path = value["field"]
        except KeyError as e:
            raise ValueError(
                f"scheduled_for dict form requires 'from_entity' and "
                f"'field' keys; got {value!r}"
            ) from e
        base = _read_datetime_from_entity(
            resolved_entities, entity_type, field_path,
        )
        offset_str = value.get("offset")
        if offset_str:
            base = base + _parse_offset(offset_str)
        return base.isoformat()

    # From here the value must be a string.
    if not isinstance(value, str):
        raise ValueError(
            f"scheduled_for must be a string or a dict, got "
            f"{type(value).__name__}: {value!r}"
        )

    s = value.strip()
    if not s:
        return None

    # Relative offset form (signed).
    m = _OFFSET_PATTERN.match(s)
    if m:
        return (now + _parse_offset(s)).isoformat()

    # Absolute ISO 8601 form. Parse to validate. Preserve the
    # original string when it was already timezone-aware (so ``Z``
    # stays ``Z``, ``+02:00`` stays ``+02:00``), normalize when it
    # was naive so the stored string is always unambiguous.
    # We detect "aware" by parsing once and checking `tzinfo` on the
    # result — a cheaper heuristic like "does the string end in Z"
    # misses offset suffixes.
    try:
        s_for_parse = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(s_for_parse)
    except ValueError as e:
        raise ValueError(
            f"Invalid scheduled_for value {value!r}: expected an ISO "
            f"8601 datetime (e.g. '2026-05-01T12:00:00Z'), a signed "
            f"relative offset (e.g. '+20d', '-7d', '+2h', '+45m', "
            f"'+3w'), or a dict {{from_entity, field, offset?}}"
        ) from e

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return value


# --------------------------------------------------------------------
# Deadline resolution for workflow rules (not_after / not_before)
# --------------------------------------------------------------------


async def resolve_deadline(
    value: str | dict | None,
    plugin: "Any",
    repo: "Any",
    dossier_id: "Any",
    *,
    rule_name: str,
) -> datetime | None:
    """Resolve a workflow-rule deadline declaration to an aware UTC
    datetime (or None if the rule is absent).

    Used by ``validate_workflow_rules`` for ``forbidden.not_after`` and
    ``requirements.not_before``. Three accepted forms — **no relative
    offset from "now"**, because "now" at rule-evaluation time is not
    a meaningful anchor for a deadline (the deadline would slide every
    time the check ran).

    - Absolute ISO 8601 — ``"2026-12-31T23:59:59Z"``.
    - Entity field reference — ``{from_entity, field}``. The entity
      **must be a singleton type** (plugin.is_singleton). The engine
      fetches it via ``lookup_singleton``; if the dossier has no
      instance, the rule is treated as "no deadline applies" and this
      function returns None. Plugin validator rejects non-singleton
      types at startup so this path never has to defend against them.
    - Entity field + offset — ``{from_entity, field, offset}``. Same
      singleton lookup, then the field value is shifted by the signed
      offset (``+30d`` / ``-7d``).

    Returns ``None`` when:
      - ``value`` is None / empty (rule not declared).
      - The singleton entity isn't in the dossier yet (rule can't
        fire — no anchor to compute against). Callers treat this
        as "deadline rule not active".

    Raises ``ValueError`` when the declaration is malformed, when
    the referenced type isn't a singleton, or when the field is
    unparseable. ``validate_workflow_rules`` catches and wraps as a
    422 so plugin authors get a clear error.
    """
    if value is None:
        return None

    # Absolute ISO 8601 form — no entity lookup needed.
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Reject relative offsets explicitly: the grammar doesn't
        # include "+Nd from now" for deadlines.
        if _OFFSET_PATTERN.match(s):
            raise ValueError(
                f"Invalid {rule_name} value {value!r}: relative "
                f"offsets are not supported for deadlines (offset "
                f"from 'now' has no fixed meaning in a deadline "
                f"check). Use an absolute ISO 8601 datetime or a "
                f"{{from_entity, field, offset?}} dict."
            )
        try:
            return _parse_iso(s)
        except ValueError as e:
            raise ValueError(
                f"Invalid {rule_name} value {value!r}: expected an "
                f"ISO 8601 datetime (e.g. '2026-12-31T23:59:59Z') or "
                f"a dict {{from_entity, field, offset?}}"
            ) from e

    if not isinstance(value, dict):
        raise ValueError(
            f"{rule_name} must be a string or a dict, got "
            f"{type(value).__name__}: {value!r}"
        )

    # Dict form — entity field reference, optionally with offset.
    try:
        entity_type = value["from_entity"]
        field_path = value["field"]
    except KeyError as e:
        raise ValueError(
            f"{rule_name} dict form requires 'from_entity' and "
            f"'field' keys; got {value!r}"
        ) from e

    # Singletons-only enforcement. The plugin validator also rejects
    # non-singleton types at startup, but this runtime check protects
    # against the case where someone bypassed the validator (direct
    # Plugin construction in tests, workflow reload without restart,
    # etc.). The two layers are deliberate.
    if not plugin.is_singleton(entity_type):
        raise ValueError(
            f"{rule_name} references {entity_type!r}, which is "
            f"not a singleton. Only singleton entity types can be "
            f"used in deadline rules — for multi-cardinality types, "
            f"'which instance's deadline applies' has no answer."
        )

    from .lookups import lookup_singleton
    entity = await lookup_singleton(plugin, repo, dossier_id, entity_type)
    if entity is None:
        # Singleton not yet in the dossier → the deadline rule has
        # no anchor and can't fire. Return None; caller treats that
        # as "rule inactive for now". Plugins can still compose this
        # with other rules (e.g. requirements.entities) to gate
        # activities behind the existence of the entity.
        return None

    # Reuse the shared entity-datetime resolver — it already handles
    # the content / field / type-check path. Pass our rule_name as
    # context so errors point at the right rule.
    base = _read_datetime_from_entity(
        {entity_type: entity}, entity_type, field_path,
        context_name=rule_name,
    )

    offset_str = value.get("offset")
    if offset_str:
        base = base + _parse_offset(offset_str)
    return base
