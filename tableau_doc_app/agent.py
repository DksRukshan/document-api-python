"""Google Antigravity SDK engine for Tableau Documentation, Performance Audit, and Coach Chat."""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import dotenv_values, load_dotenv, set_key
import google.antigravity as ag
from google.antigravity import (
    Agent,
    LiteRTAgentConfig,
    LocalAgentConfig,
    LocalOpenAIAgentConfig,
    types,
)
from google.antigravity.hooks import hooks, policy

from tableau_doc_app.audit_models import (
    AIEvaluationReport,
    AgentTelemetry,
    WorkbookAuditReport,
)
from tableau_doc_app.docs_search import SKILLS_DIR, search_tableau_docs
from tableau_doc_app.models import (
    BackendConfig,
    DashboardDocumentationReport,
    DocumentationBundle,
    TokenUsageSummary,
    WorkbookSnapshot,
)

EventCallback = Callable[[dict[str, Any]], Any]

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_FILE, override=False)
GOOGLE_AI_STUDIO_URL = "https://aistudio.google.com/app/api-keys"

DISABLED_UNNEEDED_BUILTINS = [
    types.BuiltinTools.RUN_COMMAND,
    types.BuiltinTools.CREATE_FILE,
    types.BuiltinTools.EDIT_FILE,
    types.BuiltinTools.SEARCH_WEB,
    types.BuiltinTools.READ_URL_CONTENT,
    types.BuiltinTools.GENERATE_IMAGE,
    types.BuiltinTools.SCHEDULE,
]

DEFAULT_RETRY_CONFIG = types.RetryConfig(
    api_retry=types.ModelAPIRetryConfig(
        max_retries=5,
        initial_sleep_duration_ms=1500,
        exponential_multiplier=2.0,
        jitter_range=0.2,
    ),
)

_RUNTIME_SESSION_API_KEY: Optional[str] = None


class AntigravityConfigurationError(RuntimeError):
    """Raised when required Antigravity SDK backend credentials or paths are missing."""


def set_session_api_key(api_key: str, persist_to_env: bool = False) -> dict[str, Any]:
    """Store a runtime session Gemini API key in memory and optionally persist to gitignored .env."""
    global _RUNTIME_SESSION_API_KEY
    cleaned = api_key.strip()
    if len(cleaned) < 5:
        raise ValueError("API key must be at least 5 characters.")
    _RUNTIME_SESSION_API_KEY = cleaned
    os.environ["GEMINI_API_KEY"] = cleaned
    if persist_to_env:
        if not ENV_FILE.exists():
            ENV_FILE.write_text("", encoding="utf-8")
        set_key(str(ENV_FILE), "GEMINI_API_KEY", cleaned)
    return get_credential_status()


def clear_session_api_key(remove_from_env: bool = False) -> dict[str, Any]:
    """Clear the runtime session API key and optionally remove GEMINI_API_KEY from .env."""
    global _RUNTIME_SESSION_API_KEY
    _RUNTIME_SESSION_API_KEY = None
    os.environ.pop("GEMINI_API_KEY", None)
    if remove_from_env and ENV_FILE.exists():
        vals = dotenv_values(ENV_FILE)
        if "GEMINI_API_KEY" in vals:
            set_key(str(ENV_FILE), "GEMINI_API_KEY", "")
    return get_credential_status()


def _mask_secret(secret: Optional[str]) -> Optional[str]:
    if not secret:
        return None
    if len(secret) > 10:
        return f"{secret[:4]}...{secret[-4:]}"
    return "configured"


def get_credential_status(backend_config: Optional[BackendConfig] = None) -> dict[str, Any]:
    """Inspect whether Antigravity SDK credentials/backends are configured without exposing secrets."""
    backend = (backend_config.backend if backend_config else None) or os.environ.get(
        "ANTIGRAVITY_BACKEND", "gemini"
    )
    explicit_key = backend_config.api_key if backend_config and backend_config.api_key else None
    env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    session_key = explicit_key or _RUNTIME_SESSION_API_KEY

    if backend == "gemini":
        active_key = session_key or env_key
        configured = bool(active_key)
        source = "session" if session_key else ("env" if env_key else "missing")
    elif backend == "vertex":
        active_key = explicit_key
        configured = True
        source = "vertex"
    elif backend in ("ollama", "litert"):
        active_key = None
        configured = True
        source = backend
    else:
        active_key = None
        configured = False
        source = "missing"

    return {
        "backend": backend,
        "configured": configured,
        "has_api_key": configured,
        "masked_api_key": _mask_secret(active_key),
        "source": source,
        "aistudio_url": GOOGLE_AI_STUDIO_URL,
        "default_model": (
            (backend_config.model if backend_config else None)
            or os.environ.get("ANTIGRAVITY_MODEL")
            or "SDK Default"
        ),
    }


def _truncate_json(data: Any, max_chars: int = 24000) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated; query a specific item by name]"


