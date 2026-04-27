"""
Engine-owned exception-grant mechanism: validator + consume/retract
handlers.

Exception grants let a workflow's administrator authorize one-shot
legal bypass of the workflow-rules layer. This module provides:

* ``valideer_exception`` — the validator that enforces the
  one-logical-entity-per-activity uniqueness invariant, activity-field
  immutability across revisions, and the required ``status: active``
  / declared-activity-name submission rules.
* ``handle_consume_exception`` — side-effect handler that revises a
  ``system:exception`` with ``status: consumed``. Auto-invoked by the
  orchestrator when an activity ran thanks to a bypass.
* ``handle_retract_exception`` — admin-initiated handler that revises
  a ``system:exception`` with ``status: cancelled``. The admin supplies
  the exception in the used block; this handler flips its status.

All three are referenced by dotted path from the engine-provided
activity defs in ``dossier_engine.entities``
(``GRANT_EXCEPTION_ACTIVITY_DEF``, ``RETRACT_EXCEPTION_ACTIVITY_DEF``,
``CONSUME_EXCEPTION_ACTIVITY_DEF``). App boot auto-registers those
three activity defs on every plugin that declared ``exceptions:`` in
its workflow YAML, and wires these callables into the plugin's
registries — so they dispatch through the normal plugin-callable path,
no special-case logic needed in the pipeline.

Why engine-owned, not plugin-owned: the entire mechanism is workflow-
agnostic. The only pieces a workflow chooses are (1) whether
exceptions are available at all and (2) which roles may grant /
retract them. The YAML ``exceptions:`` block carries both. Everything
else — the entity type, the three activities, their validator, their
handlers, the bypass phase — was pure mechanism and has been moved
here as the natural home.
"""
from __future__ import annotations

from ..engine.context import ActivityContext, HandlerResult
from ..engine.errors import ActivityError


_ENTITY_TYPE = "system:exception"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


