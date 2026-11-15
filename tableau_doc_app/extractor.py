"""Deep Tableau workbook (.twb / .twbx) metadata extractor built on tableaudocumentapi + lxml."""

from __future__ import annotations

import base64
import os
import re
import zipfile
from typing import Any, Optional

from lxml import etree as ET
from tableaudocumentapi import Datasource, Workbook

from tableau_doc_app.models import (
    ActionInfo,
    ConnectionInfo,
    DashboardInfo,
    DatasourceInfo,
    EncodingInfo,
    FieldInfo,
    FilterInfo,
    ParameterInfo,
    RelationshipLinkInfo,
    TableRelationInfo,
    ThumbnailInfo,
    WorkbookSnapshot,
    WorksheetInfo,
    ZoneInfo,
)

# Tableau shelf derivation codes mapped to readable function names
_DERIVATION_MAP = {
    "none": "",
    "usr": "",
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "cnt": "COUNT",
    "ctd": "COUNTD",
    "med": "MEDIAN",
    "stdev": "STDEV",
    "var": "VAR",
    "attr": "ATTR",
    "yr": "YEAR",
    "qr": "QUARTER",
    "mn": "MONTH",
    "wk": "WEEK",
    "dy": "DAY",
    "tyr": "YEAR(trunc)",
    "tqr": "QUARTER(trunc)",
    "tmn": "MONTH(trunc)",
    "twk": "WEEK(trunc)",
    "tdy": "DAY(trunc)",
    "md": "MONTH/DAY",
    "my": "MONTH/YEAR",
    "io": "IN/OUT",
    "pcto": "% of Total",
    "win": "Window Calc",
}

_ACTION_COMMAND_MAP = {
    "tsc:brush": "Highlight",
    "tsc:tsl-filter": "Filter",
    "tsc:url": "URL",
    "tsc:navigate": "Navigate",
}

_TOKEN_RE = re.compile(
    r"\[([^\]]+)\]\.(?:\[__tableau_internal_object_id__\]\.)?\[([^\]]+)\]"
)
_BRACKET_ID_RE = re.compile(r"\[([^\]]+)\]")


def _local_tag(elem: ET._Element) -> str:
    """Return tag name stripped of XML namespaces and Tableau FCP feature prefixes."""
    tag = str(elem.tag)
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    if "..." in tag and tag.startswith("_.fcp."):
        tag = tag.split("...", 1)[1]
    return tag


def _get_fcp_attr(elem: ET._Element, key: str, default: Optional[str] = None) -> Optional[str]:
    """Read an attribute directly or via Tableau's _.fcp.<Feature>.<bool>...<key> prefix."""
    if key in elem.attrib:
        return elem.attrib[key]
    suffix = f"...{key}"
    for attr_name, val in elem.attrib.items():
        if attr_name.endswith(suffix):
            return val
    return default


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_formatted_text(elem: Optional[ET._Element]) -> str:
    """Extract readable text from a Tableau <formatted-text> element."""
    if elem is None:
        return ""
    raw = "".join(elem.itertext())
    # Tableau uses 'Æ' as an inline soft-break marker inside formatted-text <run> tags
    cleaned = raw.replace("Æ\r\n", "\n").replace("Æ\n", "\n").replace("Æ", " ")
    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _clean_description_xml(desc_str: Optional[str]) -> Optional[str]:
    if not desc_str:
        return None
    try:
        elem = ET.fromstring(desc_str.encode("utf-8"))
        text = _clean_formatted_text(elem)
        return text or None
    except ET.XMLSyntaxError:
        text = re.sub(r"<[^>]+>", " ", desc_str)
        return " ".join(text.split()) or None


def _strip_brackets(name: str) -> str:
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