def create_tableau_tools(
    snapshot: WorkbookSnapshot,
    *,
    audit: Optional[WorkbookAuditReport] = None,
    include_audit_tools: bool = False,
) -> list[Callable[..., Any]]:
    """Build read-only Tableau inspection and audit tools bound to parsed workbook state."""

    def get_workbook_overview() -> str:
        """Return high-level workbook inventory including dashboards, worksheets, data sources, health score, and action counts."""
        payload: dict[str, Any] = {
            "filename": snapshot.filename,
            "workbook_name": snapshot.workbook_name,
            "tableau_version": snapshot.tableau_version,
            "source_platform": snapshot.source_platform,
            "repository_path": snapshot.repository_path,
            "dashboards": [
                {
                    "name": d.name,
                    "size": f"{d.min_width or '?'}x{d.min_height or '?'}",
                    "worksheets": d.worksheets,
                    "exposed_filters_count": len(d.exposed_filters),
                    "exposed_parameters_count": len(d.exposed_parameters),
                    "has_thumbnail": d.has_thumbnail,
                }
                for d in snapshot.dashboards
            ],
            "worksheets": [
                {
                    "name": w.name,
                    "title": w.title,
                    "mark_types": w.mark_types,
                    "used_in_dashboards": w.used_in_dashboards,
                }
                for w in snapshot.worksheets
            ],
            "datasources": [
                {
                    "name": ds.name,
                    "caption": ds.caption,
                    "connections": [
                        f"{c.dbclass or 'unknown'}:{c.dbname or c.filename or c.server or ''}"
                        for c in ds.connections
                    ],
                    "field_count": len(ds.fields),
                    "calculated_field_count": len(ds.calculated_fields),
                    "has_extract": ds.has_extract,
                }
                for ds in snapshot.datasources
                if not ds.is_parameters
            ],
            "parameters_count": len(snapshot.parameters),
            "actions_count": len(snapshot.actions),
            "packaged_files": snapshot.packaged_files[:30],
        }
        if audit is not None:
            payload["audit_health"] = {
                "overall_score": audit.overall_score,
                "grade": audit.grade,
                "summary_stats": audit.summary_stats,
                "category_scores": [c.model_dump(mode="json") for c in audit.category_scores],
                "assets": [
                    {"path": a.path, "size_kb": round(a.size_bytes / 1024, 1), "kind": a.kind}
                    for a in audit.assets[:25]
                ],
                "performance_recording": audit.performance_recording.model_dump(mode="json"),
            }
        return _truncate_json(payload)

    def get_dashboard_detail(dashboard_name: str) -> str:
        """Inspect a specific dashboard's layout, embedded worksheets, exposed filters, parameter controls, text notes, and related actions.

        Args:
            dashboard_name: Exact or partial name of the dashboard to inspect.
        """
        target = next(
            (d for d in snapshot.dashboards if d.name.lower() == dashboard_name.lower()),
            None,
        ) or next(
            (d for d in snapshot.dashboards if dashboard_name.lower() in d.name.lower()),
            None,
        )
        if not target:
            return json.dumps(
                {
                    "error": f"Dashboard '{dashboard_name}' not found.",
                    "available": [d.name for d in snapshot.dashboards],
                }
            )

        related_actions = [
            a.model_dump()
            for a in snapshot.actions
            if a.source_dashboard == target.name or a.target_scope == target.name
        ]
        meaningful_zones = [
            z.model_dump(exclude_none=True)
            for z in target.zones
            if z.zone_type not in ("layout-basic", "layout-flow", "empty")
        ]
        payload = {
            "name": target.name,
            "dimensions": {
                "min_width": target.min_width,
                "min_height": target.min_height,
                "max_width": target.max_width,
                "max_height": target.max_height,
            },
            "worksheets": target.worksheets,
            "exposed_filters": target.exposed_filters,
            "exposed_parameters": target.exposed_parameters,
            "text_blocks": target.text_blocks,
            "device_layouts": target.device_layouts,
            "has_thumbnail": target.has_thumbnail,
            "related_actions": related_actions,
            "zones": meaningful_zones[:60],
        }
        return _truncate_json(payload)

    def get_worksheet_detail(worksheet_name: str) -> str:
        """Inspect a worksheet's title, mark types, rows/columns shelves, visual encodings, filters, sorts, tooltip, and referenced fields.

        Args:
            worksheet_name: Exact or partial name of the worksheet to inspect.
        """
        target = next(
            (w for w in snapshot.worksheets if w.name.lower() == worksheet_name.lower()),
            None,
        ) or next(
            (w for w in snapshot.worksheets if worksheet_name.lower() in w.name.lower()),
            None,
        )
        if not target:
            return json.dumps(
                {
                    "error": f"Worksheet '{worksheet_name}' not found.",
                    "available": [w.name for w in snapshot.worksheets],
                }
            )
        return _truncate_json(target.model_dump(exclude_none=True))

    def get_datasource_detail(datasource_name: str = "") -> str:
        """Inspect data source connections, physical tables, custom SQL, Tableau object-graph relationships, and field summary.

        Args:
            datasource_name: Optional exact or partial data source name/caption. Leave empty to list all data sources.
        """
        candidates = [ds for ds in snapshot.datasources if not ds.is_parameters]
        if datasource_name:
            matched = [
                ds
                for ds in candidates
                if datasource_name.lower() in ds.caption.lower()
                or datasource_name.lower() in ds.name.lower()
            ]
            if not matched:
                return json.dumps(
                    {
                        "error": f"Datasource '{datasource_name}' not found.",
                        "available": [ds.caption for ds in candidates],
                    }
                )
            candidates = matched

        summaries = []
        for ds in candidates:
            raw_fields = [
                {
                    "name": f.name,
                    "datatype": f.datatype,
                    "role": f.role,
                    "aggregation": f.default_aggregation,
                    "used_in_sheets": len(f.worksheets),
                }
                for f in ds.fields
                if not f.calculation
            ]
            summaries.append(
                {
                    "name": ds.name,
                    "caption": ds.caption,
                    "version": ds.version,
                    "has_extract": ds.has_extract,
                    "connections": [c.model_dump(exclude_none=True) for c in ds.connections],
                    "relations_and_custom_sql": [
                        r.model_dump(exclude_none=True) for r in ds.relations
                    ],
                    "logical_tables": ds.logical_tables,
                    "relationships": [r.model_dump() for r in ds.relationships],
                    "calculated_fields_count": len(ds.calculated_fields),
                    "raw_fields_sample": raw_fields[:80],
                    "total_raw_fields": len(raw_fields),
                }
            )
        return _truncate_json(summaries)

    def get_calculated_fields(datasource_name: str = "", only_flagged: bool = False) -> str:
        """Return calculated fields, formulas, resolved human-readable formulas, complexity flags, and worksheets where used.

        Args:
            datasource_name: Optional filter by data source caption/name.
            only_flagged: When true and audit telemetry exists, return only calculations with performance/unused flags.
        """
        audit_flag_map = (
            {(c.datasource_name.lower(), c.caption.lower()): c for c in audit.calculations}
            if audit is not None
            else {}
        )
        items = []
        for ds in snapshot.datasources:
            if ds.is_parameters:
                continue
            if (
                datasource_name
                and datasource_name.lower() not in ds.caption.lower()
                and datasource_name.lower() not in ds.name.lower()
            ):
                continue
            for cf in ds.calculated_fields:
                caption = cf.caption or cf.name
                audit_c = audit_flag_map.get((ds.caption.lower(), caption.lower()))
                if only_flagged and audit_c and not audit_c.flags and not audit_c.is_unused:
                    continue
                entry: dict[str, Any] = {
                    "datasource": ds.caption,
                    "name": cf.name,
                    "id": cf.id,
                    "datatype": cf.datatype,
                    "role": cf.role,
                    "formula": cf.readable_calculation or cf.calculation,
                    "raw_formula": cf.calculation,
                    "description": cf.description,
                    "worksheets": cf.worksheets,
                }
                if audit_c:
                    entry["audit_flags"] = audit_c.flags
                    entry["is_unused"] = audit_c.is_unused
                    entry["nesting_depth"] = audit_c.nesting_depth
                    entry["complexity_score"] = audit_c.complexity_score
                items.append(entry)
        return _truncate_json(items)

    def get_parameters_and_actions() -> str:
        """Return all workbook parameters (with ranges/allowable values) and all interactive dashboard/worksheet actions."""
        payload = {
            "parameters": [p.model_dump(exclude_none=True) for p in snapshot.parameters],
            "actions": [a.model_dump(exclude_none=True) for a in snapshot.actions],
        }
        return _truncate_json(payload)

    tools: list[Callable[..., Any]] = [
        get_workbook_overview,
        get_dashboard_detail,
        get_worksheet_detail,
        get_datasource_detail,
        get_calculated_fields,
        get_parameters_and_actions,
    ]

    if include_audit_tools or audit is not None:

        def inspect_heuristic_findings(category: str = "all") -> str:
            """Return static performance, calculation, formatting, and UX audit findings grounded in InterWorks 2021 & Tableau docs.

            Args:
                category: Filter by 'all', 'calculations', 'formatting', 'data_source', 'filters', 'layout_dashboard', 'accessibility', or 'server_publishing'.
            """
            if audit is None:
                return json.dumps({"overall_score": 100, "grade": "A", "findings": [], "note": "No static audit loaded."})
            items = audit.findings
            if category and category != "all":
                items = [f for f in items if f.category.value == category]
            dumped = [f.model_dump(mode="json") for f in items]
            kept: list[dict[str, Any]] = []
            running_len = 256
            for entry in dumped:
                entry_len = len(json.dumps(entry, ensure_ascii=False))
                if kept and running_len + entry_len > 22000:
                    break
                kept.append(entry)
                running_len += entry_len
            return json.dumps(
                {
                    "overall_score": audit.overall_score,
                    "grade": audit.grade,
                    "total_findings": len(dumped),
                    "returned_findings": len(kept),
                    "truncated": len(kept) < len(dumped),
                    "findings": kept,
                },
                indent=2,
                ensure_ascii=False,
            )

        def inspect_formatting_and_ux() -> str:
            """Inspect workbook/worksheet formatting overrides, fonts, tooltips, gridlines/borders, accessibility cues, and dashboard container layouts."""
            if audit is None:
                return json.dumps({"note": "No formatting audit telemetry loaded."})
            payload = {
                "workbook_style": audit.workbook_style.model_dump(mode="json"),
                "worksheets": [w.model_dump(mode="json") for w in audit.worksheets[:40]],
                "dashboards": [d.model_dump(mode="json") for d in audit.dashboards[:20]],
                "filters": [f.model_dump(mode="json") for f in audit.filters[:60]],
                "blending_worksheets": [w.name for w in audit.worksheets if w.uses_blending],
            }
            return _truncate_json(payload)

        def search_tableau_best_practices(query: str) -> str:
            """Search curated InterWorks Designing Efficient Workbooks (2021) and Tableau Desktop best-practice docs.

            Args:
                query: Keywords such as 'ATTR MIN', 'CASE IF', 'Only Relevant Values', 'custom number format', 'tooltip', 'fixed size', 'WCAG'.
            """
            return _truncate_json(search_tableau_docs(query, max_results=4))

        tools.extend(
            [
                inspect_heuristic_findings,
                inspect_formatting_and_ux,
                search_tableau_best_practices,
            ]
        )

    return tools


