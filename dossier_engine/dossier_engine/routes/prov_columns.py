"""
PROV Graph — Column Layout

Three bands:
- Top: client activities + scheduled activities + cross-dossier dummies
- Middle: side effects (systemAction always here)
- Bottom: entities in per-type rows with derivation arrows

Features:
- Hover entity → highlight connected (generatedBy, attributedTo, derivation chain)
- Hover activity → highlight used entities
- Scheduled activities (type 3 results) in top row with wasInformedBy
- Cross-dossier dummies for remote wasInformedBy
- Recorded tasks: latest version under generating activity
"""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ..db.models import (
    EntityRow, ActivityRow, AssociationRow, UsedRow, Repository
)
from ..db import get_session_factory
from ..auth import User
from .access import check_dossier_access, get_visibility_from_entry


def register_columns_graph(app, registry, get_user, global_access=None):

    @app.get(
        "/dossiers/{dossier_id}/prov/graph/columns",
        tags=["prov"],
        summary="PROV graph — column layout",
        response_class=HTMLResponse,
    )
    async def get_prov_graph_columns(
        dossier_id: UUID,
        include_tasks: bool = True,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            plugin = registry.get(dossier.workflow)
            access_entry = await check_dossier_access(repo, dossier_id, user, global_access)
            visible_types, _ = get_visibility_from_entry(access_entry)

            activities = await repo.get_activities_for_dossier(dossier_id)
            all_entities_result = await session.execute(
                select(EntityRow).where(EntityRow.dossier_id == dossier_id).order_by(EntityRow.created_at)
            )
            all_entities = list(all_entities_result.scalars().all())
            if visible_types is not None:
                all_entities = [e for e in all_entities if e.type in visible_types]

            activity_ids = [a.id for a in activities]
            assoc_result = await session.execute(
                select(AssociationRow).where(AssociationRow.activity_id.in_(activity_ids))
            )
            assoc_by_activity = {}
            for a in assoc_result.scalars().all():
                assoc_by_activity.setdefault(a.activity_id, []).append(a)

            used_result = await session.execute(
                select(UsedRow).where(UsedRow.activity_id.in_(activity_ids))
            )
            used_by_activity = {}
            for u in used_result.scalars().all():
                used_by_activity.setdefault(u.activity_id, []).append(u)

            system_activity_types = set()
            if plugin:
                for act_def in plugin.workflow.get("activities", []):
                    if act_def.get("client_callable") is False:
                        system_activity_types.add(act_def["name"])

            if not include_tasks:
                activities = [a for a in activities if a.type != "systemAction"]
                all_entities = [e for e in all_entities if e.type != "system:task"]

            activity_by_id = {a.id: a for a in activities}
            entity_by_id = {e.id: e for e in all_entities}

            # --- Classify activities ---

            def resolve_parent(act):
                if act.informed_by:
                    try:
                        pid = UUID(str(act.informed_by))
                        return activity_by_id.get(pid)
                    except (ValueError, AttributeError):
                        pass
                return None

            def find_root(act):
                visited = set()
                current = act
                while current:
                    if current.id in visited:
                        break
                    visited.add(current.id)
                    if current.type not in system_activity_types and current.type != "systemAction":
                        return current
                    parent = resolve_parent(current)
                    if parent:
                        current = parent
                    else:
                        break
                return current

            # Find scheduled activity IDs (type 3 task results)
            scheduled_ids = set()
            for e in all_entities:
                if e.type == "system:task" and e.content:
                    if e.content.get("kind") == "scheduled_activity":
                        raid = e.content.get("result_activity_id")
                        if raid:
                            try:
                                scheduled_ids.add(UUID(raid))
                            except (ValueError, AttributeError):
                                pass

            # Build top row
            top_row = []
            col_for_act = {}

            # Collect cross-dossier URIs to insert as dummies
            cross_dossier_before = {}  # col_idx → uri (informed_by from another dossier)
            cross_dossier_after = {}   # col_idx → uri (systemAction informed by remote)

            # Find systemAction activities that generated task entities (task completions)
            task_completion_ids = set()
            for e in all_entities:
                if e.type == "system:task" and e.generated_by:
                    task_completion_ids.add(e.generated_by)

            # First pass: client + scheduled + standalone systemAction activities
            for act in activities:
                is_client = act.type not in system_activity_types and act.type != "systemAction"
                is_scheduled = act.id in scheduled_ids
                is_standalone_system = (act.type == "systemAction" and act.id not in task_completion_ids)

                if is_client or is_scheduled or is_standalone_system:
                    col_idx = len(top_row)
                    if is_scheduled:
                        kind = "scheduled"
                    elif is_standalone_system:
                        kind = "system"
                    else:
                        kind = "client"
                    top_row.append({
                        "id": str(act.id),
                        "type": act.type,
                        "kind": kind,
                        "time": act.started_at.isoformat() if act.started_at else "",
                        "informed_by": str(act.informed_by) if act.informed_by else None,
                        "agents": list(set(a.agent_name or a.agent_id for a in assoc_by_activity.get(act.id, []))),
                        "side_effects": [],
                        "entities": [],
                    })
                    col_for_act[act.id] = col_idx

                    # Check if this activity was informed by a cross-dossier URI
                    if act.informed_by and str(act.informed_by).startswith("urn:"):
                        cross_dossier_before[col_idx] = str(act.informed_by)

            # Check systemAction for cross-dossier informed_by
            for act in activities:
                if act.type == "systemAction" and act.informed_by and str(act.informed_by).startswith("urn:"):
                    root = find_root(act)
                    col_idx = col_for_act.get(root.id)
                    if col_idx is not None:
                        cross_dossier_after[col_idx] = str(act.informed_by)

            # Insert cross-dossier dummies (insert from right to left to preserve indices)
            insertions = []
            for col_idx, uri in cross_dossier_before.items():
                insertions.append((col_idx, uri, "before"))
            for col_idx, uri in cross_dossier_after.items():
                insertions.append((col_idx + 1, uri, "after"))

            # Sort by insertion index descending so earlier insertions don't shift later ones
            insertions.sort(key=lambda x: x[0], reverse=True)
            for ins_idx, uri, _ in insertions:
                parts = uri.split("/")
                short = parts[-1][:12] + "…" if len(parts[-1]) > 12 else parts[-1]
                top_row.insert(ins_idx, {
                    "id": None,
                    "type": short,
                    "kind": "cross_dossier",
                    "uri": uri,
                    "time": "",
                    "informed_by": None,
                    "agents": [],
                    "side_effects": [],
                    "entities": [],
                })

            # Rebuild col_for_act after insertions
            col_for_act = {}
            for i, col in enumerate(top_row):
                if col["id"]:
                    try:
                        col_for_act[UUID(col["id"])] = i
                    except (ValueError, AttributeError):
                        pass

            # Assign side effects to columns
            side_effect_ids = set()
            for act in activities:
                if act.id in col_for_act:
                    continue
                # Skip systemAction with no informed_by (recorded task completions — no visual value)
                if act.type == "systemAction" and not (act.informed_by and str(act.informed_by).strip()):
                    col_for_act[act.id] = -1  # track but don't render
                    side_effect_ids.add(act.id)
                    continue
                root = find_root(act)
                col_idx = col_for_act.get(root.id, len(top_row) - 1 if top_row else 0)
                col_for_act[act.id] = col_idx
                side_effect_ids.add(act.id)
                if col_idx < len(top_row):
                    top_row[col_idx]["side_effects"].append({
                        "id": str(act.id),
                        "type": act.type,
                    })

            # Latest task versions (for recorded tasks only)
            task_latest = {}
            task_first = {}
            task_kind_map = {}  # entity_id → kind
            for e in all_entities:
                if e.type == "system:task" and e.content:
                    kind = e.content.get("kind", "")
                    if e.entity_id not in task_kind_map:
                        task_kind_map[e.entity_id] = kind
                    ex = task_latest.get(e.entity_id)
                    if not ex or (e.created_at and ex.created_at and e.created_at > ex.created_at):
                        task_latest[e.entity_id] = e
                    ex2 = task_first.get(e.entity_id)
                    if not ex2 or (e.created_at and ex2.created_at and e.created_at < ex2.created_at):
                        task_first[e.entity_id] = e

            # Assign entities to columns
            entity_type_order = []
            seen_types = set()

            # Build reverse lookup: entity version id → list of activity ids that used it
            entity_used_by = {}
            for act_id, used_list in used_by_activity.items():
                for u in used_list:
                    entity_used_by.setdefault(u.entity_id, []).append(act_id)

            for entity in all_entities:
                col_idx = None

                if entity.generated_by:
                    if entity.type == "system:task":
                        kind = task_kind_map.get(entity.entity_id, "")

                        if kind == "recorded":
                            latest = task_latest.get(entity.entity_id)
                            if not latest or latest.id != entity.id:
                                continue
                            first = task_first.get(entity.entity_id)
                            if first and first.generated_by:
                                col_idx = col_for_act.get(first.generated_by, None)
                            else:
                                col_idx = col_for_act.get(entity.generated_by, None)
                        else:
                            col_idx = col_for_act.get(entity.generated_by, None)
                    else:
                        col_idx = col_for_act.get(entity.generated_by, None)
                elif entity.type == "external":
                    # External entities without generated_by: assign to first activity that used them
                    using_acts = entity_used_by.get(entity.id, [])
                    for act_id in using_acts:
                        col_idx = col_for_act.get(act_id)
                        if col_idx is not None:
                            break
                else:
                    continue

                if col_idx is not None and col_idx < len(top_row):
                    # Row key: tasks and externals each get their own row per logical entity
                    if entity.type == "system:task":
                        row_key = f"task:{entity.entity_id}"
                    elif entity.type == "external":
                        row_key = f"external:{entity.entity_id}"
                    else:
                        row_key = entity.type

                    if row_key not in seen_types:
                        entity_type_order.append(row_key)
                        seen_types.add(row_key)

                    # Determine label
                    if entity.type == "external" and entity.content:
                        label = entity.content.get("uri", entity.type)
                    elif entity.type == "system:task" and entity.content:
                        fn = entity.content.get("function")
                        ta = entity.content.get("target_activity")
                        label = fn or ta or "task"
                    else:
                        label = entity.type

                    task_kind = ""
                    if entity.type == "system:task" and entity.content:
                        task_kind = entity.content.get("kind", "")

                    by_side_effect = entity.generated_by in side_effect_ids

                    # Track whether external entity was generated or used
                    external_kind = ""
                    if entity.type == "external":
                        external_kind = "generated" if entity.generated_by else "used"

                    top_row[col_idx]["entities"].append({
                        "id": str(entity.id),
                        "entity_id": str(entity.entity_id),
                        "type": entity.type,
                        "row_key": row_key,
                        "row": 0,  # set below
                        "label": label,
                        "derived_from": str(entity.derived_from) if entity.derived_from else None,
                        "generated_by": str(entity.generated_by) if entity.generated_by else "",
                        "attributed_to": entity.attributed_to or "",
                        "url": f"/dossiers/{dossier_id}/entities/{entity.type}/{entity.entity_id}/{entity.id}",
                        "is_task": entity.type == "system:task",
                        "task_status": entity.content.get("status", "") if entity.type == "system:task" and entity.content else "",
                        "task_kind": task_kind,
                        "by_side_effect": by_side_effect,
                        "external_kind": external_kind,
                    })

            # Sort entity rows: regular entities first, then externals, then tasks
            def row_sort_key(key):
                if key.startswith("task:"):
                    return (2, key)
                elif key.startswith("external:"):
                    return (1, key)
                else:
                    return (0, key)
            entity_type_order.sort(key=row_sort_key)

            # Set row indices
            for col in top_row:
                for ent in col["entities"]:
                    ent["row"] = entity_type_order.index(ent["row_key"]) if ent["row_key"] in entity_type_order else 0

            # Used links
            activity_used_map = {}
            for act_id, used_list in used_by_activity.items():
                activity_used_map[str(act_id)] = [str(u.entity_id) for u in used_list]

            # Derivations
            derivations = []
            for entity in all_entities:
                if entity.derived_from and entity.derived_from in entity_by_id:
                    derivations.append({
                        "from": str(entity.derived_from),
                        "to": str(entity.id),
                    })

            # Build wasInformedBy edges for top row
            informed_edges = []
            for col in top_row:
                if col["informed_by"] and col["id"]:
                    informed_edges.append({
                        "from": col["informed_by"],
                        "to": col["id"],
                    })

            html = _build_columns_html(
                dossier_id=str(dossier_id),
                workflow=dossier.workflow,
                columns_json=json.dumps(top_row),
                entity_types_json=json.dumps(entity_type_order),
                activity_used_json=json.dumps(activity_used_map),
                derivations_json=json.dumps(derivations),
                informed_edges_json=json.dumps(informed_edges),
            )

            return HTMLResponse(content=html)


def _build_columns_html(
    dossier_id: str, workflow: str,
    columns_json: str, entity_types_json: str,
    activity_used_json: str, derivations_json: str,
    informed_edges_json: str,
) -> str:

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PROV Columns — {dossier_id[:8]}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; overflow: hidden; }}