def _decode_column_instance(
    token: str,
    ds_name: Optional[str],
    field_lookup: dict[tuple[str, str], str],
    global_field_lookup: dict[str, str],
) -> str:
    """Turn a Tableau column-instance token like 'sum:Sales:qk' into 'SUM(Sales)'."""
    inner = _strip_brackets(token)
    if inner == ":Measure Names":
        return "Measure Names"
    if inner == "Multiple Values":
        return "Measure Values"

    parts = inner.split(":")
    if len(parts) >= 3 and parts[0] in _DERIVATION_MAP:
        deriv = parts[0]
        raw_field = ":".join(parts[1:-1])
        bracketed = f"[{raw_field}]"
        # Internal object count tokens look like Orders_6D2EF74...
        display = (
            (field_lookup.get((ds_name, bracketed)) if ds_name else None)
            or global_field_lookup.get(bracketed)
            or raw_field
        )
        # Clean Tableau internal table hash suffixes like Orders_6D2EF74F348B46...
        if re.match(r"^[A-Za-z0-9_ ]+_[0-9A-Fa-f]{32}$", display):
            display = display.rsplit("_", 1)[0]
        prefix = _DERIVATION_MAP[deriv]
        return f"{prefix}({display})" if prefix else display

    bracketed = f"[{inner}]"
    return (
        (field_lookup.get((ds_name, bracketed)) if ds_name else None)
        or global_field_lookup.get(bracketed)
        or inner
    )


def _resolve_expression(
    expr: Optional[str],
    field_lookup: dict[tuple[str, str], str],
    global_field_lookup: dict[str, str],
    ds_captions: dict[str, str],
) -> Optional[str]:
    """Resolve [datasource].[derivation:field:role] references in shelves and tooltips."""
    if not expr:
        return expr

    def _repl(match: re.Match[str]) -> str:
        ds_id = match.group(1)
        col_tok = match.group(2)
        readable = _decode_column_instance(
            col_tok, ds_id, field_lookup, global_field_lookup
        )
        if ds_id == "Parameters":
            return f"Param:{readable}"
        return readable

    resolved = _TOKEN_RE.sub(_repl, expr)
    # Clean Tableau shelf operators (* for cross, / for nest, + for union)
    resolved = re.sub(r"\s+", " ", resolved).strip()
    return resolved


def _resolve_formula(
    formula: Optional[str],
    ds_name: str,
    field_lookup: dict[tuple[str, str], str],
    global_field_lookup: dict[str, str],
) -> Optional[str]:
    """Replace internal [Calculation_xxx] & [Parameters].[Parameter x] IDs in formulas."""
    if not formula:
        return formula

    # First replace [Parameters].[Parameter X]
    def _param_repl(match: re.Match[str]) -> str:
        p_id = f"[{match.group(1)}]"
        caption = field_lookup.get(("Parameters", p_id)) or global_field_lookup.get(p_id)
        if caption and caption != match.group(1):
            return f"[Parameters].[{caption}]"
        return match.group(0)

    out = re.sub(r"\[Parameters\]\.\[([^\]]+)\]", _param_repl, formula)

    # Next replace internal calculation IDs like [Calculation_0831103151444568]
    def _field_repl(match: re.Match[str]) -> str:
        raw_inner = match.group(1)
        bracketed = f"[{raw_inner}]"
        caption = field_lookup.get((ds_name, bracketed)) or global_field_lookup.get(
            bracketed
        )
        if caption and caption != raw_inner and raw_inner.startswith("Calculation_"):
            return f"[{caption}]"
        return match.group(0)

    return _BRACKET_ID_RE.sub(_field_repl, out)


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _safe_mermaid_id(prefix: str, text: str, index: int) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")[:28]
    return f"{prefix}_{index}_{slug or 'node'}"


def _escape_mermaid_label(text: str) -> str:
    cleaned = text.replace('"', "'").replace("\n", " ").strip()
    return cleaned[:48] + ("..." if len(cleaned) > 48 else "")