def _resolve_backend_config(backend_config: Optional[BackendConfig]) -> BackendConfig:
    cfg = backend_config.model_copy() if backend_config else BackendConfig()
    if not backend_config:
        env_backend = os.environ.get("ANTIGRAVITY_BACKEND", "").strip().lower()
        if env_backend in ("gemini", "vertex", "ollama", "litert"):
            cfg.backend = env_backend  # type: ignore[assignment]
    if not cfg.model:
        cfg.model = os.environ.get("ANTIGRAVITY_MODEL") or None
    if cfg.backend == "gemini" and not cfg.api_key:
        cfg.api_key = (
            _RUNTIME_SESSION_API_KEY
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
    if cfg.backend == "vertex":
        cfg.project = cfg.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        cfg.location = cfg.location or os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1"
    if cfg.backend == "ollama":
        cfg.base_url = (
            cfg.base_url
            or os.environ.get("LOCAL_OPENAI_BASE_URL")
            or "http://localhost:11434/v1"
        )
        cfg.model = cfg.model or os.environ.get("LOCAL_OPENAI_MODEL") or "gemma4:26b"
    if cfg.backend == "litert":
        cfg.litert_model_path = (
            cfg.litert_model_path
            or os.environ.get("LITERT_MODEL_PATH")
            or "~/.litert-lm/models/gemma4-26b/model.litertlm"
        )
    return cfg


def _make_observability_hooks(
    trace_log: list[dict[str, Any]],
    on_event: Optional[EventCallback] = None,
) -> list[Any]:
    start_times: dict[str, float] = {}

    @hooks.pre_tool_call_decide
    async def _trace_pre_tool(data: types.ToolCall) -> hooks.HookResult:
        call_id = data.id or f"{data.name}-{len(trace_log)}"
        start_times[call_id] = time.perf_counter()
        event = {
            "type": "tool_start",
            "tool": data.name.value if hasattr(data.name, "value") else str(data.name),
            "args": data.args or {},
            "timestamp": round(time.time(), 3),
        }
        trace_log.append(event)
        await _emit(on_event, event)
        return hooks.HookResult(allow=True)

    @hooks.post_tool_call
    async def _trace_post_tool(data: types.ToolResult) -> None:
        tname = data.name.value if hasattr(data.name, "value") else str(data.name)
        call_id = data.id or tname
        started = start_times.pop(call_id, None)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1) if started else None
        event = {
            "type": "tool_end",
            "tool": tname,
            "duration_ms": elapsed_ms,
            "has_error": bool(data.error or data.exception),
            "timestamp": round(time.time(), 3),
        }
        trace_log.append(event)
        await _emit(on_event, event)

    @hooks.on_tool_error
    async def _trace_tool_error(data: Any) -> None:
        event = {
            "type": "tool_error",
            "detail": str(data)[:400],
            "timestamp": round(time.time(), 3),
        }
        trace_log.append(event)
        await _emit(on_event, event)

    return [_trace_pre_tool, _trace_post_tool, _trace_tool_error]