#header {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 10;
    padding: 10px 20px; background: rgba(15, 23, 42, 0.95);
    border-bottom: 1px solid #334155;
    display: flex; align-items: center; gap: 16px;
}}
#header h1 {{ font-size: 15px; font-weight: 600; }}
#header .badge {{ font-size: 11px; padding: 2px 8px; border-radius: 4px; background: #1e3a5f; color: #93c5fd; }}

#tooltip {{
    position: fixed; padding: 8px 12px; background: rgba(30, 41, 59, 0.97);
    border: 1px solid #475569; border-radius: 6px; font-size: 11px;
    pointer-events: none; opacity: 0; transition: opacity 0.15s;
    max-width: 340px; white-space: pre-wrap; z-index: 20;
}}

#legend {{
    position: fixed; bottom: 12px; left: 12px; z-index: 10;
    padding: 10px 14px; background: rgba(30, 41, 59, 0.95);
    border: 1px solid #334155; border-radius: 8px; font-size: 10px;
}}
.legend-item {{ display: flex; align-items: center; gap: 6px; margin: 2px 0; }}
.legend-dot {{ width: 10px; height: 10px; border-radius: 2px; }}

svg {{ width: 100vw; height: 100vh; }}
.dim {{ opacity: 0.12 !important; }}
.highlight {{ opacity: 1 !important; }}
</style>
</head>
<body>

