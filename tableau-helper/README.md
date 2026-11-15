# Tableau Performance & UX Auditor (Google Antigravity SDK)

A web application powered by the **Google Antigravity SDK** (`google-antigravity`) that evaluates Tableau workbooks (`.twb` and `.twbx`) for query performance, calculated field optimization, formatting & visual hierarchy, data modeling, filter order of operations, dashboard layout, WCAG accessibility, and server publishing best practices.

---

## Features

- **Instant Local XML / Archive Telemetry (`.twb` & `.twbx`)**:
  - **Calculations Audit**: Flags `ELSE IF`, multi-branch `IF/ELSEIF` -> `CASE`, chained `OR` -> `IN`, `ATTR()` -> `MIN()/MAX()`, `COUNTD()`, cache-busting `NOW()`, `TODAY()`, `{FIXED/INCLUDE/EXCLUDE}` LODs, chained string operations -> `REGEXP_*` or extract materialization, repeated nested calc references, deep dependency chains, row-level parameter checks inside `SUM(IF ...)`, redundant `IF ... THEN TRUE ELSE FALSE END`, unused fields, and KPI arrow string calcs better replaced with Custom Number Formatting (`▲ +0.0%;▼ -0.0%;— 0.0%`).
  - **Formatting, UX & Accessibility (WCAG 2.2 AA)**: Audits Workbook-level vs Worksheet-level style overrides (`Format -> Workbook`), font standardization (`Tableau Book`/`Arial`/etc.), clutter (gridlines, borders, shading), narrative tooltips & command buttons, ALL-CAPS titles, captions, and client-side rendering mark density.
  - **Data Sources & Filters**: Flags single-threaded live Excel/CSV connectors, Custom SQL (`<relation type='text'>`), legacy physical joins vs Logical Relationships, Data Blending, unhidden unused extract fields, `Only Relevant Values`, high-cardinality multi-value dropdowns, `MY()`/`MDY()` date filters, Top N filters missing Context filters, and Action Filters lacking `Exclude all values`.
  - **Dashboards, Assets & Publishing**: Flags `Automatic` sizing (cache-breaking), oversized view/filter counts, deeply nested layout containers + Blank spacers, unused Device Layouts, Polygon marks, heavy crosstabs, Pages shelf, `Show Sheets as Tabs`, `.twbx` images `>50 KB`, and Tableau Performance Recording workbooks.
- **Google Antigravity SDK AI Evaluation**:
  - Uses `LocalAgentConfig` (default Gemini model) with custom typed Tableau inspection tools, curated skill knowledge (`skills/tableau-performance-auditor/`), explicit tool safety policies, observability hooks streaming live tool traces over SSE, and `response_schema=AIEvaluationReport`.
  - Supports **Deep Multi-Agent Mode** (`calculation_optimizer` + `formatting_ux_critic` subagents) and an interactive **Coach Chat** tab for formula rewrites and Q&A.

---

## Domain Knowledge Grounding

Distilled into `skills/tableau-performance-auditor/` from:
- *Designing Efficient Workbooks* (InterWorks / Tableau Whitepaper, 2021)
- Official Tableau Desktop & Web Authoring Documentation (`llms-full.txt`)

---

## Setup & Running Locally

### 1. Create / Activate Virtual Environment & Install Dependencies

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Gemini API Key

Get a Gemini API key from Google AI Studio:
**<https://aistudio.google.com/app/api-keys>**

Either copy `.env.example` to `.env` and add your key:

```bash
cp .env.example .env
# Edit .env and set GEMINI_API_KEY=...
```

Or paste the key directly in the web app UI via **Set Gemini API Key** in the top navigation bar.

### 3. Start the Web App

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Then open <http://127.0.0.1:8000> in your browser. You can upload your own `.twb` / `.twbx` workbook or click **Retail Executive Overview (Unoptimized)** / **Executive Performance Brief (Best-Practice Reference)** to test right away.

---

## Running Tests

```bash
.venv/bin/pytest -v
```
