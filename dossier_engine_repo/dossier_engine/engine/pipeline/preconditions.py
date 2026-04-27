"""
Pre-execution phases — the steps that run before any state mutation.

These are the phases that decide whether the request can proceed at all,
or whether it should short-circuit (idempotent replay), be rejected
(authorization, workflow rules), or be promoted to a new dossier.

Keeping them in one module makes the entry path readable as a single
unit. Each function takes the `ActivityState` and either mutates it,
returns a value, or raises `ActivityError`.
"""

from __future__ import annotations

from .authorization import authorize_activity, validate_workflow_rules
from ..errors import ActivityError
from ..response import build_replay_response
from ..state import ActivityState


async def check_idempotency(state: ActivityState) -> dict | None:
    """Return a replay response if this activity_id has already run.

    PUTs in this API are fully idempotent. The client owns the
    activity_id, so the same request can be retried safely. If the id
    already exists in the database, we return a synthesized response
    describing the existing activity — the new request is rejected as
    a conflict only when the existing activity belongs to a different
    dossier or has a different type, which would mean the client
    reused an id by mistake.

    Reads:  state.repo, state.activity_id, state.dossier_id,
            state.activity_def, state.plugin, state.user
    Writes: nothing (orchestrator handles short-circuit)

    Returns the replay response dict if a replay is needed, or None if
    this is a fresh execution that should proceed through the rest of
    the pipeline.
    """
    existing = await state.repo.get_activity(state.activity_id)
    if existing is None:
        return None

    if existing.dossier_id != state.dossier_id:
        raise ActivityError(409, "Activity ID already exists for different dossier")
    # Match by local name to tolerate legacy rows stored with a bare
    # name (pre-qualification) being re-replayed after the engine
    # started normalizing names to qualified form.
    from ...prov.activity_names import local_name
    if local_name(existing.type) != local_name(state.activity_def["name"]):
        raise ActivityError(409, "Activity ID already exists with different type")

    return await build_replay_response(
        state.plugin, state.repo, state.dossier_id, existing, state.user,
    )


async def ensure_dossier(state: ActivityState) -> None:
    """Look up the dossier, or create it if this activity bootstraps one.

    Takes a row-level exclusive lock (SELECT ... FOR UPDATE) on the
    dossier row. This serializes concurrent activities against the
    same dossier — two API calls that both try to act on dossier X
    will execute one after the other, not in parallel. The lock is
    held for the duration of the enclosing transaction and released
    on commit/rollback, so other dossiers are unaffected.

    The lock is the optimistic-concurrency replacement: rather than
    requiring clients to pass a version/ETag and retrying on mismatch,
    the DB serializes at the natural boundary (the dossier) where
    activities genuinely conflict. Fine-grained row locks keep
    unrelated dossiers fully parallel.

    A dossier may not exist yet for the very first activity in its
    lifetime. The activity definition's `can_create_dossier` flag
    controls whether such a creation is allowed — only a small number
    of activities (typically the "submit" activity at the start of a
    workflow) are permitted to bring a dossier into being. The client
    must supply `workflow_name` so the engine knows which plugin's
    workflow the new dossier belongs to.

    Reads:  state.repo, state.dossier_id, state.activity_def,
            state.workflow_name
    Writes: state.dossier
    Raises: 404 if no dossier and the activity can't create one.
            400 if creation is allowed but `workflow_name` is missing.
    """
    state.dossier = await state.repo.get_dossier_for_update(state.dossier_id)
    if state.dossier is not None:
        return

    if not state.activity_def.get("can_create_dossier"):
        raise ActivityError(404, "Dossier not found")
    if not state.workflow_name:
        raise ActivityError(400, "workflow field required for first activity")

    state.dossier = await state.repo.create_dossier(
        state.dossier_id, state.workflow_name,
    )


async def authorize(state: ActivityState) -> None:
    """Run the activity's authorization block against the calling user.

    Delegates to `pipeline.authorization.authorize_activity`, which
    walks the activity's role config (direct match, scoped match, or
    entity-derived match) and decides whether the user qualifies.

    Reads:  state.plugin, state.activity_def, state.user, state.repo,
            state.dossier_id
    Writes: nothing
    Raises: 403 if the user is not authorized.
    """
    authorized, error = await authorize_activity(
        state.plugin, state.activity_def, state.user, state.repo, state.dossier_id,
    )
    if not authorized:
        raise ActivityError(403, error)


def resolve_role(state: ActivityState) -> None:
    """Validate or default the functional role recorded in the PROV
    `wasAssociatedWith` edge.

    The activity's `allowed_roles` lists the functional roles its
    operator can take (e.g. `oe:behandelaar`, `oe:aanvrager`). If the
    client didn't supply one, fall back to `default_role`, then to the
    first allowed role, then to a generic `"participant"`. If the
    client did supply one, it must be in the allowed list.

    Reads:  state.activity_def, state.role
    Writes: state.role
    Raises: 422 if the supplied role is not in `allowed_roles`.
    """
    allowed_roles = state.activity_def.get("allowed_roles", [])
    default_role = state.activity_def.get("default_role")

    if not state.role and default_role:
        state.role = default_role
    if not state.role and allowed_roles:
        state.role = allowed_roles[0]
    if not state.role:
        state.role = "participant"

    if allowed_roles and state.role not in allowed_roles:
        raise ActivityError(
            422, f"Role '{state.role}' not allowed. Allowed: {allowed_roles}",
        )


async def check_workflow_rules(state: ActivityState) -> None:
    """Verify the activity's structural preconditions (`requirements`,
    `forbidden`).

    Skipped on the very first activity of a brand-new dossier — there
    are no prior activities to satisfy `requirements.activities` and no
    status to check against `requirements.statuses`. Once the dossier
    has at least one activity (even a freshly-created one), all
    subsequent activities go through the full check.

    Also skipped when ``check_exceptions`` (the immediately preceding
    phase) found an active ``oe:exception`` that authorizes bypass of
    the workflow-rules layer. The exception's bypass is bypass-or-
    nothing: ``check_exceptions`` only flags ``state.exempted_by_exception``
    when the workflow rules would otherwise have failed, so a no-op
    skip here is always legitimate — there was nothing that would
    have passed to re-validate.

    Reads:  state.activity_def, state.repo, state.dossier_id,
            state.exempted_by_exception
    Writes: nothing
    Raises: 409 if any structural rule is violated (and no bypass).
    """
    is_bootstrap = state.activity_def.get("can_create_dossier")
    if is_bootstrap and not await state.repo.get_activities_for_dossier(state.dossier_id):
        return  # First activity of a new dossier — skip structural checks.

    # Exception bypass — the previous phase already established that
    # the rules would fail AND an active exception legally overrides
    # them. Skip the raise; side-effects will consume the exception
    # after persistence.
    if state.exempted_by_exception is not None:
        return

    valid, error = await validate_workflow_rules(
        state.activity_def, state.repo, state.dossier_id,
        plugin=state.plugin, now=state.now,
    )
    if not valid:
        raise ActivityError(409, error)