<div id="header">
    <h1>PROV Columns</h1>
    <span class="badge">{workflow}</span>
    <span class="badge">{dossier_id[:8]}…</span>
</div>
<div id="tooltip"></div>
<div id="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#2563eb"></div> Client activity</div>
    <div class="legend-item"><div class="legend-dot" style="background:#9333ea"></div> Scheduled activity</div>
    <div class="legend-item"><div class="legend-dot" style="background:#0891b2"></div> System action</div>
    <div class="legend-item"><div class="legend-dot" style="background:#6b7280;border:1px dashed #9ca3af"></div> Cross-dossier</div>
    <div class="legend-item"><div class="legend-dot" style="background:#312e81;border:1px solid #818cf8"></div> Side effect</div>
    <div class="legend-item"><div class="legend-dot" style="background:#059669"></div> Entity</div>
    <div class="legend-item"><div class="legend-dot" style="background:#be185d"></div> Entity (by side effect)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#0284c7"></div> External (used)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#ea580c"></div> External (generated)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#7c3aed"></div> Task entity</div>
    <div style="margin-top:4px;border-top:1px solid #475569;padding-top:4px;">
    <div class="legend-item"><div style="width:20px;height:3px;background:#34d399"></div> wasDerivedFrom</div>
    <div class="legend-item"><div style="width:20px;height:2px;background:#60a5fa"></div> wasInformedBy</div>
    <div class="legend-item"><div style="width:20px;height:2px;background:#f59e0b;border-top:1px dashed #f59e0b"></div> used (on hover)</div>
    <div class="legend-item"><div style="width:20px;height:2px;background:#a78bfa;border-top:1px dashed #a78bfa"></div> generated (on hover)</div>
    </div>
