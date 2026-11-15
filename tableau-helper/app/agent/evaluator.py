"""Google Antigravity SDK agent orchestrator, custom Tableau tools, policies, and hooks."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from google.antigravity import Agent, LocalAgentConfig, ToolContext, types
from google.antigravity.hooks import hooks, policy
from google.antigravity.types import (
    AgentBehavior,
    BuiltinTools,
    CapabilitiesConfig,
    SubagentCapabilities,
    SubagentConfig,
)

from app.analyzer.docs_search import search_tableau_docs
from app.analyzer.models import (
    AIEvaluationReport,
    AgentTelemetry,
    WorkbookAnalysis,
)
from app.config import SKILLS_DIR, settings

logger = logging.getLogger(__name__)

DISABLED_UNNEEDED_BUILTINS = [
    BuiltinTools.RUN_COMMAND,
    BuiltinTools.CREATE_FILE,
    BuiltinTools.EDIT_FILE,
    BuiltinTools.SEARCH_WEB,
    BuiltinTools.READ_URL_CONTENT,
    BuiltinTools.GENERATE_IMAGE,
    BuiltinTools.SCHEDULE,
]

EVALUATOR_SYSTEM_INSTRUCTIONS = """\
You are a Tableau Principal Performance Architect and Visual Analytics Designer powered by the Google Antigravity SDK.

Your job is to evaluate a Tableau workbook (.twb/.twbx) across query efficiency, calculations, data source architecture, filter order of operations, formatting hierarchy, visual clarity, dashboard layout, WCAG 2.2 AA accessibility, and Tableau Server/Cloud publishing.