def _instantiate_backend_config(
    resolved: BackendConfig, common_kwargs: dict[str, Any]
) -> LocalAgentConfig | LocalOpenAIAgentConfig | LiteRTAgentConfig:
    if resolved.backend == "gemini":
        if resolved.model:
            common_kwargs["model"] = resolved.model
        return LocalAgentConfig(api_key=resolved.api_key, **common_kwargs)
    if resolved.backend == "vertex":
        if resolved.model:
            common_kwargs["model"] = resolved.model
        if resolved.api_key:
            return LocalAgentConfig(vertex=True, api_key=resolved.api_key, **common_kwargs)
        return LocalAgentConfig(
            vertex=True,
            project=resolved.project,
            location=resolved.location,
            **common_kwargs,
        )
    if resolved.backend == "ollama":
        return LocalOpenAIAgentConfig(
            model=resolved.model or "gemma4:26b",
            base_url=resolved.base_url or "http://localhost:11434/v1",
            **common_kwargs,
        ).lightweight()
    if resolved.backend == "litert":
        model_path = os.path.expanduser(
            resolved.litert_model_path or "~/.litert-lm/models/gemma4-26b/model.litertlm"
        )
        return LiteRTAgentConfig(model_path=model_path, **common_kwargs)
    raise AntigravityConfigurationError(f"Unsupported backend: {resolved.backend}")


def _validate_backend_credentials(resolved: BackendConfig) -> None:
    if resolved.backend == "gemini" and not resolved.api_key:
        raise AntigravityConfigurationError(
            "GEMINI_API_KEY is not configured. Create a free API key at "
            f"{GOOGLE_AI_STUDIO_URL} and set GEMINI_API_KEY in .env or enter it in the Web UI / CLI."
        )