</div>

<svg id="graph"></svg>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>

const columns = {columns_json};
const entityTypes = {entity_types_json};
const activityUsed = {activity_used_json};
const derivations = {derivations_json};
const informedEdges = {informed_edges_json};

const COL_W = 160, COL_GAP = 30, M = {{ t: 70, l: 50 }};
const ACT_H = 32, SE_H = 22, ENT_H = 24, GAP = 4;

const svg = d3.select("#graph");
const W = window.innerWidth, H = window.innerHeight;
svg.attr("width", W).attr("height", H);

const defs = svg.append("defs");
["#34d399","#60a5fa","#f59e0b"].forEach((c, i) => {{
    defs.append("marker").attr("id", `arr${{i}}`).attr("viewBox","0 -4 8 8")
        .attr("refX",8).attr("refY",0).attr("markerWidth",5).attr("markerHeight",5)
        .attr("orient","auto").append("path").attr("d","M0,-4L8,0L0,4").attr("fill",c);
}});

const g = svg.append("g");
svg.call(d3.zoom().scaleExtent([0.1,4]).on("zoom", e => g.attr("transform", e.transform)));
const tip = d3.select("#tooltip");

// Compute layout
const maxSE = Math.max(1, ...columns.map(c => c.side_effects.length));
const BAND1_Y = M.t;
const BAND1_H = ACT_H + 50;
const BAND2_Y = BAND1_Y + BAND1_H;
const BAND2_H = maxSE * (SE_H + GAP) + 20;
const BAND3_Y = BAND2_Y + BAND2_H;
const ENT_ROW_H = ENT_H + GAP + 4;
const BAND3_H = entityTypes.length * ENT_ROW_H + 20;
const totalW = M.l + columns.length * (COL_W + COL_GAP);

