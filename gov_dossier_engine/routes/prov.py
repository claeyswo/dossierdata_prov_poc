"""
PROV export and visualization endpoints.

- GET /dossiers/{id}/prov          → PROV-JSON export
- GET /dossiers/{id}/prov/graph    → Interactive HTML visualization
"""

from __future__ import annotations

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse

from ..auth import User
from ..db import get_session_factory, Repository
from ..db.models import ActivityRow, EntityRow, AssociationRow, UsedRow
from ..plugin import PluginRegistry

from sqlalchemy import select


router = APIRouter(tags=["prov"])


def register_prov_routes(app, registry: PluginRegistry, get_user):
    """Register PROV export and visualization routes."""

    @app.get(
        "/dossiers/{dossier_id}/prov",
        tags=["prov"],
        summary="PROV-JSON export",
        description="Export the full provenance graph for a dossier in PROV-JSON format.",
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

            # Load all data
            activities = await repo.get_activities_for_dossier(dossier_id)

            all_entities_result = await session.execute(
                select(EntityRow).where(EntityRow.dossier_id == dossier_id).order_by(EntityRow.created_at)
            )
            all_entities = list(all_entities_result.scalars().all())

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

                # wasGeneratedBy
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
        "/dossiers/{dossier_id}/prov/graph",
        tags=["prov"],
        summary="PROV graph visualization",
        description="Interactive visualization of the full provenance graph.",
        response_class=HTMLResponse,
    )
    async def get_prov_graph(
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

            # Load all data
            activities = await repo.get_activities_for_dossier(dossier_id)

            all_entities_result = await session.execute(
                select(EntityRow).where(EntityRow.dossier_id == dossier_id).order_by(EntityRow.created_at)
            )
            all_entities = list(all_entities_result.scalars().all())

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

            entity_by_id = {e.id: e for e in all_entities}

            # Build graph data for D3
            nodes = []
            edges = []
            node_ids = set()

            # Color scheme
            # Activities: blue, Entities: green, Agents: orange

            # Add activities as nodes
            for idx, act in enumerate(activities):
                act_id = f"act-{act.id}"
                label = act.type
                if act.started_at:
                    label += f"\n{act.started_at.strftime('%H:%M:%S')}"
                nodes.append({
                    "id": act_id,
                    "label": act.type,
                    "type": "activity",
                    "order": idx,
                    "time": act.started_at.isoformat() if act.started_at else "",
                    "detail": f"Activity: {act.type}",
                    "informed_by": str(act.informed_by) if act.informed_by else None,
                })
                node_ids.add(act_id)

                # wasInformedBy edges
                if act.informed_by:
                    edges.append({
                        "source": f"act-{act.informed_by}",
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
                            "detail": f"Agent: {assoc.agent_name} ({assoc.agent_type})",
                        })
                        node_ids.add(agent_id)
                    edges.append({
                        "source": agent_id,
                        "target": act_id,
                        "label": f"wasAssociatedWith\n({assoc.role})",
                        "type": "associated",
                    })

                # used edges
                for used in used_by_activity.get(act.id, []):
                    entity = entity_by_id.get(used.entity_id)
                    if entity:
                        ent_id = f"ent-{entity.id}"
                        if ent_id not in node_ids:
                            ent_label = f"{entity.type}"
                            nodes.append({
                                "id": ent_id,
                                "label": ent_label,
                                "type": "entity",
                                "entity_type": entity.type,
                                "time": entity.created_at.isoformat() if entity.created_at else "",
                                "detail": f"Entity: {entity.type}\nID: {entity.entity_id}\nVersion: {entity.id}",
                            })
                            node_ids.add(ent_id)
                        edges.append({
                            "source": ent_id,
                            "target": act_id,
                            "label": "used",
                            "type": "used",
                        })

            # Add entities and wasGeneratedBy edges
            for entity in all_entities:
                ent_id = f"ent-{entity.id}"
                if ent_id not in node_ids:
                    nodes.append({
                        "id": ent_id,
                        "label": f"{entity.type}",
                        "type": "entity",
                        "entity_type": entity.type,
                        "time": entity.created_at.isoformat() if entity.created_at else "",
                        "detail": f"Entity: {entity.type}\nID: {entity.entity_id}\nVersion: {entity.id}",
                    })
                    node_ids.add(ent_id)

                # wasGeneratedBy
                edges.append({
                    "source": f"act-{entity.generated_by}",
                    "target": ent_id,
                    "label": "wasGeneratedBy",
                    "type": "generated",
                })

                # wasDerivedFrom
                if entity.derived_from:
                    parent_id = f"ent-{entity.derived_from}"
                    edges.append({
                        "source": parent_id,
                        "target": ent_id,
                        "label": "wasDerivedFrom",
                        "type": "derived",
                    })

            import json
            nodes_json = json.dumps(nodes)
            edges_json = json.dumps(edges)

            html = _build_graph_html(
                dossier_id=str(dossier_id),
                workflow=dossier.workflow,
                nodes_json=nodes_json,
                edges_json=edges_json,
            )

            return HTMLResponse(content=html)


def _build_graph_html(dossier_id: str, workflow: str, nodes_json: str, edges_json: str) -> str:
    """Build the interactive timeline-based PROV graph HTML page."""

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

.link {{ fill: none; stroke-opacity: 0.4; }}
.link:hover {{ stroke-opacity: 0.9; }}
.link-label {{ font-size: 8px; fill: #64748b; pointer-events: none; }}

.node-activity {{ fill: #3b82f6; stroke: #93c5fd; stroke-width: 2; }}
.node-entity {{ fill: #10b981; stroke: #6ee7b7; stroke-width: 1.5; }}
.node-agent {{ fill: #f59e0b; stroke: #fcd34d; stroke-width: 1.5; }}

.node-label {{ fill: #f1f5f9; pointer-events: none; text-anchor: middle; font-weight: 500; }}
.node-time {{ font-size: 9px; fill: #64748b; pointer-events: none; text-anchor: middle; }}

.timeline-line {{ stroke: #334155; stroke-width: 1; stroke-dasharray: 4,4; }}
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
    <div class="legend-item"><div class="legend-dot" style="background:#10b981"></div> Entity</div>
    <div class="legend-item"><div class="legend-dot" style="background:#f59e0b"></div> Agent</div>
    <div style="margin-top:6px; border-top:1px solid #475569; padding-top:6px;">
    <div class="legend-item"><div class="legend-line" style="background:#60a5fa"></div> wasGeneratedBy</div>
    <div class="legend-item"><div class="legend-line" style="background:#f472b6"></div> used</div>
    <div class="legend-item"><div class="legend-line" style="background:#a78bfa"></div> wasInformedBy</div>
    <div class="legend-item"><div class="legend-line" style="background:#34d399"></div> wasDerivedFrom</div>
    <div class="legend-item"><div class="legend-line" style="background:#fbbf24"></div> wasAssociatedWith</div>
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
}};

// ── Layout constants ──
const MARGIN = {{ top: 80, right: 80, bottom: 80, left: 80 }};
const ACTIVITY_ROW_Y = height * 0.45;    // activities on the middle band
const AGENT_ROW_Y = height * 0.15;       // agents above
const ENTITY_ROW_Y = height * 0.75;      // entities below
const COL_SPACING = 200;                  // horizontal space between activity columns

// ── Pre-compute positions ──
// Activities get fixed X positions based on their order
const activityNodes = nodes.filter(n => n.type === "activity").sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
const entityNodes = nodes.filter(n => n.type === "entity");
const agentNodes = nodes.filter(n => n.type === "agent");

const totalWidth = Math.max(width, (activityNodes.length + 1) * COL_SPACING + MARGIN.left + MARGIN.right);

// Map activity IDs to their column X
const actXMap = {{}};
activityNodes.forEach((act, i) => {{
    const x = MARGIN.left + (i + 0.5) * COL_SPACING;
    act.fx = x;
    act.fy = ACTIVITY_ROW_Y;
    actXMap[act.id] = x;
}});

// Position entities below the activity that generated them
const genEdges = edges.filter(e => e.type === "generated");
const entityColCount = {{}};  // track how many entities per activity column

entityNodes.forEach(ent => {{
    // Find which activity generated this entity
    const genEdge = genEdges.find(e => e.target === ent.id || (e.target && e.target.id === ent.id));
    let parentX = totalWidth / 2;
    if (genEdge) {{
        const sourceId = typeof genEdge.source === "string" ? genEdge.source : genEdge.source.id;
        parentX = actXMap[sourceId] || parentX;
    }}

    // Stack entities vertically if multiple per activity
    const colKey = Math.round(parentX);
    entityColCount[colKey] = (entityColCount[colKey] || 0);
    const offset = entityColCount[colKey] * 50;
    entityColCount[colKey]++;

    ent.fx = parentX;
    ent.fy = ENTITY_ROW_Y + offset;
}});

// Position agents above their first associated activity
const assocEdges = edges.filter(e => e.type === "associated");
const agentPlaced = {{}};

agentNodes.forEach(agent => {{
    const assocEdge = assocEdges.find(e => {{
        const sid = typeof e.source === "string" ? e.source : e.source.id;
        return sid === agent.id;
    }});
    let parentX = totalWidth / 2;
    if (assocEdge) {{
        const targetId = typeof assocEdge.target === "string" ? assocEdge.target : assocEdge.target.id;
        parentX = actXMap[targetId] || parentX;
    }}

    // Offset if multiple agents at same X
    const colKey = Math.round(parentX);
    agentPlaced[colKey] = (agentPlaced[colKey] || 0);
    const offset = agentPlaced[colKey] * 45;
    agentPlaced[colKey]++;

    agent.fx = parentX + offset - 20;
    agent.fy = AGENT_ROW_Y;
}});

// ── SVG setup ──
const svg = d3.select("#graph")
    .attr("width", width)
    .attr("height", height);

// Arrow markers
const defs = svg.append("defs");
Object.entries(edgeColors).forEach(([type, color]) => {{
    defs.append("marker")
        .attr("id", `arrow-${{type}}`)
        .attr("viewBox", "0 -4 8 8")
        .attr("refX", 30)
        .attr("refY", 0)
        .attr("markerWidth", 5)
        .attr("markerHeight", 5)
        .attr("orient", "auto")
        .append("path")
        .attr("d", "M0,-4L8,0L0,4")
        .attr("fill", color);
}});

const g = svg.append("g");

// Zoom + pan
const zoom = d3.zoom()
    .scaleExtent([0.15, 3])
    .on("zoom", (event) => g.attr("transform", event.transform));
svg.call(zoom);

// ── Timeline baseline ──
g.append("line")
    .attr("class", "timeline-line")
    .attr("x1", MARGIN.left - 30)
    .attr("y1", ACTIVITY_ROW_Y)
    .attr("x2", totalWidth - MARGIN.right + 30)
    .attr("y2", ACTIVITY_ROW_Y);

// ── Links (curved paths) ──
const link = g.append("g")
    .selectAll("path")
    .data(edges)
    .join("path")
    .attr("class", "link")
    .attr("stroke", d => edgeColors[d.type] || "#475569")
    .attr("stroke-width", d => d.type === "informed" ? 2 : 1.5)
    .attr("marker-end", d => `url(#arrow-${{d.type}})`)
    .attr("fill", "none");

// Link labels (hidden by default, show on hover — less clutter)
const linkLabel = g.append("g")
    .selectAll("text")
    .data(edges)
    .join("text")
    .attr("class", "link-label")
    .text(d => d.label.split("\\n")[0])
    .style("opacity", 0);

// ── Nodes ──
const node = g.append("g")
    .selectAll("g")
    .data(nodes)
    .join("g")
    .attr("cursor", "pointer");

// Node shapes
node.each(function(d) {{
    const el = d3.select(this);

    if (d.type === "activity") {{
        el.append("rect")
            .attr("x", -55).attr("y", -20)
            .attr("width", 110).attr("height", 40)
            .attr("rx", 8)
            .attr("class", "node-activity");
    }} else if (d.type === "entity") {{
        el.append("ellipse")
            .attr("rx", 50).attr("ry", 16)
            .attr("class", "node-entity");
    }} else {{
        el.append("circle")
            .attr("r", 16)
            .attr("class", "node-agent");
    }}
}});

// Node labels
node.append("text")
    .attr("class", "node-label")
    .attr("dy", d => d.type === "entity" ? 4 : d.type === "agent" ? 5 : 5)
    .attr("font-size", d => d.type === "activity" ? "10px" : "9px")
    .text(d => {{
        let label = d.label;
        // Strip "oe:" prefix for cleaner display
        if (label.startsWith("oe:")) label = label.slice(3);
        return label.length > 20 ? label.slice(0, 18) + "…" : label;
    }});

// Time labels below activities
node.filter(d => d.type === "activity" && d.time)
    .append("text")
    .attr("class", "node-time")
    .attr("dy", 34)
    .text(d => {{
        const date = new Date(d.time);
        return date.toLocaleTimeString("nl-BE", {{ hour: "2-digit", minute: "2-digit", second: "2-digit" }});
    }});

// ── Tooltip ──
const tooltip = d3.select("#tooltip");

node.on("mouseover", (event, d) => {{
    tooltip.style("opacity", 1).html(d.detail || d.label);
    // Show labels on connected edges
    linkLabel.style("opacity", e => {{
        const sid = typeof e.source === "string" ? e.source : e.source.id;
        const tid = typeof e.target === "string" ? e.target : e.target.id;
        return (sid === d.id || tid === d.id) ? 1 : 0;
    }});
}})
.on("mousemove", (event) => {{
    tooltip.style("left", (event.clientX + 16) + "px")
        .style("top", (event.clientY - 10) + "px");
}})
.on("mouseout", () => {{
    tooltip.style("opacity", 0);
    linkLabel.style("opacity", 0);
}});

// ── Minimal simulation (just to resolve edge references, positions are fixed) ──
const simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(edges).id(d => d.id).strength(0))
    .alphaDecay(0.5)
    .on("tick", () => {{
        // Curved links
        link.attr("d", d => {{
            const sx = d.source.x, sy = d.source.y;
            const tx = d.target.x, ty = d.target.y;
            const dx = tx - sx, dy = ty - sy;

            // Slight curve for horizontal links, more curve for vertical
            if (Math.abs(dy) < 30) {{
                // Nearly horizontal — gentle arc
                const mid = (sy + ty) / 2 - 30;
                return `M${{sx}},${{sy}} Q${{(sx+tx)/2}},${{mid}} ${{tx}},${{ty}}`;
            }}
            // Default: straight line
            return `M${{sx}},${{sy}} L${{tx}},${{ty}}`;
        }});

        linkLabel
            .attr("x", d => (d.source.x + d.target.x) / 2)
            .attr("y", d => (d.source.y + d.target.y) / 2 - 5);

        node.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
    }});

// ── Fit to view ──
setTimeout(() => {{
    const bounds = g.node().getBBox();
    const bw = bounds.width || width;
    const bh = bounds.height || height;
    const midX = bounds.x + bw / 2;
    const midY = bounds.y + bh / 2;
    const scale = 0.85 / Math.max(bw / width, bh / height);
    const tx = width / 2 - scale * midX;
    const ty = height / 2 - scale * midY;

    svg.transition().duration(600).call(
        zoom.transform,
        d3.zoomIdentity.translate(tx, ty).scale(scale)
    );
}}, 500);

</script>
</body>
</html>"""
