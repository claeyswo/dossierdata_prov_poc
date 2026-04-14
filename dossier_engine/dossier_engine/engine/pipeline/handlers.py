"""
Handler invocation phase.

If the active activity has a handler registered, this phase calls it
with an `ActivityContext` carrying the resolved used entities. The
handler can:

* Return content for the activity's primary generated entity (the
  legacy single-content shape).
* Return a `HandlerResult` listing multiple generated entities, an
  override status, and/or task definitions to schedule.
* Return None — the activity proceeds with whatever the client sent.

What this phase does with the result:

1. **Append handler-generated entities into `state.generated`** so the
   downstream persistence phase writes them. This only happens if the
   client didn't already send `generated` items — handler output and
   client output are mutually exclusive (handler-generated activities
   are typically system-internal and the client wouldn't even try to
   supply content).
2. **Resolve auto-fill for `entity_id` and `derived_from`** so handlers
   can return tuples like `(type, content)` without having to know
   whether the type is singleton or multi-cardinality:
   * If the handler explicitly specified `entity_id` and
     `derived_from`, use them.
   * If the type is a singleton, find the existing entity (if any)
     and revise it — `entity_id` from the existing row, `derived_from`
     pointing at the current latest version. If no existing row,
     mint a fresh `entity_id`.
   * If the type is multi-cardinality, always mint a fresh
     `entity_id` and don't set `derived_from`.
3. **Stash the full `HandlerResult` on `state.handler_result`** so
   downstream phases (status determination, task scheduling) can read
   the handler's `status` override and appended `tasks`.

External entities returned by handlers are routed to
`state.generated_externals` instead of `state.generated`.
"""

from __future__ import annotations

from uuid import uuid4

from ..context import ActivityContext, HandlerResult
from ..state import ActivityState
from ._identity import resolve_handler_generated_identity


async def run_handler(state: ActivityState) -> None:
    """Invoke the activity's handler if one is registered.

    No-op for activities without a handler. Sets `state.handler_result`
    to the returned `HandlerResult` (or None) so downstream phases can
    read its status override and appended tasks.

    Reads:  state.activity_def, state.plugin, state.repo,
            state.dossier_id, state.resolved_entities, state.generated
    Writes: state.handler_result, state.generated,
            state.generated_externals
    """
    handler_name = state.activity_def.get("handler")
    if not handler_name:
        return

    handler_fn = state.plugin.handlers.get(handler_name)
    if handler_fn is None:
        return

    ctx = ActivityContext(
        repo=state.repo,
        dossier_id=state.dossier_id,
        used_entities=state.resolved_entities,
        entity_models=state.plugin.entity_models,
        plugin=state.plugin,
    )

    # The handler receives "client content" for the primary generated
    # entity (the legacy shape where the client's entire intent is
    # captured in one content dict). Handlers that don't care about
    # client content ignore this argument; handlers that do (e.g.
    # `set_dossier_access`) read it.
    client_content = state.generated[0]["content"] if state.generated else None
    state.handler_result = await handler_fn(ctx, client_content)

    if not isinstance(state.handler_result, HandlerResult):
        return

    # Handler-generated entities only land in `state.generated` if
    # the client didn't already supply some. The two are mutually
    # exclusive — handler-generated activities are typically system-
    # internal and the client wouldn't even try to supply content.
    if state.handler_result.generated and not state.generated:
        await _append_handler_generated(state, state.handler_result.generated)


async def _append_handler_generated(
    state: ActivityState, items: list[dict],
) -> None:
    """Normalize each handler-returned generated entry and append it
    to `state.generated` (or `state.generated_externals` for URIs).

    Three cases for entity identity:
    * `external` type with a `uri` content field → routed to
      `state.generated_externals`, persisted later as a `type=external`
      row by the persistence phase.
    * Type is a singleton → find the existing entity to revise; if
      none exists, mint a fresh entity_id. (See `_identity`.)
    * Type is multi-cardinality → always mint a fresh entity_id, no
      derivation. (See `_identity`.)
    """
    allowed_types = state.activity_def.get("generates", [])

    for gen_item in items:
        gen_type = gen_item.get("type")
        gen_content = gen_item.get("content")

        # External case: route to the externals list and skip the rest.
        # This must happen before the shared identity helper, which
        # only handles real entities.
        if (
            gen_type == "external"
            and isinstance(gen_content, dict)
            and "uri" in gen_content
        ):
            state.generated_externals.append(gen_content["uri"])
            continue

        identity = await resolve_handler_generated_identity(
            plugin=state.plugin,
            repo=state.repo,
            dossier_id=state.dossier_id,
            gen_item=gen_item,
            allowed_types=allowed_types,
        )
        if identity is None:
            continue

        state.generated.append({
            "version_id": uuid4(),
            "entity_id": identity.entity_id,
            "type": identity.gen_type,
            "content": gen_item["content"],
            "derived_from": identity.derived_from_id,
            "ref": None,
        })
