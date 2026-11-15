"""Tests for deterministic Tableau .twb/.twbx extraction."""

from __future__ import annotations

import base64
import warnings
from pathlib import Path

import pytest

from tableau_doc_app.extractor import build_deterministic_mermaid, extract_workbook

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_extract_geocoding_twbx_comprehensive():
    snap = extract_workbook(str(REPO_ROOT / "samples/show_workbook_info/geocoding.twbx"))

    assert snap.filename == "geocoding.twbx"
    assert snap.workbook_name == "geocoding"
    assert len(snap.dashboards) == 6
    assert len(snap.worksheets) == 22
    real_ds = [d for d in snap.datasources if not d.is_parameters]
    assert len(real_ds) == 4
    assert len(snap.parameters) == 6
    assert len(snap.actions) == 10
    assert len(snap.thumbnails) == 10

    # Verify decoded thumbnails are real PNG bytes
    raw_png = base64.b64decode(snap.thumbnails[0].base64_png)
    assert raw_png.startswith(b"\x89PNG\r\n\x1a\n")

    # Verify FCP zones extracted and no duplicate devicelayouts inflation for Commission Model
    comm_dash = next(d for d in snap.dashboards if d.name == "Commission Model")
    assert set(comm_dash.worksheets) == {
        "QuotaAttainment",
        "CommissionProjection",
        "Sales",
        "OTE",
    }
    assert len(comm_dash.exposed_parameters) == 4
    assert comm_dash.has_thumbnail is True
    assert any(z.zone_type == "worksheet" for z in comm_dash.zones)
    assert any(z.zone_type == "paramctrl" for z in comm_dash.zones)

    # Verify calculated fields have resolved human-readable formulas
    all_calcs = [cf for ds in real_ds for cf in ds.calculated_fields]
    assert len(all_calcs) >= 20
    assert any((cf.readable_calculation or cf.calculation) for cf in all_calcs)

    # Verify deterministic Mermaid diagram starts cleanly
    assert snap.deterministic_mermaid.startswith("flowchart LR")
    assert "Commission Model" in snap.deterministic_mermaid


def test_extract_diverse_tableau_versions_and_packaged_workbooks():
    paths = [
        "test/assets/filtering.twb",
        "test/assets/shapes_test.twb",
        "test/assets/multiple_connections.twb",
        "test/assets/TABLEAU_10_TWBX.twbx",
        "test/assets/Cache.twbx",
        "test/assets/empty_workbook.twb",
        "samples/replicate-workbook/sample-superstore.twb",
    ]
    for rel in paths:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            snap = extract_workbook(str(REPO_ROOT / rel))
        assert snap.filename == Path(rel).name
        assert snap.deterministic_mermaid.startswith("flowchart LR")


def test_extract_filtering_twb_dashboard_and_worksheet_details():
    snap = extract_workbook(str(REPO_ROOT / "test/assets/filtering.twb"))
    assert len(snap.dashboards) == 1
    assert len(snap.worksheets) == 2
    assert any(ws.used_in_dashboards for ws in snap.worksheets)
    scoped_mermaid = build_deterministic_mermaid(snap, target_dashboard=snap.dashboards[0].name)
    assert snap.dashboards[0].name in scoped_mermaid
