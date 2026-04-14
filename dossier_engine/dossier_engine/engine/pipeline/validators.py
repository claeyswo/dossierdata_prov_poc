"""
Custom validator dispatch.

Activities can declare arbitrary plugin-defined validators in their
YAML `validators:` block. Each entry is `{name: ..., description: ...}`,
where `name` is a key into `plugin.validators`. The validator function
receives an `ActivityContext` with the resolved used entities and any
in-flight pending entities, and returns either:

* `None` or a truthy value → accepted.
* A falsy value (other than `None`) → rejected with 409.

Validators that need richer rejection control should raise
`ActivityError` directly with a custom payload — the simple boolean
return is a convenience for the common "is this request valid?" case.

This module owns the dispatch loop only. The validator implementations
live in the plugin (e.g. `dossier_toelatingen/validators/`).
"""

from __future__ import annotations

from ..context import ActivityContext
from ..errors import ActivityError
from ..state import ActivityState


async def run_custom_validators(state: ActivityState) -> None:
    """Invoke every validator the activity declares in its YAML block.

    Each validator receives an `ActivityContext` carrying the same
    state the handler will see — so validators can use the same
    typed-entity access pattern (`context.get_typed`) that handlers do.

    A validator can reject by raising `ActivityError` (full control
    over status code and payload) or by returning a falsy value (the
    engine wraps it in a generic 409 with the validator's name).

    Reads:  state.activity_def, state.plugin, state.repo,
            state.dossier_id, state.resolved_entities
    Writes: nothing
    Raises: 409 if any validator rejects, or whatever the validator
            itself raises.
    """
    for validator_def in state.activity_def.get("validators", []):
        validator_name = validator_def["name"]
        validator_fn = state.plugin.validators.get(validator_name)
        if validator_fn is None:
            continue

        ctx = ActivityContext(
            repo=state.repo,
            dossier_id=state.dossier_id,
            used_entities=state.resolved_entities,
            entity_models=state.plugin.entity_models,
            plugin=state.plugin,
        )
        result = await validator_fn(ctx)
        if result is not None and not result:
            raise ActivityError(
                409, f"Validator '{validator_name}' failed",
            )
