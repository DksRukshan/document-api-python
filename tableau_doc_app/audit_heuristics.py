"""Deterministic Tableau performance, calculation, formatting, and UX audit rules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from tableau_doc_app.audit_models import (
    AuditCalculationInfo,
    AuditCategory,
    CategoryScore,
    Finding,
    Severity,
    WorkbookAuditReport,
)
from tableau_doc_app.audit_parser import parse_workbook_telemetry

SAFE_TABLEAU_FONTS = {
    "tableau book",
    "tableau regular",
    "tableau medium",
    "tableau bold",
    "tableau light",
    "tableau semibold",
    "arial",
    "trebuchet ms",
    "verdana",
    "times new roman",
    "lucida sans",
    "helvetica",
    "sans-serif",
}

SEVERITY_PENALTY = {
    Severity.CRITICAL: 24,
    Severity.HIGH: 14,
    Severity.MEDIUM: 7,
    Severity.LOW: 3,
    Severity.INFO: 0,
}

CATEGORY_LABELS = {
    AuditCategory.CALCULATIONS: "Calculations",
    AuditCategory.DATA_SOURCE: "Data Source & Modeling",
    AuditCategory.FILTERS: "Filters & Order of Operations",
    AuditCategory.LAYOUT_DASHBOARD: "Worksheets, Layout & Assets",
    AuditCategory.FORMATTING: "Formatting & Visual UX",
    AuditCategory.ACCESSIBILITY: "Accessibility (WCAG)",
    AuditCategory.SERVER_PUBLISHING: "Server & Publishing",
}


def _rewrite_else_if(formula: str) -> str:
    return re.sub(r"\bELSE\s+IF\b", "ELSEIF", formula, flags=re.IGNORECASE)


def _rewrite_redundant_bool(formula: str) -> str | None:
    m = re.match(
        r"^\s*IF\s+(.+?)\s+THEN\s+TRUE\s+ELSE\s+FALSE\s+END\s*$",
        formula,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return m.group(1).strip() if m else None


def _rewrite_chained_or(formula: str) -> str | None:
    matches = re.findall(
        r"(\[[^\]]+\])\s*=\s*('[^']*'|\"[^\"]*\"|\d+(?:\.\d+)?)",
        formula,
    )
    if len(matches) >= 3 and len({m[0] for m in matches}) == 1:
        field = matches[0][0]
        vals = ", ".join(m[1] for m in matches)
        return f"{field} IN ({vals})"
    return None


def _audit_calculations(report: WorkbookAuditReport, findings: list[Finding]) -> None:
    idx = 0

    def add(
        severity: Severity,
        title: str,
        calc: AuditCalculationInfo,
        evidence: str,
        impact: str,
        rec: str,
        before: str | None = None,
        after: str | None = None,
        citation: str = "InterWorks 2021 / Tableau Calc Best Practices",
        cat: AuditCategory = AuditCategory.CALCULATIONS,
    ) -> None:
        nonlocal idx
        idx += 1
        findings.append(
            Finding(
                id=f"calc-{idx}",
                category=cat,
                severity=severity,
                title=title,
                target=f"{calc.caption} ({calc.datasource_name})",
                evidence=evidence,
                impact_summary=impact,
                recommendation=rec,
                rewrite_before=before if before is not None else calc.formula,
                rewrite_after=after,
                source_citation=citation,
            )
        )

    unused_calcs = [c for c in report.calculations if c.is_unused]
    if unused_calcs:
        sample_names = ", ".join(c.caption for c in unused_calcs[:6])
        findings.append(
            Finding(
                id="calc-unused-bulk",
                category=AuditCategory.CALCULATIONS,
                severity=Severity.LOW if len(unused_calcs) < 5 else Severity.MEDIUM,
                title=f"{len(unused_calcs)} Unused Calculated Field{'s' if len(unused_calcs) != 1 else ''} Found",
                target=", ".join(c.caption for c in unused_calcs[:4]),
                evidence=f"Fields unreferenced by any sheet, filter, dashboard, or downstream calc: {sample_names}.",
                impact_summary="Clutters the Data pane, slows metadata parsing, and prevents lean extracts unless hidden.",
                recommendation="Delete obsolete calculations or right-click -> Hide unused fields before regenerating the extract.",
                source_citation="InterWorks 2021 (Hide Unused Fields)",
            )
        )

    for c in report.calculations:
        if "else_if_space" in c.flags:
            add(
                Severity.MEDIUM,
                "Nested 'ELSE IF' Instead of 'ELSEIF'",
                c,
                "Formula uses two keywords ('ELSE IF'), creating unnecessary nested conditional blocks.",
                "Adds query compilation overhead and hurts formula readability.",
                "Collapse all 'ELSE IF' occurrences into single-keyword 'ELSEIF' with a single trailing END.",
                after=_rewrite_else_if(c.formula),
            )
        if "multi_if_to_case" in c.flags:
            add(
                Severity.HIGH,
                "Multi-Branch Equality IF/ELSEIF Should Use CASE",
                c,
                "Repeated equality comparisons on the same field in an IF/ELSEIF chain.",
                "CASE statements rank above IF/ELSEIF for VizQL compilation and SQL CASE WHEN pushdown.",
                "Rewrite equality branches to a single CASE [Field] WHEN ... ELSE ... END statement, ordering most frequent values first.",
            )
        if "chained_or" in c.flags:
            add(
                Severity.MEDIUM,
                "Chained OR Equality Checks Can Use IN ()",
                c,
                "Multiple OR clauses testing the same dimension.",
                "Tableau's IN operator compiles into cleaner SQL IN (...) predicates than stacked ORs.",
                "Replace stacked '[Field] = ... OR [Field] = ...' clauses with '[Field] IN (...)' and order common values first.",
                after=_rewrite_chained_or(c.formula),
                citation="InterWorks 2021 (Use IN Instead of OR)",
            )
        if "uses_attr" in c.flags:
            add(
                Severity.HIGH,
                "ATTR() Doubles Aggregation Cost",
                c,
                "Formula calls ATTR(...), which computes both MIN() and MAX() under the hood.",
                "ATTR() executes two aggregations per mark and blocks certain SQL optimizations and extract materialization.",
                "Replace ATTR([Field]) with MIN([Field]) or MAX([Field]) wherever single-value uniqueness per mark is guaranteed.",
                after=re.sub(r"\bATTR\s*\(", "MIN(", c.formula, flags=re.IGNORECASE),
                citation="InterWorks 2021 (Prefer MIN/MAX over ATTR)",
            )
        if "uses_countd" in c.flags:
            add(
                Severity.HIGH,
                "Expensive COUNTD() Distinct Count Calculation",
                c,
                "Uses COUNTD(...) across marks/rows.",
                "COUNTD prevents pre-aggregation and forces high-cardinality sorting/hashing on every filter change.",
                "Evaluate whether COUNT(), SUM([Row Flag]), SIZE(), or an ETL/Hyper pre-aggregated count satisfies the business metric.",
            )
        if "uses_now" in c.flags:
            add(
                Severity.HIGH,
                "NOW() Invalidates Query Caching Every Second",
                c,
                "Calls volatile NOW() function inside a calculated field.",
                "Because NOW() changes every second, VizQL/Data Server cannot reuse cached query results across users or interactions.",
                "Switch to TODAY() if day-level precision is sufficient, or bind to a precomputed extract timestamp / parameter.",
                after=re.sub(r"\bNOW\s*\(\s*\)", "TODAY()", c.formula, flags=re.IGNORECASE),
            )
        elif "uses_today" in c.flags:
            add(
                Severity.LOW,
                "TODAY() Prevents Extract Pre-Materialization",
                c,
                "References TODAY() inside a calculated field.",
                "Volatile date functions cannot be materialized by Hyper 'Compute Calculations Now'.",
                "Keep TODAY() if live current-day anchoring is required; otherwise compare against {MAX([Date])} or an extract refresh timestamp.",
            )
        if "uses_lod" in c.flags:
            add(
                Severity.MEDIUM,
                "Level-of-Detail (LOD) Expression Overhead",
                c,
                "Uses {FIXED / INCLUDE / EXCLUDE} LOD expression.",
                "LODs generate extra subqueries (and FIXED bypasses regular dimension filters unless promoted to Context).",
                "For static cohorts/first-purchase dates, precompute via Relationships or database ETL. For small viz scopes, compare against WINDOW_SUM() table calcs.",
            )
        if "uses_rawsql_or_script" in c.flags:
            add(
                Severity.CRITICAL,
                "External RAWSQL / SCRIPT_* Extension Call",
                c,
                "Invokes external analytics extension or RAWSQL pass-through.",
                "Serializes rows out-of-process (TabPy/Rserve) or bypasses VizQL query fusion per interaction.",
                "Pre-score models or complex transformations in batch pipelines/warehouse tables before loading into Tableau.",
            )
        if "chained_string_checks" in c.flags:
            add(
                Severity.HIGH,
                "Chained String Search / Slicing Operations",
                c,
                "Combines multiple CONTAINS/STARTSWITH/FIND/MID/LEFT/RIGHT calls.",
                "Row-level string scanning slows both SQL databases and Hyper extracts.",
                "Best: materialize boolean/integer classification flags in ETL or Hyper 'Compute Calculations Now'. Otherwise consolidate chained checks with REGEXP_MATCH / REGEXP_EXTRACT.",
                citation="InterWorks 2021 & Tableau Docs (REGEXP vs Chained String Ops)",
            )
        if "repeated_calc_call" in c.flags:
            add(
                Severity.HIGH,
                "Repeated Reference to Nested Calculated Field",
                c,
                f"References nested calculation(s) multiple times: {', '.join(c.referenced_calcs)}.",
                "Tableau expands referenced calculations inline each time they appear, causing exponential query tree growth.",
                "Refactor into a clean single expression or materialize the base intermediate metric in the data source / Hyper extract.",
                citation="Tableau Docs (Do Not Repeat Calculations Inside a Calculation)",
            )
        if "deep_calc_chain" in c.flags:
            add(
                Severity.MEDIUM,
                f"Deep Calculation Dependency Chain (Depth {c.nesting_depth})",
                c,
                f"Depends transitively across {c.nesting_depth} calculation layers.",
                "Deeply nested expression trees bloat 'Compiling query' time and SQL subquery size.",
                "Flatten intermediate calculations or push foundational metrics down to the database view / Hyper extract.",
            )
        if "aggregate_wrapping_branch" in c.flags:
            add(
                Severity.MEDIUM,
                "Conditional Logic Evaluated Row-by-Row Inside Aggregation",
                c,
                "Pattern like SUM(IF ... THEN [Measure] ... END) executes branching per row.",
                "When switching metrics via a Parameter or dimension check at viz grain, testing before/outside aggregation avoids scanning branches on every row.",
                "If branching on a Parameter (e.g. [Metric Selector]), move CASE/IF outside SUM(): 'CASE [Param] WHEN 1 THEN SUM([Sales]) WHEN 2 THEN SUM([Profit]) END'.",
            )
        if "redundant_if_bool" in c.flags:
            add(
                Severity.LOW,
                "Redundant 'IF ... THEN TRUE ELSE FALSE END' Wrapper",
                c,
                "Wraps an already-boolean expression in IF/THEN TRUE ELSE FALSE.",
                "Unnecessary conditional branching hurts readability and expression simplicity.",
                "Return the boolean predicate directly without IF/THEN/ELSE.",
                after=_rewrite_redundant_bool(c.formula),
            )
        if "kpi_arrow_string_calc" in c.flags:
            add(
                Severity.LOW,
                "KPI Arrow / Status String Calculation Can Use Custom Number Format",
                c,
                "Generates string symbols (▲/▼/Up/Down) in a calculated field.",
                "String indicators require extra calc + Shape mark rendering instead of a single native measure pill.",
                "Use numeric delta directly with Tableau Custom Number Format: '▲ +0.0%;▼ -0.0%;— 0.0%' (no string calc or Shape marks needed).",
                after="▲ +0.0%;▼ -0.0%;— 0.0%",
                cat=AuditCategory.FORMATTING,
                citation="InterWorks 2021 (Custom Number Formatting Trick)",
            )


def _audit_datasources(report: WorkbookAuditReport, findings: list[Finding]) -> None:
    single_thread_classes = {"excel-direct", "excel", "textscan", "csv"}
    for i, ds in enumerate(report.datasources, start=1):
        classes_lower = {c.lower() for c in ds.connection_classes}
        if not ds.is_extract and (classes_lower & single_thread_classes):
            findings.append(
                Finding(
                    id=f"ds-singlethread-{i}",
                    category=AuditCategory.DATA_SOURCE,
                    severity=Severity.CRITICAL,
                    title="Single-Threaded Live Excel / CSV Connection",
                    target=ds.caption,
                    evidence=f"Live connection using {', '.join(ds.connection_classes)} without a Hyper extract.",
                    impact_summary="Excel/CSV drivers run 1 query at a time (vs up to 16 parallel queries on Hyper Extracts).",
                    recommendation="Convert the connection to a Tableau Hyper Extract (.hyper), hide unused fields, and run Compute Calculations Now.",
                    source_citation="InterWorks 2021 (Parallel Query Limits)",
                )
            )
        elif not ds.is_extract and not ds.is_published:
            findings.append(
                Finding(
                    id=f"ds-live-{i}",
                    category=AuditCategory.DATA_SOURCE,
                    severity=Severity.MEDIUM,
                    title="Embedded Live Database Connection Without Extract",
                    target=ds.caption,
                    evidence=f"Connection ({', '.join(ds.connection_classes) or 'live'}) is embedded and live.",
                    impact_summary="Every dashboard filter change executes live SQL; performance depends entirely on DB concurrency and indexes.",
                    recommendation="Unless sub-second real-time data is mandatory, use a scheduled Hyper Extract (aggregated to visible dimensions if possible).",
                    source_citation="InterWorks 2021 (Hyper Extracts)",
                )
            )

        if ds.has_custom_sql:
            findings.append(
                Finding(
                    id=f"ds-customsql-{i}",
                    category=AuditCategory.DATA_SOURCE,
                    severity=Severity.CRITICAL,
                    title=f"Custom SQL Detected ({ds.custom_sql_count} block(s))",
                    target=ds.caption,
                    evidence="Datasource embeds <relation type='text'> Custom SQL.",
                    impact_summary="Forces Tableau to wrap every query in an opaque subquery, disabling join culling and partition pruning.",
                    recommendation="Replace Custom SQL with a database view/materialized table and connect tables via Tableau Logical Relationships.",
                    source_citation="InterWorks 2021 (Avoid Custom SQL)",
                )
            )

        if ds.physical_join_count > 0 and not ds.uses_relationships:
            findings.append(
                Finding(
                    id=f"ds-joins-{i}",
                    category=AuditCategory.DATA_SOURCE,
                    severity=Severity.HIGH,
                    title=f"Legacy Physical Joins ({ds.physical_join_count}) Without Logical Relationships",
                    target=ds.caption,
                    evidence="Datasource uses physical join nodes instead of Tableau's logical Relationships layer.",
                    impact_summary="Physical joins risk row duplication and force multi-table joins even when a sheet only queries one table.",
                    recommendation="Migrate to Logical Relationships (Tableau 2020.2+ object model) so Tableau queries only needed tables per viz.",
                    source_citation="InterWorks 2021 (Relationships vs Joins)",
                )
            )

        if ds.is_extract and ds.hidden_field_count == 0 and ds.total_field_count >= 12:
            findings.append(
                Finding(
                    id=f"ds-unhidden-{i}",
                    category=AuditCategory.DATA_SOURCE,
                    severity=Severity.MEDIUM,
                    title="Extract Keeps All Unused Columns Unhidden",
                    target=ds.caption,
                    evidence=f"{ds.total_field_count} fields defined with 0 hidden fields.",
                    impact_summary="Storing unused wide text/ID columns slows extract generation, upload times, and memory scans.",
                    recommendation="Click 'Hide All Unused Fields' in the Data pane, set extract date/row filters if appropriate, and refresh the extract.",
                    source_citation="Tableau Docs (perf_reduce_upload_times.md)",
                )
            )

        if ds.is_extract and ds.extract_filter_count == 0 and ds.datasource_filter_count == 0:
            findings.append(
                Finding(
                    id=f"ds-nofilter-{i}",
                    category=AuditCategory.DATA_SOURCE,
                    severity=Severity.INFO,
                    title="No Extract or Data Source Filters Configured",
                    target=ds.caption,
                    evidence="0 extract filters and 0 data source filters detected.",
                    impact_summary="If historical archive rows aren't needed in views, scanning full history inflates query and extract cost.",
                    recommendation="Add an Extract Filter (e.g., last 24–36 months or active status) before publishing.",
                    source_citation="InterWorks 2021 (Filter Order of Operations)",
                )
            )


def _audit_filters(report: WorkbookAuditReport, findings: list[Finding]) -> None:
    rel_filters = [f for f in report.filters if f.uses_only_relevant_values]
    rel_dash_zones = sum(d.only_relevant_values_zone_count for d in report.dashboards)
    if rel_filters or rel_dash_zones > 0:
        targets = ", ".join(sorted({f"{f.worksheet}:{f.field}" for f in rel_filters})[:5])
        findings.append(
            Finding(
                id="flt-only-relevant",
                category=AuditCategory.FILTERS,
                severity=Severity.HIGH,
                title="'Only Relevant Values' Cascading Quick Filters Detected",
                target=targets or "Dashboard Quick Filters",
                evidence=f"{len(rel_filters)} sheet filter(s) and {rel_dash_zones} dashboard zone(s) use 'Only Relevant Values'.",
                impact_summary="Triggers cascading SELECT DISTINCT queries on every filter selection across the dashboard.",
                recommendation="Switch filters to 'All Values in Database' (or 'All Values in Context'), or replace with Dashboard Action Filters.",
                source_citation="InterWorks 2021 (Avoid Only Relevant Values)",
            )
        )

    volatile_date_filters = [f for f in report.filters if f.uses_date_part_string_or_volatile]
    if volatile_date_filters:
        findings.append(
            Finding(
                id="flt-date-string-volatile",
                category=AuditCategory.FILTERS,
                severity=Severity.MEDIUM,
                title="Non-Indexed Discrete Date (MY/MDY) or Volatile Date Filters",
                target=", ".join(sorted({f.worksheet for f in volatile_date_filters})[:4]),
                evidence=f"Found {len(volatile_date_filters)} filter(s) matching MY/MDY or NOW/TODAY patterns.",
                impact_summary="Discrete MY/MDY date strings defeat index range scans; NOW()/TODAY() reduce query cache reuse.",
                recommendation="Prefer Continuous Range Date filters or bounded Relative Date filters where appropriate.",
                source_citation="InterWorks 2021 (Date Filter Best Practices)",
            )
        )

    sheet_has_context = {
        ws_name: any(f.is_context for f in report.filters if f.worksheet == ws_name)
        for ws_name in {w.name for w in report.worksheets}
    }
    topn_no_ctx = [
        f
        for f in report.filters
        if f.is_top_n and not sheet_has_context.get(f.worksheet, False)
    ]
    if topn_no_ctx:
        findings.append(
            Finding(
                id="flt-topn-context",
                category=AuditCategory.FILTERS,
                severity=Severity.MEDIUM,
                title="Top N Filter Without Supporting Context Filter",
                target=", ".join(sorted({f"{f.worksheet} ({f.field})" for f in topn_no_ctx})),
                evidence="Top N filter runs alongside regular dimension filters without a Context filter.",
                impact_summary="By Tableau's Order of Operations, Top N runs BEFORE Dimension filters—causing unexpected fewer-than-N rows and wasted sorting.",
                recommendation="Promote structural scoping filters (e.g., Year, Region) to Context filters ONLY when scoping Top N or FIXED LODs accurately.",
                source_citation="Tableau Order of Operations",
            )
        )

    multi_list_zones = sum(d.multi_value_list_zone_count for d in report.dashboards)
    if multi_list_zones > 0:
        findings.append(
            Finding(
                id="flt-multi-list",
                category=AuditCategory.FILTERS,
                severity=Severity.MEDIUM,
                title=f"Multi-Value Dropdown/Checklist Filter Controls ({multi_list_zones})",
                target="Dashboard Filter Zones",
                evidence=f"{multi_list_zones} filter control(s) configured as multi-select dropdown/checklist.",
                impact_summary="High-cardinality checkbox lists pull full distinct domains and fire repeated queries on every individual click.",
                recommendation="Enable 'Show Apply Button' on multi-select filters, or switch high-cardinality lists to Wildcard Match / Custom Value List / Single-Value Dropdown.",
                source_citation="InterWorks 2021 (Limit Quick Filter Types)",
            )
        )


def _audit_worksheets_dashboards_server(
    report: WorkbookAuditReport, findings: list[Finding]
) -> None:
    blending_sheets = [w.name for w in report.worksheets if w.uses_blending]
    if blending_sheets:
        findings.append(
            Finding(
                id="ws-blending",
                category=AuditCategory.DATA_SOURCE,
                severity=Severity.HIGH,
                title=f"Data Blending Used on {len(blending_sheets)} Worksheet(s)",
                target=", ".join(blending_sheets[:4]),
                evidence="Multiple primary/secondary datasources referenced inside the same worksheet.",
                impact_summary="Data blending executes separate queries and joins in-memory, degrading sharply on high-cardinality linking keys.",
                recommendation="Replace Data Blending with Tableau Logical Relationships or warehouse/ETL joins.",
                source_citation="InterWorks 2021 (Blending Cardinality)",
            )
        )

    for w in report.worksheets:
        if w.uses_pages_shelf:
            findings.append(
                Finding(
                    id=f"ws-pages-{w.name}",
                    category=AuditCategory.LAYOUT_DASHBOARD,
                    severity=Severity.HIGH,
                    title="Pages Shelf Loads Entire Domain Upfront",
                    target=w.name,
                    evidence="Worksheet uses the <pages> shelf.",
                    impact_summary="Tableau computes and transfers data for every page simultaneously on initial load.",
                    recommendation="Replace the Pages Shelf with a clean single-value Dimension Filter or Date Parameter.",
                    source_citation="InterWorks 2021 (Avoid Pages Shelf)",
                )
            )
        if w.uses_polygons:
            findings.append(
                Finding(
                    id=f"ws-polygon-{w.name}",
                    category=AuditCategory.LAYOUT_DASHBOARD,
                    severity=Severity.HIGH,
                    title="Heavy Polygon Mark Type Forces Server-Side Rendering Risk",
                    target=w.name,
                    evidence="Mark class 'Polygon' configured on worksheet.",
                    impact_summary="Dense polygon vertices bloat data transfer and disable crisp client-side rendering & accessibility.",
                    recommendation="Use Tableau's built-in geographic roles or simplified GeoJSON/spatial geometries where possible.",
                    source_citation="InterWorks 2021 (Marks & Rendering)",
                )
            )
        if w.is_text_table_heavy:
            findings.append(
                Finding(
                    id=f"ws-texttable-{w.name}",
                    category=AuditCategory.LAYOUT_DASHBOARD,
                    severity=Severity.MEDIUM,
                    title="Dense Text Table / Crosstab Anti-Pattern",
                    target=w.name,
                    evidence=f"Text/Automatic crosstab with {len(w.row_pills) + len(w.col_pills)} shelf pills.",
                    impact_summary="Large text tables trigger high 'Computing layouts' CPU time and exceed the <1,000-mark client-side & accessibility threshold.",
                    recommendation="Replace massive crosstabs with KPI cards + bar charts, or gate the detail table behind an Action Filter set to 'Exclude all values' on clear.",
                    source_citation="InterWorks 2021 & Tableau Accessibility Docs",
                )
            )
        if w.has_viz_in_tooltip:
            findings.append(
                Finding(
                    id=f"ws-vit-{w.name}",
                    category=AuditCategory.LAYOUT_DASHBOARD,
                    severity=Severity.INFO,
                    title="Viz in Tooltip Executes On-Hover Queries",
                    target=w.name,
                    evidence="Tooltip embeds another worksheet.",
                    impact_summary="Hovering across marks triggers filtered child queries.",
                    recommendation="Ensure the referenced Viz-in-Tooltip target sheet has minimal marks, simple formatting, and lives on an extract.",
                )
            )

    for d in report.dashboards:
        if d.sizing_mode == "automatic":
            findings.append(
                Finding(
                    id=f"dash-autosize-{d.name}",
                    category=AuditCategory.LAYOUT_DASHBOARD,
                    severity=Severity.HIGH,
                    title="Automatic Dashboard Sizing Breaks Server Image Caching",
                    target=d.name,
                    evidence="Dashboard sizing-mode='automatic'.",
                    impact_summary="Each user's unique viewport invalidates Tableau Server/Cloud pre-rendered layout caches and stretches charts unpredictably.",
                    recommendation="Switch to a Fixed Size (e.g., Desktop 1366x768 or 1440x900) so Tableau caches rendered views across users.",
                    source_citation="InterWorks 2021 (Fixed vs Automatic Size)",
                )
            )
        if d.worksheet_zone_count > 5:
            findings.append(
                Finding(
                    id=f"dash-views-{d.name}",
                    category=AuditCategory.LAYOUT_DASHBOARD,
                    severity=Severity.HIGH,
                    title=f"Too Many Worksheets on Single Dashboard ({d.worksheet_zone_count} views)",
                    target=d.name,
                    evidence=f"Contains {d.worksheet_zone_count} views: {', '.join(d.worksheet_names[:6])}.",
                    impact_summary="Initial dashboard load waits on all visible sheets; recommended sweet spot is 2–3 views (max 5).",
                    recommendation="Split into focused dashboards linked by Navigation Buttons, or move secondary detail sheets inside a collapsed Show/Hide Container.",
                    source_citation="InterWorks 2021 & Tableau Dashboard Best Practices",
                )
            )
        if d.filter_zone_count > 5:
            findings.append(
                Finding(
                    id=f"dash-filters-{d.name}",
                    category=AuditCategory.FILTERS,
                    severity=Severity.MEDIUM,
                    title=f"High Quick Filter Density ({d.filter_zone_count} filters on dashboard)",
                    target=d.name,
                    evidence=f"{d.filter_zone_count} quick filter zones visible.",
                    impact_summary="Exceeds the 3–5 quick filter best practice; populates query compilation & layout clutter.",
                    recommendation="Consolidate filters using Dashboard Filter Actions or place extra filters inside a collapsible Show/Hide Container.",
                    source_citation="InterWorks 2021 (Limit Quick Filters)",
                )
            )
        if d.max_container_nesting >= 3:
            findings.append(
                Finding(
                    id=f"dash-nesting-{d.name}",
                    category=AuditCategory.LAYOUT_DASHBOARD,
                    severity=Severity.MEDIUM,
                    title=f"Deeply Nested Layout Containers (Depth {d.max_container_nesting})",
                    target=d.name,
                    evidence=f"Container hierarchy reaches {d.max_container_nesting} nested levels with {d.blank_spacer_count} blank spacer(s).",
                    impact_summary="Deep container trees slow 'Computing layouts' and complicate dashboard maintenance.",
                    recommendation="Flatten container hierarchy and replace Blank spacer zones with Outer/Inner Padding on containers.",
                    source_citation="InterWorks 2021 & Tableau Formatting Docs",
                )
            )
        if d.device_layout_count > 0:
            findings.append(
                Finding(
                    id=f"dash-devicelayouts-{d.name}",
                    category=AuditCategory.LAYOUT_DASHBOARD,
                    severity=Severity.LOW,
                    title=f"Custom/Auto Device Layouts Present ({d.device_layout_count})",
                    target=d.name,
                    evidence=f"Detected {d.device_layout_count} device-specific layout definition(s).",
                    impact_summary="Unused auto-generated Phone/Tablet layouts add XML and layout overhead unless actively supported.",
                    recommendation="Delete unused Phone/Tablet device layouts if users consume primarily on Desktop/Web.",
                    source_citation="InterWorks 2021",
                )
            )
        if d.action_count > 0 and d.action_exclude_on_clear_count == 0:
            findings.append(
                Finding(
                    id=f"dash-action-clear-{d.name}",
                    category=AuditCategory.FILTERS,
                    severity=Severity.MEDIUM,
                    title="Dashboard Actions Do Not 'Exclude All Values' on Clear",
                    target=d.name,
                    evidence=f"{d.action_count} action(s) found with 0 using on-exit='exclude'.",
                    impact_summary="Clearing a drill-down selection forces detail sheets to query and render the entire unfiltered dataset.",
                    recommendation="Set 'Clearing the selection will: Exclude all values' for drill-down target worksheets so they render 0 marks until clicked.",
                    source_citation="InterWorks 2021 (Action Filter Best Practices)",
                )
            )

    if report.workbook_style.show_tabs:
        findings.append(
            Finding(
                id="srv-show-tabs",
                category=AuditCategory.SERVER_PUBLISHING,
                severity=Severity.MEDIUM,
                title="Publishing Option 'Show Sheets as Tabs' Enabled",
                target="Workbook Windows / Preferences",
                evidence="Workbook XML specifies tabs='true' / ui.show-tabs.",
                impact_summary="Showing sheet tabs expands session initialization and encourages heavy monolithic workbooks.",
                recommendation="Uncheck 'Show sheets as tabs' when publishing and connect dashboards using Navigation Buttons.",
                source_citation="InterWorks 2021 (Publish Without Tabs)",
            )
        )

    large_images = [
        a for a in report.assets if a.kind == "image" and a.size_bytes > 50 * 1024
    ]
    if large_images:
        largest = max(large_images, key=lambda a: a.size_bytes)
        findings.append(
            Finding(
                id="srv-large-images",
                category=AuditCategory.SERVER_PUBLISHING,
                severity=Severity.MEDIUM,
                title=f"{len(large_images)} Oversized Image Asset(s) (>50 KB) in .twbx",
                target=largest.path,
                evidence=f"Largest image is {largest.size_bytes // 1024} KB ({largest.path}).",
                impact_summary="Bloats workbook upload/download sizes and dashboard image asset fetch time.",
                recommendation="Compress dashboard logos/icons under 50 KB; use tiny 32x32 transparent PNGs for transparent button/spacer overlays.",
                source_citation="InterWorks 2021 (Image Optimization)",
            )
        )


def _audit_formatting_and_accessibility(
    report: WorkbookAuditReport, findings: list[Finding]
) -> None:
    ws_override_heavy = [w for w in report.worksheets if w.style_override_count >= 5]
    if ws_override_heavy:
        findings.append(
            Finding(
                id="fmt-override-bloat",
                category=AuditCategory.FORMATTING,
                severity=Severity.MEDIUM,
                title="Worksheet-Level Formatting Overrides Instead of Workbook Theme",
                target=", ".join(w.name for w in ws_override_heavy[:4]),
                evidence=f"{len(ws_override_heavy)} sheet(s) carry >=5 explicit <style-rule> overrides.",
                impact_summary="Maintains inconsistent typography/borders across sheets and bloats .twb XML.",
                recommendation="Apply fonts, titles, shading, dividers, and gridlines top-down at Format -> Workbook first, reserving sheet overrides for exceptions.",
                source_citation="Tableau Official Docs (Formatting Hierarchy)",
            )
        )

    all_fonts: set[str] = set(report.workbook_style.workbook_fonts)
    non_standard_fonts: set[str] = set()
    for w in report.worksheets:
        for f in w.fonts_used:
            all_fonts.add(f)
            if f.lower().strip("'\"") not in SAFE_TABLEAU_FONTS:
                non_standard_fonts.add(f)

    if len(all_fonts) > 2 or non_standard_fonts:
        findings.append(
            Finding(
                id="fmt-fonts",
                category=AuditCategory.FORMATTING,
                severity=Severity.MEDIUM if non_standard_fonts else Severity.LOW,
                title="Inconsistent or Non-Standard Fonts Detected",
                target=", ".join(sorted(all_fonts)[:5]) or "Workbook Typography",
                evidence=f"Fonts observed across workbook/sheets: {', '.join(sorted(all_fonts))}.",
                impact_summary="Custom desktop-only fonts fail to render cleanly on Tableau Server/Cloud/browser and fragment visual hierarchy.",
                recommendation="Standardize on the Tableau font family (Tableau Book/Medium/Bold) or web-safe Arial/Trebuchet MS/Verdana across 3–4 type sizes.",
                source_citation="Tableau Docs (Typography & Web Rendering)",
            )
        )

    default_tooltips = [w.name for w in report.worksheets if w.tooltip_is_default]
    if default_tooltips:
        findings.append(
            Finding(
                id="fmt-default-tooltips",
                category=AuditCategory.FORMATTING,
                severity=Severity.LOW,
                title=f"{len(default_tooltips)} Worksheet(s) Using Default Generic Tooltips",
                target=", ".join(default_tooltips[:4]),
                evidence="No <customized-tooltip> narrative detected on sheets.",
                impact_summary="Default Field: Value dumps waste hover real estate and force users to decipher raw column names.",
                recommendation="Format tooltips last: write concise narrative sentences bolding key KPIs and disable Command Buttons on clean executive views.",
                source_citation="Tableau Docs (Formatting & Tooltips)",
            )
        )

    cluttered_lines = [
        w.name for w in report.worksheets if w.explicit_gridline_or_border_count >= 2
    ]
    if cluttered_lines:
        findings.append(
            Finding(
                id="fmt-clutter-lines",
                category=AuditCategory.FORMATTING,
                severity=Severity.LOW,
                title="Explicit Gridlines / Heavy Cell Borders / Banding Enabled",
                target=", ".join(cluttered_lines[:4]),
                evidence="Explicit non-none gridline, border, or row-banding style rules found.",
                impact_summary="Heavy ink/gridlines compete with data marks and reduce visual whitespace.",
                recommendation="Remove unnecessary zero lines, drop lines, axis ticks, and borders; rely on direct data labels and subtle 8–12px container padding.",
                source_citation="Tableau Docs (Formatting Best Practices)",
            )
        )

    all_caps_sheets = [w.name for w in report.worksheets if w.has_all_caps_title]
    if all_caps_sheets:
        findings.append(
            Finding(
                id="acc-allcaps",
                category=AuditCategory.ACCESSIBILITY,
                severity=Severity.LOW,
                title="ALL-CAPS View Title(s) Hurt Readability & Screen Readers",
                target=", ".join(all_caps_sheets[:4]),
                evidence=f"Uppercase-only titles on: {', '.join(all_caps_sheets[:4])}.",
                impact_summary="ALL-CAPS slows eye scanning and can cause assistive screen readers to read words letter-by-letter.",
                recommendation="Use descriptive Title Case or Sentence Case titles explaining what the chart reveals.",
                source_citation="Tableau Docs (Accessibility Guidelines)",
            )
        )

    no_caption_sheets = [w.name for w in report.worksheets if not w.has_caption]
    if report.worksheets and len(no_caption_sheets) == len(report.worksheets):
        findings.append(
            Finding(
                id="acc-captions",
                category=AuditCategory.ACCESSIBILITY,
                severity=Severity.INFO,
                title="No View Captions or Descriptive Text Detected",
                target="All Worksheets",
                evidence="All worksheets have hidden/absent captions.",
                impact_summary="Adding descriptive titles, subtitles, captions, and image Alt Text improves executive context and WCAG 2.2 AA compliance.",
                recommendation="Add concise subtitles/captions stating metric definitions, units, and date scope, and double-check 4.5:1 text / 3:1 mark color contrast.",
                source_citation="Tableau Docs (WCAG 2.2 AA Accessibility)",
            )
        )


def run_heuristic_audit(report: WorkbookAuditReport) -> WorkbookAuditReport:
    """Populate deterministic findings, category scores, and overall grade on WorkbookAuditReport."""
    findings: list[Finding] = []
    _audit_calculations(report, findings)
    _audit_datasources(report, findings)
    _audit_filters(report, findings)
    _audit_worksheets_dashboards_server(report, findings)
    _audit_formatting_and_accessibility(report, findings)

    sev_order = {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
        Severity.INFO: 4,
    }
    findings.sort(key=lambda f: (sev_order[f.severity], f.category.value, f.id))

    cat_scores: list[CategoryScore] = []
    for cat, label in CATEGORY_LABELS.items():
        cat_findings = [f for f in findings if f.category == cat]
        penalty = sum(SEVERITY_PENALTY[f.severity] for f in cat_findings)
        score = max(15, 100 - min(85, penalty))
        cat_scores.append(
            CategoryScore(
                category=cat,
                label=label,
                score=score,
                issue_count=len(cat_findings),
                critical_count=sum(1 for f in cat_findings if f.severity == Severity.CRITICAL),
                high_count=sum(1 for f in cat_findings if f.severity == Severity.HIGH),
            )
        )

    overall = round(sum(c.score for c in cat_scores) / max(1, len(cat_scores)))
    if overall >= 90:
        grade = "A"
    elif overall >= 78:
        grade = "B"
    elif overall >= 65:
        grade = "C"
    elif overall >= 50:
        grade = "D"
    else:
        grade = "F"

    report.findings = findings
    report.category_scores = cat_scores
    report.overall_score = overall
    report.grade = grade
    report.summary_stats["total_findings"] = len(findings)
    report.summary_stats["critical_findings"] = sum(
        1 for f in findings if f.severity == Severity.CRITICAL
    )
    report.summary_stats["high_findings"] = sum(
        1 for f in findings if f.severity == Severity.HIGH
    )
    return report


def audit_workbook_bytes(raw_bytes: bytes, filename: str) -> WorkbookAuditReport:
    """Parse raw .twb/.twbx bytes and run deterministic performance, UX, and WCAG heuristics."""
    return run_heuristic_audit(parse_workbook_telemetry(raw_bytes, filename))


def audit_workbook_file(
    file_path: str, display_filename: Optional[str] = None
) -> WorkbookAuditReport:
    """Read a .twb/.twbx file from disk and run deterministic performance/UX heuristics."""
    p = Path(file_path)
    shown_name = Path(display_filename).name if display_filename else p.name
    return audit_workbook_bytes(p.read_bytes(), shown_name)