async def valideer_exception(context: ActivityContext) -> bool:
    """Enforce the invariants that make exceptions auditable.

    Rules:

    1. **Status must be ``active`` in the submission.** A missing
       status field gets persisted as an absent field because the
       engine's content-validation phase does not coerce defaults
       into stored content; allowing that would falsify PROV. The
       validator rejects both "missing" and "explicit non-active"
       with the same 422 pointing at the ``status: "active"``
       requirement.
    2. **Activity must be a non-empty string naming a declared
       workflow activity.** Typos create orphan exceptions that
       never match anything; catching at grant time is vastly
       nicer than silently-wrong runtime behavior.
    3. **At most one logical system:exception per (activity) in
       the dossier, across all time.** Subsequent grants for the
       same activity are revisions of the existing entity_id, not
       new entities. The exception's status history
       (``active`` → ``consumed`` → ``active`` → ``cancelled`` →
       ...) lives on a single logical entity per activity.
    4. **The ``activity`` field is immutable across revisions.** A
       revision whose content names a different activity than the
       parent version is rejected.

    Shared by grantException and retractException; the latter
    submits no generated block, so the no-op path at the top keeps
    it safe to share.
    """
    pending = context.get_used_row(_ENTITY_TYPE)
    if pending is None:
        # No exception in the submitted generated block — nothing to
        # validate. retractException takes this path always
        # (generates: []). grantException should never take it
        # because its activity_def has generates: [system:exception],
        # but the engine allows no-op activities and we don't want
        # to crash a validator on that edge case.
        return True

    raw_content = pending.content or {}

    # --- Rule 1: status = "active" -----------------------------------

    raw_status = raw_content.get("status")
    if raw_status != "active":
        if raw_status is None:
            detail = (
                "grantException requires status='active' in the "
                "submitted system:exception content (got no status "
                "field at all). The engine does not inject "
                "defaults: stored content must match what the "
                "granting agent explicitly asserted."
            )
        else:
            detail = (
                f"grantException must submit exceptions with "
                f"status='active' (got {raw_status!r}); use "
                f"retractException to cancel or let the engine "
                f"auto-consume when the exempted activity runs."
            )
        raise ActivityError(422, detail)

    # --- Rule 2: activity is a non-empty string ----------------------

    raw_activity = raw_content.get("activity")
    if not isinstance(raw_activity, str) or not raw_activity:
        raise ActivityError(
            422,
            "grantException requires an 'activity' field in the "
            "submitted system:exception content, naming the "
            "activity the exception grants a bypass for.",
        )

    # Normalize bare names (``trekAanvraagIn``) to their qualified
    # form (``oe:trekAanvraagIn``). The workflow's default prefix
    # mirrors the same lookup plugin.normalize does at plugin load
    # time for activity cross-refs.
    submitted_activity = raw_activity
    if ":" not in submitted_activity:
        from ..prov.activity_names import qualify
        try:
            from ..prov.namespaces import namespaces
            default_prefix = namespaces().default_workflow_prefix
        except (RuntimeError, ImportError):
            default_prefix = "oe"
        submitted_activity = qualify(submitted_activity, default_prefix)

    # Rule 2b: activity must be declared in the workflow.
    declared_names = {
        a["name"] for a in context._plugin.workflow.get("activities", [])
        if isinstance(a, dict) and a.get("name")
    }
    if submitted_activity not in declared_names:
        raise ActivityError(
            422,
            f"system:exception references unknown activity "
            f"{submitted_activity!r}. Declared activities: "
            f"{sorted(declared_names)}",
        )

    # --- Rules 3 + 4: uniqueness and activity-immutability ----------

    submitted_entity_id = getattr(pending, "entity_id", None)

    existing_latest = await context.repo.get_entities_by_type_latest(
        context.dossier_id, _ENTITY_TYPE,
    )

    for existing in existing_latest:
        if existing.entity_id == submitted_entity_id:
            # Revision of the same logical entity — check activity
            # immutability (Rule 4).
            existing_activity = (existing.content or {}).get("activity")
            if existing_activity and existing_activity != submitted_activity:
                raise ActivityError(
                    422,
                    f"Cannot change the activity an exception applies "
                    f"to across revisions. Parent version targets "
                    f"{existing_activity!r}, revision targets "
                    f"{submitted_activity!r}. To grant an exception "
                    f"for a different activity, submit a fresh "
                    f"system:exception (new entity_id) instead.",
                )
            continue

        # Different entity_id, same activity → Rule 3 violation.
        existing_activity = (existing.content or {}).get("activity")
        if existing_activity == submitted_activity:
            raise ActivityError(
                422,
                f"A system:exception for activity {submitted_activity!r} "
                f"already exists (entity_id={existing.entity_id}). "
                f"Revise that one instead of creating a new entity — "
                f"the exception's history (active → consumed / "
                f"cancelled → active → ...) lives on a single logical "
                f"entity per activity.",
            )

    return True


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_consume_exception(
    context: ActivityContext, content: dict | None,
) -> HandlerResult:
    """Side-effect handler auto-invoked after a bypass. Revises the
    used ``system:exception`` with ``status: consumed``.

    Content-preservation note: we spread ``**(row.content or {})``
    rather than reconstructing the fields explicitly. This preserves
    any plugin-specific fields a workflow might have added to the
    exception content (though the engine's ``Exception_`` model only
    declares the canonical four) while forcing just ``status``.

    Multi-cardinality means the handler must supply ``entity_id``
    and ``derived_from`` explicitly. The handler-identity resolver
    otherwise mints a fresh entity_id for multi types, which would
    create a parallel exception entity instead of revising, breaking
    the one-logical-entity-per-activity invariant.
    """
    row = context.get_used_row(_ENTITY_TYPE)
    if row is None:
        # Unreachable in practice — the orchestrator only injects
        # consumeException when check_exceptions populated the
        # trigger's used list. Log and no-op rather than crash:
        # this side-effect is engine-mechanical and must not take
        # down the user-facing activity that triggered it.
        return HandlerResult()

    return HandlerResult(generated=[{
        "type": _ENTITY_TYPE,
        "entity_id": row.entity_id,
        "derived_from": row.id,
        "content": {**(row.content or {}), "status": "consumed"},
    }])


async def handle_retract_exception(
    context: ActivityContext, content: dict | None,
) -> HandlerResult:
    """Admin-initiated cancel. Same revision shape as consume,
    but forces ``status: cancelled``.

    Raises 422 if no exception supplied — a retract with empty
    used is a client bug (they forgot to specify WHICH exception
    to cancel). A silent no-op would let the retract "succeed"
    without changing anything, worse than a loud failure.
    """
    row = context.get_used_row(_ENTITY_TYPE)
    if row is None:
        raise ActivityError(
            422,
            "retractException requires a system:exception in its "
            "used block (the exception being retracted).",
        )

    return HandlerResult(generated=[{
        "type": _ENTITY_TYPE,
        "entity_id": row.entity_id,
        "derived_from": row.id,
        "content": {**(row.content or {}), "status": "cancelled"},
    }])