def build_agent_config(
    snapshot: WorkbookSnapshot,
    backend_config: Optional[BackendConfig] = None,
    *,
    audience: str = "Business & Technical Stakeholders",
    custom_instructions: str = "",
    app_data_dir: Optional[str] = None,
    audit: Optional[WorkbookAuditReport] = None,
    trace_log: Optional[list[dict[str, Any]]] = None,
    on_event: Optional[EventCallback] = None,
) -> tuple[LocalAgentConfig | LocalOpenAIAgentConfig | LiteRTAgentConfig, BackendConfig]:
    """Construct the Google Antigravity SDK AgentConfig for Tableau Documentation."""
    resolved = _resolve_backend_config(backend_config)
    _validate_backend_credentials(resolved)

    tools = create_tableau_tools(snapshot, audit=audit)
    data_dir = os.path.abspath(
        app_data_dir or os.path.join(os.getcwd(), "outputs", ".antigravity_data")
    )
    os.makedirs(data_dir, exist_ok=True)

    system_prompt = (
        "You are an expert Tableau Analytics Architect and Technical Documentation Lead.\n"
        "Your job is to inspect a Tableau workbook using the provided read-only inspection tools "
        "(and any attached dashboard thumbnail PNG images) and produce thorough, accurate, "
        "non-hallucinated documentation tailored for: "
        f"{audience}.\n\n"
        "Rules:\n"
        "1. Always start by calling `get_workbook_overview` and `get_parameters_and_actions`, then "
        "inspect relevant dashboards (`get_dashboard_detail`), data sources (`get_datasource_detail`), "
        "calculated fields (`get_calculated_fields`), and key worksheets (`get_worksheet_detail`).\n"
        + (
            "   You can also call `inspect_heuristic_findings`, `inspect_formatting_and_ux`, and `search_tableau_best_practices` to enrich recommendations.\n"
            if audit is not None
            else ""
        )
        + "2. Ground EVERY dashboard name, worksheet name, field name, formula, filter, and action "
        "in observed tool outputs. Never invent metrics, tables, or SQL that are not present.\n"
        "3. If dashboard thumbnail images are attached, describe the visual layout, chart forms, "
        "color cues, and KPI emphasis you directly observe in each thumbnail.\n"
        "4. Produce valid Mermaid `flowchart LR` syntax in `mermaid_lineage_diagram` using quoted "
        "labels and safe alphanumeric node IDs.\n"
        "5. When you have gathered complete evidence, call the `finish` tool with the JSON "
        "matching the required output schema."
    )
    if custom_instructions.strip():
        system_prompt += f"\n\nAdditional user guidance:\n{custom_instructions.strip()}"

    capabilities = types.CapabilitiesConfig(
        enabled_tools=[types.BuiltinTools.FINISH],
        enable_subagents=False,
        agent_behavior=types.AgentBehavior.AUTONOMOUS,
    )
    budget = types.BudgetConfig(max_model_calls=25, max_tool_calls=50)
    common_kwargs: dict[str, Any] = {
        "system_instructions": system_prompt,
        "tools": tools,
        "capabilities": capabilities,
        "policies": [policy.allow_all()],
        "app_data_dir": data_dir,
        "response_schema": DashboardDocumentationReport,
        "budget_config": budget,
        "retry_config": DEFAULT_RETRY_CONFIG,
    }
    if trace_log is not None or on_event is not None:
        common_kwargs["hooks"] = _make_observability_hooks(
            trace_log if trace_log is not None else [], on_event
        )
    if SKILLS_DIR.exists():
        common_kwargs["skills_paths"] = [str(SKILLS_DIR)]

    return _instantiate_backend_config(resolved, common_kwargs), resolved