Operating Rules:
1. Always inspect the actual workbook telemetry via your tools (`get_workbook_summary`, `inspect_heuristic_findings`, `inspect_calculations`, `inspect_datasources_and_filters`, `inspect_formatting_and_ux`) BEFORE writing conclusions.
2. Consult `search_tableau_best_practices` when verifying subtle Tableau rules (e.g., InterWorks 2021 calculation hierarchy vs official REGEXP/Sets guidance, Order of Operations, Custom Number Formatting arrows, WCAG mark limits).
3. Cite ONLY exact datasource names, calculation captions/formulas, worksheet names, and dashboard names returned by tools. Never invent or guess unverified names or metrics.
4. Provide actionable, drop-in Tableau formula rewrites (exact Before -> After formulas) wherever calculations can be optimized (e.g., CASE vs IF/ELSEIF, IN vs OR, ELSEIF vs ELSE IF, MIN/MAX vs ATTR, TODAY vs NOW, aggregating outside parameters, Custom Number Format '▲ +0.0%;▼ -0.0%;— 0.0%' instead of string arrow calcs, REGEXP_* or extract pre-materialization).
5. Give concrete Formatting, Visual Hierarchy & Accessibility improvements: Workbook-level vs worksheet-level formatting (`Format -> Workbook`), font standardization, clutter removal (gridlines/borders/shading), descriptive narrative tooltips (and disabling command buttons), contrast/colorblind-safe encodings, and keeping mark density <1,000 for client-side rendering.
"""


def build_workbook_tools(analysis: WorkbookAnalysis) -> list[Callable[..., Any]]:
    """Create typed custom Antigravity SDK tools bound to the parsed workbook telemetry."""

    def get_workbook_summary(ctx: ToolContext) -> dict[str, Any]:
        """Retrieve high-level workbook topology, stats, datasources, sheets, dashboards, .twbx assets, and performance recording summary."""
        inspected = ctx.get_state("inspected_tools", [])
        ctx.set_state("inspected_tools", [*inspected, "get_workbook_summary"])
        return {
            "filename": analysis.filename,
            "file_type": analysis.file_type,
            "tableau_version": analysis.tableau_version,
            "file_size_kb": round(analysis.file_size_bytes / 1024, 1),
            "static_overall_score": analysis.overall_score,
            "static_grade": analysis.grade,
            "summary_stats": analysis.summary_stats,
            "category_scores": [c.model_dump() for c in analysis.category_scores],
            "workbook_style": analysis.workbook_style.model_dump(),
            "assets": [
                {"path": a.path, "size_kb": round(a.size_bytes / 1024, 1), "kind": a.kind}
                for a in analysis.assets[:25]
            ],
            "performance_recording": analysis.performance_recording.model_dump(),
        }

    def inspect_calculations(
        ctx: ToolContext, only_flagged: bool = True, limit: int = 35
    ) -> list[dict[str, Any]]:
        """Inspect calculated fields, exact formulas, nesting depth, dependencies, sheet usage, and detected anti-pattern flags.

        Args:
            only_flagged: If True, return calculations with detected flags or unused status first.
            limit: Maximum number of calculations to return (sorted by complexity score descending).
        """
        calcs = analysis.calculations
        if only_flagged:
            filtered = [c for c in calcs if c.flags or c.is_unused]
            if not filtered:
                filtered = calcs
        else:
            filtered = calcs
        ordered = sorted(filtered, key=lambda c: c.complexity_score, reverse=True)[:limit]
        return [c.model_dump() for c in ordered]

    def inspect_datasources_and_filters(ctx: ToolContext) -> dict[str, Any]:
        """Inspect data connections (live vs Hyper extract, Custom SQL, physical joins vs logical relationships, hidden fields) and filter configurations."""
        return {
            "datasources": [d.model_dump() for d in analysis.datasources],
            "filters": [f.model_dump() for f in analysis.filters[:60]],
            "blending_worksheets": [w.name for w in analysis.worksheets if w.uses_blending],
        }

    def inspect_formatting_and_ux(ctx: ToolContext) -> dict[str, Any]:
        """Inspect workbook/worksheet formatting overrides, fonts, tooltips, gridlines/borders, accessibility cues, worksheet mark types, and dashboard container layouts."""
        return {
            "workbook_style": analysis.workbook_style.model_dump(),
            "worksheets": [w.model_dump() for w in analysis.worksheets[:40]],
            "dashboards": [d.model_dump() for d in analysis.dashboards[:20]],
        }

    def inspect_heuristic_findings(
        ctx: ToolContext, category: str = "all"
    ) -> list[dict[str, Any]]:
        """Return pre-computed heuristic audit findings grounded in InterWorks 2021 & Tableau docs.

        Args:
            category: Filter by 'all', 'calculations', 'formatting', 'data_source', 'filters', 'layout_dashboard', 'accessibility', or 'server_publishing'.
        """
        items = analysis.findings
        if category and category != "all":
            items = [f for f in items if f.category.value == category]
        return [f.model_dump() for f in items]

    def search_tableau_best_practices(query: str) -> list[dict[str, Any]]:
        """Search curated InterWorks Designing Efficient Workbooks (2021) and Tableau Desktop best-practice docs.

        Args:
            query: Keywords such as 'ATTR MIN', 'CASE IF', 'Only Relevant Values', 'custom number format', 'tooltip', 'fixed size', 'WCAG'.
        """
        return search_tableau_docs(query, max_results=4)

    return [
        get_workbook_summary,
        inspect_calculations,
        inspect_datasources_and_filters,
        inspect_formatting_and_ux,
        inspect_heuristic_findings,
        search_tableau_best_practices,
    ]


def _make_observability_hooks(
    trace_log: list[dict[str, Any]],
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> list[Any]:
    """Build Antigravity SDK hooks to record live tool calls and errors safely."""
    start_times: dict[str, float] = {}

    @hooks.pre_tool_call_decide
    async def _trace_pre_tool(data: types.ToolCall) -> hooks.HookResult:
        call_id = data.id or f"{data.name}-{len(trace_log)}"
        start_times[call_id] = time.perf_counter()
        event = {
            "type": "tool_start",
            "tool": data.name,
            "args": data.args or {},
            "timestamp": round(time.time(), 3),
        }
        trace_log.append(event)
        if on_event is not None:
            await on_event(event)
        return hooks.HookResult(allow=True)

    @hooks.post_tool_call
    async def _trace_post_tool(data: types.ToolResult) -> None:
        call_id = data.id or data.name
        started = start_times.pop(call_id, None)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1) if started else None
        event = {
            "type": "tool_end",
            "tool": data.name,
            "duration_ms": elapsed_ms,
            "has_error": bool(data.error or data.exception),
            "timestamp": round(time.time(), 3),
        }
        trace_log.append(event)
        if on_event is not None:
            await on_event(event)

    @hooks.on_tool_error
    async def _trace_tool_error(data: Any) -> None:
        event = {
            "type": "tool_error",
            "detail": str(data)[:400],
            "timestamp": round(time.time(), 3),
        }
        trace_log.append(event)
        if on_event is not None:
            await on_event(event)

    return [_trace_pre_tool, _trace_post_tool, _trace_tool_error]


DEFAULT_RETRY_CONFIG = types.RetryConfig(
    api_retry=types.ModelAPIRetryConfig(
        max_retries=5,
        initial_sleep_duration_ms=1500,
        exponential_multiplier=2.0,
        jitter_range=0.2,
    ),
)


def build_evaluator_config(
    analysis: WorkbookAnalysis,
    *,
    api_key: str | None = None,
    structured: bool = True,
    deep_multi_agent: bool = False,
    retry_config: types.RetryConfig | None = None,
    trace_log: list[dict[str, Any]] | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> LocalAgentConfig:
    """Construct a Google Antigravity LocalAgentConfig (default model left unset)."""
    tools = build_workbook_tools(analysis)
    tool_map = {fn.__name__: fn for fn in tools}
    hook_list = _make_observability_hooks(trace_log if trace_log is not None else [], on_event)

    subagents: list[SubagentConfig] = []
    allowed_subagents: list[str] = []
    if deep_multi_agent:
        allowed_subagents = ["calculation_optimizer", "formatting_ux_critic"]
        subagents = [
            SubagentConfig(
                name="calculation_optimizer",
                description="Audits Tableau calculated fields, LODs, table calcs, parameters, and recommends drop-in formula rewrites.",
                system_instructions=(
                    "Inspect the workbook's calculations using inspect_calculations and search_tableau_best_practices. "
                    "Return exact Before -> After formula optimizations citing real field names."
                ),
                tools=[
                    tool_map["inspect_calculations"],
                    tool_map["inspect_datasources_and_filters"],
                    tool_map["search_tableau_best_practices"],
                ],
                capabilities=SubagentCapabilities(
                    agent_behavior=AgentBehavior.AUTONOMOUS,
                    disabled_tools=DISABLED_UNNEEDED_BUILTINS,
                ),
            ),
            SubagentConfig(
                name="formatting_ux_critic",
                description="Reviews workbook formatting hierarchy, fonts, color accessibility (WCAG), tooltips, borders/gridlines, and dashboard container layouts.",
                system_instructions=(
                    "Inspect formatting and layout telemetry using inspect_formatting_and_ux and search_tableau_best_practices. "
                    "Provide specific formatting, clutter reduction, custom number formatting arrow, tooltip, and WCAG contrast improvements."
                ),
                tools=[
                    tool_map["inspect_formatting_and_ux"],
                    tool_map["search_tableau_best_practices"],
                ],
                capabilities=SubagentCapabilities(
                    agent_behavior=AgentBehavior.AUTONOMOUS,
                    disabled_tools=DISABLED_UNNEEDED_BUILTINS,
                ),
            ),
        ]

    cap_kwargs: dict[str, Any] = {
        "enable_subagents": deep_multi_agent,
        "agent_behavior": AgentBehavior.AUTONOMOUS,
        "disabled_tools": DISABLED_UNNEEDED_BUILTINS,
    }
    if deep_multi_agent:
        cap_kwargs["max_subagent_depth"] = 1
        cap_kwargs["allowed_subagents"] = allowed_subagents

    # Do NOT specify model explicitly so SDK defaults to gemini-3.8-flash per guidelines
    return LocalAgentConfig(
        api_key=api_key or settings.api_key,
        system_instructions=EVALUATOR_SYSTEM_INSTRUCTIONS,
        tools=tools,
        skills_paths=[str(SKILLS_DIR)],
        policies=policy.confirm_run_command(),
        hooks=hook_list,
        capabilities=CapabilitiesConfig(**cap_kwargs),
        subagents=subagents,
        retry_config=retry_config or DEFAULT_RETRY_CONFIG,
        response_schema=AIEvaluationReport if structured else None,
    )


def _extract_usage(agent: Agent) -> dict[str, int]:
    usage = getattr(agent.conversation, "total_usage", None)
    if not usage:
        return {}
    return {
        "prompt_token_count": int(getattr(usage, "prompt_token_count", 0) or 0),
        "cached_content_token_count": int(getattr(usage, "cached_content_token_count", 0) or 0),
        "candidates_token_count": int(getattr(usage, "candidates_token_count", 0) or 0),
        "thoughts_token_count": int(getattr(usage, "thoughts_token_count", 0) or 0),
        "total_token_count": int(getattr(usage, "total_token_count", 0) or 0),
    }


def _build_empty_output_error(agent: Any, expected: str) -> RuntimeError:
    """Build an actionable error when the agent finishes without returning output."""
    conversation = getattr(agent, "conversation", None)
    history = getattr(conversation, "history", None) or []
    for step in reversed(history):
        status = getattr(step, "status", None)
        error_msg = (getattr(step, "error", "") or "").strip()
        if status == types.StepStatus.ERROR or error_msg:
            http_code = getattr(step, "http_code", 0) or 0
            code_prefix = f"HTTP {http_code}: " if http_code else ""
            detail = error_msg or "Agent execution terminated with an error."
            return RuntimeError(
                f"Gemini model call failed after retries ({code_prefix}{detail}). Please retry shortly."
            )
    return RuntimeError(f"Agent finished without returning {expected}.")


async def evaluate_workbook_with_antigravity(
    analysis: WorkbookAnalysis,
    *,
    deep_multi_agent: bool = False,
    focus_notes: str = "",
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[AIEvaluationReport, AgentTelemetry]:
    """Run a structured end-to-end workbook audit using Google Antigravity SDK."""
    if not settings.has_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured. Obtain a key from https://aistudio.google.com/app/api-keys"
        )

    trace_log: list[dict[str, Any]] = []
    config = build_evaluator_config(
        analysis,
        structured=True,
        deep_multi_agent=deep_multi_agent,
        trace_log=trace_log,
        on_event=on_event,
    )

    prompt = (
        f"Evaluate the uploaded Tableau workbook `{analysis.filename}` (session `{analysis.session_id}`).\n"
        f"First inspect the workbook summary, heuristic findings, calculations, data sources/filters, and formatting/layout telemetry using your tools.\n"
        f"{'Coordinate with your specialist subagents (`calculation_optimizer`, `formatting_ux_critic`) for deep formula and visual UX analysis before synthesizing.' if deep_multi_agent else ''}\n"
        f"Then produce a complete structured audit report prioritizing highest-ROI performance, calculation rewrites, formatting, and UX improvements.\n"
    )
    if focus_notes.strip():
        prompt += f"\nUser focus request: {focus_notes.strip()}\n"

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
            if not fallback_text.strip():
                raise _build_empty_output_error(agent, "a structured evaluation report")
            report = AIEvaluationReport.model_validate_json(fallback_text)

        telemetry = AgentTelemetry(
            conversation_id=getattr(agent, "conversation_id", None),
            usage=_extract_usage(agent),
            tool_trace=trace_log,
            used_multi_agent=deep_multi_agent,
        )
        return report, telemetry


async def chat_with_antigravity_coach(
    analysis: WorkbookAnalysis,
    message: str,
    chat_history: list[dict[str, str]] | None = None,
) -> tuple[str, AgentTelemetry]:
    """Interactive follow-up Tableau performance & formula coach powered by Antigravity SDK."""
    if not settings.has_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured. Obtain a key from https://aistudio.google.com/app/api-keys"
        )

    trace_log: list[dict[str, Any]] = []
    config = build_evaluator_config(
        analysis,
        structured=False,
        deep_multi_agent=False,
        trace_log=trace_log,
    )

    history_block = ""
    if chat_history:
        recent = chat_history[-6:]
        formatted = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in recent)
        history_block = f"\nRecent conversation context:\n{formatted}\n"

    prompt = (
        f"Workbook context: `{analysis.filename}` (score {analysis.overall_score}/100, grade {analysis.grade}).\n"
        f"Inspect relevant workbook details via your tools if the user references specific fields, sheets, dashboards, or asks for optimization rewrites.\n"
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
            usage=_extract_usage(agent),
            tool_trace=trace_log,
            used_multi_agent=False,
        )
        return reply_text, telemetry
