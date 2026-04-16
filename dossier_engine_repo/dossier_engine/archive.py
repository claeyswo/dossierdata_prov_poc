"""
Dossier archive generator — produces a self-contained PDF/A-3 package
suitable for long-term (30+ year) archival.

The archive contains:
- Cover page with dossier metadata (workflow, status, dates, actors)
- Provenance timeline rendered as a static SVG (no JavaScript)
- Entity content pages (one section per entity type, version history)
- Embedded attachments: the raw PROV-JSON for machine readability

The SVG is pure server-side Python — no browser, no D3, no external
dependencies. It uses the same layout logic as the interactive columns
graph but rendered as static vector graphics that survive PDF embedding.

Usage:
    from dossier_engine.archive import generate_archive

    pdf_bytes = await generate_archive(session, dossier_id, plugin, registry)
"""

from __future__ import annotations

import json
import io
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fpdf import FPDF

from .db.models import (
    ActivityRow, EntityRow, AssociationRow, UsedRow, AgentRow,
    DossierRow, Repository,
)
from .prov_iris import (
    prov_prefixes, entity_qname, activity_qname,
    agent_qname, prov_type_value, activity_full_iri,
)

from sqlalchemy import select


# ── Colours ──────────────────────────────────────────────────────

COL_BG = "#0f172a"
COL_ACTIVITY = "#3b82f6"
COL_ENTITY = "#10b981"
COL_AGENT = "#f59e0b"
COL_SYSTEM = "#8b5cf6"
COL_TASK = "#a78bfa"
COL_EXTERNAL = "#6b7280"
COL_DERIVED = "#34d399"
COL_TEXT = "#e2e8f0"
COL_MUTED = "#64748b"
COL_LINE = "#334155"


