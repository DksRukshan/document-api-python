"""FastAPI web server and REST/SSE API for Unified Tableau Documenter & Performance Auditor."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from tableau_doc_app.agent import (
    AntigravityConfigurationError,
    chat_with_antigravity_coach,
    clear_session_api_key,
    evaluate_performance_with_antigravity,
    generate_ai_documentation,
    get_credential_status,
    set_session_api_key,
)
from tableau_doc_app.audit_heuristics import audit_workbook_bytes
from tableau_doc_app.audit_models import AIEvaluationReport, AgentTelemetry, WorkbookAuditReport
from tableau_doc_app.extractor import extract_workbook
from tableau_doc_app.models import BackendConfig, DocumentationBundle, WorkbookSnapshot
from tableau_doc_app.renderer import (
    bundle_from_snapshot,
    render_html,
    render_json,
    render_markdown,
)

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
ALLOWED_SAMPLE_ROOTS = (
    (APP_DIR / "samples").resolve(),
    (WORKSPACE_ROOT / "samples").resolve(),
    (WORKSPACE_ROOT / "test" / "assets").resolve(),
)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_CACHED_ITEMS = 24
MAX_CHAT_HISTORY = 24


class ApiKeyRequest(BaseModel):
    api_key: str = Field(..., min_length=1)
    persist_to_env: bool = False


class BackendPayload(BaseModel):
    backend: Literal["gemini", "vertex", "ollama", "litert"] = "gemini"
    api_key: Optional[str] = None
    model: Optional[str] = None
    project: Optional[str] = None
    location: Optional[str] = None
    base_url: Optional[str] = None
    litert_model_path: Optional[str] = None


class AuditRequest(BackendPayload):
    deep_multi_agent: bool = False
    focus_notes: str = ""


class ChatRequest(BackendPayload):
    message: str = Field(..., min_length=1, max_length=4000)


@dataclass
class SessionEntry:
    """In-memory unified session holding extracted snapshot, static audit, AI artifacts & chat."""

    snapshot: WorkbookSnapshot
    audit: WorkbookAuditReport
    bundle: Optional[DocumentationBundle] = None
    ai_evaluation: Optional[AIEvaluationReport] = None
    ai_telemetry: Optional[AgentTelemetry] = None
    chat_history: list[dict[str, str]] = field(default_factory=list)


@dataclass
class JobState:
    """In-memory async job state and SSE broadcast channel."""

    job_id: str
    snapshot_id: str
    status: Literal["running", "completed", "failed"] = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    bundle: Optional[DocumentationBundle] = None
    markdown: Optional[str] = None
    html: Optional[str] = None
    error: Optional[str] = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: Optional[asyncio.Task[Any]] = None


_SESSIONS: dict[str, SessionEntry] = {}
_SNAPSHOTS = _SESSIONS  # Backward-compatible alias for session/snapshot cache.
_JOBS: dict[str, JobState] = {}

app = FastAPI(
    title="Tableau AI Documenter & Performance Auditor (Antigravity SDK)",
    description=(
        "Unified Tableau .twb/.twbx documentation, formula & WCAG audit scorecard, "
        "Before/After AI evaluation, and interactive coach powered by Google Antigravity SDK."
    ),
    version="2.0.0",
)


def _trim_cache(store: dict[str, Any], limit: int = MAX_CACHED_ITEMS) -> None:
    while len(store) > limit:
        oldest_key = next(iter(store))
        del store[oldest_key]


def _build_backend_config(
    *,
    backend: Literal["gemini", "vertex", "ollama", "litert"] = "gemini",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    base_url: Optional[str] = None,
    litert_model_path: Optional[str] = None,
) -> BackendConfig:
    return BackendConfig(
        backend=backend,
        api_key=(api_key or "").strip() or None,
        model=(model or "").strip() or None,
        project=(project or "").strip() or None,
        location=(location or "").strip() or None,
        base_url=(base_url or "").strip() or None,
        litert_model_path=(litert_model_path or "").strip() or None,
    )


def _require_configured_credentials(backend_cfg: BackendConfig) -> dict[str, Any]:
    cred_status = get_credential_status(backend_cfg)
    if not cred_status["configured"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "GEMINI_API_KEY is not configured. Create a free API key at "
                    "https://aistudio.google.com/app/api-keys and set GEMINI_API_KEY in .env "
                    "or paste it in the API Key settings dialog."
                ),
                "aistudio_url": cred_status["aistudio_url"],
            },
        )
    return cred_status


def _discover_samples() -> list[dict[str, str]]:
    """Return bundled .twb/.twbx files allowed for one-click loading."""
    found: list[dict[str, str]] = []
    seen: set[Path] = set()
    for root in ALLOWED_SAMPLE_ROOTS:
        if not root.exists():
            continue
        group = "Showcase" if root == (APP_DIR / "samples").resolve() else "Repository"
        for item in sorted(root.rglob("*")):
            resolved = item.resolve()
            if resolved in seen:
                continue
            if item.is_file() and item.suffix.lower() in (".twb", ".twbx"):
                seen.add(resolved)
                rel = str(item.relative_to(WORKSPACE_ROOT))
                found.append({"path": rel, "name": item.name, "group": group})
    return found


def _resolve_sample_path(sample_path: str) -> Path:
    candidate = (WORKSPACE_ROOT / sample_path).resolve()
    if not any(candidate.is_relative_to(root) for root in ALLOWED_SAMPLE_ROOTS):
        raise HTTPException(
            status_code=400,
            detail="Sample path must reside inside tableau_doc_app/samples/, samples/, or test/assets/.",
        )
    if candidate.suffix.lower() not in (".twb", ".twbx") or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"Sample workbook not found: {sample_path}")
    return candidate


async def _extract_and_audit(
    file: Optional[UploadFile],
    sample_path: Optional[str],
) -> tuple[WorkbookSnapshot, WorkbookAuditReport]:
    if sample_path:
        resolved = _resolve_sample_path(sample_path)
        try:
            raw_bytes = await asyncio.to_thread(resolved.read_bytes)
            snapshot = await asyncio.to_thread(extract_workbook, str(resolved), resolved.name)
            audit = await asyncio.to_thread(audit_workbook_bytes, raw_bytes, resolved.name)
            return snapshot, audit
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to parse workbook: {exc}") from exc

    if file is None:
        raise HTTPException(
            status_code=400,
            detail="Provide either a Tableau .twb/.twbx upload or a sample_path.",
        )

    filename = os.path.basename(file.filename or "workbook.twb")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".twb", ".twbx"):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a Tableau .twb or .twbx workbook.",
        )

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Workbook exceeds maximum supported upload size ({MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name
        snapshot = await asyncio.to_thread(extract_workbook, tmp_path, filename)
        audit = await asyncio.to_thread(audit_workbook_bytes, raw_bytes, filename)
        return snapshot, audit
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse Tableau workbook: {exc}") from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def _obtain_session(
    snapshot_id: Optional[str],
    file: Optional[UploadFile] = None,
    sample_path: Optional[str] = None,
) -> tuple[str, SessionEntry]:
    if snapshot_id and snapshot_id in _SESSIONS and file is None and not sample_path:
        return snapshot_id, _SESSIONS[snapshot_id]
    if snapshot_id and file is None and not sample_path:
        raise HTTPException(status_code=404, detail="Session/snapshot not found or expired.")
    snapshot, audit = await _extract_and_audit(file, sample_path)
    new_id = str(uuid.uuid4())
    entry = SessionEntry(snapshot=snapshot, audit=audit)
    _SESSIONS[new_id] = entry
    _trim_cache(_SESSIONS)
    return new_id, entry


def _sanitize_error(message: str, *secrets: Optional[str]) -> str:
    cleaned = message
    candidates = [*secrets, os.environ.get("GEMINI_API_KEY"), os.environ.get("GOOGLE_API_KEY")]
    for secret in candidates:
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def _safe_filename_stem(snapshot: WorkbookSnapshot, target_dashboard: Optional[str]) -> str:
    base = snapshot.workbook_name or "tableau_workbook"
    if target_dashboard:
        base = f"{base}_{target_dashboard}"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("_")
    return cleaned or "tableau_documentation"


def _format_download_response(
    bundle: DocumentationBundle,
    fmt: Literal["md", "html", "json"],
    *,
    include_thumbnails: bool = False,
) -> Response:
    stem = _safe_filename_stem(bundle.snapshot, bundle.target_dashboard)
    if fmt == "md":
        content = render_markdown(bundle)
        media_type = "text/markdown; charset=utf-8"
        ext = "md"
    elif fmt == "html":
        content = render_html(bundle)
        media_type = "text/html; charset=utf-8"
        ext = "html"
    elif fmt == "json":
        content = render_json(bundle, include_thumbnails=include_thumbnails)
        media_type = "application/json; charset=utf-8"
        ext = "json"
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported export format: {fmt}")

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{stem}_docs.{ext}"'},
    )


async def _push_job_event(job: JobState, event: dict[str, Any]) -> None:
    async with job.condition:
        job.events.append(event)
        job.condition.notify_all()


def _compact_bundle_dict(bundle: DocumentationBundle) -> dict[str, Any]:
    compact = bundle.model_copy(deep=True)
    for thumb in compact.snapshot.thumbnails:
        thumb.base64_png = ""
    return compact.model_dump(mode="json")


@app.get("/")
async def serve_index() -> FileResponse:
    """Serve the unified Tableau AI Documenter & Performance Auditor web UI."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="Web UI index.html not found.")
    return FileResponse(index_path)


