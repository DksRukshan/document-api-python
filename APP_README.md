# Tableau AI Documenter & Performance Auditor (Google Antigravity SDK)

A unified interactive Web UI, REST/SSE API, and CLI application that parses Tableau workbooks (`.twb` / `.twbx`) to produce **stakeholder documentation**, **0–100 Performance, Formatting & WCAG Accessibility Audits**, **Before -> After formula rewrites**, and **Interactive Coach Q&A** powered by the **Google Antigravity Python SDK** (`google-antigravity`).

## Unified Capabilities

- **Deep Structural Extraction** (`tableau_doc_app/extractor.py`):
  - Parses Dashboards, normalized layout zones (`_.fcp.*` compatible), Worksheets, Mark types, Shelves, Visual encodings, Filters, Sorts, Tooltip templates, Parameters, Interactive Actions, Data Sources, Connections, Physical Joins / Custom SQL, Logical Relationships, Readable Calculated Fields, and base64 PNG Thumbnails.
- **Deterministic Performance, Formatting & WCAG Accessibility Auditor** (`tableau_doc_app/audit_parser.py`, `tableau_doc_app/audit_heuristics.py`):
  - Computes an instant **0–100 Health Score (Grade A–F)** across **Calculations**, **Formatting & Typography**, **Data Source & Hyper Architecture**, **Filters & Order of Operations**, **Layout & Dashboard Design**, **WCAG 2.2 AA Accessibility**, **Server & Publishing**, and embedded **Performance Recording** events.
  - Emits concrete Before -> After formula diffs and best-practice citations grounded in Tableau documentation and the InterWorks Performance Checklist (`tableau_doc_app/skills/tableau-performance-auditor/`).
- **Google Antigravity SDK AI Engine** (`tableau_doc_app/agent.py`):
  - **AI Documentation Generator**: Uses `LocalAgentConfig` (defaulting to SDK Gemini unless overridden), read-only workbook inspection tools, multimodal PNG thumbnail vision analysis, and structured `DashboardDocumentationReport` output.
  - **AI Performance & UX Evaluator**: Runs single-agent or **Deep Multi-Agent** orchestration (`calculation_optimizer` + `formatting_ux_critic` subagents) with `DEFAULT_RETRY_CONFIG` (5 retries), observability hooks, and structured `AIEvaluationReport` output.
  - **Interactive Coach Chat**: Multi-turn follow-up Q&A grounded in workbook tools and curated Tableau best-practice search (`tableau_doc_app/docs_search.py`).
  - Supports **Gemini API** (Google AI Studio), **Vertex AI** (`--backend vertex`), **Local OpenAI/Ollama** (`--backend ollama`), and **LiteRT-LM** (`--backend litert`).
- **Combined Multi-Format Exports** (`tableau_doc_app/renderer.py`):
  - Exports Markdown (`.md`), interactive dark/light Standalone HTML (`.html` with zone wireframes, thumbnails, Mermaid lineage diagram, Health Scorecard, Before/After diffs), and structured JSON (`.json`).

## Quick Setup

1. Activate the Python 3.13 virtual environment and install dependencies (if not already done):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-app.txt
```

2. Configure your Google AI Studio Gemini API key:

   Create a free API key at <https://aistudio.google.com/app/api-keys>, copy `.env.example` to `.env`, and set `GEMINI_API_KEY`:

```bash
cp .env.example .env
# Edit .env and set GEMINI_API_KEY=your_key_here
```

   *(Optional: Configure or clear the key per session via the **API Key** modal in the Web UI, pass `--api-key` to the CLI, or run deterministic `--no-ai` commands with no API key.)*

## Running the Web App

Start the FastAPI server:

```bash
.venv/bin/python cli.py serve --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000` in your browser to:
- Drag-and-drop a `.twb` / `.twbx` file or click a **Showcase Sample** (`retail_exec_unoptimized.twb`, `retail_exec_optimized.twb`, `geocoding.twbx`).
- Review the instant **0–100 Health Scorecard**, **Category Breakdown**, **Severity-Filtered Audit Findings**, **Dashboard Zone Wireframes**, and **Thumbnails**.
- Run **Generate AI Docs**, **Run AI Perf & UX Audit** (with optional **Deep Multi-Agent** subagents), live SSE trace streaming, **Coach Chat**, and download combined `.md` / `.html` / `.json` artifacts.

## CLI Usage

Inspect metadata and run the static audit deterministically:

```bash
.venv/bin/python cli.py inspect tableau_doc_app/samples/retail_exec_unoptimized.twb
```

Run a standalone Performance, Formatting & WCAG Audit (deterministic `--no-ai`, or AI + `--deep` multi-agent):

```bash
.venv/bin/python cli.py audit tableau_doc_app/samples/retail_exec_unoptimized.twb --no-ai --out-dir outputs
.venv/bin/python cli.py audit tableau_doc_app/samples/retail_exec_unoptimized.twb --deep --out-dir outputs
```

Generate full documentation (includes static scorecard unless `--no-audit` is passed):

```bash
.venv/bin/python cli.py generate samples/show_workbook_info/geocoding.twbx \
  --dashboard "Commission Model" \
  --audience "Sales Leadership & RevOps" \
  --out-dir outputs
```

Deterministic documentation without calling any AI provider:

```bash
.venv/bin/python cli.py generate samples/show_workbook_info/geocoding.twbx --no-ai --out-dir outputs
```

## Troubleshooting

### `HTTP 2: ... lookup generativelanguage.googleapis.com: no such host`
This error occurs in the Google Antigravity SDK sidecar when your machine, VPN, proxy, or sandboxed environment cannot resolve DNS or reach `generativelanguage.googleapis.com`:
1. Verify outbound internet / DNS resolution (e.g., corporate VPN/proxy or `HTTPS_PROXY`).
2. Or switch to an offline/local Antigravity backend (`--backend ollama` / `--backend litert`), or use deterministic inspection/audit (`--no-ai` in CLI, or instant Scorecard & Exports in Web UI).

## Verification Tests

Run the application test suite and the upstream `tableaudocumentapi` test suite:

```bash
.venv/bin/pytest tests_app -v
.venv/bin/python -m unittest discover test
```
