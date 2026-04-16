"""
Entity reference parsing and construction.

The engine uses a single canonical string format for entity references:

    prefix:type/entity_id@version_id

For example: `oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001`.

The `prefix:type` part is the entity type (e.g. `oe:aanvraag`, `system:task`).
The `entity_id` is the logical identity — every revision of the same
conceptual entity shares it. The `version_id` is the row identity —
each revision gets its own.

Anything that doesn't match this pattern is treated as an external URI
(e.g. `https://id.erfgoed.net/erfgoedobjecten/10001`). External URIs are
persisted as `type=external` entities so the PROV graph stays complete.

This module is the single source of truth for parsing and constructing
the canonical string form. Callers that need to build a ref from
components use ``EntityRef(...)`` and let its ``__str__`` render; callers
that need to parse a string use ``EntityRef.parse(s)`` which returns
``None`` for external URIs. No f-string concatenation of
``f"{type}/{eid}@{vid}"`` should live outside this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID


ENTITY_REF_PATTERN = re.compile(
    r'^(?P<prefix>[a-z_]+:[a-z_]+)/(?P<id>[0-9a-f-]+)@(?P<version>[0-9a-f-]+)$'
)


@dataclass(frozen=True)
class EntityRef:
    """A parsed, typed entity reference.

    Frozen (hashable) so refs can be used as dict keys and in sets —
    useful for the disjoint-set invariant and deduplication elsewhere.

    Fields:
        type       — the prefix:type (e.g. "oe:aanvraag").
        entity_id  — the logical UUID (stable across revisions).
        version_id — the row UUID (specific revision).

    Constructing:
        EntityRef(type="oe:aanvraag", entity_id=..., version_id=...)

    Rendering to the canonical string form:
        str(ref)  →  "oe:aanvraag/<eid>@<vid>"

    Parsing:
        EntityRef.parse("oe:aanvraag/...@...")  →  EntityRef or None
    """

    type: str
    entity_id: UUID
    version_id: UUID

    def __str__(self) -> str:
        return f"{self.type}/{self.entity_id}@{self.version_id}"

    @classmethod
    def parse(cls, ref: str | None) -> "EntityRef | None":
        """Parse a canonical entity reference.

        Returns an ``EntityRef`` on success, ``None`` for anything that
        doesn't match — callers should treat ``None`` as "this is an
        external URI or not a ref at all". Use ``is_external_uri`` for
        the boolean form if you only need the classification.

        Accepts ``None`` as an input and returns ``None`` — a small
        convenience for call sites that already handle "ref might be
        missing" and don't want to special-case the None check before
        calling parse.
        """
        if ref is None:
            return None
        match = ENTITY_REF_PATTERN.match(ref)
        if not match:
            return None
        return cls(
            type=match.group("prefix"),
            entity_id=UUID(match.group("id")),
            version_id=UUID(match.group("version")),
        )


# --- Top-level helpers ------------------------------------------------------


def is_external_uri(ref: str) -> bool:
    """True if ``ref`` is not a canonical entity reference (so it must
    be an external URI like a URL or other identifier)."""
    return EntityRef.parse(ref) is None