@app.get("/api/health")
async def health_check() -> JSONResponse:
    """Return backend credential status and bundled Tableau sample workbooks."""
    return JSONResponse(
        {
            "status": "ok",
            "credentials": get_credential_status(),
            "backends": ["gemini", "vertex", "ollama", "litert"],
            "samples": _discover_samples(),
        }
    )


@app.get("/api/config")
async def get_config() -> JSONResponse:
    """Return masked Gemini credential status, supported backends, and bundled samples."""
    return JSONResponse(
        {
            **get_credential_status(),
            "backends": ["gemini", "vertex", "ollama", "litert"],
            "samples": _discover_samples(),
        }
    )


@app.post("/api/config/api-key")
async def update_api_key(payload: ApiKeyRequest) -> JSONResponse:
    """Save Gemini API key into server process env (and optionally .env)."""
    cleaned = payload.api_key.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="API key cannot be empty.")
    status = set_session_api_key(cleaned, persist_to_env=payload.persist_to_env)
    return JSONResponse(status)


@app.delete("/api/config/api-key")
async def delete_api_key(remove_from_env: bool = Query(default=False)) -> JSONResponse:
    """Clear session Gemini API key (and optionally remove from .env)."""
    return JSONResponse(clear_session_api_key(remove_from_env=remove_from_env))