def build_evaluator_config(
    snapshot: WorkbookSnapshot,
    audit: WorkbookAuditReport,
    backend_config: Optional[BackendConfig] = None,
    *,
    structured: bool = True,
    deep_multi_agent: bool = False,
    app_data_dir: Optional[str] = None,
    trace_log: Optional[list[dict[str, Any]]] = None,
    on_event: Optional[EventCallback] = None,
) -> tuple[LocalAgentConfig | LocalOpenAIAgentConfig | LiteRTAgentConfig, BackendConfig]:
    """Construct the Google Antigravity SDK AgentConfig for Performance, Formatting & UX Audit."""
    resolved = _resolve_backend_config(backend_config)
    _validate_backend_credentials(resolved)

    tools = create_tableau_tools(snapshot, audit=audit, include_audit_tools=True)
    tool_map = {fn.__name__: fn for fn in tools}
    data_dir = os.path.abspath(
        app_data_dir or os.path.join(os.getcwd(), "outputs", ".antigravity_data")
    )
    os.makedirs(data_dir, exist_ok=True)

    subagents: list[types.SubagentConfig] = []
    allowed_subagents: list[str] = []
    if deep_multi_agent and resolved.backend in ("gemini", "vertex"):
        allowed_subagents = ["calculation_optimizer", "formatting_ux_critic"]
        subagents = [
            types.SubagentConfig(
                name="calculation_optimizer",
                description="Audits Tableau calculated fields, LODs, table calcs, parameters, and recommends drop-in formula rewrites.",
                system_instructions=(
                    "Inspect the workbook's calculations using get_calculated_fields and search_tableau_best_practices. "
                    "Return exact Before -> After formula optimizations citing real field names."
                ),
                tools=[
                    tool_map["get_calculated_fields"],
                    tool_map["get_datasource_detail"],
                    tool_map["search_tableau_best_practices"],
                ],
                capabilities=types.SubagentCapabilities(
                    agent_behavior=types.AgentBehavior.AUTONOMOUS,
                    disabled_tools=DISABLED_UNNEEDED_BUILTINS,
                ),
            ),
            types.SubagentConfig(
                name="formatting_ux_critic",
                description="Reviews workbook formatting hierarchy, fonts, color accessibility (WCAG), tooltips, borders/gridlines, and dashboard container layouts.",
                system_instructions=(
                    "Inspect formatting and layout telemetry using inspect_formatting_and_ux, get_dashboard_detail, and search_tableau_best_practices. "
                    "Provide specific formatting, clutter reduction, custom number formatting arrow, tooltip, and WCAG contrast improvements."
                ),
                tools=[
                    tool_map["inspect_formatting_and_ux"],
                    tool_map["get_dashboard_detail"],
                    tool_map["search_tableau_best_practices"],
                ],
                capabilities=types.SubagentCapabilities(
                    agent_behavior=types.AgentBehavior.AUTONOMOUS,
                    disabled_tools=DISABLED_UNNEEDED_BUILTINS,
                ),
            ),
        ]

    system_instructions = (
        "You are a Tableau Principal Performance Architect and Visual Analytics Designer powered by the Google Antigravity SDK.\n"
        "Evaluate the uploaded Tableau workbook across query efficiency, calculations, data source architecture, filter order of operations, "
        "formatting hierarchy, visual clarity, dashboard layout, WCAG 2.2 AA accessibility, and Tableau Server/Cloud publishing.\n\n"
        "Operating Rules:\n"
        "1. Always inspect workbook telemetry via your tools (`get_workbook_overview`, `inspect_heuristic_findings`, "
        "`get_calculated_fields`, `get_datasource_detail`, `inspect_formatting_and_ux`) BEFORE writing conclusions.\n"
        "2. Consult `search_tableau_best_practices` to ground subtle Tableau rules (Order of Operations, ATTR vs MIN/MAX, REGEXP vs chained string ops, Custom Number Formatting arrows, WCAG limits).\n"
        "3. Cite ONLY exact datasource names, calculation captions/formulas, worksheet names, and dashboard names returned by tools.\n"
        "4. Provide actionable Before -> After Tableau formula rewrites and concrete Quick Wins vs Strategic Refactors."
    )

    cap_kwargs: dict[str, Any] = {
        "enable_subagents": bool(subagents),
        "agent_behavior": types.AgentBehavior.AUTONOMOUS,
        "disabled_tools": DISABLED_UNNEEDED_BUILTINS,
    }
    if subagents:
        cap_kwargs["max_subagent_depth"] = 1
        cap_kwargs["allowed_subagents"] = allowed_subagents

    common_kwargs: dict[str, Any] = {
        "system_instructions": system_instructions,
        "tools": tools,
        "skills_paths": [str(SKILLS_DIR)] if SKILLS_DIR.exists() else [],
        "policies": [policy.allow_all()],
        "hooks": _make_observability_hooks(trace_log if trace_log is not None else [], on_event),
        "capabilities": types.CapabilitiesConfig(**cap_kwargs),
        "subagents": subagents,
        "app_data_dir": data_dir,
        "retry_config": DEFAULT_RETRY_CONFIG,
        "response_schema": AIEvaluationReport if structured else None,
        "budget_config": types.BudgetConfig(max_model_calls=25, max_tool_calls=60),
    }

    return _instantiate_backend_config(resolved, common_kwargs), resolved


def _select_thumbnail_media(
    snapshot: WorkbookSnapshot,
    target_dashboard: Optional[str],
    max_images: int = 6,
) -> tuple[list[types.Image], list[str]]:
    """Convert workbook PNG thumbnails to Antigravity multimodal Image objects."""
    if not snapshot.thumbnails:
        return [], []

    selected = snapshot.thumbnails
    if target_dashboard:
        exact = [t for t in snapshot.thumbnails if t.name == target_dashboard]
        if exact:
            selected = exact
        else:
            return [], []
    else:
        db_names = {d.name for d in snapshot.dashboards}
        db_thumbs = [t for t in snapshot.thumbnails if t.name in db_names]
        selected = (db_thumbs or snapshot.thumbnails)[:max_images]

    media: list[types.Image] = []
    attached_names: list[str] = []
    for thumb in selected:
        try:
            raw_bytes = base64.b64decode(thumb.base64_png)
            img = ag.from_bytes(
                raw_bytes,
                "image/png",
                description=f"Tableau thumbnail preview for '{thumb.name}'",
            )
            if isinstance(img, types.Image):
                media.append(img)
                attached_names.append(thumb.name)
        except Exception:
            continue
    return media, attached_names


def _extract_json_fallback(raw_text: str) -> Optional[dict[str, Any]]:
    if not raw_text:
        return None
    stripped = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1)
    else:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end > start:
            stripped = stripped[start : end + 1]
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


async def _emit(callback: Optional[EventCallback], payload: dict[str, Any]) -> None:
    if callback is None:
        return
    res = callback(payload)
    if inspect.isawaitable(res):
        await res


def _build_empty_output_error(agent: Any, expected: str) -> RuntimeError:
    """Inspect Antigravity conversation history to surface root model/network errors cleanly."""
    conversation = getattr(agent, "conversation", None)
    history = getattr(conversation, "history", None) or []
    for step in reversed(history):
        status = getattr(step, "status", None)
        error_msg = (getattr(step, "error", "") or "").strip()
        if status == types.StepStatus.ERROR or error_msg:
            http_code = getattr(step, "http_code", 0) or 0
            code_prefix = f"HTTP {http_code}: " if http_code else ""
            detail = error_msg or "Agent execution terminated with an error."
            if "no such host" in detail.lower() or "generativelanguage.googleapis.com" in detail.lower():
                return RuntimeError(
                    f"Model provider connection failed ({code_prefix}{detail}). "
                    "Verify internet/DNS/VPN/proxy access to generativelanguage.googleapis.com, "
                    "or switch backend to local Ollama/LiteRT or deterministic (--no-ai) mode."
                )
            return RuntimeError(
                f"Gemini model call failed after retries ({code_prefix}{detail}). Please retry shortly."
            )
    return RuntimeError(f"Antigravity agent completed without returning {expected}.")


