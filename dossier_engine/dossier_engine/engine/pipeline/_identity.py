"""
Shared identity resolution for handler-returned entities.

Both the main pipeline (`pipeline/handlers.py`) and the side-effect
pipeline (`pipeline/side_effects.py`) need to take a raw entity dict
returned by a handler — possibly missing `type`, `entity_id`, and
`derived_from` — and produce the canonical identity triple needed to
persist it. The rules are the same in both places:

1. **Type defaulting**: if the handler omitted `type`, fall back to
   the activity's `generates[0]`. If even that is empty, the item is
   unusable and we return None.
2. **Content presence**: an item with no `content` is unusable and
   we return None.
3. **Identity resolution**:
   * **Explicit override**: if the handler set `entity_id`, that's an
     explicit "I know what I'm doing" — use it as-is, with whatever
     `derived_from` (if any) the handler also supplied.
   * **Singleton revise-or-mint**: if the type is registered as a
     singleton, look up the existing instance. If found, the new
     entity reuses its `entity_id` and points `derived_from` at the
     current latest version. If not, mint a fresh entity_id with no
     derivation.
   * **Multi-cardinality fresh mint**: every handler call creates a
     new logical entity, no derivation.

The function returns the `ResolvedIdentity` namedtuple instead of a
plain tuple so callers can reference fields by name. Callers that
want to handle external URIs (which use a different code path
entirely) should detect them BEFORE calling this helper and route
externally — this function only handles real entities.
"""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID, uuid4

from ..lookups import lookup_singleton
from ...db.models import Repository
from ...plugin import Plugin


class ResolvedIdentity(NamedTuple):
    """The canonical identity triple for a handler-returned entity.

    `gen_type` may differ from the handler's declared type if the
    handler returned None and the function defaulted from
    `activity_def["generates"][0]`.
    """
    gen_type: str
    entity_id: UUID
    derived_from_id: UUID | None


async def resolve_handler_generated_identity(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    gen_item: dict,
    allowed_types: list[str],
) -> ResolvedIdentity | None:
    """Resolve a handler-returned `gen_item` into a canonical identity.

    Returns None if the item is unusable (no resolvable type, no
    content). Otherwise returns the ResolvedIdentity triple.

    Does NOT handle external URIs — callers that accept externals
    must detect and route them before invoking this function.
    Does NOT resolve schema_version — that's caller-specific because
    the activity_def carrying the version declarations differs (the
    main pipeline uses the activity_def of the running activity;
    side effects use the side-effect activity's def).
    """
    gen_type = gen_item.get("type")
    gen_content = gen_item.get("content")

    # Type defaulting from allowed_types[0].
    if gen_type is None and allowed_types:
        gen_type = allowed_types[0]
    if not (gen_type and gen_content):
        return None

    explicit_entity_id = gen_item.get("entity_id")
    explicit_derived_from = gen_item.get("derived_from")

    if explicit_entity_id is not None:
        # Handler took full responsibility for entity identity.
        entity_id_val = UUID(str(explicit_entity_id))
        derived_from_id = (
            UUID(str(explicit_derived_from))
            if explicit_derived_from else None
        )
    elif plugin.is_singleton(gen_type):
        # Singleton: revise the existing instance, or mint fresh.
        existing = await lookup_singleton(plugin, repo, dossier_id, gen_type)
        entity_id_val = existing.entity_id if existing else uuid4()
        derived_from_id = existing.id if existing else None
    else:
        # Multi-cardinality: every handler call creates a new
        # logical entity, no derivation.
        entity_id_val = uuid4()
        derived_from_id = None

    return ResolvedIdentity(
        gen_type=gen_type,
        entity_id=entity_id_val,
        derived_from_id=derived_from_id,
    )
