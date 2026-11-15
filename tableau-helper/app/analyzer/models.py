"""Pydantic schemas for Tableau structural telemetry, heuristics, and AI reports."""

from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class AuditCategory(str, Enum):
    CALCULATIONS = "calculations"
    FORMATTING = "formatting"
    DATA_SOURCE = "data_source"
    FILTERS = "filters"
    LAYOUT_DASHBOARD = "layout_dashboard"
    ACCESSIBILITY = "accessibility"
    SERVER_PUBLISHING = "server_publishing"
    PERFORMANCE_RECORDING = "performance_recording"


class CalculationInfo(BaseModel):
    name: str
    caption: str
    datasource_name: str
    formula: str
    datatype: str = "unknown"
    role: str = "unknown"
    is_hidden: bool = False
    is_unused: bool = False
    referenced_calcs: list[str] = Field(default_factory=list)
    used_in_sheets: list[str] = Field(default_factory=list)
    nesting_depth: int = 0
    flags: list[str] = Field(default_factory=list)
    complexity_score: int = 0


class DatasourceInfo(BaseModel):
    name: str
    caption: str
    is_published: bool = False
    is_extract: bool = False
    connection_classes: list[str] = Field(default_factory=list)
    has_custom_sql: bool = False
    custom_sql_count: int = 0
    uses_relationships: bool = False
    physical_join_count: int = 0
    table_count: int = 0
    total_field_count: int = 0
    hidden_field_count: int = 0
    unused_field_count: int = 0
    extract_filter_count: int = 0
    datasource_filter_count: int = 0
    uses_aggregated_extract: bool = False
    referential_integrity_assumed: bool = False


class FilterInfo(BaseModel):
    field: str
    worksheet: str
    filter_type: str = "categorical"
    is_context: bool = False
    uses_only_relevant_values: bool = False
    mode: str = "default"
    is_top_n: bool = False
    uses_date_part_string_or_volatile: bool = False


class WorksheetInfo(BaseModel):
    name: str
    datasource_names: list[str] = Field(default_factory=list)
    mark_types: list[str] = Field(default_factory=list)
    row_pills: list[str] = Field(default_factory=list)
    col_pills: list[str] = Field(default_factory=list)
    detail_pills: list[str] = Field(default_factory=list)
    filter_count: int = 0
    uses_pages_shelf: bool = False
    uses_blending: bool = False
    uses_polygons: bool = False
    is_text_table_heavy: bool = False
    uses_density_marks: bool = False
    has_custom_tooltip: bool = False
    tooltip_is_default: bool = True
    tooltip_has_command_buttons: bool = True
    has_viz_in_tooltip: bool = False
    has_title: bool = True
    title_text: str = ""
    has_caption: bool = False
    has_all_caps_title: bool = False
    style_override_count: int = 0
    fonts_used: list[str] = Field(default_factory=list)
    explicit_gridline_or_border_count: int = 0
    custom_number_formats: list[str] = Field(default_factory=list)


class DashboardInfo(BaseModel):
    name: str
    sizing_mode: str = "fixed"
    width: int | None = None
    height: int | None = None
    worksheet_zone_count: int = 0
    worksheet_names: list[str] = Field(default_factory=list)
    filter_zone_count: int = 0
    only_relevant_values_zone_count: int = 0
    multi_value_list_zone_count: int = 0
    parameter_zone_count: int = 0
    max_container_nesting: int = 0
    device_layout_count: int = 0
    has_show_hide_button: bool = False
    action_count: int = 0
    action_exclude_on_clear_count: int = 0
    blank_spacer_count: int = 0
    floating_zone_count: int = 0


class AssetInfo(BaseModel):
    path: str
    size_bytes: int
    kind: str  # hyper, image, data, geocoding, other


class WorkbookStyleInfo(BaseModel):
    show_tabs: bool = False
    workbook_fonts: list[str] = Field(default_factory=list)
    custom_palettes: list[str] = Field(default_factory=list)
    style_rule_count: int = 0


class PerfEventSummary(BaseModel):
    event_name: str
    total_duration_sec: float = 0.0
    max_duration_sec: float = 0.0
    count: int = 0


class PerformanceRecordingInfo(BaseModel):
    detected: bool = False
    notes: str = ""
    events: list[PerfEventSummary] = Field(default_factory=list)