# ---------------------------------------------------------------------------
# Plugin registration helper
# ---------------------------------------------------------------------------


def register_exception_activities_on_plugin(plugin) -> None:
    """Apply the ``exceptions:`` opt-in to a ``Plugin``.

    Reads ``plugin.workflow["exceptions"]`` and, if present, appends
    the three built-in activity defs to ``plugin.workflow["activities"]``
    (with allowed_roles / authorization.roles / default_role overlaid
    from the plugin's configured grant_allowed_roles /
    retract_allowed_roles), registers the ``system:exception`` entity
    type, and injects the engine-provided callables into the plugin's
    handler / validator registries so they dispatch via the normal
    plugin-callable path.

    Idempotent re-registration is NOT safe: calling twice would
    append the activity defs twice and crash the engine's duplicate-
    name check at plugin registration time. Callers should ensure
    this runs exactly once per plugin instance.

    Shared between the production path (``app.py``
    ``load_config_and_registry``) and test fixtures that construct
    plugins without going through config loading. Keeping the overlay
    as a helper function means there's one source of truth for how
    the ``exceptions:`` block translates into plugin state — tests
    can't drift from production behavior by forgetting to inject
    one of the callables.

    No-op if ``plugin.workflow`` lacks an ``exceptions:`` block or the
    block has neither ``grant_allowed_roles`` nor
    ``retract_allowed_roles``.
    """
    import copy

    # Imported here (not at module top) to avoid a circular import:
    # ``dossier_engine.entities`` is imported by modules that in
    # turn import the engine package at startup; pulling in the
    # activity def constants at module-import time would force that
    # chain to resolve in the wrong order for test fixtures that
    # import this helper directly.
    from ..entities import (
        Exception_,
        GRANT_EXCEPTION_ACTIVITY_DEF,
        RETRACT_EXCEPTION_ACTIVITY_DEF,
        CONSUME_EXCEPTION_ACTIVITY_DEF,
    )

    ex_cfg = plugin.workflow.get("exceptions") or {}
    grant_roles = ex_cfg.get("grant_allowed_roles") or []
    retract_roles = ex_cfg.get("retract_allowed_roles") or []
    if not (grant_roles or retract_roles):
        return

    # Register entity type.
    plugin.entity_models["system:exception"] = Exception_
    plugin.workflow.setdefault("entity_types", []).append({
        "type": "system:exception",
        "description": "Engine-provided exception-grant entity",
        "cardinality": "multiple",
        "revisable": True,
    })

    # grantException — overlay plugin-supplied roles onto the
    # engine-provided activity def.
    if grant_roles:
        g_def = copy.deepcopy(GRANT_EXCEPTION_ACTIVITY_DEF)
        g_def["allowed_roles"] = list(grant_roles)
        g_def["authorization"]["roles"] = [
            {"role": r} for r in grant_roles
        ]
        g_def["default_role"] = grant_roles[0]
        plugin.workflow.setdefault("activities", []).append(g_def)

    # retractException — parallel shape.
    if retract_roles:
        r_def = copy.deepcopy(RETRACT_EXCEPTION_ACTIVITY_DEF)
        r_def["allowed_roles"] = list(retract_roles)
        r_def["authorization"]["roles"] = [
            {"role": r} for r in retract_roles
        ]
        r_def["default_role"] = retract_roles[0]
        plugin.workflow.setdefault("activities", []).append(r_def)

    # consumeException — system-only, no role overlay. Always
    # registered whenever exceptions are opted in at all, regardless
    # of whether grant or retract roles are set (see note in
    # ``app.py`` about retract-only being degenerate but still
    # requiring consume for the auto-consume chain).
    plugin.workflow.setdefault("activities", []).append(
        copy.deepcopy(CONSUME_EXCEPTION_ACTIVITY_DEF)
    )

    # Inject engine-provided callables into the plugin registries
    # under their dotted-path keys. The engine dispatches by that
    # key at runtime (see ``build_callable_registries_from_workflow``
    # and the handler/validator phase lookups); wiring them in here
    # makes the engine-owned functions discoverable through the
    # normal plugin-callable path — no special case in the pipeline
    # is needed.
    plugin.validators[
        "dossier_engine.builtins.exceptions.valideer_exception"
    ] = valideer_exception
    plugin.handlers[
        "dossier_engine.builtins.exceptions.handle_consume_exception"
    ] = handle_consume_exception
    plugin.handlers[
        "dossier_engine.builtins.exceptions.handle_retract_exception"
    ] = handle_retract_exception
