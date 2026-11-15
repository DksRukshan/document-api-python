"""Comprehensive tests for unified Tableau Performance Audit, AI Evaluation, Coach Chat, API & CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from google.antigravity import types

import cli
from tableau_doc_app import server
from tableau_doc_app.agent import (
    AntigravityConfigurationError,
    _build_empty_output_error,
    build_evaluator_config,
    chat_with_antigravity_coach,
    clear_session_api_key,
    create_tableau_tools,
    evaluate_performance_with_antigravity,
    get_credential_status,
    set_session_api_key,
)
from tableau_doc_app.audit_heuristics import audit_workbook_file
from tableau_doc_app.audit_models import (
    AIEvaluationReport,
    AgentTelemetry,
    ArchitectureTip,
    CalculationRewriteTip,
    DesignImprovementTip,
)
from tableau_doc_app.docs_search import search_tableau_docs
from tableau_doc_app.extractor import extract_workbook
from tableau_doc_app.models import BackendConfig
from tableau_doc_app.renderer import bundle_from_snapshot, render_html, render_markdown

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_SAMPLES = REPO_ROOT / "tableau_doc_app" / "samples"


@pytest.fixture(autouse=True)
def _clean_runtime_state(monkeypatch: pytest.MonkeyPatch):
    server._SESSIONS.clear()
    server._JOBS.clear()
    clear_session_api_key(remove_from_env=False)
    yield
    server._SESSIONS.clear()
    server._JOBS.clear()
    clear_session_api_key(remove_from_env=False)


def test_deterministic_audit_samples_and_ranking():
    unopt = audit_workbook_file(str(APP_SAMPLES / "retail_exec_unoptimized.twb"))
    opt = audit_workbook_file(str(APP_SAMPLES / "retail_exec_optimized.twb"))
    geo = audit_workbook_file(str(REPO_ROOT / "samples/show_workbook_info/geocoding.twbx"))

    assert unopt.overall_score < opt.overall_score
    assert unopt.grade in ("C", "D", "F")
    assert opt.overall_score >= 70
    assert len(unopt.findings) > len(opt.findings)
    assert len(unopt.category_scores) >= 6
    assert geo.file_type == "twbx"
    assert geo.overall_score > 0


def test_docs_search_and_unified_tool_set():
    hits = search_tableau_docs("regex nested if attr", max_results=4)
    assert hits
    assert any(str(h["source"]).endswith(".md") for h in hits)

    twb = str(APP_SAMPLES / "retail_exec_unoptimized.twb")
    snap = extract_workbook(twb)
    audit = audit_workbook_file(twb)
    tools = {fn.__name__: fn for fn in create_tableau_tools(snap, audit=audit)}

    assert {
        "get_workbook_overview",
        "get_dashboard_detail",
        "get_worksheet_detail",
        "get_datasource_detail",
        "get_calculated_fields",
        "get_parameters_and_actions",
        "inspect_heuristic_findings",
        "inspect_formatting_and_ux",
        "search_tableau_best_practices",
    }.issubset(set(tools.keys()))

    findings_payload = json.loads(tools["inspect_heuristic_findings"]())
    assert findings_payload["overall_score"] == audit.overall_score
    assert findings_payload["findings"]
    ux_payload = json.loads(tools["inspect_formatting_and_ux"]())
    assert "workbook_style" in ux_payload
    docs_payload = json.loads(tools["search_tableau_best_practices"]("WCAG contrast"))
    assert isinstance(docs_payload, list) and docs_payload


def test_build_evaluator_config_single_and_deep_multi_agent(tmp_path: Path):
    twb = str(APP_SAMPLES / "retail_exec_unoptimized.twb")
    snap = extract_workbook(twb)
    audit = audit_workbook_file(twb)

    with pytest.raises(AntigravityConfigurationError):
        build_evaluator_config(
            snap,
            audit,
            BackendConfig(backend="gemini", api_key=None),
            app_data_dir=str(tmp_path),
        )

    single_cfg, single_res = build_evaluator_config(
        snap,
        audit,
        BackendConfig(backend="gemini", api_key="AIzaTestSingle"),
        deep_multi_agent=False,
        app_data_dir=str(tmp_path),
    )
    assert single_res.model is None
    assert single_cfg.subagents == []
    assert single_cfg.skills_paths

    deep_cfg, _ = build_evaluator_config(
        snap,
        audit,
        BackendConfig(backend="gemini", api_key="AIzaTestDeep"),
        deep_multi_agent=True,
        app_data_dir=str(tmp_path),
    )
    sub_names = {s.name for s in deep_cfg.subagents}
    assert sub_names == {"calculation_optimizer", "formatting_ux_critic"}


def test_empty_output_error_surfacing_dns_and_http503():
    dns_step = SimpleNamespace(
        status=types.StepStatus.ERROR,
        http_code=2,
        error=(
            'There was a network issue connecting to the server: '
            'Post "https://generativelanguage.googleapis.com/v1beta/models/gemini:streamGenerateContent": '
            "dial tcp: lookup generativelanguage.googleapis.com: no such host"
        ),
    )
    dns_agent = SimpleNamespace(conversation=SimpleNamespace(history=[dns_step]))
    dns_err = _build_empty_output_error(dns_agent, "structured output")
    assert "generativelanguage.googleapis.com" in str(dns_err)
    assert "Ollama/LiteRT" in str(dns_err)

    overload_step = SimpleNamespace(
        status=types.StepStatus.ERROR,
        http_code=503,
        error="Service temporarily unavailable",
    )
    overload_agent = SimpleNamespace(conversation=SimpleNamespace(history=[overload_step]))
    overload_err = _build_empty_output_error(overload_agent, "structured output")
    assert "HTTP 503" in str(overload_err)


def test_evaluate_performance_and_coach_mocked(tmp_path: Path):
    twb = str(APP_SAMPLES / "retail_exec_unoptimized.twb")
    snap = extract_workbook(twb)
    audit = audit_workbook_file(twb)

    expected_eval = AIEvaluationReport(
        executive_summary="Workbook needs REGEXP rewrite and fixed dashboard sizing.",
        estimated_performance_grade="D",
        top_bottlenecks=["Chained CONTAINS in Segment Tag", "Automatic dashboard sizing"],
        calculation_rewrites=[
            CalculationRewriteTip(
                field_name="Segment Tag",
                datasource="Orders",
                current_formula='IF CONTAINS([Name], "A") THEN "X" ELSE "Y" END',
                optimized_formula='REGEXP_MATCH([Name], "A")',
                why_it_is_faster="Single compiled regex scan avoids repeated substring passes.",
                expected_impact="High",
            )
        ],
        formatting_and_ux_tips=[
            DesignImprovementTip(
                target="Executive Dashboard",
                area="Color & Accessibility",
                observation="Red/green palette without encoding cues.",
                recommendation="Switch to colorblind-safe Okabe-Ito blue/orange palette.",
                benefit="WCAG 2.2 AA compliance.",
            )
        ],
        data_and_filter_tips=[
            ArchitectureTip(
                target="Filters",
                priority="High",
                issue="Too many 'Only Relevant Values' cascading filters.",
                action="Limit cascading filters to hierarchical dimensions.",
                reference_principle="Order of Operations",
            )
        ],
        quick_wins_checklist=["Switch dashboard sizing to Fixed 1440x900."],
        strategic_refactors=["Push complex string classification to warehouse view."],
    )

    class _FakeResp:
        def __init__(self, structured=None, text_val=""):
            self._structured = structured
            self._text_val = text_val

        async def structured_output(self):
            return self._structured

        async def text(self):
            return self._text_val

    class _FakeAgent:
        def __init__(self, config):
            self.config = config
            self.conversation_id = "conv-audit-1"
            self.conversation = SimpleNamespace(
                history=[],
                total_usage=SimpleNamespace(
                    prompt_token_count=120,
                    cached_content_token_count=0,
                    candidates_token_count=80,
                    thoughts_token_count=10,
                    total_token_count=210,
                ),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def chat(self, prompt: str):
            if "USER QUESTION:" in prompt:
                return _FakeResp(text_val="Start by switching dashboard sizing to Fixed 1440x900.")
            return _FakeResp(structured=expected_eval)

    events: list[dict] = []
    with patch("tableau_doc_app.agent.Agent", _FakeAgent):
        report, telemetry = asyncio.run(
            evaluate_performance_with_antigravity(
                snap,
                audit,
                BackendConfig(backend="gemini", api_key="AIzaMockEval"),
                deep_multi_agent=True,
                app_data_dir=str(tmp_path),
                on_event=lambda e: events.append(e),
            )
        )
        reply, coach_tel = asyncio.run(
            chat_with_antigravity_coach(
                snap,
                audit,
                "What should I fix first?",
                BackendConfig(backend="gemini", api_key="AIzaMockEval"),
                chat_history=[{"role": "user", "content": "hi"}],
                app_data_dir=str(tmp_path),
            )
        )

    assert report.estimated_performance_grade == "D"
    assert report.calculation_rewrites[0].field_name == "Segment Tag"
    assert telemetry.used_multi_agent is True
    assert telemetry.usage["total_token_count"] == 210
    assert "Fixed 1440x900" in reply
    assert coach_tel.conversation_id == "conv-audit-1"


def test_unified_renderer_includes_scorecard_and_xss_escapes():
    twb = str(APP_SAMPLES / "retail_exec_unoptimized.twb")
    snap = extract_workbook(twb)
    audit = audit_workbook_file(twb)
    ai_eval = AIEvaluationReport(
        executive_summary="<script>alert('xss')</script> Performance summary.",
        estimated_performance_grade="C",
        top_bottlenecks=["Nested IF"],
        calculation_rewrites=[
            CalculationRewriteTip(
                field_name="Evil<script>Calc",
                datasource="DS",
                current_formula="IF [A]=1 THEN 1 END",
                optimized_formula="CASE [A] WHEN 1 THEN 1 END",
                why_it_is_faster="Cleaner branch evaluation.",
                expected_impact="Medium",
            )
        ],
        formatting_and_ux_tips=[
            DesignImprovementTip(
                target="Sheet 1",
                area="Typography",
                observation="Mixed fonts",
                recommendation="Unify font to Tableau Book",
                benefit="Clean hierarchy",
            )
        ],
        quick_wins_checklist=["Enable Fixed Sizing"],
    )
    bundle = bundle_from_snapshot(snap, audit=audit, ai_evaluation=ai_eval)

    md = render_markdown(bundle)
    assert "## Performance, Formatting & Accessibility Health Scorecard" in md
    assert "## Antigravity AI Performance & UX Playbook" in md
    assert "CASE [A] WHEN 1 THEN 1 END" in md

    html_doc = render_html(bundle)
    assert "Performance, Formatting &amp; Accessibility Scorecard" in html_doc
    assert "Antigravity AI Performance &amp; UX Playbook" in html_doc
    assert "<script>alert('xss')</script>" not in html_doc
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in html_doc


def test_unified_server_endpoints(monkeypatch: pytest.MonkeyPatch):
    client = TestClient(server.app)

    # Config & key lifecycle (session-only)
    cfg0 = client.get("/api/config")
    assert cfg0.status_code == 200
    assert cfg0.json()["configured"] is False

    save_res = client.post(
        "/api/config/api-key",
        json={"api_key": "AIzaSessionSecretKey9876", "persist_to_env": False},
    )
    assert save_res.status_code == 200
    assert save_res.json()["configured"] is True
    assert "AIzaSessionSecretKey9876" not in json.dumps(save_res.json())

    # Showcase samples exposed
    samples_res = client.get("/api/samples")
    assert samples_res.status_code == 200
    names = {s["name"] for s in samples_res.json()["samples"]}
    assert {"retail_exec_unoptimized.twb", "retail_exec_optimized.twb", "geocoding.twbx"}.issubset(names)

    # Inspect unoptimized sample includes audit scorecard
    insp = client.post(
        "/api/inspect",
        data={"sample_path": "tableau_doc_app/samples/retail_exec_unoptimized.twb"},
    )
    assert insp.status_code == 200
    insp_data = insp.json()
    sid = insp_data["snapshot_id"]
    assert insp_data["audit"]["overall_score"] < 85
    assert "Performance, Formatting & Accessibility Health Scorecard" in insp_data["preview_markdown"]

    # Mock AI audit + stream + chat
    mock_eval = AIEvaluationReport(
        executive_summary="Mocked AI audit output.",
        estimated_performance_grade="C",
        top_bottlenecks=["LOD expression in tooltip"],
        quick_wins_checklist=["Switch sizing to Fixed"],
    )
    mock_tel = AgentTelemetry(
        conversation_id="conv-1",
        usage={"total_tokens": 55},
        used_multi_agent=True,
    )

    async def _fake_eval(snapshot, audit, backend_cfg=None, **kwargs):
        cb = kwargs.get("on_event")
        if cb:
            await cb({"type": "status", "message": "Starting evaluator"})
            await cb({"type": "tool_start", "tool": "inspect_heuristic_findings", "args": {}})
        return mock_eval, mock_tel

    async def _fake_chat(snapshot, audit, msg, backend_cfg=None, **kwargs):
        return f"Coach answer for: {msg}", AgentTelemetry(usage={"total_tokens": 20})

    monkeypatch.setattr(server, "evaluate_performance_with_antigravity", _fake_eval)
    monkeypatch.setattr(server, "chat_with_antigravity_coach", _fake_chat)

    audit_res = client.post(
        f"/api/audit/{sid}",
        json={"backend": "gemini", "deep_multi_agent": True, "focus_notes": "Check LODs"},
    )
    assert audit_res.status_code == 200
    assert audit_res.json()["evaluation"]["estimated_performance_grade"] == "C"

    stream_res = client.post(
        f"/api/audit/{sid}/stream",
        json={"backend": "gemini", "deep_multi_agent": False},
    )
    assert stream_res.status_code == 200
    assert "event: tool_start" in stream_res.text
    assert "event: complete" in stream_res.text

    chat_res = client.post(f"/api/chat/{sid}", json={"message": "How do I speed up tooltip?"})
    assert chat_res.status_code == 200
    assert "Coach answer for: How do I speed up tooltip?" in chat_res.json()["reply"]

    # Combined session export includes static audit + mocked AI evaluation
    md_exp = client.get(f"/api/sessions/{sid}/export/md")
    assert md_exp.status_code == 200
    assert "Mocked AI audit output." in md_exp.text
    assert "Health Scorecard" in md_exp.text

    del_res = client.delete("/api/config/api-key")
    assert del_res.status_code == 200
    assert get_credential_status()["configured"] is False


def test_cli_audit_no_ai_and_mocked_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    sample = str(APP_SAMPLES / "retail_exec_unoptimized.twb")
    out_dir = tmp_path / "audit_out"

    rc = cli.main(["audit", sample, "--no-ai", "--out-dir", str(out_dir)])
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "Workbook Health Score:" in stdout
    assert (out_dir / "retail_exec_unoptimized_docs.md").is_file()
    assert (out_dir / "retail_exec_unoptimized_docs.html").is_file()

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    rc_missing = cli.main(["audit", sample])
    assert rc_missing == 2
    assert "https://aistudio.google.com/app/api-keys" in capsys.readouterr().err

    mock_eval = AIEvaluationReport(
        executive_summary="CLI AI evaluation complete.",
        estimated_performance_grade="D",
        top_bottlenecks=["Nested IF"],
        quick_wins_checklist=["Fix sizing"],
    )

    async def _fake_cli_eval(*args, **kwargs):
        return mock_eval, AgentTelemetry(usage={"total_tokens": 77})

    monkeypatch.setattr(cli, "evaluate_performance_with_antigravity", _fake_cli_eval)
    rc_ai = cli.main(["audit", sample, "--api-key", "AIzaCliAudit", "--deep", "-q"])
    assert rc_ai == 0
    assert "AI Performance Grade: D" in capsys.readouterr().out