def build_deterministic_mermaid(
    snapshot: WorkbookSnapshot, target_dashboard: Optional[str] = None
) -> str:
    """Build a reliable Mermaid flowchart LR showing workbook lineage."""
    lines = ["flowchart LR"]

    dashboards = snapshot.dashboards
    if target_dashboard:
        filtered = [d for d in dashboards if d.name == target_dashboard]
        if filtered:
            dashboards = filtered

    active_sheets: set[str] = set()
    for d in dashboards:
        active_sheets.update(d.worksheets)
    if not dashboards:
        active_sheets = {w.name for w in snapshot.worksheets}

    ds_nodes: dict[str, str] = {}
    calc_nodes: dict[str, str] = {}
    ws_nodes: dict[str, str] = {}
    db_nodes: dict[str, str] = {}

    non_param_ds = [ds for ds in snapshot.datasources if not ds.is_parameters]
    for i, ds in enumerate(non_param_ds):
        nid = _safe_mermaid_id("DS", ds.caption or ds.name, i)
        ds_nodes[ds.name] = nid
        conn_desc = ds.connections[0].dbclass if ds.connections and ds.connections[0].dbclass else "Data"
        label = _escape_mermaid_label(f"{ds.caption or ds.name} ({conn_desc})")
        lines.append(f'    {nid}[("{label}")]')

    # Limit displayed calculated fields per lineage graph to keep diagram legible
    calc_index = 0
    for ds in non_param_ds:
        ds_nid = ds_nodes[ds.name]
        for cf in ds.calculated_fields[:12]:
            if active_sheets and cf.worksheets and not (set(cf.worksheets) & active_sheets):
                continue
            cnid = _safe_mermaid_id("CALC", cf.name, calc_index)
            calc_index += 1
            calc_nodes[f"{ds.name}::{cf.name}"] = cnid
            lines.append(f'    {cnid}{{"{_escape_mermaid_label(cf.name)}"}}')
            lines.append(f"    {ds_nid} --> {cnid}")

    ws_map = {w.name: w for w in snapshot.worksheets}
    ws_list = [w for w in snapshot.worksheets if not active_sheets or w.name in active_sheets]
    for i, ws in enumerate(ws_list[:30]):
        wnid = _safe_mermaid_id("WS", ws.name, i)
        ws_nodes[ws.name] = wnid
        mark = ws.mark_types[0] if ws.mark_types else "View"
        lines.append(f'    {wnid}["{_escape_mermaid_label(ws.name)} ({mark})"]')

        linked_from_calc = False
        for ds in non_param_ds:
            for cf in ds.calculated_fields[:12]:
                key = f"{ds.name}::{cf.name}"
                if key in calc_nodes and ws.name in cf.worksheets:
                    lines.append(f"    {calc_nodes[key]} --> {wnid}")
                    linked_from_calc = True
        if not linked_from_calc:
            for ds_caption in ws.datasources:
                matched_ds = next(
                    (d.name for d in non_param_ds if d.caption == ds_caption or d.name == ds_caption),
                    None,
                )
                if matched_ds and matched_ds in ds_nodes:
                    lines.append(f"    {ds_nodes[matched_ds]} --> {wnid}")

    for i, db in enumerate(dashboards):
        dnid = _safe_mermaid_id("DB", db.name, i)
        db_nodes[db.name] = dnid
        lines.append(f'    {dnid}[["Dashboard: {_escape_mermaid_label(db.name)}"]]')
        for sname in db.worksheets:
            if sname in ws_nodes:
                lines.append(f"    {ws_nodes[sname]} --> {dnid}")
            elif sname in ws_map:
                wnid = _safe_mermaid_id("WS", sname, len(ws_nodes))
                ws_nodes[sname] = wnid
                lines.append(f'    {wnid}["{_escape_mermaid_label(sname)}"]')
                lines.append(f"    {wnid} --> {dnid}")

    if len(lines) == 1:
        lines.append('    EMPTY["No worksheets or datasources found"]')

    return "\n".join(lines)


def _extract_parameters(root: ET._Element) -> list[ParameterInfo]:
    params: list[ParameterInfo] = []
    for ds_elem in root.findall("./datasources/datasource"):
        if ds_elem.get("name") != "Parameters":
            continue
        for col in ds_elem.findall("./column"):
            col_id = col.get("name", "")
            caption = col.get("caption") or _strip_brackets(col_id)
            rng = col.find("./range")
            members = col.findall("./members/member")
            allowable = [
                m.get("alias") or m.get("value", "")
                for m in members
                if (m.get("alias") or m.get("value"))
            ]
            params.append(
                ParameterInfo(
                    id=col_id,
                    name=caption,
                    datatype=col.get("datatype"),
                    domain_type=col.get("param-domain-type"),
                    current_value=col.get("value"),
                    min_value=rng.get("min") if rng is not None else None,
                    max_value=rng.get("max") if rng is not None else None,
                    step_size=rng.get("granularity") if rng is not None else None,
                    allowable_values=allowable,
                )
            )
    return params


