"""Tests for Google Antigravity SDK config/tools and FastAPI endpoints."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.agent.evaluator import (
    DEFAULT_RETRY_CONFIG,
    build_evaluator_config,
    build_workbook_tools,
    chat_with_antigravity_coach,
    evaluate_workbook_with_antigravity,
)
from app.analyzer.heuristics import run_heuristic_audit
from app.analyzer.models import (
    AIEvaluationReport,
    AgentTelemetry,
    CalculationRewriteTip,
    DesignImprovementTip,
)
from app.analyzer.twb_parser import parse_tableau_workbook
from app.config import GOOGLE_AI_STUDIO_KEY_URL, SAMPLES_DIR, settings
from app.main import app


class DummyToolContext:
    """Minimal stand-in implementing ToolContext state methods for direct unit testing."""

    def __init__(self) -> None:
        self.conversation_id = "test-conv"
        self._state: dict[str, Any] = {}

    def get_state(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        self._state[key] = value


def test_antigravity_sdk_config_and_custom_tools() -> None:
    raw = (SAMPLES_DIR / "retail_exec_unoptimized.twb").read_bytes()
    analysis = run_heuristic_audit(parse_tableau_workbook(raw, "retail_exec_unoptimized.twb"))

    cfg_single = build_evaluator_config(analysis, api_key="test-key", deep_multi_agent=False)
    assert cfg_single.model is None  # Rely on SDK default (gemini-3.8-flash)
    assert isinstance(cfg_single.response_schema, str)
    assert "AIEvaluationReport" in cfg_single.response_schema
    assert len(cfg_single.skills_paths) == 1
    assert cfg_single.subagents == []
    assert cfg_single.retry_config == DEFAULT_RETRY_CONFIG
    assert cfg_single.retry_config.api_retry.max_retries == 5

    cfg_multi = build_evaluator_config(analysis, api_key="test-key", deep_multi_agent=True)
    assert len(cfg_multi.subagents) == 2
    assert {s.name for s in cfg_multi.subagents} == {"calculation_optimizer", "formatting_ux_critic"}

    tools = {fn.__name__: fn for fn in build_workbook_tools(analysis)}
    ctx = DummyToolContext()
    summary = tools["get_workbook_summary"](ctx)
    assert summary["filename"] == "retail_exec_unoptimized.twb"
    assert ctx.get_state("inspected_tools") == ["get_workbook_summary"]

    calcs = tools["inspect_calculations"](ctx, only_flagged=True, limit=10)
    assert len(calcs) == 10
    assert any(c["caption"] == "Unique Orders Status" for c in calcs)

    ds_filters = tools["inspect_datasources_and_filters"](ctx)
    assert len(ds_filters["datasources"]) == 2
    assert len(ds_filters["filters"]) >= 3

    ux = tools["inspect_formatting_and_ux"](ctx)
    assert len(ux["worksheets"]) == 6
    assert len(ux["dashboards"]) == 1

    findings = tools["inspect_heuristic_findings"](ctx, category="calculations")
    assert all(f["category"] == "calculations" for f in findings)

    docs = tools["search_tableau_best_practices"]("Only Relevant Values context filter")
    assert len(docs) >= 1


def test_fastapi_endpoints_and_mocked_agent_flow() -> None:
    client = TestClient(app)

    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert len(client.get("/api/samples").json()) == 2

    # Ensure missing key error points user to AI Studio
    settings.clear_api_key(remove_from_env=False)
    cfg = client.get("/api/config").json()
    assert cfg["has_api_key"] is False
    assert cfg["api_key_setup_url"] == GOOGLE_AI_STUDIO_KEY_URL

    sample_res = client.post("/api/analyze/sample/unoptimized")
    assert sample_res.status_code == 200
    analysis_json = sample_res.json()
    session_id = analysis_json["session_id"]

    # Missing key blocks live SDK run with helpful URL
    eval_blocked = client.post(f"/api/evaluate/{session_id}", json={"deep_multi_agent": False})
    assert eval_blocked.status_code == 400
    assert GOOGLE_AI_STUDIO_KEY_URL in eval_blocked.json()["detail"]

    # Invalid file extension rejected cleanly
    bad_upload = client.post(
        "/api/analyze/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert bad_upload.status_code == 400

    # Set API key (not persisted to .env) and verify masked status
    key_res = client.post(
        "/api/config/api-key",
        json={"api_key": "AIzaSyTestFakeKey123456", "persist_to_env": False},
    )
    assert key_res.status_code == 200
    assert key_res.json()["has_api_key"] is True
    assert "AIzaSyTestFakeKey" not in str(key_res.json())

    mock_report = AIEvaluationReport(
        executive_summary="Workbook suffers from live Excel + Custom SQL and chained calculation overhead.",
        estimated_performance_grade="D",
        top_bottlenecks=["Live Excel + Custom SQL", "Only Relevant Values cascading filters"],
        calculation_rewrites=[
            CalculationRewriteTip(
                field_name="Unique Orders Status",
                datasource="Orders Live (Excel + Custom SQL)",
                current_formula='IF ATTR([Category]) = "Technology" THEN COUNTD([Order ID]) ELSE 0 END',
                optimized_formula='IF MIN([Category]) = "Technology" THEN COUNTD([Order ID]) ELSE 0 END',
                why_it_is_faster="MIN() executes half the aggregations of ATTR().",
                expected_impact="High",
            )
        ],
        formatting_and_ux_tips=[
            DesignImprovementTip(
                target="OVERVIEW KPI TABLE",
                area="Formatting",
                observation="Comic Sans MS override and ALL-CAPS title.",
                recommendation="Move font styling to Workbook level using Tableau Book and Title Case.",
                benefit="Consistent Server/Cloud rendering and improved scannability.",
            )
        ],
        data_and_filter_tips=[],
        dashboard_and_server_tips=[],
        quick_wins_checklist=["Convert Excel connection to Hyper Extract"],
        strategic_refactors=["Replace Custom SQL with Logical Relationships"],
    )
    mock_telemetry = AgentTelemetry(
        conversation_id="mock-conv-1",
        usage={"total_token_count": 420},
        tool_trace=[{"type": "tool_end", "tool": "get_workbook_summary"}],
        used_multi_agent=False,
    )

    try:
        with (
            patch(
                "app.main.evaluate_workbook_with_antigravity",
                new=AsyncMock(return_value=(mock_report, mock_telemetry)),
            ),
            patch(
                "app.main.chat_with_antigravity_coach",
                new=AsyncMock(return_value=("Replace ELSE IF with ELSEIF first.", mock_telemetry)),
            ),
        ):
            eval_res = client.post(
                f"/api/evaluate/{session_id}",
                json={"deep_multi_agent": False, "focus_notes": "Focus on speed"},
            )
            assert eval_res.status_code == 200
            assert eval_res.json()["report"]["estimated_performance_grade"] == "D"

            stream_res = client.post(
                f"/api/evaluate/{session_id}/stream",
                json={"deep_multi_agent": False},
            )
            assert stream_res.status_code == 200
            assert '"type": "complete"' in stream_res.text

            chat_res = client.post(
                f"/api/chat/{session_id}",
                json={"message": "How do I fix Region Bucket?"},
            )
            assert chat_res.status_code == 200
            assert "ELSEIF" in chat_res.json()["reply"]
    finally:
        settings.clear_api_key(remove_from_env=False)


def test_evaluate_workbook_with_mocked_antigravity_agent_runtime() -> None:
    import asyncio
    from types import SimpleNamespace

    raw = (SAMPLES_DIR / "retail_exec_optimized.twb").read_bytes()
    analysis = run_heuristic_audit(parse_tableau_workbook(raw, "retail_exec_optimized.twb"))

    payload = {
        "executive_summary": "Clean Hyper-backed workbook.",
        "estimated_performance_grade": "A",
        "top_bottlenecks": [],
        "calculation_rewrites": [],
        "formatting_and_ux_tips": [],
        "data_and_filter_tips": [],
        "dashboard_and_server_tips": [],
        "quick_wins_checklist": ["Keep extract refresh scheduled"],
        "strategic_refactors": [],
    }

    class FakeResponse:
        async def structured_output(self) -> dict[str, Any]:
            return payload

    class FakeAgent:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.conversation_id = "conv-123"
            self.conversation = SimpleNamespace(
                total_usage=SimpleNamespace(
                    prompt_token_count=100,
                    cached_content_token_count=0,
                    candidates_token_count=50,
                    thoughts_token_count=10,
                    total_token_count=160,
                )
            )

        async def __aenter__(self) -> "FakeAgent":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def chat(self, prompt: str) -> FakeResponse:
            assert "retail_exec_optimized.twb" in prompt
            return FakeResponse()

    settings.set_api_key("AIzaSyFakeKey99999", persist_to_env=False)
    try:
        with patch("app.agent.evaluator.Agent", FakeAgent):
            report, telemetry = asyncio.run(evaluate_workbook_with_antigravity(analysis))
            assert isinstance(report, AIEvaluationReport)
            assert report.estimated_performance_grade == "A"
            assert telemetry.conversation_id == "conv-123"
            assert telemetry.usage["total_token_count"] == 160
    finally:
        settings.clear_api_key(remove_from_env=False)


def test_antigravity_503_system_error_surfaces_cleanly() -> None:
    import asyncio
    from types import SimpleNamespace
    from google.antigravity import types as sdk_types

    raw = (SAMPLES_DIR / "retail_exec_optimized.twb").read_bytes()
    analysis = run_heuristic_audit(parse_tableau_workbook(raw, "retail_exec_optimized.twb"))

    class EmptyResponse:
        async def structured_output(self) -> None:
            return None

        async def text(self) -> str:
            return ""

    class FailingAgent:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.conversation_id = "conv-503"
            self.conversation = SimpleNamespace(
                total_usage=None,
                history=[
                    SimpleNamespace(
                        status=sdk_types.StepStatus.ERROR,
                        source=sdk_types.StepSource.SYSTEM,
                        http_code=503,
                        error="Encountered retryable error from model provider: Deadline expired before operation could complete.",
                    )
                ],
            )

        async def __aenter__(self) -> "FailingAgent":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def chat(self, prompt: str) -> EmptyResponse:
            return EmptyResponse()

    settings.set_api_key("AIzaSyFakeKey99999", persist_to_env=False)
    try:
        with patch("app.agent.evaluator.Agent", FailingAgent):
            with pytest.raises(RuntimeError, match="HTTP 503.*Deadline expired"):
                asyncio.run(evaluate_workbook_with_antigravity(analysis))
            with pytest.raises(RuntimeError, match="HTTP 503.*Deadline expired"):
                asyncio.run(chat_with_antigravity_coach(analysis, "Why is it slow?"))
    finally:
        settings.clear_api_key(remove_from_env=False)
