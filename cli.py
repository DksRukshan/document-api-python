#!/usr/bin/env python3
"""Command-line interface for Unified Tableau AI Documenter & Performance Auditor."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import warnings
from pathlib import Path
from typing import Any, Sequence

warnings.filterwarnings("ignore", category=FutureWarning, module=r"tableaudocumentapi\..*")

from tableau_doc_app.agent import (  # noqa: E402
    AntigravityConfigurationError,
    evaluate_performance_with_antigravity,
    generate_ai_documentation,
    get_credential_status,
)
from tableau_doc_app.audit_heuristics import audit_workbook_file  # noqa: E402
from tableau_doc_app.extractor import extract_workbook  # noqa: E402
from tableau_doc_app.models import BackendConfig  # noqa: E402
from tableau_doc_app.renderer import (  # noqa: E402
    _grade_for_score,
    bundle_from_snapshot,
    render_html,
    render_json,
    render_markdown,
)


def _safe_stem(name: str, dashboard: str | None) -> str:
    base = f"{name}_{dashboard}" if dashboard else name
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("_")
    return cleaned or "tableau_documentation"


def _add_backend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend",
        choices=["gemini", "vertex", "ollama", "litert"],
        default="gemini",
        help="Antigravity SDK model backend (default: gemini).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Session Gemini API key (overrides GEMINI_API_KEY env).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model override (defaults to SDK default).",
    )
    parser.add_argument("--project", default=None, help="GCP project for Vertex AI backend.")
    parser.add_argument("--location", default=None, help="GCP location for Vertex AI backend.")
    parser.add_argument("--base-url", default=None, help="Local OpenAI/Ollama base URL.")
    parser.add_argument("--litert-model-path", default=None, help="Path to .litertlm model file.")


def _build_backend_config(args: argparse.Namespace) -> BackendConfig:
    return BackendConfig(
        backend=args.backend,
        api_key=args.api_key or None,
        model=args.model or None,
        project=args.project or None,
        location=args.location or None,
        base_url=args.base_url or None,
        litert_model_path=args.litert_model_path or None,
    )


def _make_cli_event_printer(quiet: bool):
    def _on_event(evt: dict[str, Any]) -> None:
        if quiet:
            return
        etype = evt.get("type")
        if etype == "status":
            print(f"[agent] {evt.get('message')}")
        elif etype == "thought":
            text = (evt.get("text") or "").strip()
            if text:
                print(f"[thought] {text[:240]}")
        elif etype in ("tool_call", "tool_start"):
            tool_name = evt.get("name") or evt.get("tool")
            print(f"[tool ->] {tool_name}({json.dumps(evt.get('args') or {})})")
        elif etype in ("tool_result", "tool_end"):
            tool_name = evt.get("name") or evt.get("tool")
            err = evt.get("error")
            suffix = f"ERROR: {err}" if err else (f"{evt.get('elapsed_ms')} ms" if "elapsed_ms" in evt else "ok")
            print(f"[tool <-] {tool_name}: {suffix}")
        elif etype == "complete":
            usage = evt.get("usage") or {}
            total = usage.get("total_tokens", 0)
            print(f"[done] Complete ({total} total tokens)")

    return _on_event


def _cmd_inspect(args: argparse.Namespace) -> int:
    snap = extract_workbook(args.workbook)
    audit = None if args.no_audit else audit_workbook_file(args.workbook)

    if args.json:
        payload = snap.model_dump(mode="json")
        if audit is not None:
            payload["audit"] = audit.model_dump(mode="json")
        output_text = json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        real_ds = [d for d in snap.datasources if not d.is_parameters]
        calc_count = sum(len(d.calculated_fields) for d in real_ds)
        lines = [
            f"Workbook:        {snap.workbook_name} ({snap.filename})",
            f"Tableau Version: {snap.tableau_version or 'Unknown'} ({snap.source_platform or 'desktop'})",
            f"Dashboards:      {len(snap.dashboards)}",
        ]
        for d in snap.dashboards:
            if args.dashboard and args.dashboard.lower() not in d.name.lower():
                continue
            lines.append(
                f"  - {d.name}: {len(d.worksheets)} sheets "
                f"({', '.join(d.worksheets) or 'none'}), "
                f"{len(d.exposed_filters)} filters, {len(d.exposed_parameters)} params"
            )
        lines.append(f"Worksheets:      {len(snap.worksheets)}")
        for w in snap.worksheets:
            lines.append(
                f"  - {w.name} [{', '.join(w.mark_types) or 'Auto'}] -> "
                f"R={w.rows_readable or '—'} | C={w.cols_readable or '—'}"
            )
        lines.extend(
            [
                f"Data Sources:    {len(real_ds)}",
                f"Calculated Flds: {calc_count}",
                f"Parameters:      {len(snap.parameters)}",
                f"Actions:         {len(snap.actions)}",
                f"Thumbnails:      {len(snap.thumbnails)}",
            ]
        )
        if audit is not None:
            lines.extend(
                [
                    f"Health Score:    {audit.overall_score}/100 (Grade {audit.grade})",
                    f"Audit Findings:  {len(audit.findings)}",
                ]
            )
            for f in audit.findings[:10]:
                lines.append(f"  * [{f.severity.value.upper()}] {f.title} ({f.target})")
        output_text = "\n".join(lines) + "\n"

    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
        print(f"Wrote inspection output to {args.output}")
    else:
        print(output_text, end="")
    return 0


def _write_outputs(
    bundle,
    *,
    formats: Sequence[str],
    out_dir: str,
    include_json_thumbnails: bool,
) -> list[Path]:
    dest_dir = Path(out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(bundle.snapshot.workbook_name, bundle.target_dashboard)

    written: list[Path] = []
    for fmt in dict.fromkeys(formats):
        if fmt == "md":
            content = render_markdown(bundle)
        elif fmt == "html":
            content = render_html(bundle)
        elif fmt == "json":
            content = render_json(bundle, include_thumbnails=include_json_thumbnails)
        else:
            raise ValueError(f"Unsupported output format: {fmt}")
        out_path = dest_dir / f"{stem}_docs.{fmt}"
        out_path.write_text(content, encoding="utf-8")
        written.append(out_path)
    return written


def _cmd_generate(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.workbook):
        print(f"Error: File not found: {args.workbook}", file=sys.stderr)
        return 1

    snap = extract_workbook(args.workbook)
    audit = None if args.no_audit else audit_workbook_file(args.workbook)

    if args.no_ai:
        bundle = bundle_from_snapshot(
            snap,
            audit=audit,
            target_dashboard=args.dashboard,
            audience=args.audience,
        )
    else:
        backend_cfg = _build_backend_config(args)
        status = get_credential_status(backend_cfg)
        if not status["configured"]:
            print(
                "Error: GEMINI_API_KEY is not configured.\n"
                "Create a free API key at https://aistudio.google.com/app/api-keys and set GEMINI_API_KEY in .env "
                "(or pass --api-key, or pass --no-ai for deterministic metadata docs).",
                file=sys.stderr,
            )
            return 2

        try:
            bundle = asyncio.run(
                generate_ai_documentation(
                    snap,
                    backend_cfg,
                    audit=audit,
                    target_dashboard=args.dashboard,
                    audience=args.audience,
                    custom_instructions=args.instructions,
                    include_thumbnails=not args.no_thumbnails,
                    on_event=_make_cli_event_printer(args.quiet),
                )
            )
        except AntigravityConfigurationError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2

    written = _write_outputs(
        bundle,
        formats=args.formats,
        out_dir=args.out_dir,
        include_json_thumbnails=args.json_thumbnails,
    )
    print("Generated documentation files:")
    for path in written:
        print(f"  {path}")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.workbook):
        print(f"Error: File not found: {args.workbook}", file=sys.stderr)
        return 1

    snap = extract_workbook(args.workbook)
    audit = audit_workbook_file(args.workbook)

    print(f"Workbook Health Score: {audit.overall_score}/100 (Grade {audit.grade})")
    for cs in audit.category_scores:
        print(
            f"  - {cs.label:<26} {cs.score:>3}/100 ({_grade_for_score(cs.score)}) "
            f"[Issues:{cs.issue_count} C:{cs.critical_count} H:{cs.high_count}]"
        )
    if audit.findings:
        print(f"\nDeterministic Findings ({len(audit.findings)}):")
        for f in audit.findings:
            print(f"  * [{f.severity.value.upper()}] {f.category.value}: {f.title} ({f.target})")

    evaluation = None
    if not args.no_ai:
        backend_cfg = _build_backend_config(args)
        status = get_credential_status(backend_cfg)
        if not status["configured"]:
            print(
                "\nError: GEMINI_API_KEY is not configured.\n"
                "Create a free API key at https://aistudio.google.com/app/api-keys and set GEMINI_API_KEY in .env "
                "(or pass --api-key, or pass --no-ai for deterministic audit only).",
                file=sys.stderr,
            )
            return 2
        try:
            evaluation, telemetry = asyncio.run(
                evaluate_performance_with_antigravity(
                    snap,
                    audit,
                    backend_cfg,
                    deep_multi_agent=args.deep,
                    focus_notes=args.focus,
                    on_event=_make_cli_event_printer(args.quiet),
                )
            )
        except AntigravityConfigurationError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2

        print(f"\nAI Performance Grade: {evaluation.estimated_performance_grade}")
        print(evaluation.executive_summary)
        if evaluation.calculation_rewrites:
            print(f"\nCalculation Rewrites ({len(evaluation.calculation_rewrites)}):")
            for r in evaluation.calculation_rewrites:
                print(f"  - {r.field_name} ({r.expected_impact} impact): {r.why_it_is_faster}")
        if evaluation.quick_wins_checklist:
            print("\nQuick Wins:")
            for q in evaluation.quick_wins_checklist:
                print(f"  [ ] {q}")
        if telemetry.usage:
            print(f"\nTelemetry: {telemetry.usage}")

    if args.out_dir:
        bundle = bundle_from_snapshot(snap, audit=audit, ai_evaluation=evaluation)
        written = _write_outputs(
            bundle,
            formats=args.formats,
            out_dir=args.out_dir,
            include_json_thumbnails=False,
        )
        print("\nWrote audit reports:")
        for path in written:
            print(f"  {path}")

    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "tableau_doc_app.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="tableau-doc",
        description="Unified Tableau Documenter, Performance Auditor & UX Coach using the Google Antigravity SDK.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # inspect
    p_inspect = sub.add_parser("inspect", help="Inspect metadata and run static audit without calling AI.")
    p_inspect.add_argument("workbook", help="Path to .twb or .twbx workbook.")
    p_inspect.add_argument("--dashboard", help="Optional filter by dashboard name.")
    p_inspect.add_argument("--json", action="store_true", help="Output raw extracted WorkbookSnapshot + Audit JSON.")
    p_inspect.add_argument("--no-audit", action="store_true", help="Skip static performance & UX audit.")
    p_inspect.add_argument("-o", "--output", help="Write output to a file instead of stdout.")
    p_inspect.set_defaults(func=_cmd_inspect)

    # generate
    p_gen = sub.add_parser("generate", help="Generate Markdown, HTML, and/or JSON documentation.")
    p_gen.add_argument("workbook", help="Path to .twb or .twbx workbook.")
    p_gen.add_argument("--dashboard", default=None, help="Focus documentation on a specific dashboard.")
    p_gen.add_argument(
        "--audience",
        default="Business & Technical Stakeholders",
        help="Intended reader persona(s).",
    )
    p_gen.add_argument("--instructions", default="", help="Extra instructions for the Antigravity agent.")
    p_gen.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip AI synthesis and emit deterministic metadata documentation.",
    )
    p_gen.add_argument(
        "--no-audit",
        action="store_true",
        help="Exclude deterministic performance/WCAG audit scorecard from generated docs.",
    )
    p_gen.add_argument(
        "--no-thumbnails",
        action="store_true",
        help="Do not attach PNG thumbnails for multimodal vision inspection.",
    )
    _add_backend_args(p_gen)
    p_gen.add_argument(
        "--formats",
        nargs="+",
        choices=["md", "html", "json"],
        default=["md", "html", "json"],
        help="Output formats to write (default: md html json).",
    )
    p_gen.add_argument("--out-dir", default="outputs", help="Directory for generated docs (default: outputs).")
    p_gen.add_argument(
        "--json-thumbnails",
        action="store_true",
        help="Embed base64 PNG strings in JSON export.",
    )
    p_gen.add_argument("-q", "--quiet", action="store_true", help="Suppress live agent thought/tool logs.")
    p_gen.set_defaults(func=_cmd_generate)

    # audit
    p_audit = sub.add_parser("audit", help="Run Tableau Performance, Formatting & WCAG Accessibility Audit.")
    p_audit.add_argument("workbook", help="Path to .twb or .twbx workbook.")
    p_audit.add_argument(
        "--no-ai",
        action="store_true",
        help="Run deterministic 7-category heuristics only without calling AI.",
    )
    p_audit.add_argument(
        "--deep",
        action="store_true",
        help="Enable multi-agent orchestrator with calculation_optimizer & formatting_ux_critic subagents.",
    )
    p_audit.add_argument("--focus", default="", help="Optional optimization or accessibility focus notes.")
    _add_backend_args(p_audit)
    p_audit.add_argument("--out-dir", default=None, help="Optional output directory to write md/html/json reports.")
    p_audit.add_argument(
        "--formats",
        nargs="+",
        choices=["md", "html", "json"],
        default=["md", "html", "json"],
        help="Output formats when --out-dir is specified.",
    )
    p_audit.add_argument("-q", "--quiet", action="store_true", help="Suppress live agent logs.")
    p_audit.set_defaults(func=_cmd_audit)

    # serve
    p_serve = sub.add_parser("serve", help="Run the FastAPI Web UI and REST/SSE API server.")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    p_serve.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    p_serve.add_argument("--reload", action="store_true", help="Enable auto-reload for development.")
    p_serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