def _extract_datasources(
    wb: Workbook,
    root: ET._Element,
    field_lookup: dict[tuple[str, str], str],
    global_field_lookup: dict[str, str],
) -> list[DatasourceInfo]:
    datasources: list[DatasourceInfo] = []

    # Map datasource XML elements by name for supplemental parsing (extracts, relations, object-graph)
    ds_xml_by_name: dict[str, ET._Element] = {}
    for ds_elem in root.findall("./datasources/datasource"):
        key = ds_elem.get("name") or ds_elem.get("formatted-name") or ""
        ds_xml_by_name[key] = ds_elem

    for ds in wb.datasources:
        ds_name = ds.name or ds.caption or "Unnamed Datasource"
        is_params = ds_name == "Parameters"
        ds_elem = ds_xml_by_name.get(ds_name)

        connections: list[ConnectionInfo] = []
        for conn in ds.connections:
            cxml = getattr(conn, "_connectionXML", None)
            connections.append(
                ConnectionInfo(
                    dbclass=conn.dbclass,
                    server=conn.server,
                    port=conn.port,
                    dbname=conn.dbname,
                    schema_name=conn.schema,
                    service=conn.service,
                    username=conn.username,
                    authentication=conn.authentication,
                    filename=cxml.get("filename") if cxml is not None else None,
                    initial_sql=conn.initial_sql or None,
                )
            )

        relations: list[TableRelationInfo] = []
        logical_tables: list[str] = []
        relationships: list[RelationshipLinkInfo] = []
        has_extract = False

        if ds_elem is not None:
            has_extract = ds_elem.find("./extract") is not None
            seen_rels: set[tuple[Optional[str], Optional[str], Optional[str]]] = set()
            for elem in ds_elem.iter():
                ltag = _local_tag(elem)
                if ltag == "relation":
                    rname = elem.get("name")
                    rtype = elem.get("type")
                    rtable = elem.get("table")
                    rconn = elem.get("connection")
                    sql_text = (elem.text or "").strip() if rtype == "text" else None
                    sig = (rname, rtype, rtable)
                    if rtype == "join":
                        continue
                    if sig not in seen_rels and (rname or rtable or sql_text):
                        seen_rels.add(sig)
                        relations.append(
                            TableRelationInfo(
                                name=rname,
                                relation_type=rtype,
                                table=rtable,
                                connection=rconn,
                                custom_sql=sql_text or None,
                            )
                        )
                elif ltag == "object":
                    obj_name = elem.get("caption") or elem.get("id")
                    if obj_name and obj_name not in logical_tables:
                        logical_tables.append(obj_name)
                elif ltag == "relationship":
                    first_ep = elem.find("./first-end-point")
                    second_ep = elem.find("./second-end-point")
                    expr_elem = elem.find("./expression")
                    if first_ep is not None and second_ep is not None:
                        expr_parts = [
                            e.get("op", "")
                            for e in (expr_elem.findall(".//expression") if expr_elem is not None else [])
                            if e.get("op")
                        ]
                        op = expr_elem.get("op", "=") if expr_elem is not None else "="
                        expr_str = (
                            f"{expr_parts[0]} {op} {expr_parts[1]}"
                            if len(expr_parts) >= 2
                            else op
                        )
                        relationships.append(
                            RelationshipLinkInfo(
                                left_object=first_ep.get("object-id", "").rsplit("_", 1)[0],
                                right_object=second_ep.get("object-id", "").rsplit("_", 1)[0],
                                expression=expr_str,
                            )
                        )

        fields_list: list[FieldInfo] = []
        calc_fields_list: list[FieldInfo] = []
        for field in ds.fields.values():
            # Skip internal Tableau object count synthetic columns unless calculated
            if field.id.startswith("[__tableau_internal_object_id__]") and not field.calculation:
                continue
            hidden_flag = (
                field.hidden.lower() == "true"
                if isinstance(field.hidden, str)
                else None
            )
            readable_calc = _resolve_formula(
                field.calculation, ds_name, field_lookup, global_field_lookup
            )
            finfo = FieldInfo(
                id=field.id,
                name=field.name,
                caption=field.caption,
                alias=field.alias,
                datatype=field.datatype,
                role=field.role,
                field_type=field.type,
                default_aggregation=field.default_aggregation,
                calculation=field.calculation,
                readable_calculation=readable_calc,
                description=_clean_description_xml(field.description),
                hidden=hidden_flag,
                worksheets=sorted(field.worksheets),
            )
            fields_list.append(finfo)
            if finfo.calculation:
                calc_fields_list.append(finfo)

        fields_list.sort(key=lambda f: f.name.lower())
        calc_fields_list.sort(key=lambda f: f.name.lower())

        datasources.append(
            DatasourceInfo(
                name=ds_name,
                caption=ds.caption or ds_name,
                version=ds.version,
                is_parameters=is_params,
                connections=connections,
                relations=relations,
                logical_tables=logical_tables,
                relationships=relationships,
                fields=fields_list,
                calculated_fields=calc_fields_list,
                has_extract=has_extract,
            )
        )

    return datasources


