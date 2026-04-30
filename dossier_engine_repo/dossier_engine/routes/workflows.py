"""
Workflow-introspection endpoint.

``GET /workflows`` — list every workflow plugin loaded in this
engine instance, with the metadata a generic frontend needs to
render a "pick your workflow" page: name, label, description,
version, and the path of the workflow's search endpoint (when the
plugin registers one).

Why this lives outside ``dossiers.py``: those endpoints are
dossier-instance-scoped (need a dossier_id), while this is a
workflow-scoped meta-endpoint. One file per concern keeps the
routes/ boundary tidy.

Why not a DB-backed fallback for "list dossiers" here. That work
belongs to the search endpoint each plugin already registers
(``/{workflow}/search`` by convention). A DB-side fallback would
silently drop the ``__acl__`` filtering ES does — fine for an
isolated POC but it's exactly the kind of difference that ships
to production by accident. The frontend should always hit the
plugin's search endpoint; if ES isn't running, the search returns
empty with a ``reason`` field and the frontend renders an
empty-state. One code path, one ACL story.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI

from ..auth import User
from ..plugin import PluginRegistry


def register(
    app: FastAPI,
    *,
    registry: PluginRegistry,
    get_user,
) -> None:
    """Register ``GET /workflows``."""

    @app.get(
        "/workflows",
        tags=["meta"],
        summary="List loaded workflow plugins",
        description=(
            "Returns one entry per workflow plugin currently loaded "
            "in this engine instance. The generic frontend uses this "
            "to populate the workflow-picker landing page; admin "
            "tools use it to know what's available without parsing "
            "configuration. The ``search_path`` field tells the "
            "frontend where to send search queries — the plugin "
            "registers its own search route, this endpoint just "
            "tells the frontend the path it chose."
        ),
    )
    async def list_workflows(
        user: User = Depends(get_user),
    ) -> list[dict[str, Any]]:
        out = []
        for plugin in registry.all_plugins():
            wf = plugin.workflow or {}
            # Inline the can_create_dossier activities so the frontend's
            # workflow picker / new-dossier flow doesn't need a second
            # round-trip per workflow. Each entry carries the qualified
            # activity name plus a label — same shape as
            # ``form-schema``'s ``activity_names`` field, filtered to
            # creation-capable activities only. Cheap to compute (just
            # walks the in-memory workflow YAML) and small to send.
            creation_activities = []
            for a in wf.get("activities", []) or []:
                if not a.get("can_create_dossier"):
                    continue
                if a.get("client_callable") is False:
                    # System activities like consumeException are
                    # never user-facing; exclude them from the picker.
                    continue
                creation_activities.append({
                    "name": a["name"],
                    "label": a.get("label", a["name"]),
                    "description": a.get("description"),
                })
            out.append({
                "name": plugin.name,
                "label": wf.get("label", plugin.name),
                "description": wf.get("description"),
                "version": wf.get("version"),
                # Convention: every plugin that registers a search
                # route mounts it at ``/{name}/dossiers`` — that's the
                # platform's standard search-endpoint shape. Surfaced
                # here so generic frontends don't need to hard-code
                # the convention per workflow. ``None`` if the plugin
                # didn't register a search route at all.
                "search_path": (
                    f"/{plugin.name}/dossiers"
                    if plugin.search_route_factory is not None
                    else None
                ),
                "creation_activities": creation_activities,
            })
        return out