def _usage_summary(agent: Any, response: Any = None) -> TokenUsageSummary:
    conv = getattr(agent, "conversation", None)
    usage_obj = getattr(conv, "total_usage", None) or (
        getattr(response, "usage_metadata", None) if response is not None else None
    )
    if usage_obj is None:
        return TokenUsageSummary()
    return TokenUsageSummary(
        prompt_tokens=int(getattr(usage_obj, "prompt_token_count", 0) or 0),
        cached_tokens=int(getattr(usage_obj, "cached_content_token_count", 0) or 0),
        candidate_tokens=int(getattr(usage_obj, "candidates_token_count", 0) or 0),
        thoughts_tokens=int(getattr(usage_obj, "thoughts_token_count", 0) or 0),
        total_tokens=int(getattr(usage_obj, "total_token_count", 0) or 0),
    )


def _usage_dict(agent: Any) -> dict[str, int]:
    s = _usage_summary(agent)
    if not s.total_tokens and not s.prompt_tokens:
        return {}
    return {
        "prompt_token_count": s.prompt_tokens,
        "cached_content_token_count": s.cached_tokens,
        "candidates_token_count": s.candidate_tokens,
        "thoughts_token_count": s.thoughts_tokens,
        "total_token_count": s.total_tokens,
    }


async def generate_ai_documentation(
    snapshot: WorkbookSnapshot,
    backend_config: Optional[BackendConfig] = None,
    *,
    audit: Optional[WorkbookAuditReport] = None,
    target_dashboard: Optional[str] = None,
    audience: str = "Business & Technical Stakeholders",
    custom_instructions: str = "",
    include_thumbnails: bool = True,
    app_data_dir: Optional[str] = None,
    on_event: Optional[EventCallback] = None,
) -> DocumentationBundle:
    """Run the Google Antigravity SDK agent to document a Tableau workbook or dashboard."""
    config, resolved = build_agent_config(
        snapshot,
        backend_config,
        audience=audience,
        custom_instructions=custom_instructions,
        app_data_dir=app_data_dir,
        audit=audit,
    )

    warnings: list[str] = []
    media: list[types.Image] = []
    attached_thumbs: list[str] = []
    if include_thumbnails and resolved.backend in ("gemini", "vertex"):
        media, attached_thumbs = _select_thumbnail_media(snapshot, target_dashboard)

    scope_desc = (
        f"Focus specifically on dashboard '{target_dashboard}' (and all worksheets, "
        f"calculated fields, parameters, actions, and data sources that feed it)."
        if target_dashboard
        else "Document the entire Tableau workbook across all dashboards, worksheets, "
        "calculated fields, parameters, actions, and data sources."
    )

    prompt_text = (
        f"Generate comprehensive documentation for Tableau workbook `{snapshot.filename}`.\n"
        f"Scope: {scope_desc}\n"
        f"Target audience: {audience}.\n"
        + (
            f"Attached thumbnail images for: {', '.join(attached_thumbs)}.\n"
            if attached_thumbs
            else "No thumbnail images were attached.\n"
        )
        + "Use your inspection tools (`get_workbook_overview`, `get_dashboard_detail`, "
        "`get_worksheet_detail`, `get_datasource_detail`, `get_calculated_fields`, "
        "`get_parameters_and_actions`) to gather exact details, then finalize using the "
        "`finish` tool with structured documentation."
    )

    prompt: str | list[str | types.Image] = [prompt_text, *media] if media else prompt_text

    await _emit(
        on_event,
        {
            "type": "status",
            "message": f"Starting Antigravity SDK agent ({resolved.backend})...",
            "attached_thumbnails": attached_thumbs,
        },
    )

    raw_text_parts: list[str] = []
    async with Agent(config=config) as agent:
        response = await agent.chat(prompt)

        async for chunk in response.chunks:
            if isinstance(chunk, types.Thought):
                await _emit(on_event, {"type": "thought", "text": chunk.text})
            elif isinstance(chunk, types.ToolCall):
                tool_name = (
                    chunk.name.value if hasattr(chunk.name, "value") else str(chunk.name)
                )
                await _emit(
                    on_event,
                    {"type": "tool_call", "name": tool_name, "args": chunk.args},
                )
            elif isinstance(chunk, types.ToolResult):
                tool_name = (
                    chunk.name.value if hasattr(chunk.name, "value") else str(chunk.name)
                )
                preview = str(chunk.result)[:240] if chunk.result is not None else ""
                await _emit(
                    on_event,
                    {
                        "type": "tool_result",
                        "name": tool_name,
                        "error": chunk.error,
                        "preview": preview,
                    },
                )
            elif isinstance(chunk, types.Text):
                raw_text_parts.append(chunk.text)
                await _emit(on_event, {"type": "text", "text": chunk.text})

        raw_structured = await response.structured_output()
        if raw_structured is None:
            fallback_dict = _extract_json_fallback("".join(raw_text_parts))
            if fallback_dict is not None:
                raw_structured = fallback_dict

        if raw_structured is None:
            raise _build_empty_output_error(agent, "structured documentation output")

        report = DashboardDocumentationReport.model_validate(raw_structured)

        if response.stop_reason != types.StopReason.UNSPECIFIED:
            warnings.append(f"Agent stop reason: {response.stop_reason}")

        usage = _usage_summary(agent, response)
        usage_out = usage if (usage.total_tokens or usage.prompt_tokens) else None

    bundle = DocumentationBundle(
        snapshot=snapshot,
        audit=audit,
        ai_report=report,
        target_dashboard=target_dashboard,
        audience=audience,
        generated_at_iso=datetime.now(timezone.utc).isoformat(),
        used_ai=True,
        usage=usage_out,
        agent_model=resolved.model or f"{resolved.backend}-default",
        warnings=warnings,
    )
    await _emit(
        on_event,
        {
            "type": "complete",
            "usage": usage_out.model_dump() if usage_out else None,
            "warnings": warnings,
        },
    )
    return bundle


