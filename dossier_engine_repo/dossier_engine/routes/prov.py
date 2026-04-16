"""
PROV export and visualization endpoints.

- GET /dossiers/{id}/prov          → PROV-JSON export
- GET /dossiers/{id}/prov/graph    → Interactive HTML visualization
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from ..auth import User
from ..db import get_session_factory, Repository
from ..db.models import ActivityRow, EntityRow, AssociationRow, UsedRow
from ..plugin import PluginRegistry
from .access import check_dossier_access, get_visibility_from_entry

from sqlalchemy import select

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


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
        async with session_factory() as session, session.begin():
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

            # Load agent URIs from the agents table for PROV rendering.
            # The association rows carry agent_id, name, type but not the
            # canonical URI — that lives on the agent row itself.
            from ..db.models import AgentRow
            agent_ids = {a.agent_id for assocs in assoc_by_activity.values() for a in assocs}
            agent_ids |= {e.attributed_to for e in all_entities if e.attributed_to}
            agent_rows = {}
            if agent_ids:
                agent_result = await session.execute(
                    select(AgentRow).where(AgentRow.id.in_(agent_ids))
                )
                agent_rows = {a.id: a for a in agent_result.scalars().all()}

            # Build PROV-JSON using W3C-compliant IRIs
            from ..prov_iris import (
                prov_prefixes, entity_qname, activity_qname,
                agent_qname, prov_type_value, agent_type_value,
            )

            def _agent_key(agent_id: str) -> str:
                """Use the agent's canonical URI if available, otherwise
                fall back to the dossier-scoped QName."""
                row = agent_rows.get(agent_id)
                if row and row.uri:
                    return row.uri
                return agent_qname(agent_id)

            def _entity_key(entity) -> str:
                """Use the actual URI for external entities, otherwise
                the standard dossier-scoped IRI path."""
                if entity.type == "external" and entity.content:
                    ext_uri = entity.content.get("uri")
                    if ext_uri:
                        return ext_uri
                return entity_qname(entity.type, entity.entity_id, entity.id)

            prov = {
                "prefix": prov_prefixes(dossier_id),
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
                    akey = _agent_key(assoc.agent_id)
                    if akey not in agents_seen:
                        agents_seen.add(akey)
                        agent_data = {
                            "prov:label": assoc.agent_name or assoc.agent_id,
                            "prov:type": agent_type_value(assoc.agent_type or "prov:Person"),
                        }
                        # If the agent has a URI and we're using it as the key,
                        # also include the internal ID for reference
                        agent_row = agent_rows.get(assoc.agent_id)
                        if agent_row and agent_row.uri:
                            agent_data["oe:agentId"] = assoc.agent_id
                        prov["agent"][akey] = agent_data

            # Activities
            for act in activities:
                act_key = activity_qname(act.id)
                act_data = {
                    "prov:type": prov_type_value(act.type),
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
                        "prov:agent": _agent_key(assoc.agent_id),
                        "prov:hadRole": {
                            "$": assoc.role,
                            "type": "xsd:string",
                        },
                    }

                # used
                for used in used_by_activity.get(act.id, []):
                    entity = entity_by_id.get(used.entity_id)
                    if entity:
                        ent_key = _entity_key(entity)
                        used_key = f"_:used_{act.id}_{entity.id}"
                        prov["used"][used_key] = {
                            "prov:activity": act_key,
                            "prov:entity": ent_key,
                        }

                # wasInformedBy
                if act.informed_by_uri is not None:
                    # Cross-dossier: full IRI, no prefix expansion.
                    inform_key = f"_:informed_{act.id}"
                    prov["wasInformedBy"][inform_key] = {
                        "prov:informedActivity": act_key,
                        "prov:informantActivity": act.informed_by_uri,
                    }
                elif act.informed_by_activity_id is not None:
                    # Local activity: expand to the dossier QName.
                    inform_key = f"_:informed_{act.id}"
                    prov["wasInformedBy"][inform_key] = {
                        "prov:informedActivity": act_key,
                        "prov:informantActivity": activity_qname(
                            act.informed_by_activity_id
                        ),
                    }

            # Entities
            for entity in all_entities:
                ent_key = _entity_key(entity)
                entity_data = {
                    "prov:type": prov_type_value(entity.type),
                    "oe:entityId": str(entity.entity_id),
                    "oe:versionId": str(entity.id),
                }
                if entity.created_at:
                    entity_data["prov:generatedAtTime"] = {
                        "$": entity.created_at.isoformat(),
                        "type": "xsd:dateTime",
                    }
                prov["entity"][ent_key] = entity_data

                # wasGeneratedBy
                if entity.generated_by:
                    gen_key = f"_:gen_{entity.id}"
                    prov["wasGeneratedBy"][gen_key] = {
                        "prov:entity": ent_key,
                        "prov:activity": activity_qname(entity.generated_by),
                    }

                # wasAttributedTo
                if entity.attributed_to:
                    attr_key = f"_:attr_{entity.id}"
                    prov["wasAttributedTo"][attr_key] = {
                        "prov:entity": ent_key,
                        "prov:agent": _agent_key(entity.attributed_to),
                    }

                # wasDerivedFrom
                if entity.derived_from:
                    parent = entity_by_id.get(entity.derived_from)
                    if parent:
                        parent_key = _entity_key(parent)
                        deriv_key = f"_:deriv_{entity.id}"
                        prov["wasDerivedFrom"][deriv_key] = {
                            "prov:generatedEntity": ent_key,
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
        async with session_factory() as session, session.begin():
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

    # Archive endpoint
    from fastapi.responses import Response as RawResponse

    @app.get(
        "/dossiers/{dossier_id}/archive",
        tags=["prov"],
        summary="Dossier archive (PDF)",
        description=(
            "Generate a self-contained PDF/A archive of the dossier. "
            "Includes a cover page, provenance timeline (static SVG), "
            "entity content, and the raw PROV-JSON as an embedded attachment. "
            "Suitable for long-term archival."
        ),
    )
    async def get_dossier_archive(
        dossier_id: UUID,
        user: User = Depends(get_user),
    ):
        import tempfile, os
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            # Check access
            await check_dossier_access(repo, dossier_id, user, global_access)

            # Build PROV-JSON (reuse the same logic as the /prov endpoint)
            plugin = registry.get(dossier.workflow)

            # Inline a minimal PROV-JSON build for embedding
            from ..prov_iris import prov_prefixes, entity_qname, activity_qname, agent_qname, prov_type_value, agent_type_value
            from ..db.models import AgentRow

            activities_list = await repo.get_activities_for_dossier(dossier_id)
            all_ent_result = await session.execute(
                select(EntityRow).where(EntityRow.dossier_id == dossier_id).order_by(EntityRow.created_at)
            )
            all_ents = list(all_ent_result.scalars().all())
            act_ids = [a.id for a in activities_list]
            assoc_r = await session.execute(select(AssociationRow).where(AssociationRow.activity_id.in_(act_ids)))
            assoc_map = {}
            for a in assoc_r.scalars().all():
                assoc_map.setdefault(a.activity_id, []).append(a)
            used_r = await session.execute(select(UsedRow).where(UsedRow.activity_id.in_(act_ids)))
            used_map = {}
            for u in used_r.scalars().all():
                used_map.setdefault(u.activity_id, []).append(u)
            ent_by_id = {e.id: e for e in all_ents}

            # Agent URIs
            a_ids = {a.agent_id for aa in assoc_map.values() for a in aa}
            a_ids |= {e.attributed_to for e in all_ents if e.attributed_to}
            a_rows = {}
            if a_ids:
                ar = await session.execute(select(AgentRow).where(AgentRow.id.in_(a_ids)))
                a_rows = {a.id: a for a in ar.scalars().all()}

            def _akey(aid):
                r = a_rows.get(aid)
                return r.uri if r and r.uri else agent_qname(aid)

            def _ekey(e):
                if e.type == "external" and e.content:
                    u = e.content.get("uri")
                    if u:
                        return u
                return entity_qname(e.type, e.entity_id, e.id)

            prov = {"prefix": prov_prefixes(dossier_id), "entity": {}, "activity": {}, "agent": {}, "wasGeneratedBy": {}, "used": {}, "wasAssociatedWith": {}, "wasAttributedTo": {}, "wasDerivedFrom": {}, "wasInformedBy": {}}

            agents_seen = set()
            for aa in assoc_map.values():
                for a in aa:
                    ak = _akey(a.agent_id)
                    if ak not in agents_seen:
                        agents_seen.add(ak)
                        ad = {"prov:label": a.agent_name or a.agent_id, "prov:type": agent_type_value(a.agent_type or "prov:Person")}
                        ar_row = a_rows.get(a.agent_id)
                        if ar_row and ar_row.uri:
                            ad["oe:agentId"] = a.agent_id
                        prov["agent"][ak] = ad

            for act in activities_list:
                ak2 = activity_qname(act.id)
                prov["activity"][ak2] = {"prov:type": prov_type_value(act.type)}
                if act.started_at:
                    prov["activity"][ak2]["prov:startedAtTime"] = {"$": act.started_at.isoformat(), "type": "xsd:dateTime"}
                for a in assoc_map.get(act.id, []):
                    prov["wasAssociatedWith"][f"_:assoc_{a.id}"] = {"prov:activity": ak2, "prov:agent": _akey(a.agent_id), "prov:hadRole": {"$": a.role, "type": "xsd:string"}}
                for u in used_map.get(act.id, []):
                    e = ent_by_id.get(u.entity_id)
                    if e:
                        prov["used"][f"_:used_{act.id}_{e.id}"] = {"prov:activity": ak2, "prov:entity": _ekey(e)}
                if act.informed_by:
                    ibs = str(act.informed_by)
                    ik = ibs if ibs.startswith("http") else activity_qname(act.informed_by)
                    prov["wasInformedBy"][f"_:informed_{act.id}"] = {"prov:informedActivity": ak2, "prov:informantActivity": ik}

            for e in all_ents:
                ek = _ekey(e)
                ed = {"prov:type": prov_type_value(e.type), "oe:entityId": str(e.entity_id), "oe:versionId": str(e.id)}
                if e.created_at:
                    ed["prov:generatedAtTime"] = {"$": e.created_at.isoformat(), "type": "xsd:dateTime"}
                prov["entity"][ek] = ed
                if e.generated_by:
                    prov["wasGeneratedBy"][f"_:gen_{e.id}"] = {"prov:entity": ek, "prov:activity": activity_qname(e.generated_by)}
                if e.attributed_to:
                    prov["wasAttributedTo"][f"_:attr_{e.id}"] = {"prov:entity": ek, "prov:agent": _akey(e.attributed_to)}
                if e.derived_from:
                    p = ent_by_id.get(e.derived_from)
                    if p:
                        prov["wasDerivedFrom"][f"_:deriv_{e.id}"] = {"prov:generatedEntity": ek, "prov:usedEntity": _ekey(p)}

            prov = {k: v for k, v in prov.items() if v}

            from ..archive import generate_archive
            file_storage_root = app.state.config.get("file_service", {}).get("storage_root")
            pdf_bytes = await generate_archive(
                session, dossier_id, dossier, registry, prov,
                file_storage_root=file_storage_root,
            )

            # Write to temp file to avoid bytearray encoding issues
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            tmp.write(bytes(pdf_bytes) if isinstance(pdf_bytes, bytearray) else pdf_bytes)
            tmp.close()

            from fastapi.responses import FileResponse
            return FileResponse(
                tmp.name,
                media_type="application/pdf",
                filename=f"dossier-{str(dossier_id)[:8]}-archief.pdf",
                background=None,  # don't delete in background
            )


def _build_graph_html(dossier_id: str, workflow: str, nodes_json: str, edges_json: str) -> str:
    """Render the interactive timeline PROV graph from its Jinja2 template."""
    template = _jinja_env.get_template("prov_timeline.html")
    return template.render(
        dossier_id=dossier_id,
        workflow=workflow,
        nodes_json=nodes_json,
        edges_json=edges_json,
    )

