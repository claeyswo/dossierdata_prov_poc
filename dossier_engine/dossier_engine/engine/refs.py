"""
Entity reference parsing.

The engine uses a single canonical string format for entity references:

    prefix:type/entity_id@version_id

For example: `oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001`.

The `prefix:type` part is the entity type (e.g. `oe:aanvraag`). The
`entity_id` is the logical identity — every revision of the same
conceptual entity shares it. The `version_id` is the row identity —
each revision gets its own.

Anything that doesn't match this pattern is treated as an external URI
(e.g. `https://id.erfgoed.net/erfgoedobjecten/10001`). External URIs are
persisted as `type=external` entities so the PROV graph stays complete.
"""

from __future__ import annotations

import re
from uuid import UUID


ENTITY_REF_PATTERN = re.compile(
    r'^(?P<prefix>[a-z_]+:[a-z_]+)/(?P<id>[0-9a-f-]+)@(?P<version>[0-9a-f-]+)$'
)


def parse_entity_ref(ref: str) -> dict | None:
    """Parse a canonical entity reference into its components.

    Returns a dict with `prefix`, `id` (UUID), and `version` (UUID) on
    success. Returns None for anything that doesn't match — callers
    should treat None as "this is an external URI".
    """
    match = ENTITY_REF_PATTERN.match(ref)
    if match:
        return {
            "prefix": match.group("prefix"),
            "id": UUID(match.group("id")),
            "version": UUID(match.group("version")),
        }
    return None


def is_external_uri(ref: str) -> bool:
    """True if `ref` is not a canonical entity reference (so it must be
    an external URI like a URL or other identifier)."""
    return parse_entity_ref(ref) is None
