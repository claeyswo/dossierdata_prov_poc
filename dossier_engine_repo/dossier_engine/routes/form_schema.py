"""
Form-schema endpoint — drives the generic Vue frontend.

Returns everything a generic activity-form renderer needs to know:

* The activity's used slots (with the type, whether it's external
  free-text or internal entity-pick, optional auto-resolve, and any
  description).
* The activity's generated entity types (each with its Pydantic-derived
  JSON Schema, marked client-supplied or handler-supplied).
* The activity's deadline declarations, in case the frontend wants to
  surface them on the form (the eligibility entry already carries the
  resolved values; this is for completeness).
* The full list of declared activity names in the workflow — primarily
  for ``grantException``'s activity-name dropdown, but a generic
  feature.

The frontend's job is to render this JSON into a form. It needs no
domain knowledge about toelatingen, premies, or any other plugin —
the form-schema response is the contract.

Notes on shape choices:

* External vs internal slot: read from the YAML's ``external: true``
  on the slot. Already-existing flag, no schema changes needed.
* Pydantic JSON Schema: each entity type carries the full schema
  including ``$defs`` for nested models. JSONForms (or any equivalent
  JSON-Schema renderer) handles this natively.
* Activity-name list: included in every response. Adds a few hundred
  bytes per call; cheaper than a separate workflow-introspection
  endpoint and removes the need for the frontend to make two requests
  to render ``grantException``.
* Reference data hints: the YAML doesn't currently link entity-content
  fields to reference lists — that mapping is per-plugin convention.
  This endpoint exposes the available reference list names for
  frontends that want to display dropdowns. The frontend decides which
  list maps to which field via its own configuration (or, in a future
  iteration, via ``ui_schema:`` annotations in the workflow YAML).

Engine-provided activities (tombstone, grant/retract/consume Exception)
work the same way — they're activities like any other, with declared
``used`` / ``generates`` / validators, so the schema endpoint serves
them without special-casing. The frontend renders them the same way
it renders plugin activities.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from ..auth import User
from ..plugin import PluginRegistry


def register(
    app: FastAPI,
    *,
    registry: PluginRegistry,
    get_user,
) -> None:
    """Register the form-schema endpoint per workflow.

    Path: ``GET /{workflow}/activities/{activity_name}/form-schema``.
    Authenticated. Same auth gate as the other workflow-scoped utility
    endpoints — this isn't dossier-scoped data so it doesn't need
    role checking, just a "logged-in" gate to keep the workflow's
    activity surface from being enumerable by anonymous callers.
    """
    for plugin in registry.all_plugins():
        _register_for_plugin(app, plugin, get_user)


def _register_for_plugin(app: FastAPI, plugin, get_user) -> None:
    """Per-plugin closure to capture ``plugin`` cleanly. Routes are
    registered with the workflow name baked into the path."""
    workflow_name = plugin.name

    @app.get(
        f"/{workflow_name}/activities/{{activity_name}}/form-schema",
        tags=[workflow_name],
        summary=f"Form schema for a {workflow_name} activity",
    )
    async def get_form_schema(
        activity_name: str,
        user: User = Depends(get_user),
    ) -> dict[str, Any]:
        # ``find_activity_def`` accepts bare or qualified names —
        # ``oe:dienAanvraagIn`` and ``dienAanvraagIn`` both resolve.
        # Local-name matching is the same path the side-effect
        # executor uses, so behavior is consistent across the
        # surface area.
        act_def = plugin.find_activity_def(activity_name)
        if act_def is None:
            raise HTTPException(
                404,
                f"activity {activity_name!r} not found in workflow "
                f"{workflow_name!r}",
            )

        return _build_form_schema(plugin, act_def)


def _build_form_schema(plugin, act_def: dict) -> dict[str, Any]:
    """Assemble the JSON-serializable form-schema response.

    The shape is documented in the module docstring; treat it as a
    public API contract — the Vue frontend reads exactly these keys.
    """
    used = _build_used_slots(act_def)
    generates = _build_generates_entries(plugin, act_def)
    deadlines = _build_deadline_decls(act_def)
    activity_names = _list_client_callable_activities(plugin)
    reference_lists = _list_reference_lists(plugin)

    return {
        "name": act_def["name"],
        "label": act_def.get("label", act_def["name"]),
        "description": act_def.get("description"),
        "client_callable": act_def.get("client_callable", True),
        "allowed_roles": list(act_def.get("allowed_roles", [])),
        "default_role": act_def.get("default_role"),
        "can_create_dossier": bool(act_def.get("can_create_dossier")),
        "used": used,
        "generates": generates,
        "deadlines": deadlines,
        "activity_names": activity_names,
        "reference_lists": reference_lists,
    }


def _build_used_slots(act_def: dict) -> list[dict]:
    """Translate the YAML ``used:`` block into form-renderer entries.

    Each output entry tells the frontend exactly how to render the
    slot:

    * ``external: true`` → render a free-text input (typically an
      IRI / URL field). The user pastes a reference to an external
      vocabulary. We can validate format only, never existence.
    * ``external: false`` (or absent) AND no ``auto_resolve`` →
      render a dropdown of existing entities of that type in the
      current dossier. The frontend fetches the dossier and filters
      ``currentEntities`` by type to populate the dropdown.
    * ``auto_resolve`` set → hide the slot from the form; the engine
      resolves it itself. The frontend may still want to display
      the resolved entity in a "this will use" preview, so we
      surface the auto_resolve hint here.

    YAML-declared ``required`` flag (default true) propagates so
    the frontend can mark fields appropriately.
    """
    out: list[dict] = []
    for slot in act_def.get("used", []) or []:
        # Slots can be string shorthand ("oe:aanvraag") or full
        # dict; normalize to dict.
        if isinstance(slot, str):
            slot = {"type": slot}
        entry = {
            "type": slot.get("type"),
            "external": bool(slot.get("external", False)),
            "required": slot.get("required", True),
            "description": slot.get("description"),
            "auto_resolve": slot.get("auto_resolve"),
        }
        out.append(entry)
    return out


def _build_generates_entries(plugin, act_def: dict) -> list[dict]:
    """Translate the YAML ``generates:`` block into form-renderer
    entries with Pydantic-derived JSON Schema.

    Each entry tells the frontend whether the user fills in content
    (``client_supplied: true``, present a JSON-Schema form) or
    whether the activity's handler constructs it server-side
    (``client_supplied: false``, no form needed for this type —
    the user just needs to know the activity will produce one).

    Heuristic for client-supplied: an activity has a ``handler:``
    that produces this type → handler-supplied. Otherwise → client-
    supplied. The form-schema endpoint can't fully introspect what
    the handler does, but in our codebase the convention is clear:
    handlers either fully construct entities (``aanvullenAanvraag``,
    ``handle_consume_exception``) or modify dossier-level state
    (``setSystemFields``). When in doubt, we default to client-
    supplied — that produces a form, which is the safer wrong
    answer (the engine rejects unwanted client content with a 422,
    surfacing the misconfiguration; whereas hiding a needed form
    would leave the user stuck).

    JSON Schema is fetched from ``plugin.entity_models[type]``. If
    no model is registered for the type (system entities sometimes
    don't have plugin-side models when the plugin doesn't declare
    them explicitly), schema is ``None`` and the frontend should
    fall back to a permissive raw-JSON textarea or skip the form
    block entirely.
    """
    has_handler = bool(act_def.get("handler"))

    out: list[dict] = []
    for gen in act_def.get("generates", []) or []:
        # generates entries are usually bare strings ("oe:aanvraag")
        # but can be dicts with extra metadata.
        if isinstance(gen, str):
            type_ = gen
            extra: dict = {}
        else:
            type_ = gen.get("type")
            extra = gen

        model = plugin.entity_models.get(type_)
        schema = model.model_json_schema() if model is not None else None

        # Heuristic: handler-supplied if the activity has a handler.
        # See docstring for the rationale and trade-offs.
        client_supplied = not has_handler

        # Cardinality is what the frontend needs to decide how to
        # mint the entity_id at submit time. The rule:
        #   * single  → if an instance already exists in the
        #     dossier, the frontend must reuse its entity_id and
        #     declare derivedFrom (revision). If none exists, mint
        #     fresh (creation). The user doesn't pick — there's only
        #     one logical instance ever.
        #   * multiple → the user picks: revise an existing instance
        #     (reuse entity_id + derivedFrom) or create a new one
        #     (mint fresh entity_id, no derivedFrom). The form-schema
        #     doesn't enumerate existing instances; the frontend
        #     fetches them via /dossiers/{id}/entities/{type} when
        #     needed.
        # Defaults to "single" matching plugin.cardinality_of() —
        # also the platform default when a YAML doesn't declare it.
        cardinality = plugin.cardinality_of(type_)

        out.append({
            "type": type_,
            "client_supplied": client_supplied,
            "cardinality": cardinality,
            "schema": schema,
            "description": extra.get("description"),
        })
    return out


def _build_deadline_decls(act_def: dict) -> dict[str, Any]:
    """Surface declared not_before / not_after blocks as-is.

    The eligibility entry that drives the dossier view already
    carries the resolved ISO values — frontends don't need to
    re-resolve them from the form-schema. But raw declarations
    can be useful for an author-preview mode that renders forms
    outside an actual dossier. Returned with no resolution applied.
    """
    return {
        "not_before": (act_def.get("requirements") or {}).get("not_before"),
        "not_after": (act_def.get("forbidden") or {}).get("not_after"),
    }


def _list_client_callable_activities(plugin) -> list[dict]:
    """List every client-callable activity in the workflow.

    Used by ``grantException`` to populate the activity-name dropdown
    in its form (the user is asserting which activity the exception
    grants a bypass for, and that field is enum-like). Also used by
    the generic Vue frontend's "start new dossier" picker, which
    filters this list by the ``can_create_dossier`` flag — saving
    one form-schema fetch per activity at startup.

    Filtered to client-callable only because system-only activities
    (``setSystemFields``, ``setDossierAccess``, ``consumeException``)
    are never grantable as bypass targets in any meaningful way.
    """
    out = []
    for a in plugin.workflow.get("activities", []) or []:
        if a.get("client_callable") is False:
            continue
        out.append({
            "name": a["name"],
            "label": a.get("label", a["name"]),
            "can_create_dossier": bool(a.get("can_create_dossier")),
        })
    return out


def _list_reference_lists(plugin) -> list[str]:
    """Names of reference data lists this workflow exposes.

    Frontends use these to populate dropdowns for fields that are
    keyed against a reference list (bijlagetypes, documenttypes,
    etc.). The mapping from "this entity field uses reference list X"
    is currently a frontend convention — the workflow YAML doesn't
    declare it. A future iteration could add ``ui_schema`` to the
    YAML to make it explicit; until then, this list is just an
    enumeration of what's available.
    """
    return list((plugin.workflow.get("reference_data") or {}).keys())
