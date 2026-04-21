"""
Markdown documentation generation for typed activity endpoints.

Each typed activity gets its own POST endpoint (e.g. `/dossiers/{id}/
activities/{aid}/dienAanvraagIn`). The OpenAPI description for each
of these endpoints is generated from the activity's YAML definition,
not hand-written, so the docs always match the workflow.

Two functions:

* `build_activity_description` — top-level renderer. Walks the
  activity definition and emits markdown sections for the description,
  authorization, requirements, used entities (with schemas), and
  generated entities (with schemas).
* `format_entity_schemas_for_doc` — helper that renders the JSON schema
  block(s) for a content-bearing entity type. For activities that
  declare version discipline via `entities.<type>`, it enumerates every
  version (new + allowed) and emits a labeled schema block per version.
  For legacy activities without version discipline, it emits a single
  unlabeled schema block.

Both functions return plain markdown strings the caller embeds in the
endpoint's `description` parameter at registration time.
"""

from __future__ import annotations

import json
import logging

from ..plugin import Plugin

_log = logging.getLogger("dossier.routes.typed_doc")


def build_activity_description(act_def: dict, plugin: Plugin) -> str:
    """Generate rich markdown description for Swagger docs, including
    entity schemas. See module docstring for the section layout."""
    desc = f"{act_def.get('description', '')}\n\n"

    # Authorization
    auth = act_def.get("authorization", {})
    roles = auth.get("roles", [])
    if roles:
        desc += "### Authorization\n"
        for r in roles:
            if isinstance(r, dict) and "role" in r:
                scope = r.get("scope")
                if scope:
                    desc += f"- `{r['role']}` scoped from `{scope['from_entity']}.{scope['field']}`\n"
                else:
                    desc += f"- `{r['role']}`\n"
            elif isinstance(r, dict) and "from_entity" in r:
                desc += f"- Entity-derived from `{r['from_entity']}.{r['field']}`\n"
            else:
                desc += f"- `{r}`\n"
        desc += "\n"

    # Requirements
    reqs = act_def.get("requirements", {})
    if any(reqs.get(k) for k in ["activities", "entities", "statuses"]):
        desc += "### Requirements\n"
        for a in reqs.get("activities", []):
            if a:
                desc += f"- Activity: `{a}`\n"
        for e in reqs.get("entities", []):
            if e:
                desc += f"- Entity: `{e}`\n"
        for s in reqs.get("statuses", []):
            if s:
                desc += f"- Status: `{s}`\n"
        desc += "\n"

    # Used entities with schemas
    used_defs = act_def.get("used", [])
    if used_defs:
        desc += "### Used entities\n"
        for u in used_defs:
            ext = " (external URI)" if u.get("external") else ""
            req = "**required**" if u.get("required") else "optional"
            auto = f", auto-resolve: `{u['auto_resolve']}`" if u.get("auto_resolve") else ""
            accept = u.get("accept", "any")
            desc += f"\n#### `{u['type']}` — {accept}, {req}{auto}{ext}\n"
            if u.get("description"):
                desc += f"{u['description']}\n"

            # Add entity schema if it's a content-bearing type
            if not u.get("external") and accept in ("new", "any"):
                entity_type = u.get("type", "")
                desc += format_entity_schemas_for_doc(
                    plugin, act_def, entity_type, context="used",
                )
        desc += "\n"

    # Generates with schemas
    generates = act_def.get("generates", [])
    if generates:
        desc += "### Generates\n"
        for g in generates:
            desc += f"\n#### `{g}`\n"
            desc += format_entity_schemas_for_doc(
                plugin, act_def, g, context="generates",
            )
        desc += "\n"

    return desc


def format_entity_schemas_for_doc(
    plugin: Plugin, act_def: dict, entity_type: str, context: str,
) -> str:
    """Render the schema section(s) for a content-bearing entity type
    on an activity, for inclusion in the OpenAPI summary.

    For activities that declare version discipline via `entities.<type>`:
      * `new_version` → "When creating a fresh entity: version X"
      * `allowed_versions` → "When revising an existing entity:
        accepts X, Y"
      * Each distinct version emits its own JSON schema block, labeled.

    For legacy activities (no `entities` block), emits a single
    unlabeled schema block from `entity_models[type]` — identical to
    pre-versioning behavior.
    """
    ecfg = (act_def.get("entities") or {}).get(entity_type) or {}
    new_version = ecfg.get("new_version")
    allowed_versions = list(ecfg.get("allowed_versions") or [])

    # Legacy path — no version discipline declared for this type.
    if not ecfg:
        model_class = plugin.resolve_schema(entity_type, None)
        if not model_class:
            return ""
        try:
            schema = model_class.model_json_schema()
        except Exception:
            # Pydantic couldn't emit a JSON schema for this model —
            # usually a forward-ref or recursive-type problem. Skipping
            # omits the block from API docs rather than breaking the
            # whole ``/docs`` page. Log so someone notices the gap when
            # they read the Sentry stream.
            _log.warning(
                "Could not render JSON schema for %s (legacy path); "
                "docs block will be omitted",
                entity_type, exc_info=True,
            )
            return ""
        out = f"\n**Content schema (`{entity_type}`):**\n"
        out += f"```json\n{json.dumps(schema, indent=2)}\n```\n"
        return out

    # Versioned path — enumerate.
    out = ""
    if context == "generates" and new_version:
        out += (
            f"\n**Fresh entities are stamped as version `{new_version}`.** "
            f"The engine inherits the parent's stored version on "
            f"revisions (sticky).\n"
        )
    if allowed_versions:
        pretty = ", ".join(f"`{v}`" for v in allowed_versions)
        out += (
            f"\n**This activity accepts existing entities at version(s): "
            f"{pretty}.** Revisions of entities at other versions are "
            f"rejected with `422 unsupported_schema_version`.\n"
        )

    # Collect every version we need to render a schema for
    # (deduped, ordered).
    versions_to_render: list[str] = []
    seen: set[str] = set()
    for v in ([new_version] if new_version else []) + allowed_versions:
        if v and v not in seen:
            versions_to_render.append(v)
            seen.add(v)

    for v in versions_to_render:
        model_class = plugin.resolve_schema(entity_type, v)
        if not model_class:
            continue
        try:
            schema = model_class.model_json_schema()
        except Exception:
            # Same failure shape as the legacy path above — Pydantic
            # couldn't render the schema. Log and skip this version's
            # block rather than break the whole ``/docs`` page.
            _log.warning(
                "Could not render JSON schema for %s @ %s; "
                "docs block will be omitted",
                entity_type, v, exc_info=True,
            )
            continue
        out += f"\n**Schema `{entity_type}` @ `{v}`:**\n"
        out += f"```json\n{json.dumps(schema, indent=2)}\n```\n"

    return out