def _summarize_filter(
    flt: ET._Element,
    ds_captions: dict[str, str],
    field_lookup: dict[tuple[str, str], str],
    global_field_lookup: dict[str, str],
) -> FilterInfo:
    col_raw = flt.get("column", "")
    col_readable = _resolve_expression(col_raw, field_lookup, global_field_lookup, ds_captions) or col_raw
    fclass = flt.get("class")

    summary_parts: list[str] = []
    min_el = flt.find("./min")
    max_el = flt.find("./max")
    if min_el is not None or max_el is not None:
        min_v = min_el.text if min_el is not None and min_el.text else "-∞"
        max_v = max_el.text if max_el is not None and max_el.text else "+∞"
        summary_parts.append(f"Range: {min_v} to {max_v}")
    else:
        members = [
            m.get("member", "").strip('"')
            for m in flt.findall(".//groupfilter[@function='member']")
            if m.get("member") is not None
        ]
        if members:
            preview = ", ".join(members[:5])
            extra = f" (+{len(members) - 5} more)" if len(members) > 5 else ""
            summary_parts.append(f"Members: {preview}{extra}")
        else:
            gf = flt.find(".//groupfilter")
            if gf is not None and gf.get("user:ui-enumeration") == "all":
                summary_parts.append("All values")
            elif gf is not None and gf.get("function"):
                summary_parts.append(f"Mode: {gf.get('function')}")

    if flt.get("included-values"):
        summary_parts.append(flt.get("included-values", ""))

    return FilterInfo(
        filter_class=fclass,
        column_raw=col_raw,
        column_readable=col_readable,
        summary="; ".join(p for p in summary_parts if p) or (fclass or "filter"),
    )


def _extract_worksheets(
    root: ET._Element,
    ds_captions: dict[str, str],
    field_lookup: dict[tuple[str, str], str],
    global_field_lookup: dict[str, str],
) -> list[WorksheetInfo]:
    worksheets: list[WorksheetInfo] = []

    for ws_elem in root.findall("./worksheets/worksheet"):
        ws_name = ws_elem.get("name", "Unnamed Worksheet")

        title_elem = ws_elem.find("./layout-options/title/formatted-text")
        title_raw = _clean_formatted_text(title_elem)
        title = (
            _resolve_expression(title_raw, field_lookup, global_field_lookup, ds_captions)
            if title_raw
            else None
        )

        ws_ds_names: list[str] = []
        for ds_ref in ws_elem.findall(".//table/view/datasources/datasource"):
            ref_name = ds_ref.get("name", "")
            if ref_name == "Parameters":
                continue
            caption = ds_ref.get("caption") or ds_captions.get(ref_name) or ref_name
            if caption and caption not in ws_ds_names:
                ws_ds_names.append(caption)

        mark_types: list[str] = []
        encodings: list[EncodingInfo] = []
        seen_enc: set[tuple[str, str]] = set()
        for pane in ws_elem.findall(".//panes/pane"):
            mark = pane.find("./mark")
            if mark is not None and mark.get("class"):
                mclass = mark.get("class", "")
                if mclass not in mark_types:
                    mark_types.append(mclass)
            for enc in pane.findall("./encodings/*"):
                col_raw = enc.get("column", "")
                if not col_raw:
                    continue
                sig = (enc.tag, col_raw)
                if sig in seen_enc:
                    continue
                seen_enc.add(sig)
                col_read = (
                    _resolve_expression(col_raw, field_lookup, global_field_lookup, ds_captions)
                    or col_raw
                )
                encodings.append(
                    EncodingInfo(
                        channel=enc.tag,
                        column_raw=col_raw,
                        column_readable=col_read,
                    )
                )

        rows_raw = ws_elem.findtext(".//table/rows")
        cols_raw = ws_elem.findtext(".//table/cols")
        rows_readable = _resolve_expression(rows_raw, field_lookup, global_field_lookup, ds_captions)
        cols_readable = _resolve_expression(cols_raw, field_lookup, global_field_lookup, ds_captions)

        filters = [
            _summarize_filter(flt, ds_captions, field_lookup, global_field_lookup)
            for flt in ws_elem.findall(".//table/view/filter")
        ]

        sorts: list[str] = []
        for srt in ws_elem.findall(".//table/view/sort"):
            col_read = _resolve_expression(
                srt.get("column"), field_lookup, global_field_lookup, ds_captions
            )
            using_read = _resolve_expression(
                srt.get("using"), field_lookup, global_field_lookup, ds_captions
            )
            direction = srt.get("direction", "ASC")
            if col_read:
                desc = f"{col_read} ({direction})"
                if using_read:
                    desc += f" by {using_read}"
                sorts.append(desc)

        tip_elem = ws_elem.find(".//customized-tooltip/formatted-text")
        tip_raw = _clean_formatted_text(tip_elem)
        tooltip_text = (
            _resolve_expression(tip_raw, field_lookup, global_field_lookup, ds_captions)
            if tip_raw
            else None
        )

        ref_fields: list[str] = []
        ref_calcs: list[str] = []
        for dep in ws_elem.findall(".//datasource-dependencies"):
            dep_ds = dep.get("datasource", "")
            for col in dep.findall("./column"):
                cid = col.get("name", "")
                caption = (
                    col.get("caption")
                    or field_lookup.get((dep_ds, cid))
                    or global_field_lookup.get(cid)
                    or _strip_brackets(cid)
                )
                if caption not in ref_fields:
                    ref_fields.append(caption)
                if col.find("./calculation") is not None or cid.startswith("[Calculation_"):
                    if caption not in ref_calcs:
                        ref_calcs.append(caption)

        worksheets.append(
            WorksheetInfo(
                name=ws_name,
                title=title,
                datasources=ws_ds_names,
                mark_types=mark_types,
                rows_raw=rows_raw.strip() if rows_raw else None,
                cols_raw=cols_raw.strip() if cols_raw else None,
                rows_readable=rows_readable,
                cols_readable=cols_readable,
                encodings=encodings,
                filters=filters,
                sorts=sorts,
                tooltip_text=tooltip_text,
                referenced_fields=ref_fields,
                referenced_calculations=ref_calcs,
            )
        )

    return worksheets