class Finding(BaseModel):
    id: str
    category: AuditCategory
    severity: Severity
    title: str
    target: str
    evidence: str
    impact_summary: str
    recommendation: str
    rewrite_before: str | None = None
    rewrite_after: str | None = None
    source_citation: str = ""


class CategoryScore(BaseModel):
    category: AuditCategory
    label: str
    score: int
    issue_count: int
    critical_count: int
    high_count: int


class WorkbookAnalysis(BaseModel):
    session_id: str
    filename: str
    file_type: str  # twb or twbx
    tableau_version: str = "unknown"
    file_size_bytes: int = 0
    overall_score: int = 100
    grade: str = "A"
    summary_stats: dict[str, Any] = Field(default_factory=dict)
    workbook_style: WorkbookStyleInfo = Field(default_factory=WorkbookStyleInfo)
    datasources: list[DatasourceInfo] = Field(default_factory=list)
    calculations: list[CalculationInfo] = Field(default_factory=list)
    filters: list[FilterInfo] = Field(default_factory=list)
    worksheets: list[WorksheetInfo] = Field(default_factory=list)
    dashboards: list[DashboardInfo] = Field(default_factory=list)
    assets: list[AssetInfo] = Field(default_factory=list)
    performance_recording: PerformanceRecordingInfo = Field(default_factory=PerformanceRecordingInfo)
    category_scores: list[CategoryScore] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


# --- Structured output schemas for Google Antigravity SDK Agent ---


class CalculationRewriteTip(BaseModel):
    field_name: str = Field(description="Exact calculated field name or caption from the workbook.")
    datasource: str = Field(description="Datasource owning the calculation.")
    current_formula: str = Field(description="Current Tableau formula.")
    optimized_formula: str = Field(description="Optimized drop-in replacement Tableau formula or alternative pattern.")
    why_it_is_faster: str = Field(description="Technical explanation citing Tableau/Hyper/VizQL execution mechanics.")
    expected_impact: str = Field(description="High, Medium, or Low impact.")


class DesignImprovementTip(BaseModel):
    target: str = Field(description="Workbook, dashboard, or worksheet name.")
    area: str = Field(description="Formatting, Typography, Color & Accessibility, Tooltips, or Layout Polish.")
    observation: str = Field(description="Specific formatting or UX issue found in the workbook.")
    recommendation: str = Field(description="Actionable step-by-step Tableau Desktop/Server improvement.")
    benefit: str = Field(description="Performance, visual clarity, or WCAG accessibility benefit.")


class ArchitectureTip(BaseModel):
    target: str = Field(description="Datasource, filter, dashboard, or publishing setting.")
    priority: str = Field(description="Critical, High, Medium, or Low.")
    issue: str = Field(description="Specific bottleneck observed.")
    action: str = Field(description="Concrete remediation steps grounded in Tableau best practices.")
    reference_principle: str = Field(description="Relevant Tableau principle, e.g., Order of Operations or Query Fusion.")


class AIEvaluationReport(BaseModel):
    executive_summary: str = Field(description="Concise executive assessment of the workbook's speed, calculation health, and visual polish.")
    estimated_performance_grade: str = Field(description="Letter grade A, B, C, D, or F based on critical bottlenecks.")
    top_bottlenecks: list[str] = Field(description="Top 3-5 performance or design bottlenecks ordered by user-perceived load impact.")
    calculation_rewrites: list[CalculationRewriteTip] = Field(description="Concrete Before -> After formula optimizations for flagged calculations.")
    formatting_and_ux_tips: list[DesignImprovementTip] = Field(description="Specific formatting hierarchy, font, color/WCAG contrast, tooltip, border/gridline, and KPI arrow tips.")
    data_and_filter_tips: list[ArchitectureTip] = Field(description="Data connection, Hyper extract, relationships vs SQL/blending, and filter pipeline improvements.")
    dashboard_and_server_tips: list[ArchitectureTip] = Field(description="Dashboard sizing, view consolidation, container nesting, asset weight, and publishing improvements.")
    quick_wins_checklist: list[str] = Field(description="High-ROI improvements achievable in under 10 minutes.")
    strategic_refactors: list[str] = Field(description="Deeper data model, extract, or dashboard-splitting refactors.")


class AgentTelemetry(BaseModel):
    conversation_id: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    used_multi_agent: bool = False
    model_note: str = "google-antigravity default model (gemini-3.8-flash)"
