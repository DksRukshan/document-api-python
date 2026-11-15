"""XML & archive parser extracting performance, formatting, layout, and accessibility telemetry."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from tableau_doc_app.audit_models import (
    AssetInfo,
    AuditCalculationInfo,
    AuditDashboardInfo,
    AuditDatasourceInfo,
    AuditFilterInfo,
    AuditWorksheetInfo,
    PerfEventSummary,
    PerformanceRecordingInfo,
    WorkbookAuditReport,
    WorkbookStyleInfo,
)

MAX_TWB_XML_BYTES = 80 * 1024 * 1024


def _split_shelf_pills(text: str | None) -> list[str]:
    if not text or not text.strip():
        return []
    return re.findall(r"\[[^\]]+\](?:\.\[[^\]]+\])?", text)


def _strip_brackets(value: str) -> str:
    return value.strip().lstrip("[").rstrip("]")


def _fcp_attr(el: ET.Element, suffix: str) -> str | None:
    """Fetch a standard or Tableau _.fcp.* namespaced attribute."""
    direct = el.get(suffix)
    if direct is not None:
        return direct
    needle = f".{suffix}"
    for k, v in el.attrib.items():
        if k.endswith(needle):
            return v
    return None


def _classify_asset_kind(member_path: str) -> str:
    lower = member_path.lower()
    if lower.endswith((".hyper", ".tde")):
        return "hyper"
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp")):
        return "image"
    if lower.endswith((".csv", ".xlsx", ".xls", ".tsv", ".json", ".parquet")):
        return "data"
    if "geocoding" in lower or lower.endswith(".tds"):
        return "geocoding"
    return "other"


def extract_twb_xml_and_assets(
    raw_bytes: bytes, filename: str
) -> tuple[str, str, list[AssetInfo]]:
    """Extract XML text and archive asset metadata from .twb or .twbx bytes."""
    lower = filename.lower()
    assets: list[AssetInfo] = []

    if lower.endswith(".twbx"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                twb_members: list[zipfile.ZipInfo] = []
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    clean_name = str(Path(info.filename).as_posix())
                    if clean_name.startswith("/") or ".." in Path(clean_name).parts:
                        continue
                    if clean_name.lower().endswith(".twb"):
                        twb_members.append(info)
                    else:
                        assets.append(
                            AssetInfo(
                                path=clean_name,
                                size_bytes=info.file_size,
                                kind=_classify_asset_kind(clean_name),
                            )
                        )
                if not twb_members:
                    raise ValueError("Archive .twbx does not contain a .twb workbook XML file.")
                main_member = sorted(twb_members, key=lambda m: m.file_size, reverse=True)[0]
                if main_member.file_size > MAX_TWB_XML_BYTES:
                    raise ValueError("Embedded .twb XML exceeds 80 MB safety limit.")
                xml_bytes = zf.read(main_member.filename)
                return xml_bytes.decode("utf-8", errors="replace"), "twbx", assets
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Invalid .twbx archive: {exc}") from exc

    if lower.endswith(".twb"):
        if len(raw_bytes) > MAX_TWB_XML_BYTES:
            raise ValueError("Workbook XML exceeds 80 MB safety limit.")
        return raw_bytes.decode("utf-8", errors="replace"), "twb", assets

    raise ValueError("Unsupported file extension. Upload a .twb or .twbx file.")


def _analyze_formula_flags(
    formula: str,
    calc_ref_tokens: list[str],
    all_calc_names: set[str],
) -> tuple[list[str], list[str], int]:
    """Inspect a Tableau calculation formula for performance and maintainability flags."""
    flags: list[str] = []
    upper = formula.upper()

    if re.search(r"\bELSE\s+IF\b", upper):
        flags.append("else_if_space")

    or_matches = re.findall(r"\bOR\b", upper)
    if len(or_matches) >= 2 and ("=" in formula):
        flags.append("chained_or")

    elseif_count = len(re.findall(r"\bELSEIF\b", upper))
    if elseif_count >= 2 and re.search(r"\bIF\s+\[[^\]]+\]\s*=", upper):
        flags.append("multi_if_to_case")

    if re.search(r"\bATTR\s*\(", upper):
        flags.append("uses_attr")
    if re.search(r"\bCOUNTD\s*\(", upper):
        flags.append("uses_countd")

    if re.search(r"\bNOW\s*\(\s*\)", upper):
        flags.append("uses_now")
    elif re.search(r"\bTODAY\s*\(\s*\)", upper):
        flags.append("uses_today")

    if re.search(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", upper):
        flags.append("uses_lod")

    if re.search(
        r"\b(WINDOW_\w+|RUNNING_\w+|LOOKUP|INDEX|FIRST|LAST|PREVIOUS_VALUE|RANK\w*|TOTAL)\s*\(",
        upper,
    ):
        flags.append("uses_table_calc")

    if re.search(r"\b(RAWSQL\w*|SCRIPT_\w+)\s*\(", upper):
        flags.append("uses_rawsql_or_script")

    string_fns = re.findall(
        r"\b(CONTAINS|STARTSWITH|ENDSWITH|FIND|MID|LEFT|RIGHT|SPLIT|REPLACE)\s*\(",
        upper,
    )
    if len(string_fns) >= 2 and "REGEXP_" not in upper:
        flags.append("chained_string_checks")

    if re.search(r"\bIF\b.+\bTHEN\s+TRUE\s+ELSE\s+FALSE\s+END\b", upper, flags=re.DOTALL):
        flags.append("redundant_if_bool")

    if re.search(r"\b(SUM|AVG|MIN|MAX|COUNT)\s*\(\s*(IF|CASE|IIF)\b", upper):
        flags.append("aggregate_wrapping_branch")

    if re.search(r'["\']\s*(▲|▼|↑|↓|UP|DOWN|POSITIVE|NEGATIVE)\s*["\']', upper):
        flags.append("kpi_arrow_string_calc")

    referenced: list[str] = []
    seen_counts: dict[str, int] = {}
    for token in calc_ref_tokens:
        clean = _strip_brackets(token)
        if clean in all_calc_names:
            seen_counts[clean] = seen_counts.get(clean, 0) + 1
            if clean not in referenced:
                referenced.append(clean)

    if any(count >= 2 for count in seen_counts.values()):
        flags.append("repeated_calc_call")

    weight_map = {
        "uses_rawsql_or_script": 30,
        "repeated_calc_call": 20,
        "uses_now": 18,
        "uses_lod": 16,
        "uses_countd": 15,
        "uses_attr": 14,
        "aggregate_wrapping_branch": 14,
        "chained_string_checks": 12,
        "else_if_space": 10,
        "multi_if_to_case": 10,
        "chained_or": 9,
        "uses_table_calc": 8,
        "uses_today": 7,
        "kpi_arrow_string_calc": 6,
        "redundant_if_bool": 4,
    }
    complexity = sum(weight_map.get(f, 5) for f in flags) + max(0, len(formula) // 120)
    return flags, referenced, complexity


def _compute_calc_nesting_depths(calcs: list[AuditCalculationInfo]) -> None:
    by_name = {c.name: c for c in calcs}
    by_caption = {c.caption: c for c in calcs if c.caption}
    memo: dict[str, int] = {}

    def depth(calc_key: str, visiting: set[str]) -> int:
        if calc_key in memo:
            return memo[calc_key]
        if calc_key in visiting:
            return 0
        calc = by_name.get(calc_key) or by_caption.get(calc_key)
        if not calc or not calc.referenced_calcs:
            memo[calc_key] = 0
            return 0
        visiting.add(calc_key)
        max_child = 0
        for ref in calc.referenced_calcs:
            if ref != calc.name and ref != calc.caption:
                max_child = max(max_child, 1 + depth(ref, visiting))
        visiting.remove(calc_key)
        memo[calc_key] = max_child
        return max_child

    for c in calcs:
        c.nesting_depth = depth(c.name, set())
        if c.nesting_depth >= 3 and "deep_calc_chain" not in c.flags:
            c.flags.append("deep_calc_chain")
            c.complexity_score += c.nesting_depth * 5


def _parse_datasources(root: ET.Element) -> list[AuditDatasourceInfo]:
    datasources: list[AuditDatasourceInfo] = []
    for ds_el in root.findall("./datasources/datasource"):
        ds_name = ds_el.get("name", "")
        ds_caption = ds_el.get("caption") or ds_name
        if ds_name.lower() == "parameters":
            continue

        conn_classes: list[str] = []
        is_published = False
        for conn in ds_el.findall(".//connection"):
            cls = conn.get("class")
            if cls and cls not in conn_classes:
                conn_classes.append(cls)
            if cls in {"sqlproxy", "tableau-server", "published"}:
                is_published = True

        if ds_el.find("./repository-location") is not None:
            is_published = True

        extract_el = ds_el.find("./extract")
        is_extract = extract_el is not None and extract_el.get("enabled", "true").lower() != "false"
        if any(c in {"hyper", "dataengine"} for c in conn_classes):
            is_extract = True

        custom_sql_els = ds_el.findall(".//relation[@type='text']")
        join_els = ds_el.findall(".//relation[@type='join']")
        table_els = ds_el.findall(".//relation[@type='table']")
        relationships = ds_el.findall(".//relationships/relationship")
        objects = ds_el.findall(".//objects/object")
        uses_relationships = len(relationships) > 0 or len(objects) > 1

        ref_integrity = ds_el.attrib.get("assume-referential-integrity", "").lower() == "true"
        for el in ds_el.findall(".//properties/property"):
            if el.get("name") == "referential-integrity" and el.get("value", "").lower() == "true":
                ref_integrity = True

        uses_agg_extract = False
        if extract_el is not None:
            if extract_el.get("aggregate-rollup", "").lower() == "true":
                uses_agg_extract = True
            for prop in extract_el.findall(".//*[@aggregate-rollup]"):
                if prop.get("aggregate-rollup", "").lower() == "true":
                    uses_agg_extract = True

        extract_filter_count = len(extract_el.findall("./filter")) if extract_el is not None else 0
        ds_filter_count = len(ds_el.findall("./filter"))

        columns = ds_el.findall("./column")
        hidden_count = sum(1 for col in columns if col.get("hidden", "").lower() == "true")

        datasources.append(
            AuditDatasourceInfo(
                name=ds_name,
                caption=ds_caption,
                is_published=is_published,
                is_extract=is_extract,
                connection_classes=conn_classes,
                has_custom_sql=len(custom_sql_els) > 0,
                custom_sql_count=len(custom_sql_els),
                uses_relationships=uses_relationships,
                physical_join_count=len(join_els),
                table_count=max(len(table_els), len(objects)),
                total_field_count=len(columns),
                hidden_field_count=hidden_count,
                extract_filter_count=extract_filter_count,
                datasource_filter_count=ds_filter_count,
                uses_aggregated_extract=uses_agg_extract,
                referential_integrity_assumed=ref_integrity,
            )
        )
    return datasources


def _collect_sheet_text_map(root: ET.Element) -> dict[str, str]:
    return {
        ws.get("name", "Unnamed Sheet"): ET.tostring(ws, encoding="unicode")
        for ws in root.findall("./worksheets/worksheet")
    }


def _parse_calculations(
    root: ET.Element, sheet_xml_map: dict[str, str]
) -> list[AuditCalculationInfo]:
    raw_calcs: list[tuple[str, str, ET.Element]] = []
    all_names: set[str] = set()

    for ds_el in root.findall("./datasources/datasource"):
        ds_name = ds_el.get("caption") or ds_el.get("name", "")
        if ds_el.get("name", "").lower() == "parameters":
            continue
        for col_el in ds_el.findall("./column"):
            calc_el = col_el.find("./calculation")
            if calc_el is None:
                continue
            formula = calc_el.get("formula")
            if not formula:
                continue
            col_name = _strip_brackets(col_el.get("name", ""))
            caption = col_el.get("caption") or col_name
            all_names.add(col_name)
            all_names.add(caption)
            raw_calcs.append((ds_name, col_name, col_el))

    dash_xml_text = "".join(
        ET.tostring(d, encoding="unicode") for d in root.findall("./dashboards/dashboard")
    )

    calcs: list[AuditCalculationInfo] = []
    for ds_name, col_name, col_el in raw_calcs:
        calc_el = col_el.find("./calculation")
        formula = (calc_el.get("formula") or "").strip()  # type: ignore[union-attr]
        caption = col_el.get("caption") or col_name
        tokens = re.findall(r"\[[^\]]+\]", formula)
        flags, referenced, complexity = _analyze_formula_flags(formula, tokens, all_names)

        search_keys = {f"[{col_name}]"}
        if caption and caption != col_name:
            search_keys.add(f"[{caption}]")

        used_sheets = [
            s_name for s_name, s_xml in sheet_xml_map.items() if any(k in s_xml for k in search_keys)
        ]
        used_downstream = any(
            other_col.get("name", "") != col_el.get("name", "")
            and any(
                k in (other_col.find("./calculation").get("formula") or "")  # type: ignore[union-attr]
                for k in search_keys
            )
            for _, _, other_col in raw_calcs
        )
        used_in_dash = any(k in dash_xml_text for k in search_keys)
        is_unused = not used_sheets and not used_downstream and not used_in_dash

        calcs.append(
            AuditCalculationInfo(
                name=col_name,
                caption=caption,
                datasource_name=ds_name,
                formula=formula,
                datatype=col_el.get("datatype", "unknown"),
                role=col_el.get("role", "unknown"),
                is_hidden=col_el.get("hidden", "").lower() == "true",
                is_unused=is_unused,
                referenced_calcs=referenced,
                used_in_sheets=used_sheets,
                flags=flags,
                complexity_score=complexity,
            )
        )

    _compute_calc_nesting_depths(calcs)
    return calcs


def _parse_worksheets_and_filters(
    root: ET.Element,
) -> tuple[list[AuditWorksheetInfo], list[AuditFilterInfo]]:
    worksheets: list[AuditWorksheetInfo] = []
    filters: list[AuditFilterInfo] = []

    for ws_el in root.findall("./worksheets/worksheet"):
        ws_name = ws_el.get("name", "Unnamed Sheet")
        table_el = ws_el.find("./table")

        ds_names: list[str] = []
        if table_el is not None:
            for dep in table_el.findall("./view/datasources/datasource"):
                dn = dep.get("caption") or dep.get("name", "")
                if dn and dn.lower() != "parameters" and dn not in ds_names:
                    ds_names.append(dn)

        uses_blending = len(ds_names) > 1
        row_pills = _split_shelf_pills(table_el.findtext("./rows") if table_el is not None else "")
        col_pills = _split_shelf_pills(table_el.findtext("./cols") if table_el is not None else "")

        mark_types: list[str] = []
        detail_pills: list[str] = []
        has_custom_tooltip = False
        tooltip_is_default = True
        tooltip_cmd_buttons = True
        has_viz_in_tooltip = False

        if table_el is not None:
            for mark_el in table_el.findall(".//panes/pane/mark"):
                cls = mark_el.get("class")
                if cls and cls not in mark_types:
                    mark_types.append(cls)

            for enc in table_el.findall(".//panes/pane/encodings/*"):
                if enc.tag in {"lod", "tooltip"} and enc.get("column"):
                    detail_pills.append(enc.get("column", ""))

            tt_el = table_el.find(".//customized-tooltip")
            if tt_el is not None:
                tt_text = "".join(tt_el.itertext()).strip()
                if tt_text:
                    has_custom_tooltip = True
                    tooltip_is_default = False
                if tt_el.get("show-command-buttons", "").lower() == "false":
                    tooltip_cmd_buttons = False
                if "<worksheet" in tt_text.lower() or tt_el.find(".//worksheet") is not None:
                    has_viz_in_tooltip = True

            for tt_style in table_el.findall(".//tooltip"):
                if tt_style.get("show-command-buttons", "").lower() == "false":
                    tooltip_cmd_buttons = False

            ws_filters = table_el.findall("./view/filter")
        else:
            ws_filters = []

        pages_el = table_el.find("./pages") if table_el is not None else None
        uses_pages = pages_el is not None and bool("".join(pages_el.itertext()).strip())

        for f_el in ws_filters:
            col = f_el.get("column", "unknown")
            f_cls = f_el.get("class", "categorical")
            is_context = (
                f_el.get("context", "").lower() == "true"
                or "context" in (f_el.get("filter-group") or "").lower()
            )
            only_rel = (
                f_el.get("relevant-values", "").lower() == "true"
                or f_el.get("show-relevant-values", "").lower() == "true"
            )
            gf = f_el.find(".//groupfilter[@function='top']")
            is_top_n = gf is not None or f_el.get("top", "").lower() == "true"
            col_upper = col.upper()
            volatile_or_my = any(
                k in col_upper for k in ("MY(", "MDY(", "NOW(", "TODAY(")
            ) or any(k in (f_el.get("level") or "").upper() for k in ("MY", "MDY"))
            filters.append(
                AuditFilterInfo(
                    field=col,
                    worksheet=ws_name,
                    filter_type=f_cls,
                    is_context=is_context,
                    uses_only_relevant_values=only_rel,
                    mode=f_el.get("mode", "default"),
                    is_top_n=is_top_n,
                    uses_date_part_string_or_volatile=volatile_or_my,
                )
            )

        title_el = ws_el.find("./layout-options/title")
        has_title = title_el is not None and title_el.get("visible", "true").lower() != "false"
        title_text = "".join(title_el.itertext()).strip() if title_el is not None else ws_name
        has_all_caps = bool(
            title_text
            and len(title_text) >= 4
            and title_text.upper() == title_text
            and any(c.isalpha() for c in title_text)
        )
        caption_el = ws_el.find("./layout-options/caption")
        has_caption = caption_el is not None and caption_el.get("visible", "true").lower() == "true"

        style_rules = table_el.findall("./style/style-rule") if table_el is not None else []
        fonts_used: list[str] = []
        gridline_or_border_count = 0
        custom_num_formats: list[str] = []
        for sr in style_rules:
            if sr.get("element", "") in {"gridline", "border", "table-div", "band", "cell"}:
                gridline_or_border_count += 1
            for fmt in sr.findall("./format"):
                attr = fmt.get("attr", "")
                val = fmt.get("value", "")
                if attr == "font-family" and val and val not in fonts_used:
                    fonts_used.append(val)
                if attr in {"gridline-style", "border-style"} and val != "none":
                    gridline_or_border_count += 1
                if attr == "number-format" and val:
                    custom_num_formats.append(val)

        uses_polygons = any(m.lower() == "polygon" for m in mark_types)
        uses_density = any(m.lower() in {"density", "hexbin"} for m in mark_types)
        is_text_table_heavy = (
            (any(m.lower() == "text" for m in mark_types) or "Automatic" in mark_types)
            and (
                "Measure Names" in "".join(row_pills + col_pills)
                or len(row_pills) + len(col_pills) >= 6
            )
            and any(
                "[Measure Values]" in p or "[Measure Names]" in p
                for p in row_pills + col_pills + detail_pills
            )
        ) or (
            any(m.lower() == "text" for m in mark_types)
            and (len(row_pills) + len(col_pills) >= 5)
        )

        worksheets.append(
            AuditWorksheetInfo(
                name=ws_name,
                datasource_names=ds_names,
                mark_types=mark_types or ["Automatic"],
                row_pills=row_pills,
                col_pills=col_pills,
                detail_pills=detail_pills,
                filter_count=len(ws_filters),
                uses_pages_shelf=uses_pages,
                uses_blending=uses_blending,
                uses_polygons=uses_polygons,
                is_text_table_heavy=is_text_table_heavy,
                uses_density_marks=uses_density,
                has_custom_tooltip=has_custom_tooltip,
                tooltip_is_default=tooltip_is_default,
                tooltip_has_command_buttons=tooltip_cmd_buttons,
                has_viz_in_tooltip=has_viz_in_tooltip,
                has_title=has_title,
                title_text=title_text,
                has_caption=has_caption,
                has_all_caps_title=has_all_caps,
                style_override_count=len(style_rules),
                fonts_used=fonts_used,
                explicit_gridline_or_border_count=gridline_or_border_count,
                custom_number_formats=custom_num_formats,
            )
        )

    return worksheets, filters


def _zone_tree_stats(zone_el: ET.Element, depth: int = 0) -> dict:
    z_type = (_fcp_attr(zone_el, "type-v2") or _fcp_attr(zone_el, "type") or "").lower()
    is_container = "layout" in z_type
    current_nesting = depth + (1 if is_container else 0)

    stats: dict = {
        "max_nesting": current_nesting,
        "worksheet_names": [],
        "filter_zones": 0,
        "only_rel_zones": 0,
        "multi_list_zones": 0,
        "param_zones": 0,
        "blank_spacers": 0,
        "floating_zones": 0,
        "show_hide_buttons": 0,
    }

    if zone_el.get("floating", "").lower() == "true":
        stats["floating_zones"] += 1

    name_attr = zone_el.get("name")
    param_attr = zone_el.get("param")

    if z_type in {"filter", "quickfilter"} or (
        param_attr and z_type not in {"paramctrl", "parameter"} and "filter" in z_type
    ):
        stats["filter_zones"] += 1
        mode = (zone_el.get("mode") or "").lower()
        if (
            zone_el.get("relevant-values", "").lower() == "true"
            or zone_el.get("show-relevant-values", "").lower() == "true"
        ):
            stats["only_rel_zones"] += 1
        if mode in {"checkdropdown", "checklist", "list"}:
            stats["multi_list_zones"] += 1
    elif z_type in {"paramctrl", "parameter"}:
        stats["param_zones"] += 1
    elif z_type in {"empty", "blank"}:
        stats["blank_spacers"] += 1
    elif z_type in {"toggle-zone", "show-hide-button"} or zone_el.get("toggle-zone"):
        stats["show_hide_buttons"] += 1
    elif name_attr and z_type in {"", "viz", "worksheet"}:
        stats["worksheet_names"].append(name_attr)

    for child in zone_el.findall("./zone"):
        c = _zone_tree_stats(child, current_nesting)
        stats["max_nesting"] = max(stats["max_nesting"], c["max_nesting"])
        for wn in c["worksheet_names"]:
            if wn not in stats["worksheet_names"]:
                stats["worksheet_names"].append(wn)
        for key in (
            "filter_zones",
            "only_rel_zones",
            "multi_list_zones",
            "param_zones",
            "blank_spacers",
            "floating_zones",
            "show_hide_buttons",
        ):
            stats[key] += c[key]

    return stats


def _parse_dashboards(
    root: ET.Element, worksheet_names_set: set[str]
) -> list[AuditDashboardInfo]:
    dashboards: list[AuditDashboardInfo] = []
    for dash_el in root.findall("./dashboards/dashboard"):
        d_name = dash_el.get("name", "Unnamed Dashboard")
        size_el = dash_el.find("./size")
        sizing_mode = "fixed"
        width: int | None = None
        height: int | None = None
        if size_el is not None:
            sizing_mode = size_el.get("sizing-mode", "fixed").lower()
            w_str = size_el.get("maxwidth") or size_el.get("minwidth")
            h_str = size_el.get("maxheight") or size_el.get("minheight")
            width = int(w_str) if w_str and w_str.isdigit() else None
            height = int(h_str) if h_str and h_str.isdigit() else None

        zones_root = dash_el.find("./zones")
        agg: dict = {
            "max_nesting": 0,
            "worksheet_names": [],
            "filter_zones": 0,
            "only_rel_zones": 0,
            "multi_list_zones": 0,
            "param_zones": 0,
            "blank_spacers": 0,
            "floating_zones": 0,
            "show_hide_buttons": 0,
        }
        if zones_root is not None:
            for z in zones_root.findall("./zone"):
                st = _zone_tree_stats(z, 0)
                agg["max_nesting"] = max(agg["max_nesting"], st["max_nesting"])
                for wn in st["worksheet_names"]:
                    if wn in worksheet_names_set and wn not in agg["worksheet_names"]:
                        agg["worksheet_names"].append(wn)
                for key in (
                    "filter_zones",
                    "only_rel_zones",
                    "multi_list_zones",
                    "param_zones",
                    "blank_spacers",
                    "floating_zones",
                    "show_hide_buttons",
                ):
                    agg[key] += st[key]

        device_layouts = dash_el.findall("./devicelayouts/devicelayout")
        actions = dash_el.findall(".//actions/action") + root.findall("./actions/action")
        exclude_on_clear = 0
        for act in actions:
            cmd = act.find("./command")
            if act.get("on-exit", "").lower() == "exclude" or (
                cmd is not None and cmd.get("on-exit", "").lower() == "exclude"
            ):
                exclude_on_clear += 1

        dashboards.append(
            AuditDashboardInfo(
                name=d_name,
                sizing_mode=sizing_mode,
                width=width,
                height=height,
                worksheet_zone_count=len(agg["worksheet_names"]),
                worksheet_names=agg["worksheet_names"],
                filter_zone_count=agg["filter_zones"],
                only_relevant_values_zone_count=agg["only_rel_zones"],
                multi_value_list_zone_count=agg["multi_list_zones"],
                parameter_zone_count=agg["param_zones"],
                max_container_nesting=agg["max_nesting"],
                device_layout_count=len(device_layouts),
                has_show_hide_button=agg["show_hide_buttons"] > 0,
                action_count=len(actions),
                action_exclude_on_clear_count=exclude_on_clear,
                blank_spacer_count=agg["blank_spacers"],
                floating_zone_count=agg["floating_zones"],
            )
        )
    return dashboards


def _parse_workbook_style(root: ET.Element) -> WorkbookStyleInfo:
    show_tabs = False
    for pref in root.findall(".//preferences/preference"):
        if (
            pref.get("name", "").lower() in {"ui.show-tabs", "show-tabs"}
            and pref.get("value", "").lower() == "true"
        ):
            show_tabs = True
    for win in root.findall("./windows/window"):
        if win.get("tabs", "").lower() == "true":
            show_tabs = True

    wb_fonts: list[str] = []
    style_rules = root.findall("./style/style-rule")
    for sr in style_rules:
        for fmt in sr.findall("./format"):
            if fmt.get("attr") == "font-family":
                val = fmt.get("value", "")
                if val and val not in wb_fonts:
                    wb_fonts.append(val)

    palettes = [
        p.get("name", "Custom")
        for p in root.findall(".//color-palette")
        if p.get("name")
    ]
    return WorkbookStyleInfo(
        show_tabs=show_tabs,
        workbook_fonts=wb_fonts,
        custom_palettes=palettes,
        style_rule_count=len(style_rules),
    )


def _detect_performance_recording(
    root: ET.Element,
    worksheets: list[AuditWorksheetInfo],
    dashboards: list[AuditDashboardInfo],
) -> PerformanceRecordingInfo:
    ws_names = {w.name.lower() for w in worksheets}
    d_names = {d.name.lower() for d in dashboards}
    is_perf_rec = "performance summary" in d_names or {"timeline", "events", "query"}.issubset(
        ws_names
    )
    if not is_perf_rec:
        return PerformanceRecordingInfo(detected=False)

    known_events = [
        "Executing query",
        "Compiling query",
        "Computing layouts",
        "Connecting to data source",
        "Blending data",
        "Geocoding",
        "Generating extract",
        "Server rendering",
    ]
    xml_text = ET.tostring(root, encoding="unicode")
    events = [
        PerfEventSummary(event_name=ev, count=xml_text.count(ev))
        for ev in known_events
        if xml_text.count(ev) > 0
    ]

    return PerformanceRecordingInfo(
        detected=True,
        notes=(
            "Tableau Performance Recording workbook detected (Performance Summary / Timeline / Events / Query). "
            "If backed by an external .hyper/.tde extract, open inside Tableau Desktop or inspect PerformanceSummary event durations."
        ),
        events=events,
    )


def parse_workbook_telemetry(raw_bytes: bytes, filename: str) -> WorkbookAuditReport:
    """Parse .twb or .twbx bytes into a WorkbookAuditReport before heuristic scoring."""
    xml_text, file_type, assets = extract_twb_xml_and_assets(raw_bytes, filename)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Malformed Tableau XML in {filename}: {exc}") from exc

    if root.tag.lower() != "workbook":
        raise ValueError(f"Expected root <workbook> XML tag, got <{root.tag}>.")

    version = root.get("version") or root.get("source-build") or "unknown"
    datasources = _parse_datasources(root)
    sheet_xml_map = _collect_sheet_text_map(root)
    calculations = _parse_calculations(root, sheet_xml_map)
    worksheets, filters = _parse_worksheets_and_filters(root)
    ws_name_set = {w.name for w in worksheets}
    dashboards = _parse_dashboards(root, ws_name_set)
    wb_style = _parse_workbook_style(root)
    perf_rec = _detect_performance_recording(root, worksheets, dashboards)

    unused_calc_count = sum(1 for c in calculations if c.is_unused)
    for ds in datasources:
        ds.unused_field_count = sum(
            1 for c in calculations if c.datasource_name == ds.caption and c.is_unused
        )

    flagged_calcs = sum(1 for c in calculations if c.flags)

    return WorkbookAuditReport(
        filename=filename,
        file_type=file_type,
        tableau_version=version,
        file_size_bytes=len(raw_bytes),
        summary_stats={
            "datasource_count": len(datasources),
            "calculation_count": len(calculations),
            "flagged_calculation_count": flagged_calcs,
            "unused_calculation_count": unused_calc_count,
            "worksheet_count": len(worksheets),
            "dashboard_count": len(dashboards),
            "filter_count": len(filters),
            "twbx_asset_count": len(assets),
        },
        workbook_style=wb_style,
        datasources=datasources,
        calculations=calculations,
        filters=filters,
        worksheets=worksheets,
        dashboards=dashboards,
        assets=assets,
        performance_recording=perf_rec,
    )