def _extract_dashboards(
    root: ET._Element,
    ds_captions: dict[str, str],
    field_lookup: dict[tuple[str, str], str],
    global_field_lookup: dict[str, str],
    thumbnail_names: set[str],
) -> list[DashboardInfo]:
    dashboards: list[DashboardInfo] = []

    for db_elem in root.findall("./dashboards/dashboard"):
        db_name = db_elem.get("name", "Unnamed Dashboard")
        size_elem = db_elem.find("./size")

        zones: list[ZoneInfo] = []
        worksheets: list[str] = []
        exposed_filters: list[str] = []
        exposed_parameters: list[str] = []
        text_blocks: list[str] = []

        # Parse ONLY direct ./zones (desktop/default layout) to avoid duplicating device-layout clones
        zones_root = db_elem.find("./zones")
        if zones_root is not None:
            for z in zones_root.findall(".//zone"):
                ztype = (
                    _get_fcp_attr(z, "type-v2")
                    or _get_fcp_attr(z, "type")
                    or ("worksheet" if z.get("name") else "container")
                )
                zname = z.get("name")
                zparam = z.get("param")
                zparam_read = (
                    _resolve_expression(zparam, field_lookup, global_field_lookup, ds_captions)
                    if zparam
                    else None
                )
                ft_text = _clean_formatted_text(z.find("./formatted-text")) or None

                if ztype in ("worksheet", "layout-flow", "layout-basic") and zname and ztype == "worksheet":
                    worksheets.append(zname)
                elif zname and ztype not in (
                    "filter",
                    "paramctrl",
                    "color",
                    "size",
                    "shape",
                    "map",
                    "legend",
                    "title",
                    "text",
                    "empty",
                    "bitmap",
                    "web",
                    "layout-basic",
                    "layout-flow",
                ):
                    worksheets.append(zname)

                if ztype == "filter":
                    label = zparam_read or zparam or ""
                    if zname:
                        label = f"{label} (on {zname})" if label else zname
                    if label:
                        exposed_filters.append(label)
                elif ztype == "paramctrl":
                    label = zparam_read or zparam or ""
                    if z.get("mode"):
                        label = f"{label} [{z.get('mode')}]"
                    if label:
                        exposed_parameters.append(label)
                elif ztype in ("text", "title") and ft_text:
                    text_blocks.append(ft_text)

                zones.append(
                    ZoneInfo(
                        zone_id=z.get("id"),
                        zone_type=ztype,
                        name=zname,
                        param=zparam,
                        param_readable=zparam_read,
                        mode=z.get("mode"),
                        text_content=ft_text,
                        x=_parse_int(z.get("x")),
                        y=_parse_int(z.get("y")),
                        w=_parse_int(z.get("w")),
                        h=_parse_int(z.get("h")),
                    )
                )

        device_layouts = [
            dl.get("name", "Device")
            for dl in db_elem.findall("./devicelayouts/devicelayout")
        ]

        dashboards.append(
            DashboardInfo(
                name=db_name,
                min_width=_parse_int(size_elem.get("minwidth")) if size_elem is not None else None,
                min_height=_parse_int(size_elem.get("minheight")) if size_elem is not None else None,
                max_width=_parse_int(size_elem.get("maxwidth")) if size_elem is not None else None,
                max_height=_parse_int(size_elem.get("maxheight")) if size_elem is not None else None,
                worksheets=_dedupe_preserve(worksheets),
                exposed_filters=_dedupe_preserve(exposed_filters),
                exposed_parameters=_dedupe_preserve(exposed_parameters),
                text_blocks=_dedupe_preserve(text_blocks),
                device_layouts=_dedupe_preserve(device_layouts),
                zones=zones,
                has_thumbnail=db_name in thumbnail_names,
            )
        )

    return dashboards


