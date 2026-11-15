"""FastAPI backend for the Tableau Performance & Design Evaluator powered by Google Antigravity SDK."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.evaluator import (
    chat_with_antigravity_coach,
    evaluate_workbook_with_antigravity,
)
from app.analyzer.heuristics import run_heuristic_audit
from app.analyzer.models import AIEvaluationReport, AgentTelemetry, WorkbookAnalysis
from app.analyzer.twb_parser import parse_tableau_workbook
from app.config import (
    GOOGLE_AI_STUDIO_KEY_URL,
    MAX_UPLOAD_BYTES,
    SAMPLES_DIR,
    settings,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

SAMPLE_CATALOG: dict[str, dict[str, str]] = {
    "unoptimized": {
        "id": "unoptimized",
        "filename": "retail_exec_unoptimized.twb",
        "title": "Retail Executive Overview (Unoptimized)",
        "description": (
            "Deliberately slow & cluttered workbook featuring live Excel + Custom SQL, legacy joins, blending, "
            "ATTR/COUNTD/NOW()/ELSE IF calculations, Only Relevant Values filters, Automatic sizing, "
            "Polygon marks, ALL-CAPS titles, and Comic Sans overrides."
        ),
    },
    "optimized": {
        "id": "optimized",
        "filename": "retail_exec_optimized.twb",
        "title": "Executive Performance Brief (Best-Practice Reference)",
        "description": (
            "Clean reference workbook using aggregated Hyper extracts, Logical Relationships, CASE/IN/integer parameters, "
            "Custom Number Formatting arrows, Fixed size layout, narrative tooltips, and Workbook-level styling."
        ),
    },
}


@dataclass
class WorkbookSession:
    analysis: WorkbookAnalysis
    ai_report: AIEvaluationReport | None = None
    ai_telemetry: AgentTelemetry | None = None
    chat_history: list[dict[str, str]] = field(default_factory=list)


class SessionStore:
    """Bounded in-memory LRU session store."""

    def __init__(self, max_sessions: int = 25) -> None:
        self._max = max_sessions
        self._store: OrderedDict[str, WorkbookSession] = OrderedDict()

    def put(self, session: WorkbookSession) -> None:
        sid = session.analysis.session_id
        if sid in self._store:
            self._store.move_to_end(sid)
        self._store[sid] = session
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def get(self, session_id: str) -> WorkbookSession:
        if session_id not in self._store:
            raise HTTPException(status_code=404, detail="Workbook session not found.")
        self._store.move_to_end(session_id)
        return self._store[session_id]


sessions = SessionStore()

app = FastAPI(
    title="Tableau Performance & Design Evaluator",
    description="Evaluate Tableau workbooks using static heuristics + Google Antigravity SDK.",
    version="0.1.0",
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Request Payloads ---


class ApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=5)
    persist_to_env: bool = False


class EvaluateRequest(BaseModel):
    deep_multi_agent: bool = False
    focus_notes: str = ""


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=6000)


# --- Routes ---


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return settings.status_dict()


@app.post("/api/config/api-key")
async def set_api_key(payload: ApiKeyRequest) -> dict[str, Any]:
    try:
        settings.set_api_key(payload.api_key, persist_to_env=payload.persist_to_env)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return settings.status_dict()


@app.delete("/api/config/api-key")
async def delete_api_key(remove_from_env: bool = False) -> dict[str, Any]:
    settings.clear_api_key(remove_from_env=remove_from_env)
    return settings.status_dict()


@app.get("/api/samples")
async def list_samples() -> list[dict[str, str]]:
    return list(SAMPLE_CATALOG.values())


@app.post("/api/analyze/sample/{sample_id}")
async def analyze_sample(sample_id: str) -> WorkbookAnalysis:
    entry = SAMPLE_CATALOG.get(sample_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown sample '{sample_id}'.")
    sample_path = SAMPLES_DIR / entry["filename"]
    if not sample_path.exists():
        raise HTTPException(status_code=500, detail="Sample workbook file missing.")
    raw = sample_path.read_bytes()
    analysis = run_heuristic_audit(parse_tableau_workbook(raw, entry["filename"]))
    sessions.put(WorkbookSession(analysis=analysis))
    return analysis


@app.post("/api/analyze/upload")
async def analyze_upload(file: UploadFile = File(...)) -> WorkbookAnalysis:
    filename = Path(file.filename or "workbook.twb").name
    if not filename.lower().endswith((".twb", ".twbx")):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file format. Please upload a .twb or .twbx Tableau workbook.",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Workbook exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
        )

    try:
        analysis = run_heuristic_audit(parse_tableau_workbook(raw, filename))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sessions.put(WorkbookSession(analysis=analysis))
    return analysis


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    sess = sessions.get(session_id)
    return {
        "analysis": sess.analysis.model_dump(),
        "ai_report": sess.ai_report.model_dump() if sess.ai_report else None,
        "ai_telemetry": sess.ai_telemetry.model_dump() if sess.ai_telemetry else None,
        "chat_history": sess.chat_history,
    }


@app.post("/api/evaluate/{session_id}")
async def evaluate_workbook(session_id: str, payload: EvaluateRequest) -> dict[str, Any]:
    sess = sessions.get(session_id)
    if not settings.has_api_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "GEMINI_API_KEY is not set. Create a Gemini API key at "
                f"{GOOGLE_AI_STUDIO_KEY_URL} and save it in Settings."
            ),
        )
    try:
        report, telemetry = await evaluate_workbook_with_antigravity(
            sess.analysis,
            deep_multi_agent=payload.deep_multi_agent,
            focus_notes=payload.focus_notes,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Antigravity SDK error: {exc}") from exc

    sess.ai_report = report
    sess.ai_telemetry = telemetry
    return {
        "report": report.model_dump(),
        "telemetry": telemetry.model_dump(),
    }


@app.post("/api/evaluate/{session_id}/stream")
async def evaluate_workbook_stream(session_id: str, payload: EvaluateRequest) -> StreamingResponse:
    sess = sessions.get(session_id)
    if not settings.has_api_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "GEMINI_API_KEY is not set. Create a Gemini API key at "
                f"{GOOGLE_AI_STUDIO_KEY_URL} and save it in Settings."
            ),
        )

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def on_event(ev: dict[str, Any]) -> None:
        await queue.put(ev)

    async def worker() -> None:
        try:
            await queue.put({"type": "status", "message": "Starting Google Antigravity SDK Agent..."})
            report, telemetry = await evaluate_workbook_with_antigravity(
                sess.analysis,
                deep_multi_agent=payload.deep_multi_agent,
                focus_notes=payload.focus_notes,
                on_event=on_event,
            )
            sess.ai_report = report
            sess.ai_telemetry = telemetry
            await queue.put(
                {
                    "type": "complete",
                    "report": report.model_dump(),
                    "telemetry": telemetry.model_dump(),
                }
            )
        except Exception as exc:
            await queue.put({"type": "error", "detail": str(exc)})
        finally:
            await queue.put(None)

    asyncio.create_task(worker())

    async def event_generator():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/chat/{session_id}")
async def chat_coach(session_id: str, payload: ChatRequest) -> dict[str, Any]:
    sess = sessions.get(session_id)
    if not settings.has_api_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "GEMINI_API_KEY is not set. Create a Gemini API key at "
                f"{GOOGLE_AI_STUDIO_KEY_URL} and save it in Settings."
            ),
        )
    try:
        reply, telemetry = await chat_with_antigravity_coach(
            sess.analysis,
            payload.message,
            chat_history=sess.chat_history,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Antigravity SDK chat error: {exc}") from exc

    sess.chat_history.append({"role": "user", "content": payload.message})
    sess.chat_history.append({"role": "assistant", "content": reply})
    return {
        "reply": reply,
        "telemetry": telemetry.model_dump(),
        "chat_history": sess.chat_history,
    }
