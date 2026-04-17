"""
Workflow-scoped utility endpoints — reference data and validation.

* ``GET /{workflow}/reference/{list_name}`` — static reference lists
  (bijlagetypes, documenttypes, etc.) served from the plugin's YAML.
  Sub-millisecond, no DB hit, freely cacheable.

* ``POST /{workflow}/validate/{validator_name}`` — lightweight field
  validation between activities. Plugin-registered callables that
  check one thing (URI resolution, cross-field rules) without
  touching the activity pipeline.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..plugin import PluginRegistry, FieldValidator


def register(
    app: FastAPI,
    *,
    registry: PluginRegistry,
) -> None:
    """Register reference-data and validation endpoints."""

    @app.get(
        "/{workflow}/reference",
        tags=["reference"],
        summary="All reference data for a workflow",
        description=(
            "Returns every reference-data list defined in the "
            "workflow's YAML. One HTTP call populates all dropdowns."
        ),
    )
    async def get_all_reference_data(workflow: str):
        plugin = registry.get(workflow)
        if not plugin:
            raise HTTPException(404, detail=f"Unknown workflow: {workflow}")
        ref_data = plugin.workflow.get("reference_data", {})
        return ref_data

    @app.get(
        "/{workflow}/reference/{list_name}",
        tags=["reference"],
        summary="Reference data list",
        description=(
            "Returns a single reference-data list by name. "
            "Served from in-memory plugin config — sub-millisecond, "
            "no DB query."
        ),
    )
    async def get_reference_list(workflow: str, list_name: str):
        plugin = registry.get(workflow)
        if not plugin:
            raise HTTPException(404, detail=f"Unknown workflow: {workflow}")
        ref_data = plugin.workflow.get("reference_data", {})
        items = ref_data.get(list_name)
        if items is None:
            available = sorted(ref_data.keys()) if ref_data else []
            raise HTTPException(
                404,
                detail=f"No reference list '{list_name}' in workflow "
                       f"'{workflow}'. Available: {available}",
            )
        return {"items": items}

    # --- Validation endpoints ---

    # List endpoint (always generic — returns names).
    @app.get(
        "/{workflow}/validate",
        tags=["validation"],
        summary="List available validators",
        description=(
            "Returns the names of all field-level validators "
            "registered by this workflow's plugin."
        ),
    )
    async def list_validators(workflow: str):
        plugin = registry.get(workflow)
        if not plugin:
            raise HTTPException(404, detail=f"Unknown workflow: {workflow}")
        names = sorted(plugin.field_validators.keys())
        return {"validators": names}

    # Per-validator typed routes — registered at startup so each
    # validator gets its own OpenAPI schema with request/response
    # models, summary, and description.
    for plugin in registry.all_plugins():
        workflow_name = plugin.name
        for validator_name, validator_entry in plugin.field_validators.items():
            _register_validator_route(
                app=app,
                workflow_name=workflow_name,
                validator_name=validator_name,
                validator_entry=validator_entry,
            )


def _register_validator_route(
    *,
    app: FastAPI,
    workflow_name: str,
    validator_name: str,
    validator_entry,
) -> None:
    """Register one typed validation endpoint with proper OpenAPI
    schema. If the entry is a bare callable (legacy), falls back
    to generic dict input/output."""
    from ..plugin import FieldValidator
    import inspect

    if isinstance(validator_entry, FieldValidator):
        fv = validator_entry
        fn = fv.fn
        req_model = fv.request_model
        resp_model = fv.response_model
        summary = fv.summary or f"Validate {validator_name}"
        description = fv.description or ""
    else:
        fn = validator_entry
        req_model = None
        resp_model = None
        summary = f"Validate {validator_name}"
        description = ""

    # Capture fn via closure (not default arg, which leaks into
    # the OpenAPI schema as a non-serializable default).
    _fn = fn

    if req_model:
        async def endpoint(body):
            return await _fn(body.model_dump())

        endpoint.__annotations__ = {"body": req_model, "return": resp_model or dict}
    else:
        async def endpoint(body: dict):
            return await _fn(body)

    endpoint.__name__ = f"validate_{workflow_name}_{validator_name}"
    endpoint.__qualname__ = f"validate_{workflow_name}_{validator_name}"

    kwargs = {
        "tags": [workflow_name],
        "summary": summary,
        "description": description,
    }
    if resp_model:
        kwargs["response_model"] = resp_model

    app.post(
        f"/{workflow_name}/validate/{validator_name}",
        **kwargs,
    )(endpoint)