def _extract_actions(
    root: ET._Element,
    field_lookup: dict[tuple[str, str], str],
    global_field_lookup: dict[str, str],
    ds_captions: dict[str, str],
) -> list[ActionInfo]:
    actions: list[ActionInfo] = []
    actions_elem = root.find("./actions")
    if actions_elem is None:
        return actions

    for act in actions_elem:
        tag = _local_tag(act)
        if tag in ("datasources", "datasource-dependencies"):
            continue

        caption = act.get("caption") or act.get("name") or tag
        name = act.get("name") or caption
        activation = act.find("./activation")
        trigger = activation.get("type") if activation is not None else None
        source = act.find("./source")
        src_dashboard = source.get("dashboard") if source is not None else None
        src_worksheet = source.get("worksheet") if source is not None else None

        cmd = act.find("./command")
        cmd_name = cmd.get("command", "") if cmd is not None else ""
        if tag == "edit-group-action":
            action_type = "Set Action"
        elif tag == "edit-parameter-action":
            action_type = "Parameter Action"
        elif tag == "navigate-action":
            action_type = "Go to Sheet"
        elif tag == "url-action":
            action_type = "URL"
        else:
            action_type = _ACTION_COMMAND_MAP.get(cmd_name, cmd_name or tag)

        params = {p.get("name", ""): p.get("value", "") for p in act.findall(".//param")}
        target_scope = params.get("target")
        excluded = [s.strip() for s in params.get("exclude", "").split(",") if s.strip()]
        fields = [f.strip() for f in params.get("field-captions", "").split(",") if f.strip()]
        if not fields and params.get("special-fields"):
            fields = [f"[{params['special-fields']}]"]
        url_val = params.get("url") or (act.find("./link").get("expression") if act.find("./link") is not None else None)
        target_group_or_param = params.get("target-group") or params.get("target-parameter")
        if target_group_or_param:
            target_group_or_param = (
                _resolve_expression(
                    target_group_or_param, field_lookup, global_field_lookup, ds_captions
                )
                or target_group_or_param
            )

        actions.append(
            ActionInfo(
                name=name,
                caption=caption,
                action_type=action_type,
                trigger=trigger,
                source_dashboard=src_dashboard,
                source_worksheet=src_worksheet,
                target_scope=target_scope,
                excluded_sheets=excluded,
                field_captions=fields,
                url=url_val,
                target_group_or_param=target_group_or_param,
            )
        )

    return actions


def _extract_thumbnails(root: ET._Element) -> list[ThumbnailInfo]:
    thumbnails: list[ThumbnailInfo] = []
    for thumb in root.findall("./thumbnails/thumbnail"):
        name = thumb.get("name")
        raw_text = "".join((thumb.text or "").split())
        if not name or not raw_text:
            continue
        try:
            decoded = base64.b64decode(raw_text, validate=True)
            normalized_b64 = base64.b64encode(decoded).decode("ascii")
        except Exception:
            continue
        thumbnails.append(
            ThumbnailInfo(
                name=name,
                width=_parse_int(thumb.get("width")),
                height=_parse_int(thumb.get("height")),
                base64_png=normalized_b64,
            )
        )
    return thumbnails