# ── SVG timeline renderer ───────────────────────────────────────

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def render_timeline_svg(
    activities: list[dict],
    entities_by_type: dict[str, list[dict]],
    agents: dict[str, str],
    used_map: dict[str, list[str]],
    generated_map: dict[str, list[str]],
    derivations: list[tuple[str, str]],
    *,
    width: int = 1200,
) -> str:
    """Render a static SVG of the provenance timeline.

    Layout:
    - Top band: activities as columns, left to right chronologically
    - Middle: entity rows grouped by type, versions placed under their
      generating activity's column
    - Lines: wasGeneratedBy (down), used (up), wasDerivedFrom (horizontal)

    Returns SVG markup as a string.
    """
    if not activities:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="100"><text x="20" y="50" fill="#64748b" font-size="14">Geen activiteiten in dit dossier</text></svg>'

    # Layout constants
    col_w = max(140, min(200, (width - 160) // max(len(activities), 1)))
    margin_left = 180
    margin_top = 80
    act_y = margin_top + 20
    row_h = 50
    entity_start_y = act_y + 70

    # Collect entity type rows
    type_order = list(entities_by_type.keys())
    total_height = entity_start_y + len(type_order) * row_h + 60

    # Build activity position map
    act_x = {}
    for i, act in enumerate(activities):
        act_x[act["id"]] = margin_left + i * col_w

    svg_parts = []
    svg_w = margin_left + len(activities) * col_w + 80
    svg_parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{svg_w}" height="{total_height}" '
        f'viewBox="0 0 {svg_w} {total_height}" '
        f'style="background:{COL_BG}; font-family: sans-serif;">'
    )

    # ── Activity columns ──
    for i, act in enumerate(activities):
        x = act_x[act["id"]]
        # Vertical guide line
        svg_parts.append(
            f'<line x1="{x}" y1="{act_y + 25}" x2="{x}" y2="{total_height - 30}" '
            f'stroke="{COL_LINE}" stroke-width="1" stroke-dasharray="3,6" opacity="0.3"/>'
        )
        # Activity box
        bw = col_w - 20
        svg_parts.append(
            f'<rect x="{x - bw//2}" y="{act_y - 15}" width="{bw}" height="30" '
            f'rx="6" fill="{COL_ACTIVITY}" opacity="0.9"/>'
        )
        # Label
        label = act["type"]
        if label.startswith("oe:"):
            label = label[3:]
        if len(label) > 18:
            label = label[:16] + "..."
        svg_parts.append(
            f'<text x="{x}" y="{act_y + 4}" text-anchor="middle" '
            f'fill="white" font-size="9" font-weight="500">{_esc(label)}</text>'
        )
        # Time
        if act.get("time"):
            t = act["time"]
            if "T" in t:
                t = t.split("T")[1][:8]
            svg_parts.append(
                f'<text x="{x}" y="{act_y + 22}" text-anchor="middle" '
                f'fill="{COL_MUTED}" font-size="8">{t}</text>'
            )
        # Agent
        if act.get("agent"):
            svg_parts.append(
                f'<text x="{x}" y="{act_y - 22}" text-anchor="middle" '
                f'fill="{COL_AGENT}" font-size="8">{_esc(act["agent"][:20])}</text>'
            )

    # ── Entity type rows ──
    entity_positions = {}  # version_id → (x, y)

    for row_idx, etype in enumerate(type_order):
        y = entity_start_y + row_idx * row_h
        # Row label
        label = etype
        if label.startswith("oe:"):
            label = label[3:]
        svg_parts.append(
            f'<text x="{margin_left - 15}" y="{y + 5}" text-anchor="end" '
            f'fill="{COL_MUTED}" font-size="10" font-style="italic">{_esc(label)}</text>'
        )
        # Row line
        svg_parts.append(
            f'<line x1="{margin_left - 10}" y1="{y}" '
            f'x2="{margin_left + (len(activities) - 1) * col_w + 10}" y2="{y}" '
            f'stroke="{COL_LINE}" stroke-width="1" stroke-dasharray="2,6" opacity="0.3"/>'
        )

        # Place entity versions
        versions = entities_by_type[etype]
        for ver in versions:
            gen_act = ver.get("generated_by")
            if gen_act and gen_act in act_x:
                ex = act_x[gen_act]
            else:
                ex = margin_left

            is_task = etype in ("system:task",)
            is_ext = etype == "external"
            col = COL_TASK if is_task else (COL_EXTERNAL if is_ext else COL_ENTITY)

            # Entity marker
            svg_parts.append(
                f'<rect x="{ex - 30}" y="{y - 10}" width="60" height="20" '
                f'rx="10" fill="{col}" opacity="0.85"/>'
            )
            vlabel = f'v{ver.get("version_idx", "?")}' if not is_ext else "ext"
            svg_parts.append(
                f'<text x="{ex}" y="{y + 4}" text-anchor="middle" '
                f'fill="white" font-size="8">{vlabel}</text>'
            )

            entity_positions[ver["version_id"]] = (ex, y)

            # wasGeneratedBy line (activity → entity)
            if gen_act and gen_act in act_x:
                ax = act_x[gen_act]
                svg_parts.append(
                    f'<line x1="{ax}" y1="{act_y + 15}" x2="{ex}" y2="{y - 10}" '
                    f'stroke="{COL_ACTIVITY}" stroke-width="0.8" opacity="0.3"/>'
                )

    # ── Derivation arrows ──
    for from_vid, to_vid in derivations:
        if from_vid in entity_positions and to_vid in entity_positions:
            x1, y1 = entity_positions[from_vid]
            x2, y2 = entity_positions[to_vid]
            svg_parts.append(
                f'<line x1="{x1 + 30}" y1="{y1}" x2="{x2 - 30}" y2="{y2}" '
                f'stroke="{COL_DERIVED}" stroke-width="2" opacity="0.6" '
                f'/>'
            )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


def _esc(s: str) -> str:
    """Escape XML special characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ── PDF assembly ─────────────────────────────────────────────────

class ArchivePDF(FPDF):
    """Custom PDF with header/footer for the dossier archive."""

    def __init__(self, dossier_id: str, workflow: str):
        super().__init__(orientation="L", format="A4")
        self._dossier_id = dossier_id
        self._workflow = workflow
        self.set_auto_page_break(auto=True, margin=20)
        # Unicode font for full character support
        self.add_font("DejaVu", "", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        self.add_font("DejaVu", "B", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        self.add_font("DejaVu", "I", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf")
        self.add_font("DejaVuMono", "", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")

    def header(self):
        self.set_font("DejaVu", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(
            0, 6,
            f"Dossier {self._dossier_id[:8]}... — {self._workflow} — Archief",
            new_x="LMARGIN", new_y="NEXT",
        )
        self.line(10, 12, self.w - 10, 12)
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("DejaVu", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, f"Pagina {self.page_no()}/{{nb}}", align="C")


async def generate_archive(
    session,
    dossier_id: UUID,
    dossier: DossierRow,
    registry,
    prov_json: dict,
    file_storage_root: str | None = None,
) -> bytes:
    """Generate a PDF/A archive for a dossier.

    Args:
        session: active DB session
        dossier_id: the dossier UUID
        dossier: the DossierRow
        registry: PluginRegistry
        prov_json: the PROV-JSON dict (already computed by the caller)
        file_storage_root: path to the file service storage directory.
            If provided, bijlagen files are embedded in the PDF.

    Returns:
        PDF file content as bytes
    """
    repo = Repository(session)
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
    assoc_by_activity = defaultdict(list)
    for a in assoc_result.scalars().all():
        assoc_by_activity[a.activity_id].append(a)

    used_result = await session.execute(
        select(UsedRow).where(UsedRow.activity_id.in_(activity_ids))
    )
    used_by_activity = defaultdict(list)
    for u in used_result.scalars().all():
        used_by_activity[u.activity_id].append(u)

    # Load agents for names
    agent_ids = {a.agent_id for assocs in assoc_by_activity.values() for a in assocs}
    agent_result = await session.execute(
        select(AgentRow).where(AgentRow.id.in_(agent_ids))
    )
    agent_rows = {a.id: a for a in agent_result.scalars().all()}

    entity_by_id = {e.id: e for e in all_entities}

    # Filter out system internals for the visual timeline
    system_types = set()
    if plugin:
        for ad in plugin.workflow.get("activities", []):
            if ad.get("client_callable") is False:
                system_types.add(ad["name"])

    visible_activities = [
        a for a in activities
        if a.type not in system_types and a.type != "systemAction"
    ]
    visible_entities = [
        e for e in all_entities
        if e.type not in ("system:task", "system:note")
        and e.tombstoned_by is None
    ]

    # ── Build SVG data ──

    act_data = []
    for act in visible_activities:
        assocs = assoc_by_activity.get(act.id, [])
        agent_name = None
        if assocs:
            aid = assocs[0].agent_id
            ar = agent_rows.get(aid)
            agent_name = ar.name if ar else assocs[0].agent_name or aid

        act_data.append({
            "id": str(act.id),
            "type": act.type,
            "time": act.started_at.isoformat() if act.started_at else "",
            "agent": agent_name,
        })

    # Group entities by type, track versions
    entities_by_type = defaultdict(list)
    logical_groups = defaultdict(list)
    for e in visible_entities:
        logical_groups[(e.type, e.entity_id)].append(e)
    for key in logical_groups:
        logical_groups[key].sort(key=lambda x: x.created_at or datetime.min)

    for e in visible_entities:
        key = (e.type, e.entity_id)
        group = logical_groups[key]
        version_idx = next(i for i, x in enumerate(group) if x.id == e.id) + 1
        entities_by_type[e.type].append({
            "version_id": str(e.id),
            "entity_id": str(e.entity_id),
            "generated_by": str(e.generated_by) if e.generated_by else None,
            "version_idx": version_idx,
            "content": e.content,
        })

    # Derivation pairs
    derivations = []
    for e in visible_entities:
        if e.derived_from:
            derivations.append((str(e.derived_from), str(e.id)))

    # Used/generated maps (for reference, not drawn in basic SVG)
    used_map = {}
    generated_map = {}

    svg = render_timeline_svg(
        act_data, dict(entities_by_type),
        {aid: (ar.name or aid) for aid, ar in agent_rows.items()},
        used_map, generated_map, derivations,
    )

    # ── Assemble PDF ──

    pdf = ArchivePDF(str(dossier_id), dossier.workflow)
    pdf.alias_nb_pages()

    # --- Cover page ---
    pdf.add_page()
    pdf.set_font("DejaVu", "B", 20)
    pdf.cell(0, 15, f"Dossier Archief", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 12)
    pdf.cell(0, 8, f"Workflow: {dossier.workflow}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"Dossier ID: {dossier_id}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"Status: {dossier.cached_status or 'onbekend'}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"Aangemaakt: {dossier.created_at.strftime('%Y-%m-%d %H:%M') if dossier.created_at else 'onbekend'}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"Archief gegenereerd: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Agents involved
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(0, 8, "Betrokken actoren:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 10)
    seen_agents = set()
    for assocs in assoc_by_activity.values():
        for a in assocs:
            if a.agent_id not in seen_agents:
                seen_agents.add(a.agent_id)
                ar = agent_rows.get(a.agent_id)
                name = ar.name if ar else a.agent_name or a.agent_id
                uri = ar.uri if ar else ""
                role = a.agent_type or ""
                pdf.cell(0, 6, f"  • {name} ({role}) — {uri or a.agent_id}", new_x="LMARGIN", new_y="NEXT")

    # Activity summary
    pdf.ln(5)
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(0, 8, f"Activiteiten ({len(activities)} totaal, {len(visible_activities)} zichtbaar):", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 9)
    for act in visible_activities:
        assocs = assoc_by_activity.get(act.id, [])
        agent_str = ", ".join(set(
            (agent_rows.get(a.agent_id).name if agent_rows.get(a.agent_id) else a.agent_id)
            for a in assocs
        ))
        time_str = act.started_at.strftime("%Y-%m-%d %H:%M:%S") if act.started_at else ""
        pdf.cell(0, 5, f"  {time_str}  {act.type}  ({agent_str})", new_x="LMARGIN", new_y="NEXT")

    # --- Provenance graph page ---
    pdf.add_page()
    pdf.set_font("DejaVu", "B", 14)
    pdf.cell(0, 10, "Provenance Tijdlijn", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Embed SVG as image
    svg_bytes = svg.encode("utf-8")
    pdf.image(io.BytesIO(svg_bytes), x=10, y=30, w=pdf.w - 20)

    # --- Entity content pages: ALL versions, ALL types including external ---
    all_types = list(entities_by_type.keys())
    # Also include types that were filtered from the SVG (system:task, system:note)
    all_type_set = set(all_types)
    for e in all_entities:
        if e.type not in all_type_set:
            all_type_set.add(e.type)
            all_types.append(e.type)
            entities_by_type[e.type] = []

    # Re-collect ALL entities including system types for the content pages
    all_logical_groups = defaultdict(list)
    for e in all_entities:
        all_logical_groups[(e.type, e.entity_id)].append(e)
    for key in all_logical_groups:
        all_logical_groups[key].sort(key=lambda x: x.created_at or datetime.min)

    if all_types:
        pdf.add_page()
        pdf.set_font("DejaVu", "B", 14)
        pdf.cell(0, 10, "Entiteiten -- Volledige versiegeschiedenis", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, f"Alle {len(all_entities)} entiteitversies uit {len(all_logical_groups)} logische entiteiten.", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

        # Sort: external first, then domain types, then system types
        def _type_sort_key(t):
            if t == "external":
                return (0, t)
            if t.startswith("system:"):
                return (2, t)
            return (1, t)

        for etype in sorted(all_types, key=_type_sort_key):
            groups_for_type = {
                k: v for k, v in all_logical_groups.items() if k[0] == etype
            }
            if not groups_for_type:
                continue

            pdf.ln(4)
            pdf.set_font("DejaVu", "B", 11)
            pdf.set_fill_color(230, 240, 250)
            pdf.cell(0, 8, f"  {etype}  ({len(groups_for_type)} entiteit(en))", new_x="LMARGIN", new_y="NEXT", fill=True)

            for (_, eid), versions in groups_for_type.items():
                pdf.ln(2)
                pdf.set_font("DejaVu", "B", 9)
                pdf.set_text_color(50, 50, 50)
                pdf.cell(0, 5, f"  Entity: {eid}", new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)

                for vi, ver in enumerate(versions, 1):
                    # Check if we need a new page
                    if pdf.get_y() > pdf.h - 40:
                        pdf.add_page()

                    pdf.set_font("DejaVu", "I", 8)
                    pdf.set_text_color(80, 80, 80)
                    ts = ver.created_at.strftime("%Y-%m-%d %H:%M:%S") if ver.created_at else "?"
                    derived_note = ""
                    if ver.derived_from:
                        derived_note = f"  (afgeleid van versie {str(ver.derived_from)[:8]}...)"
                    tombstone_note = ""
                    if ver.tombstoned_by:
                        tombstone_note = "  [VERWIJDERD]"
                    pdf.cell(0, 4, f"    Versie {vi} -- {str(ver.id)[:12]}... -- {ts}{derived_note}{tombstone_note}", new_x="LMARGIN", new_y="NEXT")
                    pdf.set_text_color(0, 0, 0)

                    if ver.content:
                        pdf.set_font("DejaVuMono", "", 7)
                        content_str = json.dumps(ver.content, indent=2, ensure_ascii=False, default=str)
                        lines = content_str.split("\n")
                        for line in lines[:50]:
                            if pdf.get_y() > pdf.h - 20:
                                pdf.add_page()
                            safe_line = line[:140].replace("\t", "  ")
                            pdf.cell(0, 3.5, f"      {safe_line}", new_x="LMARGIN", new_y="NEXT")
                        if len(lines) > 50:
                            pdf.cell(0, 3.5, f"      ... ({len(lines) - 50} regels weggelaten)", new_x="LMARGIN", new_y="NEXT")
                    else:
                        pdf.set_font("DejaVu", "I", 8)
                        pdf.set_text_color(150, 150, 150)
                        pdf.cell(0, 4, "      (geen inhoud)", new_x="LMARGIN", new_y="NEXT")
                        pdf.set_text_color(0, 0, 0)
                    pdf.ln(1)

    # --- PROV-JSON as readable pages ---
    pdf.add_page()
    pdf.set_font("DejaVu", "B", 14)
    pdf.cell(0, 10, "PROV-JSON -- Machine-leesbare provenance", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, "Volledige W3C PROV-JSON export van dit dossier.", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    prov_str = json.dumps(prov_json, indent=2, ensure_ascii=False, default=str)
    pdf.set_font("DejaVuMono", "", 6)
    for line in prov_str.split("\n"):
        if pdf.get_y() > pdf.h - 15:
            pdf.add_page()
            pdf.set_font("DejaVuMono", "", 6)
        safe_line = line[:180].replace("\t", "  ")
        pdf.cell(0, 3, safe_line, new_x="LMARGIN", new_y="NEXT")

    # --- Collect bijlagen from all entity versions ---
    import os
    bijlagen_files = []  # [(file_id, filename, content_type, file_bytes)]
    seen_file_ids = set()

    if file_storage_root:
        for e in all_entities:
            if not e.content or not isinstance(e.content, dict):
                continue
            for bijlage in e.content.get("bijlagen", []):
                fid = bijlage.get("file_id")
                if not fid or fid in seen_file_ids:
                    continue
                seen_file_ids.add(fid)
                fname = bijlage.get("filename", fid)
                ctype = bijlage.get("content_type", "application/octet-stream")

                # Try both temp/ and permanent/ subdirectories
                file_path = None
                for subdir in ("temp", "permanent", ""):
                    candidate = os.path.join(file_storage_root, subdir, fid) if subdir else os.path.join(file_storage_root, fid)
                    if os.path.isfile(candidate):
                        file_path = candidate
                        break

                if file_path:
                    try:
                        with open(file_path, "rb") as f:
                            file_data = f.read()
                        bijlagen_files.append((fid, fname, ctype, file_data))
                    except OSError:
                        pass  # skip unreadable files

    # --- Bijlagen summary page ---
    if bijlagen_files:
        pdf.add_page()
        pdf.set_font("DejaVu", "B", 14)
        pdf.cell(0, 10, "Bijlagen -- Ingesloten bestanden", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, f"{len(bijlagen_files)} bestand(en) ingesloten als PDF/A-3 bijlage(n).", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

        for fid, fname, ctype, fdata in bijlagen_files:
            pdf.set_font("DejaVu", "B", 10)
            pdf.cell(0, 6, f"  {fname}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("DejaVu", "", 8)
            pdf.set_text_color(80, 80, 80)
            pdf.cell(0, 5, f"    Type: {ctype}  |  Grootte: {len(fdata):,} bytes  |  ID: {fid}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)

    # --- PDF/A-3b XMP metadata ---
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    xmp = f'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"
      xmlns:prov="http://www.w3.org/ns/prov#">
      <pdfaid:part>3</pdfaid:part>
      <pdfaid:conformance>B</pdfaid:conformance>
      <dc:title>Dossier Archief {dossier_id}</dc:title>
      <dc:creator>dossier-platform</dc:creator>
      <dc:description>PDF/A-3b archief van dossier {dossier_id} ({dossier.workflow})</dc:description>
      <xmp:CreateDate>{now_iso}</xmp:CreateDate>
      <xmp:ModifyDate>{now_iso}</xmp:ModifyDate>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>'''
    pdf.set_xmp_metadata(xmp)

    # Embed PROV-JSON as PDF/A-3 attachment
    prov_bytes = prov_str.encode("utf-8")
    pdf.embed_file(
        basename="prov.json",
        bytes=prov_bytes,
        mime_type="application/json",
    )

    # Embed bijlagen files as PDF/A-3 attachments
    for fid, fname, ctype, fdata in bijlagen_files:
        pdf.embed_file(
            basename=fname,
            bytes=fdata,
            mime_type=ctype,
        )

    return bytes(pdf.output())