async def evaluate_performance_with_antigravity(
    snapshot: WorkbookSnapshot,
    audit: WorkbookAuditReport,
    backend_config: Optional[BackendConfig] = None,
    *,
    deep_multi_agent: bool = False,
    focus_notes: str = "",
    app_data_dir: Optional[str] = None,
    on_event: Optional[EventCallback] = None,
) -> tuple[AIEvaluationReport, AgentTelemetry]:
    """Run a structured Performance, Calculation, Formatting & UX audit via Google Antigravity SDK."""
    trace_log: list[dict[str, Any]] = []
    config, resolved = build_evaluator_config(
        snapshot,
        audit,
        backend_config,
        structured=True,
        deep_multi_agent=deep_multi_agent,
        app_data_dir=app_data_dir,
        trace_log=trace_log,
        on_event=on_event,
    )

    prompt = (
        f"Evaluate Tableau workbook `{snapshot.filename}` (health score {audit.overall_score}/100, grade {audit.grade}).\n"
        "First inspect `get_workbook_overview`, `inspect_heuristic_findings`, `get_calculated_fields`, "
        "`get_datasource_detail`, and `inspect_formatting_and_ux` using your tools.\n"
        + (
            "Coordinate with your specialist subagents (`calculation_optimizer`, `formatting_ux_critic`) "
            "for deep formula and visual UX analysis before synthesizing.\n"
            if deep_multi_agent and resolved.backend in ("gemini", "vertex")
            else ""
        )
        + "Then produce a complete structured audit report prioritizing highest-ROI performance, "
        "Before -> After calculation rewrites, formatting/WCAG UX improvements, and quick wins."
    )
    if focus_notes.strip():
        prompt += f"\n\nUser focus request: {focus_notes.strip()}"

    async with Agent(config=config) as agent:
        response = await agent.chat(prompt)
        raw_output = await response.structured_output()
        if isinstance(raw_output, AIEvaluationReport):
            report = raw_output
        elif isinstance(raw_output, dict):
            report = AIEvaluationReport.model_validate(raw_output)
        elif isinstance(raw_output, str):
            report = AIEvaluationReport.model_validate_json(raw_output)
        else:
            fallback_text = await response.text()
            fallback_dict = _extract_json_fallback(fallback_text)
            if fallback_dict is None:
                raise _build_empty_output_error(agent, "a structured evaluation report")
            report = AIEvaluationReport.model_validate(fallback_dict)

        telemetry = AgentTelemetry(
            conversation_id=getattr(agent, "conversation_id", None),
            usage=_usage_dict(agent),
            tool_trace=trace_log,
            used_multi_agent=deep_multi_agent and resolved.backend in ("gemini", "vertex"),
            model_note=resolved.model or f"{resolved.backend}-default",
        )
        return report, telemetry


async def chat_with_antigravity_coach(
    snapshot: WorkbookSnapshot,
    audit: WorkbookAuditReport,
    message: str,
    backend_config: Optional[BackendConfig] = None,
    *,
    chat_history: Optional[list[dict[str, str]]] = None,
    app_data_dir: Optional[str] = None,
) -> tuple[str, AgentTelemetry]:
    """Interactive follow-up Tableau performance, calculation & design coach powered by Antigravity SDK."""
    trace_log: list[dict[str, Any]] = []
    config, resolved = build_evaluator_config(
        snapshot,
        audit,
        backend_config,
        structured=False,
        deep_multi_agent=False,
        app_data_dir=app_data_dir,
        trace_log=trace_log,
    )

    history_block = ""
    if chat_history:
        recent = chat_history[-6:]
        formatted = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in recent)
        history_block = f"\nRecent conversation context:\n{formatted}\n"

    prompt = (
        f"Workbook context: `{snapshot.filename}` (health score {audit.overall_score}/100, grade {audit.grade}, "
        f"{len(snapshot.dashboards)} dashboards, {len(snapshot.worksheets)} worksheets).\n"
        "Inspect relevant workbook details via your tools if the user references specific fields, sheets, dashboards, "
        "or asks for documentation/optimization rewrites.\n"
        f"{history_block}\n"
        f"USER QUESTION:\n{message.strip()}"
    )

    async with Agent(config=config) as agent:
        response = await agent.chat(prompt)
        reply_text = await response.text()
        if not reply_text.strip():
            raise _build_empty_output_error(agent, "a reply")
        telemetry = AgentTelemetry(
            conversation_id=getattr(agent, "conversation_id", None),
            usage=_usage_dict(agent),
            tool_trace=trace_log,
            used_multi_agent=False,
            model_note=resolved.model or f"{resolved.backend}-default",
        )
        return reply_text, telemetry
