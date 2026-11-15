"""Data models for Tableau metadata extraction and Antigravity AI documentation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from tableau_doc_app.audit_models import AIEvaluationReport, WorkbookAuditReport


class ConnectionInfo(BaseModel):
    """Database or file connection inside a Tableau data source."""

    dbclass: Optional[str] = None
    server: Optional[str] = None
    port: Optional[str] = None
    dbname: Optional[str] = None
    schema_name: Optional[str] = None
    service: Optional[str] = None
    username: Optional[str] = None
    authentication: Optional[str] = None
    filename: Optional[str] = None
    initial_sql: Optional[str] = None


class TableRelationInfo(BaseModel):
    """Physical table, join, or Custom SQL relation in a data source."""

    name: Optional[str] = None
    relation_type: Optional[str] = None
    table: Optional[str] = None
    connection: Optional[str] = None
    custom_sql: Optional[str] = None


class RelationshipLinkInfo(BaseModel):
    """Logical object-graph relationship between Tableau logical tables."""

    left_object: str
    right_object: str
    expression: str


class FieldInfo(BaseModel):
    """Field or calculated field in a Tableau data source."""

    id: str
    name: str
    caption: Optional[str] = None
    alias: Optional[str] = None
    datatype: Optional[str] = None
    role: Optional[str] = None
    field_type: Optional[str] = None
    default_aggregation: Optional[str] = None
    calculation: Optional[str] = None
    readable_calculation: Optional[str] = None
    description: Optional[str] = None
    hidden: Optional[bool] = None
    worksheets: list[str] = Field(default_factory=list)


class DatasourceInfo(BaseModel):
    """Extracted metadata for a Tableau data source."""

    name: str
    caption: str
    version: Optional[str] = None
    is_parameters: bool = False
    connections: list[ConnectionInfo] = Field(default_factory=list)
    relations: list[TableRelationInfo] = Field(default_factory=list)
    logical_tables: list[str] = Field(default_factory=list)
    relationships: list[RelationshipLinkInfo] = Field(default_factory=list)
    fields: list[FieldInfo] = Field(default_factory=list)
    calculated_fields: list[FieldInfo] = Field(default_factory=list)
    has_extract: bool = False


class ParameterInfo(BaseModel):
    """Tableau workbook parameter definition."""

    id: str
    name: str
    datatype: Optional[str] = None
    domain_type: Optional[str] = None
    current_value: Optional[str] = None
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    step_size: Optional[str] = None
    allowable_values: list[str] = Field(default_factory=list)


class EncodingInfo(BaseModel):
    """Visual encoding channel on a worksheet pane."""

    channel: str
    column_raw: str
    column_readable: str


class FilterInfo(BaseModel):
    """Worksheet filter specification."""

    filter_class: Optional[str] = None
    column_raw: str
    column_readable: str
    summary: str = ""


class WorksheetInfo(BaseModel):
    """Extracted metadata for a Tableau worksheet."""

    name: str
    title: Optional[str] = None
    datasources: list[str] = Field(default_factory=list)
    mark_types: list[str] = Field(default_factory=list)
    rows_raw: Optional[str] = None
    cols_raw: Optional[str] = None
    rows_readable: Optional[str] = None
    cols_readable: Optional[str] = None
    encodings: list[EncodingInfo] = Field(default_factory=list)
    filters: list[FilterInfo] = Field(default_factory=list)
    sorts: list[str] = Field(default_factory=list)
    tooltip_text: Optional[str] = None
    referenced_fields: list[str] = Field(default_factory=list)
    referenced_calculations: list[str] = Field(default_factory=list)
    used_in_dashboards: list[str] = Field(default_factory=list)


class ZoneInfo(BaseModel):
    """Normalized dashboard layout zone."""

    zone_id: Optional[str] = None
    zone_type: str
    name: Optional[str] = None
    param: Optional[str] = None
    param_readable: Optional[str] = None
    mode: Optional[str] = None
    text_content: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None
    w: Optional[int] = None
    h: Optional[int] = None


class DashboardInfo(BaseModel):
    """Extracted metadata for a Tableau dashboard."""

    name: str
    min_width: Optional[int] = None
    min_height: Optional[int] = None
    max_width: Optional[int] = None
    max_height: Optional[int] = None
    worksheets: list[str] = Field(default_factory=list)
    exposed_filters: list[str] = Field(default_factory=list)
    exposed_parameters: list[str] = Field(default_factory=list)
    text_blocks: list[str] = Field(default_factory=list)
    device_layouts: list[str] = Field(default_factory=list)
    zones: list[ZoneInfo] = Field(default_factory=list)
    has_thumbnail: bool = False


class ActionInfo(BaseModel):
    """Dashboard or workbook interactivity action."""

    name: str
    caption: str
    action_type: str
    trigger: Optional[str] = None
    source_dashboard: Optional[str] = None
    source_worksheet: Optional[str] = None
    target_scope: Optional[str] = None
    excluded_sheets: list[str] = Field(default_factory=list)
    field_captions: list[str] = Field(default_factory=list)
    url: Optional[str] = None
    target_group_or_param: Optional[str] = None


class ThumbnailInfo(BaseModel):
    """Decoded base64 PNG thumbnail stored inside a workbook."""

    name: str
    width: Optional[int] = None
    height: Optional[int] = None
    base64_png: str


class WorkbookSnapshot(BaseModel):
    """Complete deterministic structural snapshot of a Tableau workbook."""

    filename: str
    workbook_name: str
    tableau_version: Optional[str] = None
    source_platform: Optional[str] = None
    source_build: Optional[str] = None
    repository_path: Optional[str] = None
    datasources: list[DatasourceInfo] = Field(default_factory=list)
    parameters: list[ParameterInfo] = Field(default_factory=list)
    worksheets: list[WorksheetInfo] = Field(default_factory=list)
    dashboards: list[DashboardInfo] = Field(default_factory=list)
    actions: list[ActionInfo] = Field(default_factory=list)
    thumbnails: list[ThumbnailInfo] = Field(default_factory=list)
    shapes: list[str] = Field(default_factory=list)
    packaged_files: list[str] = Field(default_factory=list)
    deterministic_mermaid: str = ""


# ---------------------------------------------------------------------------
# AI Documentation Structured Output Schema (enforced via Antigravity SDK)
# ---------------------------------------------------------------------------


class KpiDefinition(BaseModel):
    """Plain-English business definition for a key metric or KPI."""

    name: str = Field(description="KPI or metric display name.")
    business_definition: str = Field(
        description="Clear business meaning of the KPI and how stakeholders interpret it."
    )
    formula_or_aggregation: str = Field(
        description="Underlying Tableau formula or aggregation logic."
    )
    datasource_and_worksheets: str = Field(
        description="Data source and worksheets where this KPI appears."
    )


class DashboardDoc(BaseModel):
    """AI-authored documentation for a single Tableau dashboard."""

    name: str = Field(description="Exact dashboard name from the workbook.")
    purpose: str = Field(
        description="What business questions this dashboard answers and who uses it."
    )
    visual_layout_summary: str = Field(
        description="Description of visual layout, chart hierarchy, and visual observations from the thumbnail if provided."
    )
    controls_and_filters: str = Field(
        description="How exposed filters, parameters, and what-if controls affect the dashboard."
    )
    interactivity_narrative: str = Field(
        description="Step-by-step walkthrough of highlight, filter, set, URL, or parameter actions."
    )
    key_caveats: list[str] = Field(
        default_factory=list,
        description="Important edge cases, assumptions, or caveats users should keep in mind.",
    )


class WorksheetDoc(BaseModel):
    """AI-authored documentation for a single Tableau worksheet."""

    name: str = Field(description="Exact worksheet name.")
    title: Optional[str] = Field(default=None, description="Displayed worksheet title.")
    chart_type: str = Field(
        description="Inferred visualization type (e.g., Bar Chart, Choropleth Map, Scatter Plot, KPI Table, Area Trend)."
    )
    purpose: str = Field(description="Analytical role of this sheet.")
    axes_encodings_and_filters: str = Field(
        description="Summary of rows/columns, color/size/label encodings, and active filters."
    )


class CalculatedFieldDoc(BaseModel):
    """AI-authored explanation for a Tableau calculated field."""

    name: str = Field(description="Human-readable field caption.")
    datasource: str = Field(description="Parent data source caption.")
    formula: str = Field(description="Tableau calculation formula.")
    plain_english_explanation: str = Field(
        description="Non-technical explanation of what the calculation computes and why."
    )
    dependencies_and_usage: str = Field(
        description="Referenced fields/parameters and worksheets where this calculation is used."
    )


class DataArchitectureDoc(BaseModel):
    """AI-authored overview of data connections, joins, relationships, and custom SQL."""

    overview: str = Field(
        description="How data enters the workbook across all data sources."
    )
    sources_and_lineage: list[str] = Field(
        default_factory=list,
        description="Per-datasource summary covering connection type, tables, joins/relationships, and custom SQL.",
    )
    refresh_and_security_notes: str = Field(
        description="Live vs extract considerations, authentication modes, and data freshness notes."
    )


class AuditRecommendation(BaseModel):
    """Governance, maintainability, or performance recommendation."""

    severity: Literal["info", "warning", "high"] = Field(
        description="Priority level: info, warning, or high."
    )
    category: str = Field(
        description="Category such as Documentation, Performance, Governance, or UX."
    )
    finding: str = Field(description="Observed issue or opportunity in the workbook.")
    recommendation: str = Field(description="Concrete actionable improvement.")


class DashboardDocumentationReport(BaseModel):
    """Structured AI documentation output generated by the Antigravity SDK agent."""

    workbook_title: str = Field(description="Clear documentation title for the workbook.")
    executive_summary: str = Field(
        description="High-level executive overview covering business domain, primary goals, and scope."
    )
    target_audience: list[str] = Field(
        default_factory=list,
        description="Intended stakeholder personas (e.g., Sales Leadership, Finance Analysts).",
    )
    kpi_glossary: list[KpiDefinition] = Field(default_factory=list)
    dashboards: list[DashboardDoc] = Field(default_factory=list)
    worksheets: list[WorksheetDoc] = Field(default_factory=list)
    calculated_fields_explained: list[CalculatedFieldDoc] = Field(default_factory=list)
    data_architecture: DataArchitectureDoc
    mermaid_lineage_diagram: str = Field(
        description="Valid Mermaid flowchart LR code showing Data Sources -> Calculated Fields -> Worksheets -> Dashboards."
    )
    recommendations: list[AuditRecommendation] = Field(default_factory=list)


class BackendConfig(BaseModel):
    """Runtime AI backend configuration for Google Antigravity SDK."""

    backend: Literal["gemini", "vertex", "ollama", "litert"] = "gemini"
    api_key: Optional[str] = None
    model: Optional[str] = None
    project: Optional[str] = None
    location: Optional[str] = None
    base_url: Optional[str] = None
    litert_model_path: Optional[str] = None


class TokenUsageSummary(BaseModel):
    """Token telemetry reported by Antigravity SDK."""

    prompt_tokens: int = 0
    cached_tokens: int = 0
    candidate_tokens: int = 0
    thoughts_tokens: int = 0
    total_tokens: int = 0


class DocumentationBundle(BaseModel):
    """Combined deterministic snapshot, performance audit, and optional AI reports."""

    snapshot: WorkbookSnapshot
    audit: Optional[WorkbookAuditReport] = None
    ai_report: Optional[DashboardDocumentationReport] = None
    ai_evaluation: Optional[AIEvaluationReport] = None
    target_dashboard: Optional[str] = None
    audience: str = "Business & Technical Stakeholders"
    generated_at_iso: str
    used_ai: bool = False
    usage: Optional[TokenUsageSummary] = None
    agent_model: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


from tableau_doc_app.audit_models import AIEvaluationReport, WorkbookAuditReport  # noqa: E402

DocumentationBundle.model_rebuild()
