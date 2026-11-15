"""CLI end-to-end tests for cli.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import cli
from tableau_doc_app.models import (
    DashboardDocumentationReport,
    DataArchitectureDoc,
    DocumentationBundle,
    TokenUsageSummary,
)

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_cli_inspect_summary_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    sample = str(REPO_ROOT / "test/assets/filtering.twb")
    rc = cli.main(["inspect", sample])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Workbook:        filtering (filtering.twb)" in captured
    assert "Dashboards:      1" in captured

    out_json = tmp_path / "inspect.json"
    rc2 = cli.main(["inspect", sample, "--json", "-o", str(out_json)])
    assert rc2 == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["filename"] == "filtering.twb"


def test_cli_generate_no_ai_writes_all_formats(tmp_path: Path):
    sample = str(REPO_ROOT / "test/assets/filtering.twb")
    out_dir = tmp_path / "docs"
    rc = cli.main(
        [
            "generate",
            sample,
            "--no-ai",
            "--formats",
            "md",
            "html",
            "json",
            "--out-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    assert (out_dir / "filtering_docs.md").is_file()
    assert (out_dir / "filtering_docs.html").is_file()
    assert (out_dir / "filtering_docs.json").is_file()


def test_cli_generate_missing_key_returns_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    sample = str(REPO_ROOT / "test/assets/filtering.twb")
    rc = cli.main(["generate", sample, "--out-dir", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "https://aistudio.google.com/app/api-keys" in err


def test_cli_generate_ai_mocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    sample = str(REPO_ROOT / "test/assets/filtering.twb")

    async def _fake_gen(snapshot, backend_config=None, **kwargs):
        cb = kwargs.get("on_event")
        if cb:
            cb({"type": "status", "message": "Running"})
            cb({"type": "thought", "text": "Reading filtering sheets"})
            cb({"type": "complete", "usage": {"total_tokens": 42}})
        return DocumentationBundle(
            snapshot=snapshot,
            ai_report=DashboardDocumentationReport(
                workbook_title="CLI Mocked Docs",
                executive_summary="Generated through CLI test.",
                target_audience=["Analysts"],
                data_architecture=DataArchitectureDoc(
                    overview="Single datasource.",
                    sources_and_lineage=["TestData"],
                    refresh_and_security_notes="Local extract.",
                ),
                mermaid_lineage_diagram="flowchart LR\n  A --> B",
            ),
            generated_at_iso="2026-11-15T00:00:00Z",
            used_ai=True,
            usage=TokenUsageSummary(total_tokens=42),
            agent_model="gemini-default",
        )

    out_dir = tmp_path / "ai_out"
    with patch("cli.generate_ai_documentation", _fake_gen):
        rc = cli.main(
            [
                "generate",
                sample,
                "--api-key",
                "AIzaCliMockKey",
                "--formats",
                "md",
                "--out-dir",
                str(out_dir),
            ]
        )
    assert rc == 0
    md_path = out_dir / "filtering_docs.md"
    assert md_path.is_file()
    assert "# CLI Mocked Docs" in md_path.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "42 total tokens" in out