@app.get("/api/samples")
async def list_samples() -> JSONResponse:
    """List bundled showcase and repository Tableau sample workbooks."""
    return JSONResponse({"samples": _discover_samples()})


@app.post("/api/inspect")
async def inspect_workbook(
    file: Optional[UploadFile] = File(default=None),
    sample_path: Optional[str] = Form(default=None),
) -> JSONResponse:
    """Parse a Tableau .twb/.twbx file and return snapshot metadata, static audit, and Markdown preview."""
    snapshot_id, session = await _obtain_session(None, file, sample_path)
    det_bundle = bundle_from_snapshot(session.snapshot, audit=session.audit)
    preview_md = render_markdown(det_bundle)
    return JSONResponse(
        {
            "snapshot_id": snapshot_id,
            "snapshot": session.snapshot.model_dump(mode="json"),
            "audit": session.audit.model_dump(mode="json"),
            "preview_markdown": preview_md,
        }
    )


@app.get("/api/sessions/{snapshot_id}")
async def get_session_state(snapshot_id: str) -> JSONResponse:
    """Fetch cached snapshot, static audit, and any generated AI documentation/evaluation."""
    _, session = await _obtain_session(snapshot_id)
    return JSONResponse(
        {
            "snapshot_id": snapshot_id,
            "snapshot": session.snapshot.model_dump(mode="json"),
            "audit": session.audit.model_dump(mode="json"),
            "bundle": _compact_bundle_dict(session.bundle) if session.bundle else None,
            "ai_evaluation": (
                session.ai_evaluation.model_dump(mode="json") if session.ai_evaluation else None
            ),
            "ai_telemetry": (
                session.ai_telemetry.model_dump(mode="json") if session.ai_telemetry else None
            ),
            "chat_history": session.chat_history,
        }
    )


