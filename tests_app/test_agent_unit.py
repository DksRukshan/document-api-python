"""Unit tests for Google Antigravity SDK integration, tools, and multimodal pipeline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.antigravity import (
    LiteRTAgentConfig,
    LocalAgentConfig,
    LocalOpenAIAgentConfig,
    types,
)

from tableau_doc_app.agent import (
    AntigravityConfigurationError,
    _extract_json_fallback,
    _select_thumbnail_media,
    build_agent_config,
    create_tableau_tools,
    generate_ai_documentation,
    get_credential_status,
)
from tableau_doc_app.extractor import extract_workbook
from tableau_doc_app.models import (
    BackendConfig,
    DashboardDocumentationReport,
    DataArchitectureDoc,
)

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sample_report_dict() -> dict:
    return DashboardDocumentationReport(
        workbook_title="Geocoding & Commission Model",
        executive_summary="Commission calculator and executive regional sales overview.",
        target_audience=["Sales Leadership"],
        kpi_glossary=[],
        dashboards=[],
        worksheets=[],
        calculated_fields_explained=[],
        data_architecture=DataArchitectureDoc(
            overview="Packaged Excel and CSV data sources.",
            sources_and_lineage=["Sales Commission (textscan)"],
            refresh_and_security_notes="Static packaged extracts.",
        ),
        mermaid_lineage_diagram="flowchart LR\n  A[Sales] --> B[Overview]",
        recommendations=[],
    ).model_dump()


def test_credential_status_variants(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_BACKEND", raising=False)

    missing = get_credential_status()
    assert missing["backend"] == "gemini"
    assert missing["configured"] is False
    assert missing["source"] == "missing"
    assert missing["aistudio_url"] == "https://aistudio.google.com/app/api-keys"

    session = get_credential_status(BackendConfig(api_key="AIzaSessionKey"))
    assert session["configured"] is True
    assert session["source"] == "session"

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaEnvKey")
    env_status = get_credential_status()
    assert env_status["configured"] is True
    assert env_status["source"] == "env"


def test_tableau_tools_invocation():
    snap = extract_workbook(str(REPO_ROOT / "samples/show_workbook_info/geocoding.twbx"))
    tools = {fn.__name__: fn for fn in create_tableau_tools(snap)}

    assert set(tools.keys()) == {
        "get_workbook_overview",
        "get_dashboard_detail",
        "get_worksheet_detail",
        "get_datasource_detail",
        "get_calculated_fields",
        "get_parameters_and_actions",
    }

    overview = json.loads(tools["get_workbook_overview"]())
    assert len(overview["dashboards"]) == 6

    dash = json.loads(tools["get_dashboard_detail"]("Commission Model"))
    assert dash["name"] == "Commission Model"
    assert "QuotaAttainment" in dash["worksheets"]

    missing_dash = json.loads(tools["get_dashboard_detail"]("Does Not Exist"))
    assert "error" in missing_dash

    ws = json.loads(tools["get_worksheet_detail"]("CommissionProjection"))
    assert ws["name"] == "CommissionProjection"

    ds_list = json.loads(tools["get_datasource_detail"]())
    assert len(ds_list) == 4

    calcs = json.loads(tools["get_calculated_fields"]())
    assert len(calcs) >= 20

    pa = json.loads(tools["get_parameters_and_actions"]())
    assert len(pa["parameters"]) == 6
    assert len(pa["actions"]) == 10


def test_build_agent_config_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_MODEL", raising=False)

    snap = extract_workbook(str(REPO_ROOT / "test/assets/filtering.twb"))

    with pytest.raises(AntigravityConfigurationError) as exc_info:
        build_agent_config(snap, app_data_dir=str(tmp_path))
    assert "https://aistudio.google.com/app/api-keys" in str(exc_info.value)

    # Gemini default leaves model unset (None or SDK default on config)
    gem_cfg, resolved = build_agent_config(
        snap,
        BackendConfig(backend="gemini", api_key="AIzaFakeKey"),
        app_data_dir=str(tmp_path),
    )
    assert isinstance(gem_cfg, LocalAgentConfig)
    assert resolved.model is None
    assert isinstance(gem_cfg.response_schema, str)
    assert "DashboardDocumentationReport" in gem_cfg.response_schema

    ollama_cfg, _ = build_agent_config(
        snap,
        BackendConfig(backend="ollama", base_url="http://127.0.0.1:11434/v1"),
        app_data_dir=str(tmp_path),
    )
    assert isinstance(ollama_cfg, LocalOpenAIAgentConfig)

    litert_cfg, _ = build_agent_config(
        snap,
        BackendConfig(backend="litert", litert_model_path=str(tmp_path / "m.litertlm")),
        app_data_dir=str(tmp_path),
    )
    assert isinstance(litert_cfg, LiteRTAgentConfig)


def test_select_thumbnail_media_and_json_fallback():
    snap = extract_workbook(str(REPO_ROOT / "samples/show_workbook_info/geocoding.twbx"))
    media_all, names_all = _select_thumbnail_media(snap, None, max_images=3)
    assert len(media_all) == 3
    assert all(isinstance(m, types.Image) for m in media_all)
    assert len(names_all) == 3

    media_one, names_one = _select_thumbnail_media(snap, "Commission Model")
    assert names_one == ["Commission Model"]
    assert len(media_one) == 1

    assert _extract_json_fallback('```json\n{"workbook_title": "X"}\n```') == {"workbook_title": "X"}
    assert _extract_json_fallback("not json") is None


def test_generate_ai_documentation_streaming_and_structured_output(tmp_path: Path):
    snap = extract_workbook(str(REPO_ROOT / "test/assets/filtering.twb"))
    report_dict = _sample_report_dict()

    class _FakeResponse:
        stop_reason = types.StopReason.UNSPECIFIED
        usage_metadata = types.UsageMetadata(
            prompt_token_count=120,
            cached_content_token_count=20,
            candidates_token_count=80,
            thoughts_token_count=40,
            total_token_count=240,
        )

        @property
        def chunks(self):
            async def _gen():
                yield types.Thought(step_index=0, text="Inspecting workbook inventory.")
                yield types.ToolCall(name="get_workbook_overview", args={})
                yield types.ToolResult(name="get_workbook_overview", result='{"dashboards": []}')
                yield types.Text(step_index=1, text="Summary generated.")

            return _gen()

        async def structured_output(self):
            return report_dict

    class _FakeAgent:
        def __init__(self, config):
            self.config = config
            self.conversation = SimpleNamespace(
                total_usage=types.UsageMetadata(
                    prompt_token_count=120,
                    cached_content_token_count=20,
                    candidates_token_count=80,
                    thoughts_token_count=40,
                    total_token_count=240,
                )
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def chat(self, prompt):
            # Multimodal prompt should pass [str, Image, ...] because filtering.twb has thumbnails
            assert isinstance(prompt, list)
            assert any(isinstance(p, types.Image) for p in prompt)
            return _FakeResponse()

    events: list[dict] = []
    with patch("tableau_doc_app.agent.Agent", _FakeAgent):
        bundle = asyncio.run(
            generate_ai_documentation(
                snap,
                BackendConfig(backend="gemini", api_key="AIzaTestKey"),
                app_data_dir=str(tmp_path),
                on_event=events.append,
            )
        )

    assert bundle.used_ai is True
    assert bundle.ai_report is not None
    assert bundle.ai_report.workbook_title == "Geocoding & Commission Model"
    assert bundle.usage is not None
    assert bundle.usage.total_tokens == 240

    event_types = [e["type"] for e in events]
    assert event_types == ["status", "thought", "tool_call", "tool_result", "text", "complete"]


def test_generate_ai_documentation_json_text_fallback(tmp_path: Path):
    snap = extract_workbook(str(REPO_ROOT / "test/assets/filtering.twb"))
    report_json = json.dumps(_sample_report_dict())

    class _FakeResponseFallback:
        stop_reason = types.StopReason.UNSPECIFIED
        usage_metadata = None

        @property
        def chunks(self):
            async def _gen():
                yield types.Text(step_index=0, text=f"```json\n{report_json}\n```")

            return _gen()

        async def structured_output(self):
            return None

    class _FakeAgentFallback:
        def __init__(self, config):
            self.conversation = SimpleNamespace(total_usage=None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def chat(self, prompt):
            assert isinstance(prompt, str)  # include_thumbnails=False
            return _FakeResponseFallback()

    with patch("tableau_doc_app.agent.Agent", _FakeAgentFallback):
        bundle = asyncio.run(
            generate_ai_documentation(
                snap,
                BackendConfig(backend="gemini", api_key="AIzaTestKey"),
                include_thumbnails=False,
                app_data_dir=str(tmp_path),
            )
        )

    assert bundle.ai_report is not None
    assert bundle.ai_report.workbook_title == "Geocoding & Commission Model"
