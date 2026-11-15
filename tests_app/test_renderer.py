"""Tests for Markdown, interactive HTML, and JSON documentation renderers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tableau_doc_app.extractor import extract_workbook
from tableau_doc_app.models import (
    AuditRecommendation,
    CalculatedFieldDoc,
    DashboardDoc,
    DashboardDocumentationReport,
    DataArchitectureDoc,
    DocumentationBundle,
    KpiDefinition,
    TokenUsageSummary,
    WorksheetDoc,
)
from tableau_doc_app.renderer import (
    bundle_from_snapshot,
    render_html,
    render_json,
    render_markdown,
)

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sample_ai_report() -> DashboardDocumentationReport:
    return DashboardDocumentationReport(
        workbook_title="Commission & Sales Executive Suite",
        executive_summary="Tracks rep quota attainment, forecasted commissions, and regional revenue performance.",
        target_audience=["Sales Leadership", "RevOps Analysts"],
        kpi_glossary=[
            KpiDefinition(
                name="Quota Attainment %",
                business_definition="Share of assigned annual sales quota closed to date.",
                formula_or_aggregation="SUM([Sales]) / SUM([Quota])",
                datasource_and_worksheets="Sales Commission -> QuotaAttainment",
            )
        ],
        dashboards=[
            DashboardDoc(
                name="Commission Model",
                purpose="Interactive compensation modeling across quota tiers.",
                visual_layout_summary="2x2 analytical canvas with parameter sliders on top-right.",
                controls_and_filters="Adjusts accelerator rate and OTE target parameters.",
                interactivity_narrative="Selecting a sales rep filters commission projection bars.",
                key_caveats=["Assumes linear accelerator between 100% and 120% attainment."],
            )
        ],
        worksheets=[
            WorksheetDoc(
                name="CommissionProjection",
                title="Projected Commission",
                chart_type="Bar Chart",
                purpose="Compares modeled variable pay across reps.",
                axes_encodings_and_filters="Rows: Sales Person; Columns: Measure Values.",
            )
        ],
        calculated_fields_explained=[
            CalculatedFieldDoc(
                name="% of quota achieved",
                datasource="Sales Commission",
                formula="SUM([Sales]) / SUM([Quota])",
                plain_english_explanation="Divides actual revenue by quota target.",
                dependencies_and_usage="Used on QuotaAttainment worksheet.",
            )
        ],
        data_architecture=DataArchitectureDoc(
            overview="Combines CSV commission inputs with Excel Superstore benchmarks.",
            sources_and_lineage=["Sales Commission (textscan)", "Sample - Superstore (excel-direct)"],
            refresh_and_security_notes="Static packaged extracts stored in workbook archive.",
        ),
        mermaid_lineage_diagram="```mermaid\nflowchart LR\n  A[Sales] --> B[Commission Model]\n```",
        recommendations=[
            AuditRecommendation(
                severity="info",
                category="Documentation",
                finding="Some legacy calculated fields lack XML descriptions.",
                recommendation="Add tooltip comments inside Tableau Data Pane.",
            )
        ],
    )


def test_deterministic_render_all_formats():
    snap = extract_workbook(str(REPO_ROOT / "samples/show_workbook_info/geocoding.twbx"))
    bundle = bundle_from_snapshot(snap, target_dashboard="Commission Model")

    md = render_markdown(bundle)
    assert "# geocoding" in md
    assert "### Dashboard: Commission Model *(Primary Scope)*" in md
    assert "```mermaid" in md
    assert "QuotaAttainment" in md

    html_doc = render_html(bundle)
    assert "<!DOCTYPE html>" in html_doc
    assert 'class="wireframe-box"' in html_doc
    assert "data:image/png;base64," in html_doc
    assert "Commission Model" in html_doc

    json_compact = render_json(bundle, include_thumbnails=False)
    parsed_compact = json.loads(json_compact)
    assert parsed_compact["used_ai"] is False
    assert all(t["base64_png"] == "" for t in parsed_compact["snapshot"]["thumbnails"])

    json_full = render_json(bundle, include_thumbnails=True)
    parsed_full = json.loads(json_full)
    assert any(len(t["base64_png"]) > 100 for t in parsed_full["snapshot"]["thumbnails"])


def test_ai_report_merge_and_html_xss_escaping():
    snap = extract_workbook(str(REPO_ROOT / "samples/show_workbook_info/geocoding.twbx"))
    report = _sample_ai_report()
    report.executive_summary = 'Overview with <script>alert("xss")</script> payload'

    bundle = DocumentationBundle(
        snapshot=snap,
        ai_report=report,
        target_dashboard=None,
        audience="RevOps",
        generated_at_iso="2026-11-15T00:00:00Z",
        used_ai=True,
        usage=TokenUsageSummary(prompt_tokens=100, candidate_tokens=50, total_tokens=150),
        agent_model="gemini-default",
    )

    md = render_markdown(bundle)
    assert "# Commission & Sales Executive Suite" in md
    assert "## Executive Summary" in md
    assert "## KPI & Metric Glossary" in md
    assert "Divides actual revenue by quota target." in md
    assert "## Governance & Performance Recommendations" in md
    # Ensure mermaid fence stripped inside rendered markdown block (no nested ```mermaid```)
    assert "```mermaid\n```mermaid" not in md

    html_out = render_html(bundle)
    assert '<script>alert("xss")</script>' not in html_out
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in html_out
    assert "AI-Enriched (gemini-default)" in html_out
