"""
PROV export and visualization endpoints.

- GET /dossiers/{id}/prov          → PROV-JSON export
- GET /dossiers/{id}/prov/graph    → Interactive HTML visualization
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse

from ..auth import User
from ..db import get_session_factory, Repository
from ..db.models import ActivityRow, EntityRow, AssociationRow, UsedRow
from ..plugin import PluginRegistry
from .access import check_dossier_access, get_visibility_from_entry

from sqlalchemy import select


router = APIRouter(tags=["prov"])


def register_prov_routes(app, registry: PluginRegistry, get_user, global_access: list[dict] | None = None):
    """Register PROV export and visualization routes."""

    @app.get(
        "/dossiers/{dossier_id}/prov",
        tags=["prov"],
        summary="PROV-JSON export",
        description="Export the provenance graph for a dossier in PROV-JSON format. Filtered by dossier_access.",
    )
    async def get_prov_json(
        dossier_id: UUID,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            plugin = registry.get(dossier.workflow)
            prefix = "oe"

            # Check access + determine visibility
            access_entry = await check_dossier_access(repo, dossier_id, user, global_access)
            visible_types, activity_view_mode = get_visibility_from_entry(access_entry)

            # Load all data
            activities = await repo.get_activities_for_dossier(dossier_id)

            all_entities_result = await session.execute(
                select(EntityRow).where(EntityRow.dossier_id == dossier_id).order_by(EntityRow.created_at)
            )
            all_entities = list(all_entities_result.scalars().all())

            # Filter entities by visibility
            if visible_types is not None:
                all_entities = [e for e in all_entities if e.type in visible_types]

            # Load associations for all activities
            activity_ids = [a.id for a in activities]
            assoc_result = await session.execute(
                select(AssociationRow).where(AssociationRow.activity_id.in_(activity_ids))
            )
            all_associations = list(assoc_result.scalars().all())
            assoc_by_activity = {}
            for a in all_associations:
                assoc_by_activity.setdefault(a.activity_id, []).append(a)

            # Load used links
            used_result = await session.execute(
                select(UsedRow).where(UsedRow.activity_id.in_(activity_ids))
            )
            all_used = list(used_result.scalars().all())
            used_by_activity = {}
            for u in all_used:
                used_by_activity.setdefault(u.activity_id, []).append(u)

            # Entity lookup
            entity_by_id = {e.id: e for e in all_entities}

            # Build PROV-JSON
            prov = {
                "prefix": {
                    "prov": "http://www.w3.org/ns/prov#",
                    "xsd": "http://www.w3.org/2001/XMLSchema#",
                    prefix: f"https://data.vlaanderen.be/ns/{prefix}/",
                    "dossier": f"https://data.vlaanderen.be/id/dossier/{dossier_id}/",
                },
                "entity": {},
                "activity": {},
                "agent": {},
                "wasGeneratedBy": {},
                "used": {},
                "wasAssociatedWith": {},
                "wasAttributedTo": {},
                "wasDerivedFrom": {},
                "wasInformedBy": {},
                "actedOnBehalfOf": {},
            }

            # Filter activities by activity_view_mode
            visible_entity_ids = set(e.id for e in all_entities)
            if activity_view_mode != "all":
                filtered_activities = []
                for act in activities:
                    if activity_view_mode == "own":
                        assocs = assoc_by_activity.get(act.id, [])
                        if any(a.agent_id == user.id for a in assocs):
                            filtered_activities.append(act)
                    elif activity_view_mode == "related":
                        assocs = assoc_by_activity.get(act.id, [])
                        used = used_by_activity.get(act.id, [])
                        is_own = any(a.agent_id == user.id for a in assocs)
                        touches_visible = any(u.entity_id in visible_entity_ids for u in used)
                        if is_own or touches_visible:
                            filtered_activities.append(act)
                activities = filtered_activities

            # Agents (deduplicated)
            agents_seen = set()
            for assocs in assoc_by_activity.values():
                for assoc in assocs:
                    agent_key = f"{prefix}:agent/{assoc.agent_id}"
                    if agent_key not in agents_seen:
                        agents_seen.add(agent_key)
                        prov["agent"][agent_key] = {
                            "prov:label": assoc.agent_name or assoc.agent_id,
                            "prov:type": {"$": assoc.agent_type or "prov:Person", "type": "xsd:QName"},
                        }

            # Activities
            for act in activities:
                act_key = f"dossier:activiteit/{act.id}"
                act_data = {
                    f"{prefix}:type": act.type,
                }
                if act.started_at:
                    act_data["prov:startedAtTime"] = {
                        "$": act.started_at.isoformat(),
                        "type": "xsd:dateTime",
                    }
                if act.ended_at:
                    act_data["prov:endedAtTime"] = {
                        "$": act.ended_at.isoformat(),
                        "type": "xsd:dateTime",
                    }
                prov["activity"][act_key] = act_data

                # wasAssociatedWith
                for assoc in assoc_by_activity.get(act.id, []):
                    assoc_key = f"_:assoc_{assoc.id}"
                    prov["wasAssociatedWith"][assoc_key] = {
                        "prov:activity": act_key,
                        "prov:agent": f"{prefix}:agent/{assoc.agent_id}",
                        "prov:hadRole": {
                            "$": assoc.role,
                            "type": "xsd:string",
                        },
                    }

                # used
                for used in used_by_activity.get(act.id, []):
                    entity = entity_by_id.get(used.entity_id)
                    if entity:
                        entity_key = f"{prefix}:{entity.type}/{entity.entity_id}@{entity.id}"
                        used_key = f"_:used_{act.id}_{entity.id}"
                        prov["used"][used_key] = {
                            "prov:activity": act_key,
                            "prov:entity": entity_key,
                        }

                # wasInformedBy
                if act.informed_by:
                    inform_key = f"_:informed_{act.id}"
                    prov["wasInformedBy"][inform_key] = {
                        "prov:informedActivity": act_key,
                        "prov:informantActivity": f"dossier:activiteit/{act.informed_by}",
                    }

            # Entities
            for entity in all_entities:
                entity_key = f"{prefix}:{entity.type}/{entity.entity_id}@{entity.id}"
                entity_data = {
                    f"{prefix}:type": entity.type,
                    f"{prefix}:entityId": str(entity.entity_id),
                    f"{prefix}:versionId": str(entity.id),
                }
                if entity.created_at:
                    entity_data["prov:generatedAtTime"] = {
                        "$": entity.created_at.isoformat(),
                        "type": "xsd:dateTime",
                    }
                prov["entity"][entity_key] = entity_data

                # wasGeneratedBy (skip for external entities with no generating activity)
                if entity.generated_by:
                    gen_key = f"_:gen_{entity.id}"
                    prov["wasGeneratedBy"][gen_key] = {
                        "prov:entity": entity_key,
                        "prov:activity": f"dossier:activiteit/{entity.generated_by}",
                    }

                # wasAttributedTo
                if entity.attributed_to:
                    attr_key = f"_:attr_{entity.id}"
                    prov["wasAttributedTo"][attr_key] = {
                        "prov:entity": entity_key,
                        "prov:agent": f"{prefix}:agent/{entity.attributed_to}",
                    }

                # wasDerivedFrom
                if entity.derived_from:
                    parent = entity_by_id.get(entity.derived_from)
                    if parent:
                        parent_key = f"{prefix}:{parent.type}/{parent.entity_id}@{parent.id}"
                        deriv_key = f"_:deriv_{entity.id}"
                        prov["wasDerivedFrom"][deriv_key] = {
                            "prov:generatedEntity": entity_key,
                            "prov:usedEntity": parent_key,
                        }

            # Remove empty sections
            prov = {k: v for k, v in prov.items() if v}

            return prov

    @app.get(
        "/dossiers/{dossier_id}/prov/graph/timeline",
        tags=["prov"],
        summary="PROV graph visualization",
        description="Interactive visualization of the provenance graph.",
        response_class=HTMLResponse,
    )
    async def get_prov_graph(
        dossier_id: UUID,
        include_system_activities: bool = False,
        include_tasks: bool = False,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            plugin = registry.get(dossier.workflow)

            # Check access + determine visibility
            access_entry = await check_dossier_access(repo, dossier_id, user, global_access)
            visible_types, activity_view_mode = get_visibility_from_entry(access_entry)

            # Load all data
            activities = await repo.get_activities_for_dossier(dossier_id)

            all_entities_result = await session.execute(
                select(EntityRow).where(EntityRow.dossier_id == dossier_id).order_by(EntityRow.created_at)
            )
            all_entities = list(all_entities_result.scalars().all())

            # Filter entities by access visibility
            if visible_types is not None:
                all_entities = [e for e in all_entities if e.type in visible_types]

            activity_ids = [a.id for a in activities]
            assoc_result = await session.execute(
                select(AssociationRow).where(AssociationRow.activity_id.in_(activity_ids))
            )
            all_associations = list(assoc_result.scalars().all())
            assoc_by_activity = {}
            for a in all_associations:
                assoc_by_activity.setdefault(a.activity_id, []).append(a)

            used_result = await session.execute(
                select(UsedRow).where(UsedRow.activity_id.in_(activity_ids))
            )
            all_used = list(used_result.scalars().all())
            used_by_activity = {}
            for u in all_used:
                used_by_activity.setdefault(u.activity_id, []).append(u)

            # Build graph data for D3
            nodes = []
            edges = []
            node_ids = set()

            # Build set of system activity types (client_callable: false)
            system_activity_types = set()
            if plugin:
                for act_def in plugin.workflow.get("activities", []):
                    if act_def.get("client_callable") is False:
                        system_activity_types.add(act_def["name"])

            # Build set of activity IDs to skip (system activities)
            skipped_activity_ids = set()
            if not include_system_activities:
                for act in activities:
                    if act.type in system_activity_types:
                        skipped_activity_ids.add(act.id)

            # Skip completeTask activities and system:task entities unless include_tasks
            if not include_tasks:
                for act in activities:
                    if act.type == "systemAction":
                        skipped_activity_ids.add(act.id)
                all_entities = [e for e in all_entities if e.type != "system:task"]
            else:
                # When showing tasks, make sure completeTask isn't hidden by system activity filter
                skipped_activity_ids -= {act.id for act in activities if act.type == "systemAction"}

            # Apply activity_view access filtering
            visible_entity_version_ids = set(e.id for e in all_entities)

            if activity_view_mode != "all":
                for act in activities:
                    if act.id in skipped_activity_ids:
                        continue
                    visible = False
                    if activity_view_mode == "own":
                        for assoc in assoc_by_activity.get(act.id, []):
                            if assoc.agent_id == user.id:
                                visible = True
                                break
                    elif activity_view_mode == "related":
                        for used in used_by_activity.get(act.id, []):
                            if used.entity_id in visible_entity_version_ids:
                                visible = True
                                break
                        if not visible:
                            for assoc in assoc_by_activity.get(act.id, []):
                                if assoc.agent_id == user.id:
                                    visible = True
                                    break
                    if not visible:
                        skipped_activity_ids.add(act.id)

            # Only hide entities generated by SYSTEM-skipped activities (not access-skipped)
            if not include_system_activities:
                system_skipped = set(
                    act.id for act in activities if act.type in system_activity_types
                )
                all_entities = [e for e in all_entities if e.generated_by not in system_skipped]

            entity_by_id = {e.id: e for e in all_entities}

            # Add activities as nodes
            order_idx = 0
            for act in activities:
                if act.id in skipped_activity_ids:
                    continue

                act_id = f"act-{act.id}"
                nodes.append({
                    "id": act_id,
                    "label": act.type,
                    "type": "activity",
                    "order": order_idx,
                    "time": act.started_at.isoformat() if act.started_at else "",
                    "detail": f"Activity: {act.type}\nTime: {act.started_at.strftime('%Y-%m-%d %H:%M:%S') if act.started_at else 'n/a'}\nID: {act.id}",
                    "informed_by": str(act.informed_by) if act.informed_by else None,
                })
                node_ids.add(act_id)
                order_idx += 1

                # wasInformedBy edges
                if act.informed_by and str(act.informed_by) not in [str(s) for s in skipped_activity_ids]:
                    informed_str = str(act.informed_by)
                    if informed_str.startswith("urn:"):
                        # Cross-dossier: create a phantom node for the external activity
                        ext_act_id = f"ext-{informed_str}"
                        if ext_act_id not in node_ids:
                            # Extract short label from URI
                            parts = informed_str.split("/")
                            short_label = parts[-1][:12] + "..." if len(parts[-1]) > 12 else parts[-1]
                            nodes.append({
                                "id": ext_act_id,
                                "label": short_label,
                                "type": "external_activity",
                                "order": -1,
                                "time": "",
                                "detail": f"Cross-dossier activity\n{informed_str}",
                                "url": None,
                            })
                            node_ids.add(ext_act_id)
                        edges.append({
                            "source": ext_act_id,
                            "target": act_id,
                            "label": "wasInformedBy",
                            "type": "informed",
                        })
                    else:
                        # Local: edge to existing activity node
                        source_id = f"act-{informed_str}"
                        if source_id in node_ids:
                            edges.append({
                                "source": source_id,
                                "target": act_id,
                                "label": "wasInformedBy",
                                "type": "informed",
                            })

                # wasAssociatedWith edges
                for assoc in assoc_by_activity.get(act.id, []):
                    agent_id = f"agent-{assoc.agent_id}"
                    if agent_id not in node_ids:
                        nodes.append({
                            "id": agent_id,
                            "label": assoc.agent_name or assoc.agent_id,
                            "type": "agent",
                            "detail": f"Agent: {assoc.agent_name}\nType: {assoc.agent_type}\nID: {assoc.agent_id}",
                        })
                        node_ids.add(agent_id)
                    edges.append({
                        "source": agent_id,
                        "target": act_id,
                        "label": f"wasAssociatedWith ({assoc.role})",
                        "type": "associated",
                    })

                # used edges
                for used in used_by_activity.get(act.id, []):
                    entity = entity_by_id.get(used.entity_id)
                    if entity:
                        ent_id = f"ent-{entity.id}"
                        if ent_id not in node_ids:
                            entity_url = f"/dossiers/{dossier_id}/entities/{entity.type}/{entity.entity_id}/{entity.id}"
                            nodes.append({
                                "id": ent_id,
                                "label": entity.content.get("uri", entity.type) if entity.type == "external" and entity.content else entity.type,
                                "type": "entity",
                                "entity_type": entity.type,
                                "logical_id": str(entity.entity_id),
                                "time": entity.created_at.isoformat() if entity.created_at else "",
                                "url": entity_url,
                                "detail": f"Entity: {entity.type}\nLogical ID: {entity.entity_id}\nVersion: {entity.id}\nAttributed to: {entity.attributed_to or 'n/a'}",
                            })
                            node_ids.add(ent_id)
                        edges.append({
                            "source": ent_id,
                            "target": act_id,
                            "label": "used",
                            "type": "used",
                        })

            # Build version ordering per logical entity
            # Group entities by (type, logical_id) to determine version_order
            logical_groups = defaultdict(list)
            for entity in all_entities:
                key = f"{entity.type}:{entity.entity_id}"
                logical_groups[key].append(entity)

            # Sort each group by created_at
            for key in logical_groups:
                logical_groups[key].sort(key=lambda e: e.created_at or datetime.min)

            # Assign version_order and row_key
            entity_version_order = {}
            logical_row_keys = list(logical_groups.keys())
            for entity in all_entities:
                key = f"{entity.type}:{entity.entity_id}"
                group = logical_groups[key]
                version_idx = next(i for i, e in enumerate(group) if e.id == entity.id)
                entity_version_order[str(entity.id)] = {
                    "version_order": version_idx,
                    "row_index": logical_row_keys.index(key),
                    "total_rows": len(logical_row_keys),
                }

            # Add entities and wasGeneratedBy edges
            for entity in all_entities:
                ent_id = f"ent-{entity.id}"
                entity_url = f"/dossiers/{dossier_id}/entities/{entity.type}/{entity.entity_id}/{entity.id}"
                ver_info = entity_version_order.get(str(entity.id), {})

                if ent_id not in node_ids:
                    nodes.append({
                        "id": ent_id,
                        "label": entity.content.get("uri", entity.type) if entity.type == "external" and entity.content else entity.type,
                        "type": "entity",
                        "entity_type": entity.type,
                        "logical_id": str(entity.entity_id),
                        "version_order": ver_info.get("version_order", 0),
                        "row_index": ver_info.get("row_index", 0),
                        "total_rows": ver_info.get("total_rows", 1),
                        "time": entity.created_at.isoformat() if entity.created_at else "",
                        "url": entity_url,
                        "detail": f"Entity: {entity.type}\nLogical ID: {entity.entity_id}\nVersion: {entity.id}\nAttributed to: {entity.attributed_to or 'n/a'}",
                    })
                    node_ids.add(ent_id)
                else:
                    # Update existing node with version info
                    for n in nodes:
                        if n["id"] == ent_id:
                            n["version_order"] = ver_info.get("version_order", 0)
                            n["row_index"] = ver_info.get("row_index", 0)
                            n["total_rows"] = ver_info.get("total_rows", 1)
                            break

                # wasGeneratedBy (skip if generating activity is hidden or entity is external)
                if entity.generated_by and entity.generated_by not in skipped_activity_ids:
                    edges.append({
                        "source": f"act-{entity.generated_by}",
                        "target": ent_id,
                        "label": "wasGeneratedBy",
                        "type": "generated",
                    })

                # wasAttributedTo
                if entity.attributed_to:
                    attr_agent_id = f"agent-{entity.attributed_to}"
                    if attr_agent_id not in node_ids:
                        nodes.append({
                            "id": attr_agent_id,
                            "label": entity.attributed_to,
                            "type": "agent",
                            "detail": f"Agent: {entity.attributed_to}",
                        })
                        node_ids.add(attr_agent_id)
                    edges.append({
                        "source": attr_agent_id,
                        "target": ent_id,
                        "label": "wasAttributedTo",
                        "type": "attributed",
                    })

                # wasDerivedFrom (only if parent is visible)
                if entity.derived_from and entity.derived_from in entity_by_id:
                    parent_id = f"ent-{entity.derived_from}"
                    edges.append({
                        "source": parent_id,
                        "target": ent_id,
                        "label": "wasDerivedFrom",
                        "type": "derived",
                    })

            nodes_json = json.dumps(nodes)
            edges_json = json.dumps(edges)

            html = _build_graph_html(
                dossier_id=str(dossier_id),
                workflow=dossier.workflow,
                nodes_json=nodes_json,
                edges_json=edges_json,
            )

            return HTMLResponse(content=html)

    # Import and register the columns graph
    from .prov_columns import register_columns_graph
    register_columns_graph(app, registry, get_user, global_access)


def _build_graph_html(dossier_id: str, workflow: str, nodes_json: str, edges_json: str) -> str:
    """Build the interactive timeline PROV graph with horizontal entity version chains."""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PROV Timeline — Dossier {dossier_id}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; overflow: hidden; }}

#header {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 10;
    padding: 12px 20px; background: rgba(15, 23, 42, 0.95);
    border-bottom: 1px solid #334155;
    display: flex; align-items: center; gap: 16px;
}}
#header h1 {{ font-size: 16px; font-weight: 600; color: #f1f5f9; }}
#header .badge {{ font-size: 12px; padding: 2px 8px; border-radius: 4px; background: #1e3a5f; color: #93c5fd; }}

#tooltip {{
    position: fixed; padding: 10px 14px; background: rgba(30, 41, 59, 0.97);
    border: 1px solid #475569; border-radius: 8px; font-size: 12px;
    pointer-events: none; opacity: 0; transition: opacity 0.15s;
    max-width: 360px; white-space: pre-wrap; line-height: 1.5;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4); z-index: 20;
}}

#legend {{
    position: fixed; bottom: 16px; left: 16px; z-index: 10;
    padding: 12px 16px; background: rgba(30, 41, 59, 0.95);
    border: 1px solid #334155; border-radius: 8px; font-size: 11px;
}}
.legend-item {{ display: flex; align-items: center; gap: 8px; margin: 3px 0; }}
.legend-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
.legend-line {{ width: 20px; height: 2px; }}

svg {{ width: 100vw; height: 100vh; }}
.link {{ fill: none; stroke-opacity: 0.35; }}
.link-label {{ font-size: 8px; fill: #64748b; pointer-events: none; }}
.node-activity {{ fill: #3b82f6; stroke: #93c5fd; stroke-width: 2; }}
.node-entity {{ fill: #10b981; stroke: #6ee7b7; stroke-width: 1.5; cursor: pointer; }}
.node-entity:hover {{ fill: #059669; stroke: #a7f3d0; stroke-width: 2.5; }}
.node-agent {{ fill: #f59e0b; stroke: #fcd34d; stroke-width: 1.5; }}
.node-task {{ fill: #8b5cf6; stroke: #c4b5fd; stroke-width: 1.5; cursor: pointer; }}
.node-task:hover {{ fill: #7c3aed; stroke: #ddd6fe; stroke-width: 2.5; }}
.node-external_activity {{ fill: #6b7280; stroke: #9ca3af; stroke-width: 1.5; stroke-dasharray: 4,2; }}
.node-label {{ fill: #f1f5f9; pointer-events: none; text-anchor: middle; font-weight: 500; }}
.node-time {{ font-size: 9px; fill: #64748b; pointer-events: none; text-anchor: middle; }}
.row-label {{ font-size: 10px; fill: #475569; font-style: italic; }}
.timeline-line {{ stroke: #1e293b; stroke-width: 1; }}
</style>
</head>
<body>

<div id="header">
    <h1>PROV Timeline</h1>
    <span class="badge">{workflow}</span>
    <span class="badge">{dossier_id[:8]}…</span>
</div>

<div id="tooltip"></div>

<div id="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#3b82f6"></div> Activity</div>
    <div class="legend-item"><div class="legend-dot" style="background:#10b981"></div> Entity (click to view)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#8b5cf6"></div> Task</div>
    <div class="legend-item"><div class="legend-dot" style="background:#f59e0b"></div> Agent</div>
    <div class="legend-item"><div class="legend-dot" style="background:#6b7280;border:1px dashed #9ca3af"></div> External activity</div>
    <div style="margin-top:6px; border-top:1px solid #475569; padding-top:6px;">
    <div class="legend-item"><div class="legend-line" style="background:#60a5fa"></div> wasGeneratedBy</div>
    <div class="legend-item"><div class="legend-line" style="background:#f472b6"></div> used</div>
    <div class="legend-item"><div class="legend-line" style="background:#a78bfa"></div> wasInformedBy</div>
    <div class="legend-item"><div class="legend-line" style="background:#34d399;height:3px"></div> wasDerivedFrom</div>
    <div class="legend-item"><div class="legend-line" style="background:#fbbf24"></div> wasAssociatedWith</div>
    <div class="legend-item"><div class="legend-line" style="background:#fb923c"></div> wasAttributedTo</div>
    </div>
</div>

<svg id="graph"></svg>

<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>

const nodes = {nodes_json};
const edges = {edges_json};

const width = window.innerWidth;
const height = window.innerHeight;

const edgeColors = {{
    generated: "#60a5fa",
    used: "#f472b6",
    informed: "#a78bfa",
    derived: "#34d399",
    associated: "#fbbf24",
    attributed: "#fb923c",
}};

// ── Layout ──
const COL_SPACING = 180;
const ROW_SPACING = 55;
const MARGIN = {{ top: 80, left: 100, bottom: 40, right: 80 }};

const activityNodes = nodes.filter(n => n.type === "activity").sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
const entityNodes = nodes.filter(n => n.type === "entity");
const agentNodes = nodes.filter(n => n.type === "agent");

const numCols = activityNodes.length;
const ACTIVITY_Y = MARGIN.top + 60;

// Activities: evenly spaced left to right
const actXMap = {{}};
activityNodes.forEach((act, i) => {{
    act.fx = MARGIN.left + i * COL_SPACING;
    act.fy = ACTIVITY_Y;
    actXMap[act.id] = act.fx;
}});

// Entity rows: one row per logical entity (type + logical_id)
// Collect unique row keys from entity nodes
const rowKeys = [];
const rowKeySeen = new Set();
// Sort entities by their version_order to get consistent row assignment
const sortedEntities = [...entityNodes].sort((a, b) => (a.row_index ?? 0) - (b.row_index ?? 0));
sortedEntities.forEach(ent => {{
    const key = (ent.entity_type || "") + ":" + (ent.logical_id || ent.id);
    if (!rowKeySeen.has(key)) {{
        rowKeySeen.add(key);
        rowKeys.push(key);
    }}
}});

const ENTITY_BASE_Y = ACTIVITY_Y + 90;

// Position entities: row by logical entity, X by version_order
// Find the generating activity's X as the base position, then offset by version_order
const genEdges = edges.filter(e => e.type === "generated");

entityNodes.forEach(ent => {{
    const key = (ent.entity_type || "") + ":" + (ent.logical_id || ent.id);
    const rowIdx = rowKeys.indexOf(key);
    ent.fy = ENTITY_BASE_Y + rowIdx * ROW_SPACING;

    // Find which activity generated this version
    const genEdge = genEdges.find(e => e.target === ent.id);
    let baseX = MARGIN.left;
    if (genEdge) {{
        const sourceId = typeof genEdge.source === "string" ? genEdge.source : genEdge.source.id;
        baseX = actXMap[sourceId] ?? baseX;
    }}
    ent.fx = baseX;
}});

// Agents: above the timeline, positioned near their first activity
const agentPlaced = {{}};
const assocEdges = edges.filter(e => e.type === "associated");
const AGENT_Y = MARGIN.top - 10;

agentNodes.forEach(agent => {{
    const assoc = assocEdges.find(e => {{
        const sid = typeof e.source === "string" ? e.source : e.source.id;
        return sid === agent.id;
    }});
    let x = MARGIN.left;
    if (assoc) {{
        const tid = typeof assoc.target === "string" ? assoc.target : assoc.target.id;
        x = actXMap[tid] ?? x;
    }}
    const col = Math.round(x);
    agentPlaced[col] = (agentPlaced[col] || 0);
    agent.fx = x + agentPlaced[col] * 50;
    agent.fy = AGENT_Y;
    agentPlaced[col]++;
}});

// ── SVG ──
const svg = d3.select("#graph").attr("width", width).attr("height", height);
const defs = svg.append("defs");

Object.entries(edgeColors).forEach(([type, color]) => {{
    defs.append("marker")
        .attr("id", `arrow-${{type}}`)
        .attr("viewBox", "0 -4 8 8")
        .attr("refX", type === "derived" ? 52 : 30)
        .attr("refY", 0)
        .attr("markerWidth", 5)
        .attr("markerHeight", 5)
        .attr("orient", "auto")
        .append("path")
        .attr("d", "M0,-4L8,0L0,4")
        .attr("fill", color);
}});

const g = svg.append("g");
const zoom = d3.zoom().scaleExtent([0.15, 3]).on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

// ── Row labels ──
rowKeys.forEach((key, i) => {{
    const label = key.split(":")[0].replace("oe:", "");
    g.append("text")
        .attr("class", "row-label")
        .attr("x", MARGIN.left - 15)
        .attr("y", ENTITY_BASE_Y + i * ROW_SPACING + 4)
        .attr("text-anchor", "end")
        .text(label);
    // Row background line
    g.append("line")
        .attr("class", "timeline-line")
        .attr("x1", MARGIN.left - 10)
        .attr("y1", ENTITY_BASE_Y + i * ROW_SPACING)
        .attr("x2", MARGIN.left + (numCols - 1) * COL_SPACING + 10)
        .attr("y2", ENTITY_BASE_Y + i * ROW_SPACING)
        .attr("stroke-dasharray", "2,6")
        .attr("stroke-opacity", 0.3);
}});

// Activity timeline line
g.append("line")
    .attr("class", "timeline-line")
    .attr("x1", MARGIN.left - 20).attr("y1", ACTIVITY_Y)
    .attr("x2", MARGIN.left + (numCols - 1) * COL_SPACING + 20).attr("y2", ACTIVITY_Y)
    .attr("stroke-dasharray", "4,4").attr("stroke-opacity", 0.4);

// ── Edges ──
const link = g.append("g").selectAll("path").data(edges).join("path")
    .attr("class", "link")
    .attr("stroke", d => edgeColors[d.type] || "#475569")
    .attr("stroke-width", d => d.type === "derived" ? 2.5 : d.type === "informed" ? 2 : 1.2)
    .attr("stroke-dasharray", d => d.type === "attributed" ? "3,3" : null)
    .attr("marker-end", d => `url(#arrow-${{d.type}})`)
    .attr("fill", "none");

const linkLabel = g.append("g").selectAll("text").data(edges).join("text")
    .attr("class", "link-label")
    .text(d => d.label.split("\\n")[0])
    .style("opacity", 0);

// ── Nodes ──
const node = g.append("g").selectAll("g").data(nodes).join("g").attr("cursor", d => d.url ? "pointer" : "default");

node.each(function(d) {{
    const el = d3.select(this);
    if (d.type === "activity") {{
        el.append("rect").attr("x", -55).attr("y", -18).attr("width", 110).attr("height", 36).attr("rx", 8).attr("class", "node-activity");
    }} else if (d.type === "entity" && d.entity_type === "system:task") {{
        // Task entities: purple diamond shape (rotated square)
        el.append("rect").attr("x", -16).attr("y", -16).attr("width", 32).attr("height", 32).attr("rx", 4)
            .attr("class", "node-task").attr("transform", "rotate(45)");
    }} else if (d.type === "entity") {{
        el.append("rect").attr("x", -44).attr("y", -14).attr("width", 88).attr("height", 28).attr("rx", 14).attr("class", "node-entity");
    }} else if (d.type === "external_activity") {{
        // Cross-dossier activity: grey dashed rounded rect
        el.append("rect").attr("x", -50).attr("y", -16).attr("width", 100).attr("height", 32).attr("rx", 6).attr("class", "node-external_activity");
    }} else {{
        el.append("circle").attr("r", 14).attr("class", "node-agent");
    }}
}});

node.append("text")
    .attr("class", "node-label")
    .attr("dy", d => d.type === "entity" ? 4 : d.type === "agent" ? 5 : 5)
    .attr("font-size", d => d.type === "activity" ? "9px" : d.type === "entity" ? "8.5px" : "9px")
    .text(d => {{
        let label = d.label;
        if (label.startsWith("oe:")) label = label.slice(3);
        return label.length > 18 ? label.slice(0, 16) + "…" : label;
    }});

// Version number badge on entities
node.filter(d => d.type === "entity" && d.version_order !== undefined)
    .append("text")
    .attr("font-size", "7px")
    .attr("fill", "#94a3b8")
    .attr("text-anchor", "middle")
    .attr("dy", -18)
    .text(d => `v${{d.version_order + 1}}`);

// Time labels below activities
node.filter(d => d.type === "activity" && d.time)
    .append("text")
    .attr("class", "node-time")
    .attr("dy", 30)
    .text(d => {{
        const date = new Date(d.time);
        return date.toLocaleTimeString("nl-BE", {{ hour: "2-digit", minute: "2-digit", second: "2-digit" }});
    }});

// ── Click: open entity endpoint ──
node.filter(d => d.url).on("click", (event, d) => {{
    window.open(d.url, "_blank");
}});

// ── Tooltip + Highlight ──
const tooltip = d3.select("#tooltip");

function getConnected(d) {{
    // Find all edges connected to this node
    const connectedNodeIds = new Set([d.id]);
    const connectedEdgeIndices = new Set();
    edges.forEach((e, i) => {{
        const sid = typeof e.source === "string" ? e.source : e.source.id;
        const tid = typeof e.target === "string" ? e.target : e.target.id;
        if (sid === d.id || tid === d.id) {{
            connectedNodeIds.add(sid);
            connectedNodeIds.add(tid);
            connectedEdgeIndices.add(i);
        }}
    }});
    return {{ connectedNodeIds, connectedEdgeIndices }};
}}

node.on("mouseover", (event, d) => {{
    let html = d.detail || d.label;
    if (d.url) html += "\\n\\n(click to view entity)";
    tooltip.style("opacity", 1).html(html);

    const {{ connectedNodeIds, connectedEdgeIndices }} = getConnected(d);

    // Dim unconnected nodes
    node.transition().duration(150)
        .style("opacity", n => connectedNodeIds.has(n.id) ? 1 : 0.15);

    // Highlight connected edges, dim the rest
    link.transition().duration(150)
        .style("opacity", (e, i) => connectedEdgeIndices.has(i) ? 1 : 0.05)
        .attr("stroke-width", (e, i) => {{
            if (!connectedEdgeIndices.has(i)) return 1;
            return e.type === "derived" ? 4 : e.type === "informed" ? 3 : 2.5;
        }});

    // Show and enlarge connected edge labels
    linkLabel.transition().duration(150)
        .style("opacity", (e, i) => connectedEdgeIndices.has(i) ? 1 : 0)
        .attr("font-size", (e, i) => connectedEdgeIndices.has(i) ? "10px" : "8px")
        .attr("font-weight", (e, i) => connectedEdgeIndices.has(i) ? "bold" : "normal")
        .attr("fill", (e, i) => connectedEdgeIndices.has(i) ? "#e2e8f0" : "#64748b");
}})
.on("mousemove", (event) => {{
    tooltip.style("left", (event.clientX + 16) + "px").style("top", (event.clientY - 10) + "px");
}})
.on("mouseout", () => {{
    tooltip.style("opacity", 0);

    // Restore everything
    node.transition().duration(200).style("opacity", 1);
    link.transition().duration(200)
        .style("opacity", 0.35)
        .attr("stroke-width", d => d.type === "derived" ? 2.5 : d.type === "informed" ? 2 : 1.2);
    linkLabel.transition().duration(200)
        .style("opacity", 0)
        .attr("font-size", "8px")
        .attr("font-weight", "normal")
        .attr("fill", "#64748b");
}});

// ── Minimal simulation for edge resolution ──
const simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(edges).id(d => d.id).strength(0))
    .alphaDecay(0.5)
    .on("tick", () => {{
        link.attr("d", d => {{
            const sx = d.source.x, sy = d.source.y;
            const tx = d.target.x, ty = d.target.y;
            const dx = Math.abs(tx - sx), dy = Math.abs(ty - sy);

            if (d.type === "derived") {{
                // Horizontal arrow between versions in same row
                return `M${{sx + 44}},${{sy}} L${{tx - 44}},${{ty}}`;
            }}
            if (dy < 10) {{
                // Horizontal: gentle arc
                const mid = (sy + ty) / 2 - 25;
                return `M${{sx}},${{sy}} Q${{(sx+tx)/2}},${{mid}} ${{tx}},${{ty}}`;
            }}
            // Vertical or diagonal: straight
            return `M${{sx}},${{sy}} L${{tx}},${{ty}}`;
        }});
        linkLabel.attr("x", d => (d.source.x + d.target.x) / 2).attr("y", d => (d.source.y + d.target.y) / 2 - 5);
        node.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
    }});

// ── Fit to view ──
setTimeout(() => {{
    const bounds = g.node().getBBox();
    const bw = bounds.width || width;
    const bh = bounds.height || height;
    const midX = bounds.x + bw / 2;
    const midY = bounds.y + bh / 2;
    const scale = Math.min(0.9, 0.85 / Math.max(bw / width, bh / height));
    svg.transition().duration(600).call(
        zoom.transform,
        d3.zoomIdentity.translate(width / 2 - scale * midX, height / 2 - scale * midY).scale(scale)
    );
}}, 500);

</script>
</body>
</html>"""