// Track all node positions
const actPos = {{}};    // col id → {{x, y, w, h}}
const entPos = {{}};    // entity version id → {{x, y, w, h, cx, cy}}
const sePos = {{}};     // se id → {{x, y}}
const allGroups = [];   // all d3 groups for dimming

// Band backgrounds
g.append("rect").attr("x",0).attr("y",BAND1_Y).attr("width",totalW).attr("height",BAND1_H).attr("fill","#2563eb").attr("opacity",0.04);
g.append("rect").attr("x",0).attr("y",BAND2_Y).attr("width",totalW).attr("height",BAND2_H).attr("fill","#818cf8").attr("opacity",0.04);
g.append("rect").attr("x",0).attr("y",BAND3_Y).attr("width",totalW).attr("height",BAND3_H).attr("fill","#34d399").attr("opacity",0.03);

// Band labels
g.append("text").attr("x",6).attr("y",BAND1_Y+12).attr("fill","#475569").attr("font-size","9px").attr("font-weight","700").text("ACTIVITIES");
g.append("text").attr("x",6).attr("y",BAND2_Y+12).attr("fill","#475569").attr("font-size","9px").attr("font-weight","700").text("SIDE EFFECTS");
g.append("text").attr("x",6).attr("y",BAND3_Y+12).attr("fill","#475569").attr("font-size","9px").attr("font-weight","700").text("ENTITIES");

// Entity type row labels
entityTypes.forEach((t, i) => {{
    const y = BAND3_Y + 16 + i * ENT_ROW_H;
    g.append("line").attr("x1",0).attr("y1",y).attr("x2",totalW).attr("y2",y).attr("stroke","#1e293b").attr("stroke-opacity",0.2);
}});

