"""Deterministic and AI-enriched documentation renderers (Markdown, HTML, JSON)."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from tableau_doc_app.audit_models import AIEvaluationReport, WorkbookAuditReport
from tableau_doc_app.models import (
    DashboardInfo,
    DocumentationBundle,
    WorkbookSnapshot,
    ZoneInfo,
)


def bundle_from_snapshot(
    snapshot: WorkbookSnapshot,
    *,
    audit: Optional[WorkbookAuditReport] = None,
    ai_evaluation: Optional[AIEvaluationReport] = None,
    target_dashboard: Optional[str] = None,
    audience: str = "Business & Technical Stakeholders",
    warnings: Optional[list[str]] = None,
) -> DocumentationBundle:
    """Wrap a deterministic WorkbookSnapshot (and optional audit) in a DocumentationBundle."""
    return DocumentationBundle(
        snapshot=snapshot,
        ai_report=None,
        audit=audit,
        ai_evaluation=ai_evaluation,
        target_dashboard=target_dashboard,
        audience=audience,
        generated_at_iso=datetime.now(timezone.utc).isoformat(),
        used_ai=False,
        warnings=list(warnings or []),
    )


def _escape_md_cell(value: Optional[str]) -> str:
    """Sanitize a string for safe inclusion inside a GFM table cell."""
    if not value:
        return "—"
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned.replace("|", "\\|")


def _escape_inline_code(value: Optional[str]) -> str:
    """Wrap a string safely in inline Markdown code ticks."""
    if not value:
        return "—"
    text = re.sub(r"\s+", " ", str(value)).strip()
    fence = "``" if "`" in text else "`"
    return f"{fence}{text}{fence}"


def _sanitize_mermaid(diagram: Optional[str], fallback: str = "") -> str:
    """Strip accidental Markdown fences from Mermaid code blocks."""
    raw = (diagram or "").strip() or (fallback or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    return raw


def _scoped_worksheets_and_dashboards(
    bundle: DocumentationBundle,
) -> tuple[list[DashboardInfo], set[str]]:
    """Return dashboards ordered with target first, and worksheet names in scope."""
    snap = bundle.snapshot
    target = (bundle.target_dashboard or "").strip()
    if not target:
        return list(snap.dashboards), {w.name for w in snap.worksheets}

    matching = [d for d in snap.dashboards if d.name.lower() == target.lower()]
    if not matching:
        matching = [d for d in snap.dashboards if target.lower() in d.name.lower()]
    if not matching:
        return list(snap.dashboards), {w.name for w in snap.worksheets}

    others = [d for d in snap.dashboards if d not in matching]
    in_scope_ws: set[str] = set()
    for d in matching:
        in_scope_ws.update(d.worksheets)
    return matching + others, in_scope_ws


def _format_list(items: Iterable[str], fallback: str = "None") -> str:
    cleaned = [str(i).strip() for i in items if str(i).strip()]
    return ", ".join(cleaned) if cleaned else fallback


def _calc_explanation_map(bundle: DocumentationBundle) -> dict[tuple[str, str], tuple[str, str]]:
    """Index AI calculated field docs by lowercase (datasource, name) and (_, name)."""
    mapping: dict[tuple[str, str], tuple[str, str]] = {}
    if not bundle.ai_report:
        return mapping
    for doc in bundle.ai_report.calculated_fields_explained:
        val = (doc.plain_english_explanation, doc.dependencies_and_usage)
        mapping[(doc.datasource.lower(), doc.name.lower())] = val
        mapping[("", doc.name.lower())] = val
    return mapping


def render_json(bundle: DocumentationBundle, *, include_thumbnails: bool = False) -> str:
    """Render DocumentationBundle as pretty-printed UTF-8 JSON."""
    target = bundle
    if not include_thumbnails and bundle.snapshot.thumbnails:
        target = bundle.model_copy(deep=True)
        for thumb in target.snapshot.thumbnails:
            thumb.base64_png = ""
    return json.dumps(target.model_dump(mode="json"), indent=2, ensure_ascii=False)


def render_markdown(bundle: DocumentationBundle) -> str:
    """Render a comprehensive GitHub-Flavored Markdown documentation report."""
    snap = bundle.snapshot
    ai = bundle.ai_report
    title = (ai.workbook_title if ai else "") or snap.workbook_name or snap.filename
    mode_label = (
        f"AI-Enriched (`{bundle.agent_model or 'Antigravity SDK Default'}`)"
        if bundle.used_ai and ai
        else "Deterministic Metadata Extraction (No AI)"
    )

    lines: list[str] = [
        f"# {title}",
        "",
        f"> **Source File:** `{snap.filename}` | **Tableau Version:** `{snap.tableau_version or 'Unknown'}` | **Mode:** {mode_label} | **Generated:** `{bundle.generated_at_iso}`",
        "",
    ]

    if bundle.target_dashboard:
        lines.append(f"**Focused Dashboard Scope:** `{bundle.target_dashboard}`  ")
    lines.append(f"**Target Audience:** {bundle.audience}")
    lines.append("")

    if bundle.usage and bundle.usage.total_tokens:
        lines.append(
            f"**Antigravity SDK Usage:** `{bundle.usage.total_tokens:,}` total tokens "
            f"(prompt `{bundle.usage.prompt_tokens:,}`, cached `{bundle.usage.cached_tokens:,}`, "
            f"output `{bundle.usage.candidate_tokens:,}`, thoughts `{bundle.usage.thoughts_tokens:,}`)"
        )
        lines.append("")

    if bundle.warnings:
        lines.append("## Notices & Warnings")
        lines.append("")
        for warn in bundle.warnings:
            lines.append(f"- {warn}")
        lines.append("")

    # Overview counts
    real_datasources = [ds for ds in snap.datasources if not ds.is_parameters]
    calc_count = sum(len(ds.calculated_fields) for ds in real_datasources)
    lines.extend(
        [
            "## Workbook Inventory Summary",
            "",
            "| Asset Type | Count |",
            "| --- | ---: |",
            f"| Dashboards | {len(snap.dashboards)} |",
            f"| Worksheets | {len(snap.worksheets)} |",
            f"| Data Sources (excluding Parameters) | {len(real_datasources)} |",
            f"| Calculated Fields | {calc_count} |",
            f"| Workbook Parameters | {len(snap.parameters)} |",
            f"| Interactive Actions | {len(snap.actions)} |",
            f"| Embedded Thumbnails | {len(snap.thumbnails)} |",
            "",
        ]
    )

    # AI Executive Summary & KPI Glossary
    if ai:
        lines.extend(["## Executive Summary", "", ai.executive_summary.strip(), ""])
        if ai.target_audience:
            lines.append(f"**Intended Stakeholders:** {', '.join(ai.target_audience)}")
            lines.append("")

        if ai.kpi_glossary:
            lines.extend(
                [
                    "## KPI & Metric Glossary",
                    "",
                    "| KPI / Metric | Business Definition | Formula / Aggregation | Data Source & Worksheets |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for kpi in ai.kpi_glossary:
                lines.append(
                    f"| **{_escape_md_cell(kpi.name)}** | "
                    f"{_escape_md_cell(kpi.business_definition)} | "
                    f"{_escape_inline_code(kpi.formula_or_aggregation)} | "
                    f"{_escape_md_cell(kpi.datasource_and_worksheets)} |"
                )
            lines.append("")

    # Dashboards section (AI narrative merged with deterministic layout & zones)
    ai_dash_map = {d.name.lower(): d for d in (ai.dashboards if ai else [])}
    ordered_dashboards, in_scope_ws = _scoped_worksheets_and_dashboards(bundle)
    if ordered_dashboards:
        lines.extend(["## Dashboards", ""])
        for dash in ordered_dashboards:
            is_target = bool(
                bundle.target_dashboard
                and bundle.target_dashboard.lower() in dash.name.lower()
            )
            tag = " *(Primary Scope)*" if is_target else ""
            lines.append(f"### Dashboard: {dash.name}{tag}")
            lines.append("")
            size_desc = (
                f"{dash.min_width or '?'}x{dash.min_height or '?'} -> "
                f"{dash.max_width or '?'}x{dash.max_height or '?'}"
            )
            lines.extend(
                [
                    f"- **Canvas Size Range:** `{size_desc}`",
                    f"- **Worksheets ({len(dash.worksheets)}):** {_format_list(dash.worksheets)}",
                    f"- **Exposed Filters:** {_format_list(dash.exposed_filters)}",
                    f"- **Exposed Parameters:** {_format_list(dash.exposed_parameters)}",
                    f"- **Device Layouts:** {_format_list(dash.device_layouts, 'Default Desktop only')}",
                ]
            )
            if dash.text_blocks:
                lines.append(f"- **Text Annotations:** {' | '.join(dash.text_blocks)}")
            lines.append("")

            ai_dash = ai_dash_map.get(dash.name.lower())
            if ai_dash:
                lines.extend(
                    [
                        f"**Business Purpose:** {ai_dash.purpose}",
                        "",
                        f"**Visual Layout & Hierarchy:** {ai_dash.visual_layout_summary}",
                        "",
                        f"**Controls & Filters:** {ai_dash.controls_and_filters}",
                        "",
                        f"**Interactivity Walkthrough:** {ai_dash.interactivity_narrative}",
                        "",
                    ]
                )
                if ai_dash.key_caveats:
                    lines.append("**Key Caveats:**")
                    for cav in ai_dash.key_caveats:
                        lines.append(f"- {cav}")
                    lines.append("")

            meaningful_zones = [
                z
                for z in dash.zones
                if z.zone_type not in ("layout-basic", "layout-flow", "empty")
            ]
            if meaningful_zones:
                lines.extend(
                    [
                        "#### Layout Zones",
                        "",
                        "| Zone ID | Type | Object / Target | Coords (x, y, w, h) | Details |",
                        "| --- | --- | --- | --- | --- |",
                    ]
                )
                for z in meaningful_zones[:80]:
                    coords = (
                        f"({z.x}, {z.y}, {z.w}, {z.h})"
                        if z.w is not None and z.h is not None
                        else "—"
                    )
                    target_label = z.name or z.param_readable or z.param or "—"
                    detail = z.mode or z.text_content or "—"
                    lines.append(
                        f"| `{z.zone_id or '—'}` | `{z.zone_type}` | "
                        f"{_escape_md_cell(target_label)} | `{coords}` | "
                        f"{_escape_md_cell(detail)} |"
                    )
                lines.append("")
    else:
        lines.extend(
            [
                "## Dashboards",
                "",
                "_No dashboards are defined in this workbook; documentation covers worksheets and data sources directly._",
                "",
            ]
        )

    # Worksheets section
    ai_ws_map = {w.name.lower(): w for w in (ai.worksheets if ai else [])}
    if snap.worksheets:
        lines.extend(["## Worksheets", ""])
        for ws in snap.worksheets:
            scope_note = (
                " *(in dashboard scope)*"
                if bundle.target_dashboard and ws.name in in_scope_ws
                else ""
            )
            lines.append(f"### Worksheet: {ws.name}{scope_note}")
            lines.append("")
            if ws.title and ws.title != ws.name:
                lines.append(f"- **Displayed Title:** {ws.title}")
            lines.extend(
                [
                    f"- **Mark Types:** {_format_list(ws.mark_types, 'Automatic')}",
                    f"- **Data Sources:** {_format_list(ws.datasources)}",
                    f"- **Used in Dashboards:** {_format_list(ws.used_in_dashboards, 'Standalone')}",
                    f"- **Rows Shelf:** {_escape_inline_code(ws.rows_readable or ws.rows_raw)}",
                    f"- **Columns Shelf:** {_escape_inline_code(ws.cols_readable or ws.cols_raw)}",
                ]
            )
            if ws.encodings:
                enc_summary = "; ".join(
                    f"`{e.channel}` = `{e.column_readable}`" for e in ws.encodings
                )
                lines.append(f"- **Visual Encodings:** {enc_summary}")
            if ws.filters:
                filt_summary = "; ".join(
                    f"`{f.column_readable}` ({f.summary or f.filter_class or 'filter'})"
                    for f in ws.filters
                )
                lines.append(f"- **Filters:** {filt_summary}")
            if ws.sorts:
                lines.append(f"- **Sorts:** {'; '.join(ws.sorts)}")
            if ws.referenced_calculations:
                lines.append(
                    f"- **Referenced Calculations:** {_format_list(ws.referenced_calculations)}"
                )
            if ws.tooltip_text:
                lines.append(f"- **Tooltip Template:** {_escape_inline_code(ws.tooltip_text)}")
            lines.append("")

            ai_ws = ai_ws_map.get(ws.name.lower())
            if ai_ws:
                lines.extend(
                    [
                        f"**Chart Type & Analytical Role:** *{ai_ws.chart_type}* — {ai_ws.purpose}",
                        "",
                        f"**Axes, Encodings & Filters Summary:** {ai_ws.axes_encodings_and_filters}",
                        "",
                    ]
                )

    # Calculated fields
    calc_docs = _calc_explanation_map(bundle)
    if calc_count > 0:
        lines.extend(["## Calculated Fields", ""])
        for ds in real_datasources:
            if not ds.calculated_fields:
                continue
            lines.append(f"### Data Source: {ds.caption or ds.name}")
            lines.append("")
            for cf in ds.calculated_fields:
                caption = cf.caption or cf.name
                formula = cf.readable_calculation or cf.calculation or ""
                lines.append(f"#### `{caption}`")
                lines.append("")
                lines.append(
                    f"- **Datatype / Role:** `{cf.datatype or '?'}` / `{cf.role or '?'}` | "
                    f"**Used In:** {_format_list(cf.worksheets, 'Unused / Data Source only')}"
                )
                if cf.description:
                    lines.append(f"- **Workbook Comment:** {cf.description}")
                ai_cf = calc_docs.get((ds.caption.lower(), caption.lower())) or calc_docs.get(
                    ("", caption.lower())
                )
                if ai_cf:
                    lines.append(f"- **Plain-English Meaning:** {ai_cf[0]}")
                    lines.append(f"- **Dependencies & Usage:** {ai_cf[1]}")
                lines.extend(["", "```tableau", formula, "```", ""])

    # Parameters
    if snap.parameters:
        lines.extend(
            [
                "## Parameters",
                "",
                "| Name | Datatype | Domain | Current Value | Range / Allowable Values |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for p in snap.parameters:
            if p.allowable_values:
                domain_desc = ", ".join(p.allowable_values[:12])
                if len(p.allowable_values) > 12:
                    domain_desc += f" (+{len(p.allowable_values) - 12} more)"
            elif p.min_value is not None or p.max_value is not None:
                domain_desc = f"{p.min_value or '?'} .. {p.max_value or '?'} (step {p.step_size or '1'})"
            else:
                domain_desc = "Any"
            lines.append(
                f"| **{_escape_md_cell(p.name)}** | `{p.datatype or '—'}` | "
                f"`{p.domain_type or '—'}` | {_escape_inline_code(p.current_value)} | "
                f"{_escape_md_cell(domain_desc)} |"
            )
        lines.append("")

    # Actions
    if snap.actions:
        lines.extend(
            [
                "## Interactive Actions",
                "",
                "| Action | Type | Trigger | Source | Target | Fields / URL |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for act in snap.actions:
            source = act.source_dashboard or act.source_worksheet or "Workbook"
            target = act.target_scope or "All"
            extra = act.url or ", ".join(act.field_captions) or act.target_group_or_param or "—"
            lines.append(
                f"| **{_escape_md_cell(act.caption or act.name)}** | `{act.action_type}` | "
                f"`{act.trigger or '—'}` | {_escape_md_cell(source)} | "
                f"{_escape_md_cell(target)} | {_escape_md_cell(extra)} |"
            )
        lines.append("")

    # Data Architecture & Data Sources
    lines.extend(["## Data Architecture & Data Sources", ""])
    if ai:
        lines.extend([ai.data_architecture.overview, ""])
        if ai.data_architecture.sources_and_lineage:
            lines.append("**Source Lineage Highlights:**")
            for item in ai.data_architecture.sources_and_lineage:
                lines.append(f"- {item}")
            lines.append("")
        if ai.data_architecture.refresh_and_security_notes:
            lines.append(
                f"**Refresh & Security Notes:** {ai.data_architecture.refresh_and_security_notes}"
            )
            lines.append("")

    for ds in real_datasources:
        lines.append(f"### {ds.caption or ds.name}")
        lines.append("")
        lines.append(
            f"- **Internal ID:** `{ds.name}` | **Extract Enabled:** `{'Yes' if ds.has_extract else 'No'}` | "
            f"**Fields:** `{len(ds.fields)}` (`{len(ds.calculated_fields)}` calculated)"
        )
        if ds.connections:
            for idx, conn in enumerate(ds.connections, 1):
                target = conn.dbname or conn.filename or conn.server or conn.service or "default"
                auth = f", auth=`{conn.authentication}`" if conn.authentication else ""
                lines.append(
                    f"- **Connection {idx}:** `{conn.dbclass or 'unknown'}` -> `{target}`"
                    f"{f' (server `{conn.server}`)' if conn.server else ''}{auth}"
                )
        if ds.logical_tables:
            lines.append(f"- **Logical Tables:** {_format_list(ds.logical_tables)}")
        if ds.relationships:
            rel_txt = "; ".join(
                f"`{r.left_object}` <-> `{r.right_object}` on `{r.expression}`"
                for r in ds.relationships
            )
            lines.append(f"- **Object-Graph Relationships:** {rel_txt}")
        if ds.relations:
            lines.append(
                "- **Physical Relations / Joins / Custom SQL:** "
                + ", ".join(
                    f"`{r.relation_type or 'table'}:{r.table or r.name or '?'}`"
                    for r in ds.relations[:25]
                )
            )
            for rel in ds.relations:
                if rel.custom_sql:
                    lines.extend(
                        [
                            "",
                            f"**Custom SQL (`{rel.name or rel.table or 'query'}`):**",
                            "",
                            "```sql",
                            rel.custom_sql.strip(),
                            "```",
                        ]
                    )
        lines.append("")

    # Mermaid Lineage Diagram
    mermaid_code = _sanitize_mermaid(
        ai.mermaid_lineage_diagram if ai else None,
        snap.deterministic_mermaid,
    )
    if mermaid_code:
        lines.extend(
            [
                "## Data & Dashboard Lineage Diagram",
                "",
                "```mermaid",
                mermaid_code,
                "```",
                "",
            ]
        )

    # AI Recommendations
    if ai and ai.recommendations:
        lines.extend(["## Governance & Performance Recommendations", ""])
        for rec in ai.recommendations:
            lines.append(
                f"- **[{rec.severity.upper()}] {rec.category}:** {rec.finding} "
                f"**Recommendation:** {rec.recommendation}"
            )
        lines.append("")

    audit_md = _render_audit_markdown(bundle.audit, bundle.ai_evaluation)
    if audit_md:
        lines.extend(audit_md)

    return "\n".join(lines).rstrip() + "\n"


def _grade_for_score(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 78:
        return "B"
    if score >= 65:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def _render_audit_markdown(
    audit: Optional[WorkbookAuditReport],
    ai_eval: Optional[AIEvaluationReport],
) -> list[str]:
    """Render deterministic audit scorecard, findings, and AI evaluation playbook to Markdown."""
    if not audit and not ai_eval:
        return []
    lines: list[str] = []
    if audit:
        lines.extend(
            [
                "## Performance, Formatting & Accessibility Health Scorecard",
                "",
                f"- **Overall Health Score:** **{audit.overall_score}/100** (Grade **{audit.grade}**)",
                f"- **Workbook Fonts / Explicit Style Rules:** {len(audit.workbook_style.workbook_fonts)} fonts, "
                f"{audit.workbook_style.style_rule_count} explicit style rules",
                f"- **Total Heuristic Findings:** {len(audit.findings)}",
                "",
            ]
        )
        if audit.category_scores:
            lines.extend(
                [
                    "| Category | Score | Grade | Issues | Critical | High |",
                    "|---|---:|:---:|---:|---:|---:|",
                ]
            )
            for cs in audit.category_scores:
                lines.append(
                    f"| {_escape_md_cell(cs.label)} | {cs.score}/100 | {_grade_for_score(cs.score)} | "
                    f"{cs.issue_count} | {cs.critical_count} | {cs.high_count} |"
                )
            lines.append("")

        if audit.performance_recording.detected and audit.performance_recording.events:
            lines.extend(
                [
                    "### Attached Performance Recording Hotspots",
                    "",
                    "| Event | Count | Total (s) | Max (s) |",
                    "|---|---:|---:|---:|",
                ]
            )
            for ev in audit.performance_recording.events[:12]:
                lines.append(
                    f"| {_escape_md_cell(ev.event_name)} | {ev.count} | {ev.total_duration_sec:.2f} | {ev.max_duration_sec:.2f} |"
                )
            lines.append("")

        if audit.findings:
            lines.extend(["### Deterministic Audit Findings", ""])
            for f in audit.findings:
                lines.append(
                    f"- **[{f.severity.value.upper()}] {f.category.value} — {f.title}** (`{f.target}`)"
                )
                if f.evidence:
                    lines.append(f"  - Evidence: {f.evidence}")
                if f.impact_summary:
                    lines.append(f"  - Impact: {f.impact_summary}")
                if f.recommendation:
                    lines.append(f"  - Recommendation: {f.recommendation}")
                if f.rewrite_before or f.rewrite_after:
                    if f.rewrite_before:
                        lines.append(f"  - Before: `{_escape_inline_code(f.rewrite_before)}`")
                    if f.rewrite_after:
                        lines.append(f"  - After: `{_escape_inline_code(f.rewrite_after)}`")
                if f.source_citation:
                    lines.append(f"  - Source: {f.source_citation}")
            lines.append("")

    if ai_eval:
        lines.extend(
            [
                "## Antigravity AI Performance & UX Playbook",
                "",
                ai_eval.executive_summary,
                "",
                f"- **Estimated Performance Grade:** **{ai_eval.estimated_performance_grade}**",
                "",
            ]
        )
        if ai_eval.top_bottlenecks:
            lines.append("### Top Bottlenecks")
            lines.extend([f"- {b}" for b in ai_eval.top_bottlenecks])
            lines.append("")
        if ai_eval.calculation_rewrites:
            lines.append("### Calculation Rewrite Suggestions")
            for tip in ai_eval.calculation_rewrites:
                lines.append(
                    f"- **`{tip.field_name}`** (*{tip.datasource}*, {tip.expected_impact} impact): "
                    f"{tip.why_it_is_faster}"
                )
                lines.append(f"  - Before: `{_escape_inline_code(tip.current_formula)}`")
                lines.append(f"  - After: `{_escape_inline_code(tip.optimized_formula)}`")
            lines.append("")
        if ai_eval.formatting_and_ux_tips:
            lines.append("### Formatting & WCAG Accessibility Improvements")
            for tip in ai_eval.formatting_and_ux_tips:
                lines.append(
                    f"- **[{tip.area}] {tip.target}**: {tip.observation} -> {tip.recommendation} "
                    f"*(Benefit: {tip.benefit})*"
                )
            lines.append("")
        if ai_eval.data_and_filter_tips or ai_eval.dashboard_and_server_tips:
            lines.append("### Architecture, Data, Filter & Server Guidance")
            for tip in [*ai_eval.data_and_filter_tips, *ai_eval.dashboard_and_server_tips]:
                lines.append(
                    f"- **[{tip.priority}] {tip.target}** — {tip.issue} "
                    f"**Action:** {tip.action} *(Principle: {tip.reference_principle})*"
                )
            lines.append("")
        if ai_eval.quick_wins_checklist:
            lines.append("### Quick Wins (< 15 min)")
            lines.extend([f"- [ ] {q}" for q in ai_eval.quick_wins_checklist])
            lines.append("")
        if ai_eval.strategic_refactors:
            lines.append("### Strategic Refactors")
            lines.extend([f"- {s}" for s in ai_eval.strategic_refactors])
            lines.append("")

    return lines


# ---------------------------------------------------------------------------
# Interactive Standalone HTML Renderer
# ---------------------------------------------------------------------------

_ZONE_COLORS = {
    "worksheet": ("#2563eb", "#dbeafe", "#1e3a8a"),
    "filter": ("#d97706", "#fef3c7", "#78350f"),
    "paramctrl": ("#7c3aed", "#ede9fe", "#4c1d95"),
    "text": ("#059669", "#d1fae5", "#065f46"),
    "title": ("#0d9488", "#ccfbf1", "#115e59"),
    "bitmap": ("#db2777", "#fce7f3", "#831843"),
    "web": ("#0284c7", "#e0f2fe", "#0c4a6e"),
    "color": ("#ea580c", "#ffedd5", "#7c2d12"),
    "size": ("#ea580c", "#ffedd5", "#7c2d12"),
    "shape": ("#ea580c", "#ffedd5", "#7c2d12"),
}


def _zone_style(zone: ZoneInfo) -> tuple[str, str, str]:
    ztype = (zone.zone_type or "").lower()
    if ztype in _ZONE_COLORS:
        return _ZONE_COLORS[ztype]
    if "filter" in ztype:
        return _ZONE_COLORS["filter"]
    if "param" in ztype:
        return _ZONE_COLORS["paramctrl"]
    return ("#64748b", "#f1f5f9", "#334155")


def _render_dashboard_wireframe_html(dash: DashboardInfo) -> str:
    """Render an interactive CSS/SVG proportional wireframe from normalized Tableau 0..100000 coords."""
    renderable = [
        z
        for z in dash.zones
        if z.w is not None
        and z.h is not None
        and z.x is not None
        and z.y is not None
        and z.w > 0
        and z.h > 0
        and z.zone_type not in ("layout-basic", "layout-flow", "empty")
    ]
    if not renderable:
        return '<p class="muted">No positional wireframe coordinates recorded for this dashboard.</p>'

    w_px = dash.max_width or dash.min_width or 1200
    h_px = dash.max_height or dash.min_height or 800
    ratio_pct = max(45.0, min(110.0, (h_px / max(w_px, 1)) * 100.0))

    blocks: list[str] = []
    for z in renderable[:120]:
        left = max(0.0, min(99.5, (z.x or 0) / 1000.0))
        top = max(0.0, min(99.5, (z.y or 0) / 1000.0))
        width = max(1.0, min(100.0 - left, (z.w or 0) / 1000.0))
        height = max(1.5, min(100.0 - top, (z.h or 0) / 1000.0))
        border, bg, fg = _zone_style(z)
        label = z.name or z.param_readable or z.text_content or z.zone_type
        short_label = label if len(label) <= 42 else label[:39] + "..."
        tooltip = f"[{z.zone_type}] {label} ({z.x},{z.y} {z.w}x{z.h})"
        blocks.append(
            f'<div class="wf-zone" title="{html.escape(tooltip)}" '
            f'style="left:{left:.2f}%;top:{top:.2f}%;width:{width:.2f}%;height:{height:.2f}%;'
            f'border-color:{border};background:{bg};color:{fg};">'
            f'<span class="wf-badge">{html.escape(z.zone_type)}</span>'
            f'<span class="wf-label">{html.escape(short_label)}</span>'
            f"</div>"
        )

    return (
        f'<div class="wireframe-box" style="padding-bottom:{ratio_pct:.2f}%;">'
        + "".join(blocks)
        + "</div>"
    )


def _h(value: Optional[str], fallback: str = "—") -> str:
    if value is None or str(value).strip() == "":
        return fallback
    return html.escape(str(value))


def render_html(bundle: DocumentationBundle) -> str:
    """Render a self-contained interactive HTML5 documentation report."""
    snap = bundle.snapshot
    ai = bundle.ai_report
    title = (ai.workbook_title if ai else "") or snap.workbook_name or snap.filename
    real_datasources = [ds for ds in snap.datasources if not ds.is_parameters]
    calc_count = sum(len(ds.calculated_fields) for ds in real_datasources)
    calc_docs = _calc_explanation_map(bundle)
    ordered_dashboards, in_scope_ws = _scoped_worksheets_and_dashboards(bundle)
    ai_dash_map = {d.name.lower(): d for d in (ai.dashboards if ai else [])}
    ai_ws_map = {w.name.lower(): w for w in (ai.worksheets if ai else [])}

    sections: list[str] = []

    # Warnings
    if bundle.warnings:
        warn_items = "".join(f"<li>{html.escape(w)}</li>" for w in bundle.warnings)
        sections.append(
            f'<section id="warnings" class="card warning-card"><h2>Notices &amp; Warnings</h2><ul>{warn_items}</ul></section>'
        )

    # AI Executive Summary & KPI Glossary
    if ai:
        audience_chips = "".join(
            f'<span class="chip">{html.escape(a)}</span>' for a in ai.target_audience
        )
        sections.append(
            f'<section id="executive-summary" class="card searchable">'
            f"<h2>Executive Summary</h2>"
            f'<p class="lead">{html.escape(ai.executive_summary)}</p>'
            f'<div class="chip-row">{audience_chips}</div>'
            f"</section>"
        )

        if ai.kpi_glossary:
            kpi_rows = []
            for kpi in ai.kpi_glossary:
                kpi_rows.append(
                    "<tr>"
                    f"<td><strong>{html.escape(kpi.name)}</strong></td>"
                    f"<td>{html.escape(kpi.business_definition)}</td>"
                    f"<td><code>{html.escape(kpi.formula_or_aggregation)}</code></td>"
                    f"<td>{html.escape(kpi.datasource_and_worksheets)}</td>"
                    "</tr>"
                )
            sections.append(
                '<section id="kpi-glossary" class="card searchable">'
                "<h2>KPI &amp; Metric Glossary</h2>"
                '<div class="table-wrap"><table><thead><tr>'
                "<th>KPI</th><th>Business Definition</th><th>Formula / Aggregation</th><th>Scope</th>"
                f"</tr></thead><tbody>{''.join(kpi_rows)}</tbody></table></div>"
                "</section>"
            )

    # Thumbnails gallery
    thumbs_with_img = [t for t in snap.thumbnails if t.base64_png]
    if thumbs_with_img:
        thumb_cards = []
        for t in thumbs_with_img:
            thumb_cards.append(
                '<div class="thumb-card searchable">'
                f'<img src="data:image/png;base64,{t.base64_png}" alt="{html.escape(t.name)} thumbnail" loading="lazy" />'
                f'<div class="thumb-cap"><strong>{html.escape(t.name)}</strong>'
                f'<span>{t.width or "?"}x{t.height or "?"}px</span></div>'
                "</div>"
            )
        sections.append(
            '<section id="thumbnails" class="card">'
            "<h2>Embedded Workbook Thumbnails</h2>"
            f'<div class="thumb-grid">{"".join(thumb_cards)}</div>'
            "</section>"
        )

    # Dashboards
    if ordered_dashboards:
        dash_cards = []
        for dash in ordered_dashboards:
            ai_d = ai_dash_map.get(dash.name.lower())
            is_target = bool(
                bundle.target_dashboard
                and bundle.target_dashboard.lower() in dash.name.lower()
            )
            badge = '<span class="badge badge-accent">Primary Scope</span>' if is_target else ""
            ai_html = ""
            if ai_d:
                caveat_li = "".join(f"<li>{html.escape(c)}</li>" for c in ai_d.key_caveats)
                caveat_block = (
                    f"<div class='subblock'><h4>Key Caveats</h4><ul>{caveat_li}</ul></div>"
                    if ai_d.key_caveats
                    else ""
                )
                ai_html = (
                    f"<div class='narrative-grid'>"
                    f"<div class='subblock'><h4>Business Purpose</h4><p>{html.escape(ai_d.purpose)}</p></div>"
                    f"<div class='subblock'><h4>Visual Layout &amp; Hierarchy</h4><p>{html.escape(ai_d.visual_layout_summary)}</p></div>"
                    f"<div class='subblock'><h4>Controls &amp; Filters</h4><p>{html.escape(ai_d.controls_and_filters)}</p></div>"
                    f"<div class='subblock'><h4>Interactivity Walkthrough</h4><p>{html.escape(ai_d.interactivity_narrative)}</p></div>"
                    f"</div>{caveat_block}"
                )

            ws_chips = "".join(
                f'<span class="chip">{html.escape(w)}</span>' for w in dash.worksheets
            ) or '<span class="muted">None</span>'
            filt_chips = "".join(
                f'<span class="chip chip-amber">{html.escape(f)}</span>' for f in dash.exposed_filters
            ) or '<span class="muted">None</span>'
            param_chips = "".join(
                f'<span class="chip chip-purple">{html.escape(p)}</span>'
                for p in dash.exposed_parameters
            ) or '<span class="muted">None</span>'

            wireframe = _render_dashboard_wireframe_html(dash)
            dash_cards.append(
                '<article class="subcard searchable">'
                f"<div class='subcard-head'><h3>{html.escape(dash.name)} {badge}</h3>"
                f"<span class='meta'>{dash.min_width or '?'}x{dash.min_height or '?'} -> {dash.max_width or '?'}x{dash.max_height or '?'} px | Devices: {_h(_format_list(dash.device_layouts, 'Desktop'))}</span></div>"
                f"{ai_html}"
                f"<div class='meta-rows'>"
                f"<div><strong>Worksheets ({len(dash.worksheets)}):</strong> {ws_chips}</div>"
                f"<div><strong>Exposed Filters:</strong> {filt_chips}</div>"
                f"<div><strong>Exposed Parameters:</strong> {param_chips}</div>"
                "</div>"
                f"<details open><summary>Layout Wireframe ({len(dash.zones)} raw zones)</summary>{wireframe}</details>"
                "</article>"
            )
        sections.append(
            f'<section id="dashboards" class="card"><h2>Dashboards ({len(ordered_dashboards)})</h2>{"".join(dash_cards)}</section>'
        )

    # Worksheets
    if snap.worksheets:
        ws_cards = []
        for ws in snap.worksheets:
            ai_w = ai_ws_map.get(ws.name.lower())
            in_scope_badge = (
                '<span class="badge badge-accent">In Scope</span>'
                if bundle.target_dashboard and ws.name in in_scope_ws
                else ""
            )
            enc_items = "".join(
                f"<li><code>{html.escape(e.channel)}</code>: <code>{html.escape(e.column_readable)}</code></li>"
                for e in ws.encodings
            )
            filt_items = "".join(
                f"<li><code>{html.escape(f.column_readable)}</code> — {html.escape(f.summary or f.filter_class or 'filter')}</li>"
                for f in ws.filters
            )
            ai_ws_block = ""
            if ai_w:
                ai_ws_block = (
                    f"<p class='ai-callout'><strong>{html.escape(ai_w.chart_type)}:</strong> "
                    f"{html.escape(ai_w.purpose)}<br><span class='muted'>{html.escape(ai_w.axes_encodings_and_filters)}</span></p>"
                )
            ws_cards.append(
                '<article class="subcard searchable">'
                f"<div class='subcard-head'><h3>{html.escape(ws.name)} {in_scope_badge}</h3>"
                f"<span class='meta'>Marks: {_h(_format_list(ws.mark_types, 'Automatic'))} | Dashboards: {_h(_format_list(ws.used_in_dashboards, 'Standalone'))}</span></div>"
                f"{ai_ws_block}"
                '<div class="kv-grid">'
                f"<div><strong>Title</strong><div>{_h(ws.title)}</div></div>"
                f"<div><strong>Rows Shelf</strong><div><code>{_h(ws.rows_readable or ws.rows_raw)}</code></div></div>"
                f"<div><strong>Columns Shelf</strong><div><code>{_h(ws.cols_readable or ws.cols_raw)}</code></div></div>"
                f"<div><strong>Data Sources</strong><div>{_h(_format_list(ws.datasources))}</div></div>"
                "</div>"
                + (f"<div class='list-block'><strong>Encodings:</strong><ul>{enc_items}</ul></div>" if enc_items else "")
                + (f"<div class='list-block'><strong>Filters:</strong><ul>{filt_items}</ul></div>" if filt_items else "")
                + (
                    f"<div class='list-block'><strong>Calculations Used:</strong> {_h(_format_list(ws.referenced_calculations))}</div>"
                    if ws.referenced_calculations
                    else ""
                )
                + (
                    f"<div class='list-block'><strong>Tooltip:</strong> <code>{html.escape(ws.tooltip_text)}</code></div>"
                    if ws.tooltip_text
                    else ""
                )
                + "</article>"
            )
        sections.append(
            f'<section id="worksheets" class="card"><h2>Worksheets ({len(snap.worksheets)})</h2>{"".join(ws_cards)}</section>'
        )

    # Calculated fields
    if calc_count > 0:
        calc_blocks = []
        for ds in real_datasources:
            for cf in ds.calculated_fields:
                caption = cf.caption or cf.name
                formula = cf.readable_calculation or cf.calculation or ""
                ai_cf = calc_docs.get((ds.caption.lower(), caption.lower())) or calc_docs.get(
                    ("", caption.lower())
                )
                plain_html = (
                    f"<p>{html.escape(ai_cf[0])}</p><p class='muted'><strong>Dependencies:</strong> {html.escape(ai_cf[1])}</p>"
                    if ai_cf
                    else ""
                )
                calc_blocks.append(
                    '<article class="subcard searchable">'
                    f"<div class='subcard-head'><h3>{html.escape(caption)}</h3>"
                    f"<span class='meta'>{html.escape(ds.caption or ds.name)} | {html.escape(cf.datatype or '?')} ({html.escape(cf.role or '?')}) | Used in: {_h(_format_list(cf.worksheets, 'Unused'))}</span></div>"
                    f"{plain_html}"
                    f"<pre class='code-block'><code>{html.escape(formula)}</code></pre>"
                    "</article>"
                )
        sections.append(
            f'<section id="calculated-fields" class="card"><h2>Calculated Fields ({calc_count})</h2>{"".join(calc_blocks)}</section>'
        )

    # Parameters
    if snap.parameters:
        p_rows = []
        for p in snap.parameters:
            domain_desc = (
                ", ".join(p.allowable_values[:12])
                if p.allowable_values
                else (
                    f"{p.min_value or '?'} .. {p.max_value or '?'} (step {p.step_size or '1'})"
                    if (p.min_value is not None or p.max_value is not None)
                    else "Any"
                )
            )
            p_rows.append(
                "<tr class='searchable'>"
                f"<td><strong>{html.escape(p.name)}</strong></td>"
                f"<td><code>{_h(p.datatype)}</code></td>"
                f"<td><code>{_h(p.domain_type)}</code></td>"
                f"<td><code>{_h(p.current_value)}</code></td>"
                f"<td>{html.escape(domain_desc)}</td>"
                "</tr>"
            )
        sections.append(
            f'<section id="parameters" class="card"><h2>Parameters ({len(snap.parameters)})</h2>'
            '<div class="table-wrap"><table><thead><tr>'
            "<th>Name</th><th>Datatype</th><th>Domain</th><th>Current Value</th><th>Allowed Range / List</th>"
            f"</tr></thead><tbody>{''.join(p_rows)}</tbody></table></div></section>"
        )

    # Actions
    if snap.actions:
        a_rows = []
        for act in snap.actions:
            source = act.source_dashboard or act.source_worksheet or "Workbook"
            target = act.target_scope or "All"
            extra = act.url or ", ".join(act.field_captions) or act.target_group_or_param or "—"
            a_rows.append(
                "<tr class='searchable'>"
                f"<td><strong>{html.escape(act.caption or act.name)}</strong></td>"
                f"<td><code>{html.escape(act.action_type)}</code></td>"
                f"<td><code>{_h(act.trigger)}</code></td>"
                f"<td>{html.escape(source)}</td>"
                f"<td>{html.escape(target)}</td>"
                f"<td>{html.escape(extra)}</td>"
                "</tr>"
            )
        sections.append(
            f'<section id="actions" class="card"><h2>Interactive Actions ({len(snap.actions)})</h2>'
            '<div class="table-wrap"><table><thead><tr>'
            "<th>Action</th><th>Type</th><th>Trigger</th><th>Source</th><th>Target</th><th>Fields / URL</th>"
            f"</tr></thead><tbody>{''.join(a_rows)}</tbody></table></div></section>"
        )

    # Data Architecture & Lineage
    ds_cards = []
    if ai:
        arch_items = "".join(
            f"<li>{html.escape(s)}</li>" for s in ai.data_architecture.sources_and_lineage
        )
        ds_cards.append(
            "<div class='ai-callout'>"
            f"<p>{html.escape(ai.data_architecture.overview)}</p>"
            + (f"<ul>{arch_items}</ul>" if arch_items else "")
            + (
                f"<p><strong>Refresh &amp; Security:</strong> {html.escape(ai.data_architecture.refresh_and_security_notes)}</p>"
                if ai.data_architecture.refresh_and_security_notes
                else ""
            )
            + "</div>"
        )
    for ds in real_datasources:
        conn_li = "".join(
            f"<li><code>{html.escape(c.dbclass or 'unknown')}</code> -> "
            f"<code>{html.escape(c.dbname or c.filename or c.server or c.service or 'default')}</code>"
            f"{f' (auth: {html.escape(c.authentication)})' if c.authentication else ''}</li>"
            for c in ds.connections
        )
        rel_li = "".join(
            f"<li><code>{html.escape(r.left_object)}</code> &harr; <code>{html.escape(r.right_object)}</code> "
            f"on <code>{html.escape(r.expression)}</code></li>"
            for r in ds.relationships
        )
        sql_blocks = "".join(
            f"<h4>Custom SQL ({html.escape(r.name or r.table or 'query')})</h4>"
            f"<pre class='code-block'><code>{html.escape(r.custom_sql.strip())}</code></pre>"
            for r in ds.relations
            if r.custom_sql
        )
        ds_cards.append(
            '<article class="subcard searchable">'
            f"<div class='subcard-head'><h3>{html.escape(ds.caption or ds.name)}</h3>"
            f"<span class='meta'>Extract: {'Yes' if ds.has_extract else 'No'} | Fields: {len(ds.fields)} | Calcs: {len(ds.calculated_fields)}</span></div>"
            + (f"<div><strong>Connections:</strong><ul>{conn_li}</ul></div>" if conn_li else "")
            + (
                f"<div><strong>Logical Tables:</strong> {html.escape(_format_list(ds.logical_tables))}</div>"
                if ds.logical_tables
                else ""
            )
            + (f"<div><strong>Relationships:</strong><ul>{rel_li}</ul></div>" if rel_li else "")
            + sql_blocks
            + "</article>"
        )
    sections.append(
        f'<section id="data-sources" class="card"><h2>Data Architecture &amp; Sources ({len(real_datasources)})</h2>{"".join(ds_cards)}</section>'
    )

    # Mermaid lineage diagram
    mermaid_code = _sanitize_mermaid(
        ai.mermaid_lineage_diagram if ai else None,
        snap.deterministic_mermaid,
    )
    if mermaid_code:
        sections.append(
            '<section id="lineage" class="card">'
            "<h2>Data &amp; Dashboard Lineage Diagram</h2>"
            f'<div class="mermaid">{html.escape(mermaid_code)}</div>'
            f'<details><summary>Mermaid Source</summary><pre class="code-block"><code>{html.escape(mermaid_code)}</code></pre></details>'
            "</section>"
        )

    # Recommendations
    if ai and ai.recommendations:
        rec_items = []
        for r in ai.recommendations:
            sev = r.severity.lower()
            rec_items.append(
                f'<div class="rec-item rec-{html.escape(sev)} searchable">'
                f'<span class="badge">{html.escape(r.severity.upper())}</span> '
                f"<strong>{html.escape(r.category)}:</strong> {html.escape(r.finding)} "
                f"<div class='rec-fix'><strong>Recommendation:</strong> {html.escape(r.recommendation)}</div>"
                "</div>"
            )
        sections.append(
            f'<section id="recommendations" class="card"><h2>Governance &amp; Performance Audit</h2>{"".join(rec_items)}</section>'
        )

    audit_html = _render_audit_html(bundle.audit, bundle.ai_evaluation)
    if audit_html:
        sections.append(audit_html)

    mode_badge = (
        f"AI-Enriched ({bundle.agent_model or 'Antigravity SDK'})"
        if bundle.used_ai and ai
        else "Deterministic Metadata Mode"
    )
    usage_pill = (
        f'<div class="stat"><span class="stat-val">{bundle.usage.total_tokens:,}</span><span class="stat-lbl">Tokens</span></div>'
        if bundle.usage and bundle.usage.total_tokens
        else ""
    )
    health_pill = (
        f'<div class="stat"><span class="stat-val">{bundle.audit.overall_score} ({html.escape(bundle.audit.grade)})</span><span class="stat-lbl">Health Score</span></div>'
        if bundle.audit
        else ""
    )
    audit_nav = ""
    if bundle.audit:
        audit_nav += '<a href="#performance-health">Health Scorecard</a>'
    if bundle.ai_evaluation:
        audit_nav += '<a href="#ai-playbook">AI Playbook</a>'

    template = _HTML_TEMPLATE
    replacements = {
        "__TITLE__": html.escape(title),
        "__FILENAME__": html.escape(snap.filename),
        "__VERSION__": html.escape(snap.tableau_version or "Unknown"),
        "__GENERATED_AT__": html.escape(bundle.generated_at_iso),
        "__MODE_BADGE__": html.escape(mode_badge),
        "__AUDIENCE__": html.escape(bundle.audience),
        "__DASH_COUNT__": str(len(snap.dashboards)),
        "__WS_COUNT__": str(len(snap.worksheets)),
        "__DS_COUNT__": str(len(real_datasources)),
        "__CALC_COUNT__": str(calc_count),
        "__PARAM_COUNT__": str(len(snap.parameters)),
        "__ACTION_COUNT__": str(len(snap.actions)),
        "__HEALTH_STAT__": health_pill,
        "__USAGE_STAT__": usage_pill,
        "__AUDIT_NAV__": audit_nav,
        "__BODY_SECTIONS__": "\n".join(sections),
    }
    output = template
    for key, val in replacements.items():
        output = output.replace(key, val)
    return output


def _render_audit_html(
    audit: Optional[WorkbookAuditReport],
    ai_eval: Optional[AIEvaluationReport],
) -> str:
    """Render deterministic performance/UX scorecard and Antigravity AI Playbook HTML sections."""
    if not audit and not ai_eval:
        return ""
    blocks: list[str] = []
    if audit:
        score_cards = "".join(
            f'<div class="subcard">'
            f'<div class="subcard-head"><strong>{html.escape(cs.label)}</strong>'
            f'<span class="badge">{cs.score}/100 ({_grade_for_score(cs.score)})</span></div>'
            f'<div class="meta">Issues: {cs.issue_count}'
            f' | Critical: {cs.critical_count} | High: {cs.high_count}</div>'
            f"</div>"
            for cs in audit.category_scores
        )
        perf_html = ""
        if audit.performance_recording.detected and audit.performance_recording.events:
            perf_rows = "".join(
                f"<tr><td><code>{html.escape(ev.event_name)}</code></td>"
                f"<td>{ev.count}</td><td>{ev.total_duration_sec:.2f} s</td><td>{ev.max_duration_sec:.2f} s</td></tr>"
                for ev in audit.performance_recording.events[:10]
            )
            perf_html = (
                '<div class="ai-callout"><strong>Performance Recording Hotspots Detected</strong>'
                '<div class="table-wrap"><table><thead><tr><th>Event</th><th>Count</th><th>Total</th><th>Max</th></tr></thead>'
                f"<tbody>{perf_rows}</tbody></table></div></div>"
            )
        finding_items = []
        for f in audit.findings:
            sev = f.severity.value.lower()
            diff_html = ""
            if f.rewrite_before or f.rewrite_after:
                diff_html = (
                    '<div class="diff-grid">'
                    + (
                        f'<div class="diff-before"><strong>Before</strong><pre><code>{html.escape(f.rewrite_before)}</code></pre></div>'
                        if f.rewrite_before
                        else ""
                    )
                    + (
                        f'<div class="diff-after"><strong>After</strong><pre><code>{html.escape(f.rewrite_after)}</code></pre></div>'
                        if f.rewrite_after
                        else ""
                    )
                    + "</div>"
                )
            finding_items.append(
                f'<div class="rec-item rec-{html.escape(sev)} searchable">'
                f'<div class="subcard-head"><span><span class="badge">{html.escape(sev.upper())}</span> '
                f'<span class="chip">{html.escape(f.category.value)}</span> '
                f"<strong>{html.escape(f.title)}</strong></span>"
                f'<span class="meta"><code>{html.escape(f.target)}</code></span></div>'
                + (f"<div><strong>Evidence:</strong> {html.escape(f.evidence)}</div>" if f.evidence else "")
                + (f"<div><strong>Impact:</strong> {html.escape(f.impact_summary)}</div>" if f.impact_summary else "")
                + (f"<div class='rec-fix'><strong>Recommendation:</strong> {html.escape(f.recommendation)}</div>" if f.recommendation else "")
                + diff_html
                + (f"<div class='meta'>Source: {html.escape(f.source_citation)}</div>" if f.source_citation else "")
                + "</div>"
            )
        blocks.append(
            '<section id="performance-health" class="card">'
            f"<h2>Performance, Formatting &amp; Accessibility Scorecard — {audit.overall_score}/100 (Grade {html.escape(audit.grade)})</h2>"
            f'<div class="meta">Workbook Fonts: {len(audit.workbook_style.workbook_fonts)} | '
            f"Explicit Style Rules: {audit.workbook_style.style_rule_count} | Findings: {len(audit.findings)}</div>"
            f'<div class="grid-two" style="margin-top:12px">{score_cards}</div>'
            + perf_html
            + ("".join(finding_items) if finding_items else '<p class="muted">No deterministic anti-patterns triggered.</p>')
            + "</section>"
        )

    if ai_eval:
        bottlenecks = "".join(f"<li>{html.escape(b)}</li>" for b in ai_eval.top_bottlenecks)
        rewrites_html = "".join(
            '<article class="subcard searchable">'
            f"<div class='subcard-head'><h3>{html.escape(tip.field_name)}</h3>"
            f"<span class='badge badge-accent'>{html.escape(tip.expected_impact)} Impact</span></div>"
            f"<div class='meta'>Datasource: {html.escape(tip.datasource)}</div>"
            f"<p>{html.escape(tip.why_it_is_faster)}</p>"
            '<div class="diff-grid">'
            f'<div class="diff-before"><strong>Before</strong><pre><code>{html.escape(tip.current_formula)}</code></pre></div>'
            f'<div class="diff-after"><strong>After</strong><pre><code>{html.escape(tip.optimized_formula)}</code></pre></div>'
            "</div></article>"
            for tip in ai_eval.calculation_rewrites
        )
        ux_html = "".join(
            f'<div class="rec-item searchable"><span class="chip">{html.escape(t.area)}</span> '
            f"<strong>{html.escape(t.target)}:</strong> {html.escape(t.observation)} "
            f"<div class='rec-fix'><strong>Fix:</strong> {html.escape(t.recommendation)}</div>"
            f"<div class='meta'>Benefit: {html.escape(t.benefit)}</div></div>"
            for t in ai_eval.formatting_and_ux_tips
        )
        arch_html = "".join(
            f'<div class="rec-item searchable"><span class="chip">{html.escape(t.priority)}</span> '
            f"<strong>{html.escape(t.target)}:</strong> {html.escape(t.issue)} "
            f"<div class='rec-fix'><strong>Action:</strong> {html.escape(t.action)}</div>"
            f"<div class='meta'>Principle: {html.escape(t.reference_principle)}</div></div>"
            for t in [*ai_eval.data_and_filter_tips, *ai_eval.dashboard_and_server_tips]
        )
        qw_li = "".join(f"<li>{html.escape(q)}</li>" for q in ai_eval.quick_wins_checklist)
        sr_li = "".join(f"<li>{html.escape(s)}</li>" for s in ai_eval.strategic_refactors)
        blocks.append(
            '<section id="ai-playbook" class="card">'
            f"<h2>Antigravity AI Performance &amp; UX Playbook (Est. Grade: {html.escape(ai_eval.estimated_performance_grade)})</h2>"
            f'<div class="ai-callout">{html.escape(ai_eval.executive_summary)}</div>'
            + (f"<h3>Top Bottlenecks</h3><ul>{bottlenecks}</ul>" if bottlenecks else "")
            + (f"<h3>Calculation Rewrites</h3>{rewrites_html}" if rewrites_html else "")
            + (f"<h3>Formatting &amp; Accessibility Tips</h3>{ux_html}" if ux_html else "")
            + (f"<h3>Data, Filter, Dashboard &amp; Server Tips</h3>{arch_html}" if arch_html else "")
            + '<div class="grid-two">'
            + (f'<div class="subcard"><h3>Quick Wins</h3><ul>{qw_li}</ul></div>' if qw_li else "")
            + (f'<div class="subcard"><h3>Strategic Refactors</h3><ul>{sr_li}</ul></div>' if sr_li else "")
            + "</div></section>"
        )

    return "\n".join(blocks)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>__TITLE__ — Tableau Documentation</title>
<style>
  :root {
    --bg: #f8fafc;
    --surface: #ffffff;
    --surface-2: #f1f5f9;
    --text: #0f172a;
    --muted: #475569;
    --border: #e2e8f0;
    --accent: #2563eb;
    --accent-soft: #dbeafe;
    --code-bg: #0f172a;
    --code-fg: #e2e8f0;
  }
  [data-theme="dark"] {
    --bg: #0b1120;
    --surface: #111827;
    --surface-2: #1e293b;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --border: #334155;
    --accent: #60a5fa;
    --accent-soft: #1e3a8a;
    --code-bg: #020617;
    --code-fg: #e2e8f0;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.55;
  }
  .layout { display: grid; grid-template-columns: 260px 1fr; min-height: 100vh; }
  aside {
    position: sticky; top: 0; height: 100vh; overflow-y: auto;
    background: var(--surface); border-right: 1px solid var(--border);
    padding: 20px 16px;
  }
  aside h1 { font-size: 1rem; margin: 0 0 8px; }
  aside nav a {
    display: block; padding: 7px 10px; border-radius: 6px;
    color: var(--muted); text-decoration: none; font-size: 0.9rem;
  }
  aside nav a:hover { background: var(--surface-2); color: var(--text); }
  .toolbar { display: flex; flex-direction: column; gap: 8px; margin: 14px 0; }
  .toolbar input {
    width: 100%; padding: 8px 10px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
  }
  .btn-row { display: flex; gap: 8px; }
  button {
    cursor: pointer; padding: 6px 10px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--surface-2); color: var(--text);
    font-size: 0.85rem;
  }
  main { padding: 28px 36px; max-width: 1200px; }
  .hero {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 24px; margin-bottom: 20px;
  }
  .hero h1 { margin: 0 0 6px; font-size: 1.75rem; }
  .meta { color: var(--muted); font-size: 0.88rem; }
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(115px, 1fr));
    gap: 10px; margin-top: 16px;
  }
  .stat {
    background: var(--surface-2); border-radius: 8px; padding: 10px 12px;
    display: flex; flex-direction: column;
  }
  .stat-val { font-size: 1.25rem; font-weight: 700; color: var(--accent); }
  .stat-lbl { font-size: 0.78rem; color: var(--muted); }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 22px; margin-bottom: 20px;
  }
  .card h2 { margin-top: 0; font-size: 1.28rem; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
  .subcard {
    border: 1px solid var(--border); border-radius: 10px; padding: 16px;
    margin: 14px 0; background: var(--bg);
  }
  .subcard-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 8px; }
  .subcard-head h3 { margin: 0; font-size: 1.08rem; }
  .narrative-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px; margin: 12px 0;
  }
  .subblock { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .subblock h4 { margin: 0 0 6px; font-size: 0.9rem; color: var(--accent); }
  .subblock p { margin: 0; font-size: 0.92rem; }
  .kv-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 10px; margin: 10px 0;
  }
  .chip-row, .meta-rows { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; }
  .meta-rows { flex-direction: column; gap: 6px; }
  .chip {
    display: inline-block; padding: 2px 9px; border-radius: 999px;
    background: var(--accent-soft); color: var(--accent); font-size: 0.8rem; font-weight: 500;
  }
  .chip-amber { background: #fef3c7; color: #92400e; }
  .chip-purple { background: #ede9fe; color: #5b21b6; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 0.74rem; font-weight: 700; background: var(--surface-2);
  }
  .badge-accent { background: var(--accent); color: #fff; }
  .ai-callout {
    border-left: 3px solid var(--accent); background: var(--surface);
    padding: 10px 14px; border-radius: 6px; margin: 10px 0;
  }
  .warning-card { border-color: #f59e0b; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
  th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { background: var(--surface-2); font-weight: 600; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.86em; }
  .code-block {
    background: var(--code-bg); color: var(--code-fg);
    padding: 12px 14px; border-radius: 8px; overflow-x: auto; font-size: 0.85rem;
  }
  .wireframe-box {
    position: relative; width: 100%; background: var(--surface);
    border: 1px dashed var(--border); border-radius: 8px; margin-top: 10px; overflow: hidden;
  }
  .wf-zone {
    position: absolute; border: 1.5px solid; border-radius: 4px; padding: 3px 5px;
    overflow: hidden; font-size: 0.72rem; display: flex; flex-direction: column; gap: 2px;
    opacity: 0.92;
  }
  .wf-badge { font-weight: 700; text-transform: uppercase; font-size: 0.65rem; opacity: 0.85; }
  .wf-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 600; }
  .thumb-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
  .thumb-card { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--bg); }
  .thumb-card img { width: 100%; height: auto; display: block; }
  .thumb-cap { padding: 8px 10px; display: flex; justify-content: space-between; font-size: 0.84rem; }
  .rec-item { padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border); margin: 8px 0; }
  .rec-critical { border-left: 4px solid #991b1b; }
  .rec-high { border-left: 4px solid #dc2626; }
  .rec-medium, .rec-warning { border-left: 4px solid #d97706; }
  .rec-low { border-left: 4px solid #0284c7; }
  .rec-info { border-left: 4px solid #2563eb; }
  .rec-fix { margin-top: 4px; color: var(--muted); }
  .diff-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; margin-top: 8px; }
  .grid-two { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
  .diff-before { background: rgba(220, 38, 38, 0.08); border: 1px solid rgba(220, 38, 38, 0.25); border-radius: 6px; padding: 8px; overflow-x: auto; }
  .diff-after { background: rgba(5, 150, 105, 0.08); border: 1px solid rgba(5, 150, 105, 0.25); border-radius: 6px; padding: 8px; overflow-x: auto; }
  .diff-before pre, .diff-after pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 0.82rem; }
  .muted { color: var(--muted); }
  .hidden-filter { display: none !important; }
  @media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; }
    aside { position: static; height: auto; border-right: none; border-bottom: 1px solid var(--border); }
    main { padding: 18px; }
  }
  @media print {
    aside { display: none; }
    .layout { grid-template-columns: 1fr; }
    .card, .subcard { break-inside: avoid; }
  }
</style>
</head>
<body>
<div class="layout">
  <aside>
    <h1>Tableau Docs</h1>
    <div class="meta">__MODE_BADGE__</div>
    <div class="toolbar">
      <input id="docSearch" type="search" placeholder="Filter sections, fields, sheets..." oninput="filterDocs(this.value)" />
      <div class="btn-row">
        <button type="button" onclick="toggleTheme()">Theme</button>
        <button type="button" onclick="window.print()">Print / PDF</button>
      </div>
    </div>
    <nav>
      <a href="#executive-summary">Executive Summary</a>
      <a href="#kpi-glossary">KPI Glossary</a>
      <a href="#thumbnails">Thumbnails</a>
      <a href="#dashboards">Dashboards</a>
      <a href="#worksheets">Worksheets</a>
      <a href="#calculated-fields">Calculated Fields</a>
      <a href="#parameters">Parameters</a>
      <a href="#actions">Interactive Actions</a>
      <a href="#data-sources">Data Sources</a>
      <a href="#lineage">Lineage Diagram</a>
      <a href="#recommendations">Recommendations</a>
      __AUDIT_NAV__
    </nav>
  </aside>
  <main>
    <header class="hero">
      <h1>__TITLE__</h1>
      <div class="meta">
        File: <code>__FILENAME__</code> &bull; Tableau <code>__VERSION__</code> &bull; Audience: __AUDIENCE__ &bull; Generated: <code>__GENERATED_AT__</code>
      </div>
      <div class="stats-grid">
        <div class="stat"><span class="stat-val">__DASH_COUNT__</span><span class="stat-lbl">Dashboards</span></div>
        <div class="stat"><span class="stat-val">__WS_COUNT__</span><span class="stat-lbl">Worksheets</span></div>
        <div class="stat"><span class="stat-val">__DS_COUNT__</span><span class="stat-lbl">Data Sources</span></div>
        <div class="stat"><span class="stat-val">__CALC_COUNT__</span><span class="stat-lbl">Calculations</span></div>
        <div class="stat"><span class="stat-val">__PARAM_COUNT__</span><span class="stat-lbl">Parameters</span></div>
        <div class="stat"><span class="stat-val">__ACTION_COUNT__</span><span class="stat-lbl">Actions</span></div>
        __HEALTH_STAT__
        __USAGE_STAT__
      </div>
    </header>
    __BODY_SECTIONS__
  </main>
</div>
<script>
  try {
    const saved = localStorage.getItem('tableau-doc-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch (e) {}
  function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme') || 'light';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('tableau-doc-theme', next); } catch (e) {}
  }
  function filterDocs(q) {
    const needle = (q || '').trim().toLowerCase();
    document.querySelectorAll('.searchable').forEach((el) => {
      if (!needle) { el.classList.remove('hidden-filter'); return; }
      const hit = el.textContent.toLowerCase().includes(needle);
      el.classList.toggle('hidden-filter', !hit);
    });
  }
</script>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  try {
    mermaid.initialize({ startOnLoad: true, securityLevel: 'strict', theme: 'default' });
  } catch (err) {
    console.warn('Mermaid CDN unavailable:', err);
  }
</script>
</body>
</html>
"""
