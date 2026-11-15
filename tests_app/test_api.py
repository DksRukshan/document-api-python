"""FastAPI integration tests for Tableau AI Documenter web server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tableau_doc_app import server
from tableau_doc_app.models import (
    DashboardDocumentationReport,
    DataArchitectureDoc,
    DocumentationBundle,
    TokenUsageSummary,
)

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clear_server_caches():
    server._SNAPSHOTS.clear()
    server._JOBS.clear()
    yield
    server._SNAPSHOTS.clear()
    server._JOBS.clear()


def test_index_and_health_endpoints():
    client = TestClient(server.app)

    home = client.get("/")
    assert home.status_code == 200
    assert "Tableau AI Documenter" in home.text

    health = client.get("/api/health")
    assert health.status_code == 200
    data = health.json()
    assert data["status"] == "ok"
    sample_paths = [s["path"] for s in data["samples"]]
    assert "samples/show_workbook_info/geocoding.twbx" in sample_paths


def test_inspect_sample_upload_and_security_guards():
    client = TestClient(server.app)

    # Sample inspection
    res = client.post(
        "/api/inspect",
        data={"sample_path": "samples/show_workbook_info/geocoding.twbx"},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["snapshot_id"]
    assert len(payload["snapshot"]["dashboards"]) == 6
    assert "# geocoding" in payload["preview_markdown"]

    # Multipart upload inspection
    twb_bytes = (REPO_ROOT / "test/assets/filtering.twb").read_bytes()
    upload_res = client.post(
        "/api/inspect",
        files={"file": ("filtering.twb", twb_bytes, "application/xml")},
    )
    assert upload_res.status_code == 200
    assert upload_res.json()["snapshot"]["filename"] == "filtering.twb"

    # Bad extension rejected
    bad_ext = client.post(
        "/api/inspect",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert bad_ext.status_code == 400

    # Path traversal blocked
    trav = client.post(
        "/api/inspect",
        data={"sample_path": "../../etc/passwd"},
    )
    assert trav.status_code == 400


def test_deterministic_extract_export_formats():
    client = TestClient(server.app)
    inspect_res = client.post(
        "/api/inspect",
        data={"sample_path": "test/assets/filtering.twb"},
    )
    snap_id = inspect_res.json()["snapshot_id"]

    md_res = client.post("/api/extract-export", data={"snapshot_id": snap_id, "fmt": "md"})
    assert md_res.status_code == 200
    assert "attachment;" in md_res.headers["content-disposition"]
    assert "# filtering" in md_res.text

    html_res = client.post("/api/extract-export", data={"snapshot_id": snap_id, "fmt": "html"})
    assert html_res.status_code == 200
    assert "<!DOCTYPE html>" in html_res.text

    json_res = client.post("/api/extract-export", data={"snapshot_id": snap_id, "fmt": "json"})
    assert json_res.status_code == 200
    parsed = json.loads(json_res.text)
    assert parsed["used_ai"] is False
    assert parsed["snapshot"]["filename"] == "filtering.twb"


def test_generate_requires_key_when_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    client = TestClient(server.app)
    res = client.post(
        "/api/generate",
        data={"sample_path": "test/assets/filtering.twb", "backend": "gemini"},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["aistudio_url"] == "https://aistudio.google.com/app/api-keys"


def test_generate_sse_and_export_flow(monkeypatch: pytest.MonkeyPatch):
    async def _fake_generate(snapshot, backend_config=None, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event:
            await on_event({"type": "status", "message": "Mock agent started"})
            await on_event({"type": "thought", "text": "Checking dashboard layout"})
            await on_event({"type": "tool_call", "name": "get_workbook_overview", "args": {}})
            await on_event({"type": "tool_result", "name": "get_workbook_overview", "preview": "ok"})
            await on_event({"type": "complete", "usage": {"total_tokens": 99}, "warnings": []})

        report = DashboardDocumentationReport(
            workbook_title="Filtering Dashboard Docs",
            executive_summary="Demonstrates interactive filtering between two sheets.",
            target_audience=["BI Analysts"],
            data_architecture=DataArchitectureDoc(
                overview="Single embedded data source.",
                sources_and_lineage=["Superstore"],
                refresh_and_security_notes="Local extract.",
            ),
            mermaid_lineage_diagram="flowchart LR\n  DS[Superstore] --> D[Dashboard 1]",
        )
        return DocumentationBundle(
            snapshot=snapshot,
            ai_report=report,
            target_dashboard=kwargs.get("target_dashboard"),
            audience=kwargs.get("audience") or "BI Analysts",
            generated_at_iso="2026-11-15T00:00:00Z",
            used_ai=True,
            usage=TokenUsageSummary(prompt_tokens=60, candidate_tokens=39, total_tokens=99),
            agent_model="gemini-default",
        )

    monkeypatch.setattr(server, "generate_ai_documentation", _fake_generate)

    client = TestClient(server.app)
    gen_res = client.post(
        "/api/generate",
        data={
            "sample_path": "test/assets/filtering.twb",
            "backend": "gemini",
            "api_key": "AIzaSessionOnlyKey",
            "audience": "BI Analysts",
        },
    )
    assert gen_res.status_code == 200
    job_id = gen_res.json()["job_id"]

    # Consume SSE events until completion
    sse_res = client.get(f"/api/jobs/{job_id}/events")
    assert sse_res.status_code == 200
    assert "event: thought" in sse_res.text
    assert "event: tool_call" in sse_res.text
    assert "event: complete" in sse_res.text
    # Ensure session API key is never leaked in SSE output
    assert "AIzaSessionOnlyKey" not in sse_res.text

    job_res = client.get(f"/api/jobs/{job_id}")
    assert job_res.status_code == 200
    job_payload = job_res.json()
    assert job_payload["status"] == "completed"
    assert job_payload["bundle"]["ai_report"]["workbook_title"] == "Filtering Dashboard Docs"

    for fmt in ("md", "html", "json"):
        exp = client.get(f"/api/jobs/{job_id}/export/{fmt}")
        assert exp.status_code == 200
        assert "Filtering Dashboard Docs" in exp.text
        assert "AIzaSessionOnlyKey" not in exp.text