// Render columns
columns.forEach((col, ci) => {{
    const x = M.l + ci * (COL_W + COL_GAP);

    // Column guide
    g.append("line").attr("x1",x+COL_W/2).attr("y1",BAND1_Y).attr("x2",x+COL_W/2).attr("y2",BAND3_Y+BAND3_H)
        .attr("stroke","#1e293b").attr("stroke-dasharray","2,6").attr("stroke-opacity",0.3);

    // --- Band 1: activity ---
    const actY = BAND1_Y + 18;
    const colors = {{ client:"#2563eb", scheduled:"#9333ea", cross_dossier:"#6b7280", system:"#0891b2" }};
    const strokes = {{ client:"#60a5fa", scheduled:"#c084fc", cross_dossier:"#9ca3af", system:"#67e8f9" }};
    const fill = colors[col.kind] || "#2563eb";
    const strokeCol = strokes[col.kind] || "#60a5fa";
    const isDashed = col.kind === "cross_dossier";

    const ag = g.append("g").attr("data-id", col.id || col.uri).attr("data-kind","activity").attr("transform",`translate(${{x}},${{actY}})`);
    allGroups.push(ag);
    const r = ag.append("rect").attr("width",COL_W).attr("height",ACT_H).attr("rx",isDashed?4:8).attr("fill",fill).attr("stroke",strokeCol).attr("stroke-width",isDashed?1:2);
    if (isDashed) r.attr("stroke-dasharray","4,2");
    ag.append("text").attr("x",COL_W/2).attr("y",ACT_H/2+4).attr("text-anchor","middle").attr("fill","#f1f5f9").attr("font-size","10px").attr("font-weight","600")
        .text(col.type ? (col.type.length > 20 ? col.type.slice(0,18)+"…" : col.type) : "?");

    // Agent name — prominent, above the activity
    if (col.agents.length > 0) {{
        g.append("text")
            .attr("x", x + COL_W / 2).attr("y", actY - 8)
            .attr("text-anchor","middle").attr("fill","#fbbf24").attr("font-size","11px").attr("font-weight","600")
            .text(col.agents.join(", "));
    }}
    // Time — small, below the activity
    if (col.time) {{
        const d = new Date(col.time);
        ag.append("text").attr("x",COL_W/2).attr("y",ACT_H+12).attr("text-anchor","middle").attr("fill","#64748b").attr("font-size","8px")
            .text(d.toLocaleString("nl-BE",{{hour:"2-digit",minute:"2-digit",day:"2-digit",month:"2-digit"}}));
    }}

    actPos[col.id || col.uri] = {{ x, y: actY, w: COL_W, h: ACT_H }};

    // Hover: highlight used + generated, draw arrows
    ag.on("mouseover", (event) => {{
        const usedIds = [...(activityUsed[col.id] || [])];
        col.side_effects.forEach(se => {{
            const su = activityUsed[se.id] || [];
            usedIds.push(...su);
        }});

        // Dim everything
        allGroups.forEach(gr => gr.classed("dim", true));
        g.selectAll(".deriv-arrow").classed("dim", true);
        ag.classed("dim", false).classed("highlight", true);

        // Separate generated entities (in this column) from used entities (may be in other columns)
        const genEntIds = new Set(col.entities.filter(e => e.generated_by).map(e => e.id));
        const usedSet = new Set(usedIds);

        allGroups.forEach(gr => {{
            const eid = gr.attr("data-eid");
            if (eid && (genEntIds.has(eid) || usedSet.has(eid))) {{
                gr.classed("dim", false).classed("highlight", true);
            }}
        }});

        const actCx = x + COL_W / 2;
        const actBottom = actY + ACT_H;
        const actTop = actY;

        // Collect all arrow targets to spread them
        const usedArr = [];
        usedSet.forEach(uid => {{ const pos = entPos[uid]; if (pos) usedArr.push({{ id: uid, pos }}); }});
        const genArr = [];
        genEntIds.forEach(eid => {{ const pos = entPos[eid]; if (pos) genArr.push({{ id: eid, pos }}); }});

        // Sort by horizontal distance to activity center for consistent spread
        usedArr.sort((a, b) => a.pos.x - b.pos.x);
        genArr.sort((a, b) => a.pos.x - b.pos.x);

        // "Used" arrows: from entity UP to activity
        const usedSpread = COL_W * 0.6;
        usedArr.forEach((item, i) => {{
            const n = usedArr.length;
            const startOffset = n > 1 ? (i / (n - 1) - 0.5) * usedSpread : 0;
            const ax = actCx + startOffset;
            const ex = item.pos.x + item.pos.w / 2;
            const ey = item.pos.y;
            const dx = Math.abs(ex - ax);
            const curveSpread = Math.min(dx * 0.4, 60) * (i % 2 === 0 ? 1 : -1);
            const mid1y = ey - (ey - actBottom) * 0.3;
            const mid2y = actBottom + (ey - actBottom) * 0.3;
            g.append("path")
                .attr("class", "hover-used-arrow")
                .attr("d", `M${{ex}},${{ey}} C${{ex + curveSpread}},${{mid1y}} ${{ax - curveSpread}},${{mid2y}} ${{ax}},${{actBottom}}`)
                .attr("fill", "none").attr("stroke", "#f59e0b").attr("stroke-width", 1.5)
                .attr("stroke-opacity", 0.7).attr("stroke-dasharray", "4,3")
                .attr("marker-end", "url(#arr2)");
        }});

        // "Generated" arrows: from activity DOWN to entity
        const genSpread = COL_W * 0.6;
        genArr.forEach((item, i) => {{
            const n = genArr.length;
            const startOffset = n > 1 ? (i / (n - 1) - 0.5) * genSpread : 0;
            const ax = actCx + startOffset;
            const ex = item.pos.x + item.pos.w / 2;
            const ey = item.pos.y;
            const dx = Math.abs(ex - ax);
            const curveSpread = Math.min(dx * 0.4, 60) * (i % 2 === 0 ? 1 : -1);
            const mid1y = actBottom + (ey - actBottom) * 0.3;
            const mid2y = ey - (ey - actBottom) * 0.3;
            g.append("path")
                .attr("class", "hover-gen-arrow")
                .attr("d", `M${{ax}},${{actBottom}} C${{ax + curveSpread}},${{mid1y}} ${{ex - curveSpread}},${{mid2y}} ${{ex}},${{ey}}`)
                .attr("fill", "none").attr("stroke", "#a78bfa").attr("stroke-width", 1.5)
                .attr("stroke-opacity", 0.5).attr("stroke-dasharray", "2,2");
        }});

        let detail = `${{col.kind}}: ${{col.type || col.uri}}`;
        if (col.id) detail += `\\nID: ${{col.id}}`;
        if (col.informed_by) detail += `\\nInformed by: ${{col.informed_by}}`;
        if (usedSet.size > 0) detail += `\\n\\nUsed ${{usedSet.size}} entities`;
        if (genEntIds.size > 0) detail += `\\nGenerated ${{genEntIds.size}} entities`;
        tip.style("opacity",1).html(detail);
    }})
    .on("mousemove", (event) => tip.style("left",(event.clientX+12)+"px").style("top",(event.clientY-8)+"px"))
    .on("mouseout", () => {{
        allGroups.forEach(gr => gr.classed("dim", false).classed("highlight", false));
        g.selectAll(".deriv-arrow").classed("dim", false);
        g.selectAll(".hover-used-arrow").remove();
        g.selectAll(".hover-gen-arrow").remove();
        tip.style("opacity",0);
    }});

    // --- Band 2: side effects ---
    let seY = BAND2_Y + 14;
    col.side_effects.forEach(se => {{
        const sg = g.append("g").attr("data-id",se.id).attr("data-kind","side_effect").attr("transform",`translate(${{x+6}},${{seY}})`);
        allGroups.push(sg);
        sg.append("rect").attr("width",COL_W-12).attr("height",SE_H).attr("rx",4).attr("fill","#312e81").attr("stroke","#818cf8").attr("stroke-width",1).attr("stroke-opacity",0.6);
        let label = se.type; if (label.length > 22) label = label.slice(0,20)+"…";
        sg.append("text").attr("x",(COL_W-12)/2).attr("y",SE_H/2+3).attr("text-anchor","middle").attr("fill","#c7d2fe").attr("font-size","8px").text(label);
        sg.on("mouseover", (event) => {{ tip.style("opacity",1).html(`Side effect: ${{se.type}}\\nID: ${{se.id}}`); }})
            .on("mousemove", (event) => tip.style("left",(event.clientX+12)+"px").style("top",(event.clientY-8)+"px"))
            .on("mouseout", () => tip.style("opacity",0));
        sePos[se.id] = {{ x: x+6, y: seY }};
        seY += SE_H + GAP;
    }});

    // --- Band 3: entities (each type/task gets its own row) ---
    col.entities.forEach(ent => {{
        const rowY = BAND3_Y + 16 + ent.row * ENT_ROW_H;
        let label = ent.label || ent.type;
        if (label.startsWith("oe:")) label = label.slice(3);
        if (label.length > 20) label = label.slice(0,18)+"…";
        if (ent.task_status) label += ` (${{ent.task_status}})`;

        // Color logic: task > external generated > external used > side-effect-generated > normal
        let fill, stroke;
        if (ent.is_task) {{
            fill = "#7c3aed"; stroke = "#c4b5fd";           // vivid purple
        }} else if (ent.external_kind === "generated") {{
            fill = "#ea580c"; stroke = "#fdba74";           // vibrant orange — we created this externally
        }} else if (ent.external_kind === "used") {{
            fill = "#0284c7"; stroke = "#7dd3fc";           // sky blue — external reference we consumed
        }} else if (ent.by_side_effect) {{
            fill = "#be185d"; stroke = "#f9a8d4";           // pink/rose for side-effect entities
        }} else {{
            fill = "#059669"; stroke = "#6ee7b7";           // emerald green
        }}

        const eg = g.append("g")
            .attr("data-eid", ent.id).attr("data-entity-id", ent.entity_id)
            .attr("data-gen", ent.generated_by).attr("data-attr", ent.attributed_to)
            .attr("data-derived", ent.derived_from || "").attr("data-kind","entity")
            .attr("transform",`translate(${{x+6}},${{rowY}})`).style("cursor","pointer");
        allGroups.push(eg);

        eg.append("rect").attr("width",COL_W-12).attr("height",ENT_H).attr("rx",12).attr("fill",fill).attr("stroke",stroke).attr("stroke-width",1.5);
        eg.append("text").attr("x",(COL_W-12)/2).attr("y",ENT_H/2+3).attr("text-anchor","middle").attr("fill","#f1f5f9").attr("font-size","8px").text(label);

        entPos[ent.id] = {{ x: x+6, y: rowY, w: COL_W-12, h: ENT_H, cx: x+6+(COL_W-12)/2, cy: rowY+ENT_H/2 }};

        // Click to open
        eg.on("click", () => {{ if (ent.url) window.open(ent.url, "_blank"); }});

        // Hover: highlight connected
        eg.on("mouseover", (event) => {{
            allGroups.forEach(gr => gr.classed("dim", true));
            eg.classed("dim", false).classed("highlight", true);

            // Highlight generatedBy activity
            allGroups.forEach(gr => {{
                if (gr.attr("data-id") === ent.generated_by) gr.classed("dim",false).classed("highlight",true);
            }});

            // Highlight attributedTo (find agent in activity)
            // Highlight derivation chain (both directions)
            const chain = new Set([ent.id]);
            // Walk forward
            let cursor = ent.derived_from;
            while (cursor) {{
                chain.add(cursor);
                const found = columns.flatMap(c=>c.entities).find(e=>e.id===cursor);
                cursor = found ? found.derived_from : null;
            }}
            // Walk backward (entities derived from this one)
            let changed = true;
            while (changed) {{
                changed = false;
                derivations.forEach(d => {{
                    if (chain.has(d.from) && !chain.has(d.to)) {{ chain.add(d.to); changed = true; }}
                }});
            }}
            chain.forEach(eid => {{
                allGroups.forEach(gr => {{
                    if (gr.attr("data-eid") === eid) gr.classed("dim",false).classed("highlight",true);
                }});
            }});

            // Highlight derivation arrows
            g.selectAll(".deriv-arrow").classed("dim", true);
            g.selectAll(".deriv-arrow").each(function() {{
                const el = d3.select(this);
                if (chain.has(el.attr("data-from")) || chain.has(el.attr("data-to"))) {{
                    el.classed("dim",false).classed("highlight",true);
                }}
            }});

            let detail = `${{ent.type}}\\nEntity: ${{ent.entity_id}}\\nVersion: ${{ent.id}}`;
            if (ent.attributed_to) detail += `\\nBy: ${{ent.attributed_to}}`;
            if (ent.task_status) detail += `\\nStatus: ${{ent.task_status}}`;
            detail += "\\n\\n(click to view)";
            tip.style("opacity",1).html(detail);
        }})
        .on("mousemove", (event) => tip.style("left",(event.clientX+12)+"px").style("top",(event.clientY-8)+"px"))
        .on("mouseout", () => {{
            allGroups.forEach(gr => gr.classed("dim",false).classed("highlight",false));
            g.selectAll(".deriv-arrow").classed("dim",false).classed("highlight",false);
            tip.style("opacity",0);
        }});
    }});
}});

