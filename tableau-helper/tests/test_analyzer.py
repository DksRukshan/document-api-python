"""Unit tests for .twb/.twbx parser, heuristic rules, and curated documentation search."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.analyzer.docs_search import search_tableau_docs
from app.analyzer.heuristics import run_heuristic_audit
from app.analyzer.models import AuditCategory
from app.analyzer.twb_parser import parse_tableau_workbook
from app.config import SAMPLES_DIR


def test_unoptimized_sample_triggers_key_heuristics() -> None:
    raw = (SAMPLES_DIR / "retail_exec_unoptimized.twb").read_bytes()
    analysis = run_heuristic_audit(parse_tableau_workbook(raw, "retail_exec_unoptimized.twb"))

    assert analysis.file_type == "twb"
    assert analysis.overall_score < 70
    assert analysis.grade in {"D", "F"}
    assert len(analysis.datasources) == 2
    assert len(analysis.calculations) == 13
    assert len(analysis.worksheets) == 6
    assert len(analysis.dashboards) == 1

    finding_ids = {f.id for f in analysis.findings}
    assert any(fid.startswith("ds-singlethread-") for fid in finding_ids)
    assert any(fid.startswith("ds-customsql-") for fid in finding_ids)
    assert any(fid.startswith("ds-joins-") for fid in finding_ids)
    assert "flt-only-relevant" in finding_ids
    assert "flt-topn-context" in finding_ids
    assert "flt-date-string-volatile" in finding_ids
    assert "ws-blending" in finding_ids
    assert "srv-show-tabs" in finding_ids
    assert "fmt-override-bloat" in finding_ids
    assert "fmt-fonts" in finding_ids
    assert "acc-allcaps" in finding_ids

    titles = " | ".join(f.title for f in analysis.findings)
    assert "ELSEIF" in titles
    assert "ATTR()" in titles
    assert "COUNTD()" in titles
    assert "NOW()" in titles
    assert "Automatic Dashboard Sizing" in titles
    assert "Custom Number Format" in titles


def test_optimized_sample_scores_cleanly() -> None:
    raw = (SAMPLES_DIR / "retail_exec_optimized.twb").read_bytes()
    analysis = run_heuristic_audit(parse_tableau_workbook(raw, "retail_exec_optimized.twb"))

    assert analysis.overall_score == 100
    assert analysis.grade == "A"
    assert analysis.findings == []


def test_twbx_packaging_oversized_image_and_zip_slip_guard() -> None:
    twb_bytes = (SAMPLES_DIR / "retail_exec_optimized.twb").read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("workbook.twb", twb_bytes)
        zf.writestr("Image/huge_banner.png", b"x" * (65 * 1024))
        zf.writestr("Data/Orders.hyper", b"hyper-mock-bytes")
        zf.writestr("../escape.txt", b"should-be-ignored")

    analysis = run_heuristic_audit(parse_tableau_workbook(buf.getvalue(), "packaged.twbx"))
    assert analysis.file_type == "twbx"
    asset_paths = {a.path for a in analysis.assets}
    assert "Image/huge_banner.png" in asset_paths
    assert "Data/Orders.hyper" in asset_paths
    assert "../escape.txt" not in asset_paths
    assert any(f.id == "srv-large-images" for f in analysis.findings)


def test_repeated_calc_reference_and_perf_recording_detection() -> None:
    xml = """<?xml version='1.0' encoding='utf-8'?>
    <workbook version='18.1'>
      <datasources>
        <datasource caption='PerfDS' name='ds1'>
          <connection class='hyper' />
          <extract enabled='true' />
          <column caption='Base Margin' name='[Calc_Base]' datatype='real' role='measure'>
            <calculation formula='SUM([Profit])/SUM([Sales])' />
          </column>
          <column caption='Double Call Margin' name='[Calc_Double]' datatype='string' role='measure'>
            <calculation formula='IF [Calc_Base] &gt; 0.2 THEN &quot;High&quot; ELSEIF [Calc_Base] &gt; 0 THEN &quot;Low&quot; ELSE &quot;Neg&quot; END' />
          </column>
        </datasource>
      </datasources>
      <worksheets>
        <worksheet name='Timeline'><table><view><datasources><datasource name='ds1'/></datasources><panes><pane><mark class='Bar'/></pane></panes><rows>[ds1].[Calc_Double]</rows></view></table></worksheet>
        <worksheet name='Events'><table><view><datasources><datasource name='ds1'/></datasources><panes><pane><mark class='Bar'/></pane></panes></view></table></worksheet>
        <worksheet name='Query'><table><view><datasources><datasource name='ds1'/></datasources><panes><pane><mark class='Text'/></pane></panes></view></table></worksheet>
      </worksheets>
      <dashboards>
        <dashboard name='Performance Summary'><size sizing-mode='fixed' maxwidth='1200' maxheight='800' /><zones><zone name='Timeline'/><zone name='Events'/><zone name='Query'/></zones></dashboard>
      </dashboards>
    </workbook>
    """.encode("utf-8")

    analysis = run_heuristic_audit(parse_tableau_workbook(xml, "perf_rec.twb"))
    assert analysis.performance_recording.detected is True
    double_calc = next(c for c in analysis.calculations if c.name == "Calc_Double")
    assert "repeated_calc_call" in double_calc.flags
    assert any(
        f.category == AuditCategory.CALCULATIONS and "Repeated Reference" in f.title
        for f in analysis.findings
    )


def test_docs_search_returns_relevant_sections() -> None:
    results = search_tableau_docs("ATTR MIN Only Relevant Values custom number format")
    assert len(results) >= 1
    combined = " ".join(r["excerpt"] for r in results).lower()
    assert "attr" in combined or "relevant values" in combined
