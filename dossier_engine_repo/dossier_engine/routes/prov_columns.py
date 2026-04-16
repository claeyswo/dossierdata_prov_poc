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
from pathlib import Path
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select

from ..db.models import (
    EntityRow, ActivityRow, AssociationRow, UsedRow, Repository
)
from ..db import get_session_factory
from ..auth import User
from .access import check_dossier_access, get_visibility_from_entry

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


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
        async with session_factory() as session, session.begin():
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
    """Render the columns PROV graph from its Jinja2 template."""
    template = _jinja_env.get_template("prov_columns.html")
    return template.render(
        dossier_id=dossier_id,
        workflow=workflow,
        columns_json=columns_json,
        entity_types_json=entity_types_json,
        activity_used_json=activity_used_json,
        derivations_json=derivations_json,
        informed_edges_json=informed_edges_json,
    )

