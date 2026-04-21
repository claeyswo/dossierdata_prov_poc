"""
Activity-execution endpoints — single, batch, and per-workflow typed.

Two URL families:

**Workflow-scoped** (the workflow is in the URL, no DB lookup needed
to resolve the plugin):

* ``PUT /{workflow}/dossiers/{id}/activities/{aid}/{type}`` — typed.
* ``PUT /{workflow}/dossiers/{id}/activities/{aid}`` — generic single.
* ``PUT /{workflow}/dossiers/{id}/activities`` — generic batch.

**Workflow-agnostic** (the engine resolves the workflow from the
dossier's DB row or from ``request.workflow`` on creation):

* ``PUT /dossiers/{id}/activities/{aid}`` — generic single.
* ``PUT /dossiers/{id}/activities`` — generic batch.

All call into the same ``execute_activity`` engine entry point.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException

from ..auth import User
from ..db import Repository, run_with_deadlock_retry
from ..engine import ActivityError, execute_activity
from ..plugin import Plugin, PluginRegistry
from ._errors import activity_error_to_http
from ._models import ActivityRequest, BatchActivityRequest, FullResponse
from ._typed_doc import build_activity_description


def register(
    app: FastAPI,
    *,
    registry: PluginRegistry,
    get_user,
    global_access,
) -> None:
    """Register activity execution endpoints on the FastAPI app.

    Each endpoint is registered at both URL families:

    * Workflow-agnostic: ``/dossiers/{did}/...``
    * Workflow-scoped: ``/{workflow}/dossiers/{did}/...`` (resolves
      plugin from the URL instead of from the body or DB)

    Plus per-workflow typed wrappers (workflow-scoped only).
    """

    # --- Shared handler logic (used by both URL families) ---

    async def _handle_single(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User,
        workflow_override: str | None = None,
    ):
        """Execute a single activity. If workflow_override is set
        (from the URL), it takes precedence over request.workflow."""
        wf = workflow_override or request.workflow
        if not request.type:
            raise HTTPException(
                422, detail="'type' is required on the generic endpoint",
            )
        plugin, act_def = _resolve_plugin_and_def(
            registry, request.type, wf,
        )

        async def _work(session):
            repo = Repository(session)
            return await _run_activity(
                repo=repo,
                plugin=plugin,
                act_def=act_def,
                dossier_id=dossier_id,
                activity_id=activity_id,
                user=user,
                role=request.role,
                used=request.used,
                generated=request.generated,
                relations=request.relations,
                remove_relations=request.remove_relations,
                workflow_name=wf,
                informed_by=request.informed_by,
            )

        return await run_with_deadlock_retry(_work)

    async def _handle_batch(
        dossier_id: UUID,
        request: BatchActivityRequest,
        user: User,
        workflow_override: str | None = None,
    ):
        """Execute a batch of activities atomically."""
        wf = workflow_override or request.workflow

        async def _work(session):
            repo = Repository(session)
            results = []
            for item in request.activities:
                plugin, act_def = _resolve_plugin_and_def(
                    registry, item.type, wf,
                )
                try:
                    response = await _run_activity(
                        repo=repo,
                        plugin=plugin,
                        act_def=act_def,
                        dossier_id=dossier_id,
                        activity_id=UUID(item.activity_id),
                        user=user,
                        role=item.role,
                        used=item.used,
                        generated=item.generated,
                        relations=item.relations,
                        remove_relations=item.remove_relations,
                        workflow_name=wf,
                        informed_by=item.informed_by,
                    )
                except HTTPException as e:
                    prefix = (
                        f"Activity '{item.type}' "
                        f"(#{len(results) + 1}) failed: "
                    )
                    if isinstance(e.detail, dict):
                        new_detail = {
                            **e.detail,
                            "detail": f"{prefix}{e.detail.get('detail', '')}",
                        }
                        raise HTTPException(e.status_code, detail=new_detail)
                    raise HTTPException(
                        e.status_code, detail=f"{prefix}{e.detail}",
                    )
                await repo.session.flush()
                results.append(response)
            return {
                "activities": results,
                "dossier": results[-1]["dossier"] if results else None,
            }

        # A deadlock anywhere in the batch retries the whole batch with
        # a fresh transaction. This matches the existing atomicity
        # contract — either all items commit or none do.
        return await run_with_deadlock_retry(_work)

    # --- Workflow-agnostic routes ---

    @app.put(
        "/dossiers/{dossier_id}/activities/{activity_id}",
        response_model=FullResponse,
        tags=["activities"],
        summary="Execute an activity",
    )
    async def put_activity(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User = Depends(get_user),
    ):
        return await _handle_single(dossier_id, activity_id, request, user)

    @app.put(
        "/dossiers/{dossier_id}/activities",
        tags=["activities"],
        summary="Execute multiple activities atomically",
    )
    async def execute_batch_activities(
        dossier_id: UUID,
        request: BatchActivityRequest,
        user: User = Depends(get_user),
    ):
        return await _handle_batch(dossier_id, request, user)

    # --- Workflow-scoped routes (registered per plugin so the
    # workflow name appears literally in the URL, not as a
    # {workflow} placeholder in the OpenAPI schema) ---

    for plugin in registry.all_plugins():
        _register_workflow_scoped_generic(
            app=app,
            workflow_name=plugin.name,
            handle_single=_handle_single,
            handle_batch=_handle_batch,
            get_user=get_user,
        )

    # --- Per-workflow typed wrappers ---

    for plugin in registry.all_plugins():
        workflow_name = plugin.name
        for act_def in plugin.workflow.get("activities", []):
            if act_def.get("client_callable") is False:
                continue
            _register_typed_route(
                app=app,
                registry=registry,
                get_user=get_user,
                workflow_name=workflow_name,
                act_name=act_def["name"],
                act_label=act_def.get("label", act_def["name"]),
                act_desc=build_activity_description(act_def, plugin),
            )


def _register_workflow_scoped_generic(
    *,
    app: FastAPI,
    workflow_name: str,
    handle_single,
    handle_batch,
    get_user,
) -> None:
    """Register generic single + batch activity routes for one workflow.

    The workflow name is baked into the URL literally (not as a
    path parameter) so the OpenAPI schema shows
    ``/toelatingen/dossiers/...`` instead of
    ``/{workflow}/dossiers/...``.
    """

    @app.put(
        f"/{workflow_name}/dossiers/{{dossier_id}}/activities/{{activity_id}}",
        response_model=FullResponse,
        tags=[workflow_name],
        summary="Execute an activity",
    )
    async def put_activity_scoped(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User = Depends(get_user),
    ):
        return await handle_single(
            dossier_id, activity_id, request, user,
            workflow_override=workflow_name,
        )

    put_activity_scoped.__name__ = f"put_activity_{workflow_name}"
    put_activity_scoped.__qualname__ = f"put_activity_{workflow_name}"

    @app.put(
        f"/{workflow_name}/dossiers/{{dossier_id}}/activities",
        tags=[workflow_name],
        summary="Execute multiple activities atomically",
    )
    async def execute_batch_scoped(
        dossier_id: UUID,
        request: BatchActivityRequest,
        user: User = Depends(get_user),
    ):
        return await handle_batch(
            dossier_id, request, user,
            workflow_override=workflow_name,
        )

    execute_batch_scoped.__name__ = f"batch_{workflow_name}"
    execute_batch_scoped.__qualname__ = f"batch_{workflow_name}"


def _register_typed_route(
    *,
    app: FastAPI,
    registry: PluginRegistry,
    get_user,
    workflow_name: str,
    act_name: str,
    act_label: str,
    act_desc: str,
) -> None:
    """Register one per-workflow typed route for `act_name`.

    ``act_name`` is the *qualified* activity name (e.g.
    ``oe:dienAanvraagIn``) and appears directly in the URL path
    segment. This mirrors the entity URL convention where type
    segments are also qualified (``/entities/oe:aanvraag/...``), so
    the platform has one consistent rule: type-like path segments
    always carry the full qualified name.

    A URL with a qualified name looks like::

        PUT /toelatingen/dossiers/{did}/activities/{aid}/oe:dienAanvraagIn

    FastAPI accepts colons in path segments without issue. Clients
    that would rather use bare names can use the generic endpoint
    (``PUT /{workflow}/dossiers/{did}/activities/{aid}``) with
    ``"type": "dienAanvraagIn"`` in the body — the engine qualifies
    that to ``oe:dienAanvraagIn`` before resolution.
    """

    @app.put(
        f"/{workflow_name}/dossiers/{{dossier_id}}/activities/{{activity_id}}/{act_name}",
        response_model=FullResponse,
        tags=[workflow_name],
        summary=act_label,
        description=act_desc,
    )
    async def endpoint(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User = Depends(get_user),
    ):
        # Stamp the qualified activity type on the request, so the
        # engine's resolve-plugin-and-def code sees the canonical
        # form regardless of what (if anything) the client supplied.
        request.type = act_name
        if not request.workflow:
            request.workflow = workflow_name

        plugin, act_def = _resolve_plugin_and_def(
            registry, act_name, workflow_name,
        )

        async def _work(session):
            repo = Repository(session)
            return await _run_activity(
                repo=repo,
                plugin=plugin,
                act_def=act_def,
                dossier_id=dossier_id,
                activity_id=activity_id,
                user=user,
                role=request.role,
                used=request.used,
                generated=request.generated,
                relations=request.relations,
                remove_relations=request.remove_relations,
                workflow_name=request.workflow,
                informed_by=request.informed_by,
            )

        return await run_with_deadlock_retry(_work)

    # FastAPI uses function name for route uniqueness. Strip the
    # colon from the name since it's invalid in Python identifiers.
    safe_name = act_name.replace(":", "_")
    endpoint.__name__ = f"typed_{workflow_name}_{safe_name}"
    endpoint.__qualname__ = f"typed_{workflow_name}_{safe_name}"


def _resolve_plugin_and_def(
    registry: PluginRegistry,
    activity_type: str,
    workflow_name: str | None,
) -> tuple[Plugin, dict]:
    """Find the plugin and activity definition for `activity_type`.

    ``activity_type`` can arrive in bare (``dienAanvraagIn``) or
    qualified (``oe:dienAanvraagIn``) form — both resolve to the
    same activity. The registry stores activities by their qualified
    name (guaranteed by ``_normalize_activity_names`` at plugin
    load), so we qualify the incoming value before lookup.

    Two paths:

    1. **Registered activity** — `registry.get_for_activity` knows
       which plugin owns this activity type. Returns immediately.
    2. **First-activity-on-new-dossier** — the activity hasn't been
       registered with the registry yet (because the dossier doesn't
       exist), so we fall back to the explicit `workflow_name` from
       the request body, look up the plugin, and walk its activity
       list for a matching `name`.

    Raises 404 if neither path resolves.
    """
    from ..activity_names import qualify

    qualified_type = qualify(activity_type)

    result = registry.get_for_activity(qualified_type)
    if result is not None:
        return result

    if not workflow_name:
        raise HTTPException(
            404, detail=f"Unknown activity type: {activity_type}",
        )

    plugin = registry.get(workflow_name)
    if plugin is None:
        raise HTTPException(
            404, detail=f"Unknown workflow: {workflow_name}",
        )

    for a in plugin.workflow.get("activities", []):
        if a["name"] == qualified_type:
            return plugin, a
    raise HTTPException(
        404, detail=f"Unknown activity: {activity_type}",
    )


async def _run_activity(
    *,
    repo: Repository,
    plugin: Plugin,
    act_def: dict,
    dossier_id: UUID,
    activity_id: UUID,
    user: User,
    role: str | None,
    used: list,
    generated: list,
    relations: list,
    remove_relations: list,
    workflow_name: str | None,
    informed_by: str | None,
) -> dict:
    """Call `execute_activity` with the standard argument set,
    forwarding any `ActivityError` to an `HTTPException` so FastAPI
    serializes it correctly.

    Centralizes the `[item.model_dump() for item in ...]` pattern
    that all three endpoints repeat — the engine takes plain dicts,
    not Pydantic models.

    Also the chokepoint for audit emission on writes: every activity
    execution emits exactly one audit event here. The two success
    actions (`dossier.created` for root, `dossier.updated` otherwise)
    differ by whether the activity is marked as dossier-creating in
    its workflow YAML — `can_create_dossier: true` means it's the
    entry-point activity that spawns a new dossier row (e.g.
    `dienAanvraagIn`). On authorization denial (`ActivityError` with
    code 403), we emit `dossier.denied` so SIEM sees both read-side
    denials (from `routes/access.py`) and write-side denials (from
    here) in one stream. Non-authorization errors (validation, 422,
    etc.) are not audited — those belong in the application log /
    Sentry, not the SIEM audit trail.
    """
    from ..audit import emit_audit

    is_root = bool(act_def.get("can_create_dossier"))
    action = "dossier.created" if is_root else "dossier.updated"

    try:
        result = await execute_activity(
            plugin=plugin,
            activity_def=act_def,
            repo=repo,
            dossier_id=dossier_id,
            activity_id=activity_id,
            user=user,
            role=role,
            used_items=[u.model_dump() for u in used],
            generated_items=[g.model_dump() for g in generated],
            relation_items=[r.model_dump(by_alias=True) for r in relations],
            remove_relation_items=[r.model_dump(by_alias=True) for r in remove_relations],
            workflow_name=workflow_name,
            informed_by=informed_by,
        )
    except ActivityError as e:
        # Write-side authorization denial: emit dossier.denied so this
        # shows up in the SIEM stream alongside read-side denials from
        # routes/access.py. Non-403 errors (validation, business rule
        # violations) are NOT audited — those are app-level concerns,
        # not security events.
        code = getattr(e, 'code', None)
        if code == 403:
            emit_audit(
                action="dossier.denied",
                actor_id=user.id,
                actor_name=user.name,
                target_type="Dossier",
                target_id=str(dossier_id),
                outcome="denied",
                dossier_id=str(dossier_id),
                reason=getattr(e, 'message', str(e)),
                activity_type=act_def.get("name"),
                activity_id=str(activity_id),
            )
        raise activity_error_to_http(e)

    # Success. One audit event per committed activity.
    emit_audit(
        action=action,
        actor_id=user.id,
        actor_name=user.name,
        target_type="Dossier",
        target_id=str(dossier_id),
        outcome="allowed",
        dossier_id=str(dossier_id),
        activity_type=act_def.get("name"),
        activity_id=str(activity_id),
    )
    return result