def _build_field_lookups(
    wb: Workbook, root: ET._Element
) -> tuple[dict[str, str], dict[tuple[str, str], str], dict[str, str]]:
    """Build datasource caption and field caption lookup dictionaries."""
    ds_captions: dict[str, str] = {}
    field_lookup: dict[tuple[str, str], str] = {}
    global_lookup: dict[str, str] = {}

    for ds in wb.datasources:
        ds_name = ds.name or ""
        ds_cap = ds.caption or ds_name
        if ds_name:
            ds_captions[ds_name] = ds_cap
        for f in ds.fields.values():
            cap = f.caption or f.alias or _strip_brackets(f.id)
            field_lookup[(ds_name, f.id)] = cap
            global_lookup.setdefault(f.id, cap)

    # Supplement from worksheet datasource-dependencies (captures worksheet-local captions)
    for dep in root.findall(".//worksheets/worksheet//datasource-dependencies"):
        ds_name = dep.get("datasource", "")
        for col in dep.findall("./column"):
            cid = col.get("name")
            cap = col.get("caption")
            if cid and cap:
                field_lookup.setdefault((ds_name, cid), cap)
                global_lookup.setdefault(cid, cap)

    # Supplement parameters
    for ds_elem in root.findall("./datasources/datasource"):
        if ds_elem.get("name") == "Parameters":
            ds_captions["Parameters"] = "Parameters"
            for col in ds_elem.findall("./column"):
                cid = col.get("name")
                cap = col.get("caption")
                if cid and cap:
                    field_lookup[("Parameters", cid)] = cap
                    global_lookup.setdefault(cid, cap)

    return ds_captions, field_lookup, global_lookup


def extract_workbook(file_path: str, display_filename: Optional[str] = None) -> WorkbookSnapshot:
    """Parse a Tableau .twb or .twbx workbook into a complete WorkbookSnapshot."""
    abs_path = os.path.abspath(file_path)
    wb = Workbook(abs_path)
    root: ET._Element = wb._workbookRoot  # noqa: SLF001

    packaged_files: list[str] = []
    if zipfile.is_zipfile(abs_path):
        with zipfile.ZipFile(abs_path) as zf:
            packaged_files = sorted(zf.namelist())

    ds_captions, field_lookup, global_lookup = _build_field_lookups(wb, root)

    parameters = _extract_parameters(root)
    datasources = _extract_datasources(wb, root, field_lookup, global_lookup)
    worksheets = _extract_worksheets(root, ds_captions, field_lookup, global_lookup)
    thumbnails = _extract_thumbnails(root)
    thumb_names = {t.name for t in thumbnails}
    dashboards = _extract_dashboards(root, ds_captions, field_lookup, global_lookup, thumb_names)
    actions = _extract_actions(root, field_lookup, global_lookup, ds_captions)

    # Back-populate worksheet -> dashboard memberships
    ws_to_dbs: dict[str, list[str]] = {}
    for db in dashboards:
        for wname in db.worksheets:
            ws_to_dbs.setdefault(wname, []).append(db.name)
    for ws in worksheets:
        ws.used_in_dashboards = _dedupe_preserve(ws_to_dbs.get(ws.name, []))

    repo_loc = root.find("./repository-location")
    repo_path = None
    if repo_loc is not None:
        r_path = repo_loc.get("path", "")
        r_id = repo_loc.get("id", "")
        repo_path = f"{r_path}/{r_id}".strip("/") or None

    shown_name = display_filename or os.path.basename(abs_path)
    workbook_name = os.path.splitext(os.path.basename(shown_name))[0]

    snapshot = WorkbookSnapshot(
        filename=shown_name,
        workbook_name=workbook_name,
        tableau_version=root.get("version"),
        source_platform=root.get("source-platform"),
        source_build=root.get("source-build"),
        repository_path=repo_path,
        datasources=datasources,
        parameters=parameters,
        worksheets=worksheets,
        dashboards=dashboards,
        actions=actions,
        thumbnails=thumbnails,
        shapes=list(wb.shapes),
        packaged_files=packaged_files,
    )
    snapshot.deterministic_mermaid = build_deterministic_mermaid(snapshot)
    return snapshot