// Draw derivation arrows
derivations.forEach(d => {{
    const from = entPos[d.from];
    const to = entPos[d.to];
    if (from && to) {{
        const sx = from.x + from.w + 2;
        const sy = from.y + from.h / 2;
        const tx = to.x - 2;
        const ty = to.y + to.h / 2;
        const mx = (sx + tx) / 2;
        g.append("path")
            .attr("class","deriv-arrow")
            .attr("data-from", d.from).attr("data-to", d.to)
            .attr("d",`M${{sx}},${{sy}} C${{mx}},${{sy}} ${{mx}},${{ty}} ${{tx}},${{ty}}`)
            .attr("fill","none").attr("stroke","#34d399").attr("stroke-width",2).attr("stroke-opacity",0.6)
            .attr("marker-end","url(#arr0)");
    }}
}});

// Draw wasInformedBy edges between top-row activities
informedEdges.forEach(e => {{
    // Find positions
    let fromPos = actPos[e.from];
    let toPos = actPos[e.to];
    if (fromPos && toPos) {{
        const sx = fromPos.x + fromPos.w;
        const sy = fromPos.y + fromPos.h / 2;
        const tx = toPos.x;
        const ty = toPos.y + toPos.h / 2;
        g.append("path")
            .attr("d",`M${{sx}},${{sy}} C${{sx+20}},${{sy}} ${{tx-20}},${{ty}} ${{tx}},${{ty}}`)
            .attr("fill","none").attr("stroke","#60a5fa").attr("stroke-width",2).attr("stroke-opacity",0.5)
            .attr("marker-end","url(#arr1)");
    }}
}});

// Fit to view
setTimeout(() => {{
    const b = g.node().getBBox();
    if (b.width === 0) return;
    const s = Math.min(0.95, 0.85 / Math.max(b.width / W, b.height / H));
    const tx = W/2 - s*(b.x+b.width/2);
    const ty = H/2 - s*(b.y+b.height/2);
    svg.transition().duration(400).call(d3.zoom().transform, d3.zoomIdentity.translate(tx,ty).scale(s));
}}, 100);

</script>
</body>
</html>"""