@app.post("/api/extract-export")
async def extract_and_export(
    fmt: Literal["md", "html", "json"] = Form(default="md"),
    snapshot_id: Optional[str] = Form(default=None),
    file: Optional[UploadFile] = File(default=None),
    sample_path: Optional[str] = Form(default=None),
    target_dashboard: Optional[str] = Form(default=None),
    audience: str = Form(default="Business & Technical Stakeholders"),
    include_thumbnails: bool = Form(default=False),
    include_audit: bool = Form(default=True),
) -> Response:
    """Export combined deterministic documentation (+ static audit / AI results if available)."""
    _, session = await _obtain_session(snapshot_id, file, sample_path)
    target_clean = (target_dashboard or "").strip() or None
    audience_clean = audience.strip() or "Business & Technical Stakeholders"
    if session.bundle is not None:
        bundle = session.bundle.model_copy(
            update={
                "target_dashboard": target_clean or session.bundle.target_dashboard,
                "audience": audience_clean,
                "audit": session.audit if include_audit else None,
                "ai_evaluation": session.ai_evaluation if include_audit else None,
            }
        )
    else:
        bundle = bundle_from_snapshot(
            session.snapshot,
            audit=session.audit if include_audit else None,
            ai_evaluation=session.ai_evaluation if include_audit else None,
            target_dashboard=target_clean,
            audience=audience_clean,
        )
    return _format_download_response(bundle, fmt, include_thumbnails=include_thumbnails)


@app.get("/api/sessions/{snapshot_id}/export/{fmt}")
async def export_session_artifact(
    snapshot_id: str,
    fmt: Literal["md", "html", "json"],
    include_thumbnails: bool = Query(default=False),
    include_audit: bool = Query(default=True),
) -> Response:
    """Download unified documentation + audit artifacts for an active session."""
    _, session = await _obtain_session(snapshot_id)
    if session.bundle is not None:
        bundle = session.bundle.model_copy(
            update={
                "audit": session.audit if include_audit else None,
                "ai_evaluation": session.ai_evaluation if include_audit else None,
            }
        )
    else:
        bundle = bundle_from_snapshot(
            session.snapshot,
            audit=session.audit if include_audit else None,
            ai_evaluation=session.ai_evaluation if include_audit else None,
        )
    return _format_download_response(bundle, fmt, include_thumbnails=include_thumbnails)


@app.post("/api/generate")
async def start_generation_job(
    snapshot_id: Optional[str] = Form(default=None),
    file: Optional[UploadFile] = File(default=None),
    sample_path: Optional[str] = Form(default=None),
    target_dashboard: Optional[str] = Form(default=None),
    audience: str = Form(default="Business & Technical Stakeholders"),
    custom_instructions: str = Form(default=""),
    include_thumbnails: bool = Form(default=True),
    backend: Literal["gemini", "vertex", "ollama", "litert"] = Form(default="gemini"),
    api_key: Optional[str] = Form(default=None),
    model: Optional[str] = Form(default=None),
    project: Optional[str] = Form(default=None),
    location: Optional[str] = Form(default=None),
    base_url: Optional[str] = Form(default=None),
    litert_model_path: Optional[str] = Form(default=None),
) -> JSONResponse:
    """Start an asynchronous Antigravity SDK documentation job and return job_id."""
    resolved_snap_id, session = await _obtain_session(snapshot_id, file, sample_path)
    backend_cfg = _build_backend_config(
        backend=backend,
        api_key=api_key,
        model=model,
        project=project,
        location=location,
        base_url=base_url,
        litert_model_path=litert_model_path,
    )
    _require_configured_credentials(backend_cfg)

    job_id = str(uuid.uuid4())
    job = JobState(job_id=job_id, snapshot_id=resolved_snap_id)
    _JOBS[job_id] = job
    _trim_cache(_JOBS)

    target_clean = (target_dashboard or "").strip() or None
    audience_clean = audience.strip() or "Business & Technical Stakeholders"
    raw_secret = backend_cfg.api_key

    async def _run_job() -> None:
        async def _on_event(evt: dict[str, Any]) -> None:
            if evt.get("type") == "complete":
                return
            await _push_job_event(job, evt)

        try:
            bundle = await generate_ai_documentation(
                session.snapshot,
                backend_cfg,
                audit=session.audit,
                target_dashboard=target_clean,
                audience=audience_clean,
                custom_instructions=custom_instructions.strip(),
                include_thumbnails=include_thumbnails,
                on_event=_on_event,
            )
            if session.ai_evaluation is not None and bundle.ai_evaluation is None:
                bundle.ai_evaluation = session.ai_evaluation
            session.bundle = bundle
            md_out = render_markdown(bundle)
            html_out = render_html(bundle)
            job.bundle = bundle
            job.markdown = md_out
            job.html = html_out
            job.status = "completed"
            await _push_job_event(
                job,
                {
                    "type": "complete",
                    "usage": bundle.usage.model_dump() if bundle.usage else None,
                    "warnings": bundle.warnings,
                },
            )
        except AntigravityConfigurationError as exc:
            err_msg = _sanitize_error(str(exc), raw_secret)
            job.status = "failed"
            job.error = err_msg
            await _push_job_event(job, {"type": "error", "message": err_msg})
        except Exception as exc:
            err_msg = _sanitize_error(f"{type(exc).__name__}: {exc}", raw_secret)
            job.status = "failed"
            job.error = err_msg
            await _push_job_event(job, {"type": "error", "message": err_msg})

    job.task = asyncio.create_task(_run_job())
    return JSONResponse(
        {
            "job_id": job_id,
            "snapshot_id": resolved_snap_id,
            "status": job.status,
        }
    )


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str) -> JSONResponse:
    """Return status and finished documentation artifacts for a job."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")

    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "snapshot_id": job.snapshot_id,
        "status": job.status,
        "error": job.error,
    }
    if job.bundle is not None:
        payload["bundle"] = _compact_bundle_dict(job.bundle)
        payload["markdown"] = job.markdown
    return JSONResponse(payload)


@app.get("/api/jobs/{job_id}/events")
async def stream_job_events(job_id: str) -> StreamingResponse:
    """Stream real-time Antigravity agent thoughts, tool calls, and completion via SSE."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")

    async def event_stream():
        cursor = 0
        while True:
            async with job.condition:
                while cursor >= len(job.events) and job.status == "running":
                    await job.condition.wait()
                pending = job.events[cursor:]
                cursor = len(job.events)
                finished = job.status != "running"

            for evt in pending:
                yield f"event: {evt.get('type', 'message')}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"

            if finished and cursor >= len(job.events):
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/jobs/{job_id}/export/{fmt}")
async def export_job_artifact(
    job_id: str,
    fmt: Literal["md", "html", "json"],
    include_thumbnails: bool = Query(default=False),
) -> Response:
    """Download completed AI documentation in Markdown, interactive HTML, or JSON format."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    if job.status != "completed" or job.bundle is None:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not complete yet (status: {job.status}).",
        )
    return _format_download_response(
        job.bundle,
        fmt,
        include_thumbnails=include_thumbnails,
    )


@app.post("/api/audit/{snapshot_id}")
async def run_ai_audit(
    snapshot_id: str,
    payload: Optional[AuditRequest] = None,
) -> JSONResponse:
    """Run Antigravity AI performance, formatting & WCAG audit evaluation (single or multi-agent)."""
    _, session = await _obtain_session(snapshot_id)
    req = payload or AuditRequest()
    backend_cfg = _build_backend_config(
        backend=req.backend,
        api_key=req.api_key,
        model=req.model,
        project=req.project,
        location=req.location,
        base_url=req.base_url,
        litert_model_path=req.litert_model_path,
    )
    _require_configured_credentials(backend_cfg)

    try:
        evaluation, telemetry = await evaluate_performance_with_antigravity(
            session.snapshot,
            session.audit,
            backend_cfg,
            deep_multi_agent=req.deep_multi_agent,
            focus_notes=req.focus_notes.strip(),
        )
    except AntigravityConfigurationError as exc:
        raise HTTPException(status_code=400, detail=_sanitize_error(str(exc), backend_cfg.api_key)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_sanitize_error(f"{type(exc).__name__}: {exc}", backend_cfg.api_key),
        ) from exc

    session.ai_evaluation = evaluation
    session.ai_telemetry = telemetry
    if session.bundle is not None:
        session.bundle.ai_evaluation = evaluation
    return JSONResponse(
        {
            "snapshot_id": snapshot_id,
            "evaluation": evaluation.model_dump(mode="json"),
            "telemetry": telemetry.model_dump(mode="json"),
        }
    )


@app.post("/api/audit/{snapshot_id}/stream")
async def stream_ai_audit(
    snapshot_id: str,
    payload: Optional[AuditRequest] = None,
) -> StreamingResponse:
    """Stream real-time Antigravity AI performance audit tool trace and final playbook via SSE."""
    _, session = await _obtain_session(snapshot_id)
    req = payload or AuditRequest()
    backend_cfg = _build_backend_config(
        backend=req.backend,
        api_key=req.api_key,
        model=req.model,
        project=req.project,
        location=req.location,
        base_url=req.base_url,
        litert_model_path=req.litert_model_path,
    )
    _require_configured_credentials(backend_cfg)

    queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue()
    raw_secret = backend_cfg.api_key

    async def _on_event(evt: dict[str, Any]) -> None:
        if evt.get("type") == "complete":
            return
        await queue.put(evt)

    async def _worker() -> None:
        try:
            evaluation, telemetry = await evaluate_performance_with_antigravity(
                session.snapshot,
                session.audit,
                backend_cfg,
                deep_multi_agent=req.deep_multi_agent,
                focus_notes=req.focus_notes.strip(),
                on_event=_on_event,
            )
            session.ai_evaluation = evaluation
            session.ai_telemetry = telemetry
            if session.bundle is not None:
                session.bundle.ai_evaluation = evaluation
            await queue.put(
                {
                    "type": "complete",
                    "evaluation": evaluation.model_dump(mode="json"),
                    "telemetry": telemetry.model_dump(mode="json"),
                }
            )
        except Exception as exc:
            err_msg = _sanitize_error(f"{type(exc).__name__}: {exc}", raw_secret)
            await queue.put({"type": "error", "message": err_msg})
        finally:
            await queue.put(None)

    task = asyncio.create_task(_worker())

    async def event_stream():
        try:
            while True:
                evt = await queue.get()
                if evt is None:
                    break
                yield f"event: {evt.get('type', 'message')}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat/{snapshot_id}")
async def chat_with_coach(snapshot_id: str, payload: ChatRequest) -> JSONResponse:
    """Interactive follow-up Q&A with the Antigravity Tableau Performance & Documentation Coach."""
    _, session = await _obtain_session(snapshot_id)
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    backend_cfg = _build_backend_config(
        backend=payload.backend,
        api_key=payload.api_key,
        model=payload.model,
        project=payload.project,
        location=payload.location,
        base_url=payload.base_url,
        litert_model_path=payload.litert_model_path,
    )
    _require_configured_credentials(backend_cfg)

    try:
        reply, telemetry = await chat_with_antigravity_coach(
            session.snapshot,
            session.audit,
            message,
            backend_cfg,
            chat_history=session.chat_history,
        )
    except AntigravityConfigurationError as exc:
        raise HTTPException(status_code=400, detail=_sanitize_error(str(exc), backend_cfg.api_key)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_sanitize_error(f"{type(exc).__name__}: {exc}", backend_cfg.api_key),
        ) from exc

    session.chat_history.append({"role": "user", "content": message})
    session.chat_history.append({"role": "assistant", "content": reply})
    if len(session.chat_history) > MAX_CHAT_HISTORY:
        session.chat_history = session.chat_history[-MAX_CHAT_HISTORY:]

    return JSONResponse(
        {
            "snapshot_id": snapshot_id,
            "reply": reply,
            "telemetry": telemetry.model_dump(mode="json"),
        }
    )
