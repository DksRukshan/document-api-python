const state = {
  config: null,
  analysis: null,
  aiReport: null,
  aiTelemetry: null,
  chatHistory: [],
};

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function scoreColor(score) {
  if (score >= 85) return "#16a34a";
  if (score >= 70) return "#0284c7";
  if (score >= 55) return "#ca8a04";
  return "#dc2626";
}

async function fetchJson(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const err = await res.json();
      msg = err.detail || msg;
    } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

async function loadConfig() {
  state.config = await fetchJson("/api/config");
  const dot = document.getElementById("apiKeyDot");
  const text = document.getElementById("apiKeyStatusText");
  if (state.config.has_api_key) {
    dot.classList.add("ready");
    text.textContent = `Gemini Key (${state.config.masked_api_key})`;
  } else {
    dot.classList.remove("ready");
    text.textContent = "Set Gemini API Key";
  }
}

async function loadSamples() {
  const samples = await fetchJson("/api/samples");
  const container = document.getElementById("samplesList");
  container.innerHTML = samples
    .map(
      (s) =>
        `<button class="btn btn-secondary sample-btn" data-id="${escapeHtml(s.id)}" title="${escapeHtml(s.description)}">${escapeHtml(s.title)}</button>`
    )
    .join("");
  container.querySelectorAll(".sample-btn").forEach((btn) => {
    btn.addEventListener("click", () => analyzeSample(btn.dataset.id));
  });
}

async function analyzeSample(sampleId) {
  const analysis = await fetchJson(`/api/analyze/sample/${encodeURIComponent(sampleId)}`, {
    method: "POST",
  });
  setLoadedAnalysis(analysis);
}

async function uploadWorkbook(file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const analysis = await fetchJson("/api/analyze/upload", {
      method: "POST",
      body: form,
    });
    setLoadedAnalysis(analysis);
  } catch (err) {
    alert(`Upload failed: ${err.message}`);
  }
}

function setLoadedAnalysis(analysis) {
  state.analysis = analysis;
  state.aiReport = null;
  state.aiTelemetry = null;
  state.chatHistory = [];

  document.getElementById("emptyState").classList.add("hidden");
  document.getElementById("workspace").classList.remove("hidden");
  const badge = document.getElementById("activeWorkbookBadge");
  badge.textContent = `${analysis.filename} • Score ${analysis.overall_score}/100`;
  badge.classList.remove("hidden");

  document.getElementById("liveTraceBox").classList.add("hidden");
  renderOverview();
  renderFindings();
  renderCalculations();
  renderInventory();
  renderAIReport();
  renderChatHistory();
}

function renderOverview() {
  const a = state.analysis;
  if (!a) return;

  const scoreEl = document.getElementById("overallScore");
  scoreEl.textContent = a.overall_score;
  scoreEl.style.color = scoreColor(a.overall_score);

  const gradeBadge = document.getElementById("gradeBadge");
  gradeBadge.textContent = `Grade ${a.grade}`;
  gradeBadge.className = `grade-badge grade-${a.grade}`;

  document.getElementById("workbookMetaLine").textContent =
    `${a.filename} (${a.file_type.toUpperCase()}, ${(a.file_size_bytes / 1024).toFixed(1)} KB, v${a.tableau_version})`;

  const s = a.summary_stats || {};
  const tiles = [
    ["Findings", s.total_findings ?? 0],
    ["Critical / High", `${s.critical_findings ?? 0} / ${s.high_findings ?? 0}`],
    ["Calculations", `${s.calculation_count ?? 0} (${s.flagged_calculation_count ?? 0} flagged)`],
    ["Data Sources", s.datasource_count ?? 0],
    ["Worksheets", s.worksheet_count ?? 0],
    ["Dashboards", s.dashboard_count ?? 0],
  ];
  document.getElementById("summaryStatsGrid").innerHTML = tiles
    .map(
      ([label, val]) =>
        `<div class="stat-tile"><div class="label">${escapeHtml(label)}</div><div class="val">${escapeHtml(val)}</div></div>`
    )
    .join("");

  document.getElementById("categoryScoresGrid").innerHTML = (a.category_scores || [])
    .map(
      (c) => `
      <div class="cat-card">
        <div class="cat-top">
          <span>${escapeHtml(c.label)}</span>
          <span>${c.score}/100</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill" style="width:${c.score}%; background:${scoreColor(c.score)};"></div>
        </div>
        <div class="muted small" style="margin-top:5px;">${c.issue_count} issue(s)</div>
      </div>`
    )
    .join("");

  document.getElementById("findingsCountChip").textContent = a.findings.length;
  document.getElementById("calcsCountChip").textContent = a.calculations.length;
}

function renderFindings() {
  const a = state.analysis;
  if (!a) return;
  const sev = document.getElementById("severityFilter").value;
  const cat = document.getElementById("categoryFilter").value;

  const filtered = a.findings.filter((f) => {
    if (sev !== "all" && f.severity !== sev) return false;
    if (cat !== "all" && f.category !== cat) return false;
    return true;
  });

  const container = document.getElementById("findingsList");
  if (!filtered.length) {
    container.innerHTML = `<div class="ai-report-empty"><p class="muted">No findings match the selected filters.</p></div>`;
    return;
  }

  container.innerHTML = filtered
    .map((f) => {
      const diffHtml =
        f.rewrite_before || f.rewrite_after
          ? `<div class="diff-grid">
              ${
                f.rewrite_before
                  ? `<div class="code-box code-before"><span class="code-label">Current Formula</span>${escapeHtml(f.rewrite_before)}</div>`
                  : ""
              }
              ${
                f.rewrite_after
                  ? `<div class="code-box code-after"><span class="code-label">Suggested Optimization</span>${escapeHtml(f.rewrite_after)}</div>`
                  : ""
              }
            </div>`
          : "";

      return `
        <article class="finding-card sev-${escapeHtml(f.severity)}">
          <div class="finding-head">
            <div>
              <div class="finding-badges">
                <span class="sev-pill sev-${escapeHtml(f.severity)}-pill">${escapeHtml(f.severity)}</span>
                <span class="cat-pill">${escapeHtml(f.category.replace("_", " "))}</span>
                <span class="muted small">Target: <code>${escapeHtml(f.target)}</code></span>
              </div>
              <h3>${escapeHtml(f.title)}</h3>
            </div>
            ${f.source_citation ? `<span class="muted small">${escapeHtml(f.source_citation)}</span>` : ""}
          </div>
          <div class="finding-body">
            <p><strong>Evidence:</strong> ${escapeHtml(f.evidence)}</p>
            <p><strong>Why it matters:</strong> ${escapeHtml(f.impact_summary)}</p>
            <p><strong>Fix:</strong> ${escapeHtml(f.recommendation)}</p>
            ${diffHtml}
          </div>
        </article>`;
    })
    .join("");
}

function renderCalculations() {
  const a = state.analysis;
  if (!a) return;
  const query = document.getElementById("calcSearchInput").value.trim().toLowerCase();
  const flaggedOnly = document.getElementById("calcFlaggedOnlyToggle").checked;

  const rows = a.calculations.filter((c) => {
    if (flaggedOnly && !c.flags.length && !c.is_unused) return false;
    if (query) {
      const hay = `${c.caption} ${c.name} ${c.formula} ${c.flags.join(" ")}`.toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });

  const wrap = document.getElementById("calculationsTableWrap");
  if (!rows.length) {
    wrap.innerHTML = `<div style="padding:20px;" class="muted">No matching calculations found.</div>`;
    return;
  }

  wrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Field</th>
          <th>Formula</th>
          <th>Flags &amp; Depth</th>
          <th>Sheets</th>
          <th>Score</th>
        </tr>
      </thead>
      <tbody>
        ${rows
          .map(
            (c) => `
          <tr>
            <td>
              <strong>${escapeHtml(c.caption)}</strong>
              <div class="muted small">${escapeHtml(c.datasource_name)} • ${escapeHtml(c.role)}</div>
            </td>
            <td><pre class="formula-pre">${escapeHtml(c.formula)}</pre></td>
            <td>
              ${c.is_unused ? `<span class="sev-pill sev-medium-pill">unused</span> ` : ""}
              ${c.flags.map((fl) => `<code>${escapeHtml(fl)}</code>`).join(" ") || `<span class="muted small">clean</span>`}
              <div class="muted small" style="margin-top:4px;">Nesting depth: ${c.nesting_depth}</div>
            </td>
            <td class="small">${c.used_in_sheets.length ? escapeHtml(c.used_in_sheets.join(", ")) : '<span class="muted">None</span>'}</td>
            <td><strong>${c.complexity_score}</strong></td>
          </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
}

function renderInventory() {
  const a = state.analysis;
  if (!a) return;

  const dsRows = a.datasources
    .map(
      (d) => `
      <tr>
        <td><strong>${escapeHtml(d.caption)}</strong></td>
        <td>${d.is_extract ? "Hyper Extract" : "Live"} ${d.is_published ? "(Published)" : "(Embedded)"}</td>
        <td><code>${escapeHtml(d.connection_classes.join(", ") || "n/a")}</code></td>
        <td>${d.has_custom_sql ? `<span class="sev-pill sev-critical-pill">Custom SQL (${d.custom_sql_count})</span>` : d.uses_relationships ? "Relationships" : `${d.physical_join_count} Join(s)`}</td>
        <td>${d.hidden_field_count} / ${d.total_field_count} hidden</td>
        <td>Extract: ${d.extract_filter_count}, DS: ${d.datasource_filter_count}</td>
      </tr>`
    )
    .join("");

  const wsRows = a.worksheets
    .map(
      (w) => `
      <tr>
        <td><strong>${escapeHtml(w.name)}</strong>${w.has_all_caps_title ? ' <span class="sev-pill sev-low-pill">ALL-CAPS</span>' : ""}</td>
        <td><code>${escapeHtml(w.mark_types.join(", "))}</code>${w.is_text_table_heavy ? " • Heavy Table" : ""}${w.uses_pages_shelf ? " • Pages Shelf" : ""}${w.uses_blending ? " • Blending" : ""}</td>
        <td>${w.fonts_used.length ? escapeHtml(w.fonts_used.join(", ")) : '<span class="muted">Inherited</span>'} (${w.style_override_count} style overrides)</td>
        <td>${w.tooltip_is_default ? '<span class="sev-pill sev-low-pill">Default tooltip</span>' : "Custom tooltip"}${w.has_viz_in_tooltip ? " + Viz-in-Tooltip" : ""}</td>
        <td>${w.has_caption ? "Caption On" : "No Caption"}</td>
      </tr>`
    )
    .join("");

  const dashRows = a.dashboards
    .map(
      (d) => `
      <tr>
        <td><strong>${escapeHtml(d.name)}</strong></td>
        <td>${escapeHtml(d.sizing_mode)} ${d.width ? `(${d.width}x${d.height})` : ""}</td>
        <td>${d.worksheet_zone_count} views • ${d.filter_zone_count} filters</td>
        <td>Nesting depth ${d.max_container_nesting} • ${d.blank_spacer_count} blank spacers</td>
        <td>${d.device_layout_count} device layout(s) • ${d.action_count} action(s) (${d.action_exclude_on_clear_count} exclude-on-clear)</td>
      </tr>`
    )
    .join("");

  document.getElementById("inventoryContainer").innerHTML = `
    <div class="ai-section">
      <h3>Workbook Style &amp; Publishing Baseline</h3>
      <p class="small">
        <strong>Show Sheets as Tabs:</strong> ${a.workbook_style.show_tabs ? "Enabled (consider disabling + Navigation Buttons)" : "Disabled (Good)"} &nbsp;|&nbsp;
        <strong>Workbook Fonts:</strong> ${escapeHtml(a.workbook_style.workbook_fonts.join(", ") || "Default")} &nbsp;|&nbsp;
        <strong>Workbook Style Rules:</strong> ${a.workbook_style.style_rule_count}
      </p>
      ${
        a.performance_recording.detected
          ? `<p class="small"><strong>Performance Recording Detected:</strong> ${escapeHtml(a.performance_recording.notes)}</p>`
          : ""
      }
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Datasource</th><th>Mode</th><th>Drivers</th><th>Model</th><th>Hidden / Total Fields</th><th>Filters</th></tr></thead>
        <tbody>${dsRows || '<tr><td colspan="6" class="muted">No datasources detected.</td></tr>'}</tbody>
      </table>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Worksheet</th><th>Marks &amp; Cost Signals</th><th>Formatting &amp; Fonts</th><th>Tooltips</th><th>Captions</th></tr></thead>
        <tbody>${wsRows || '<tr><td colspan="5" class="muted">No worksheets detected.</td></tr>'}</tbody>
      </table>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Dashboard</th><th>Sizing</th><th>Density</th><th>Containers</th><th>Layouts &amp; Actions</th></tr></thead>
        <tbody>${dashRows || '<tr><td colspan="5" class="muted">No dashboards detected.</td></tr>'}</tbody>
      </table>
    </div>`;
}

function renderAIReport() {
  const container = document.getElementById("aiReportContainer");
  if (!state.aiReport) {
    container.className = "ai-report-empty";
    container.innerHTML = `
      <h3>No AI Report Generated Yet</h3>
      <p class="muted">Click <strong>Run Antigravity Evaluation</strong> above to synthesize drop-in calculation rewrites, formatting/accessibility upgrades, and prioritized performance wins.</p>`;
    return;
  }

  const r = state.aiReport;
  const tel = state.aiTelemetry || {};
  const usage = tel.usage || {};

  container.className = "";
  container.innerHTML = `
    <div class="ai-section">
      <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap;">
        <h3 style="margin:0;">Executive Assessment (AI Grade: ${escapeHtml(r.estimated_performance_grade)})</h3>
        <span class="muted small">
          Tokens: ${usage.total_token_count ?? 0} (Prompt ${usage.prompt_token_count ?? 0} / Output ${usage.candidates_token_count ?? 0} / Thoughts ${usage.thoughts_token_count ?? 0})
        </span>
      </div>
      <p>${escapeHtml(r.executive_summary)}</p>
      <strong>Top Bottlenecks:</strong>
      <ul>${(r.top_bottlenecks || []).map((b) => `<li>${escapeHtml(b)}</li>`).join("")}</ul>
    </div>

    <div class="ai-section">
      <h3>Calculation Optimization Playbook</h3>
      ${
        (r.calculation_rewrites || []).length
          ? r.calculation_rewrites
              .map(
                (c) => `
          <div class="tip-item">
            <strong>${escapeHtml(c.field_name)}</strong> <span class="muted small">(${escapeHtml(c.datasource)} • Impact: ${escapeHtml(c.expected_impact)})</span>
            <p class="small">${escapeHtml(c.why_it_is_faster)}</p>
            <div class="diff-grid">
              <div class="code-box code-before"><span class="code-label">Before</span>${escapeHtml(c.current_formula)}</div>
              <div class="code-box code-after"><span class="code-label">After (Drop-In Rewrite)</span>${escapeHtml(c.optimized_formula)}</div>
            </div>
          </div>`
              )
              .join("")
          : '<p class="muted small">No calculation rewrites required.</p>'
      }
    </div>

    <div class="ai-section">
      <h3>Formatting, Visual Hierarchy &amp; Accessibility Upgrades</h3>
      ${
        (r.formatting_and_ux_tips || []).map(
          (t) => `
          <div class="tip-item">
            <strong>${escapeHtml(t.target)}</strong> — <span class="cat-pill">${escapeHtml(t.area)}</span>
            <p class="small"><strong>Observation:</strong> ${escapeHtml(t.observation)}</p>
            <p class="small"><strong>Recommendation:</strong> ${escapeHtml(t.recommendation)}</p>
            <p class="small muted"><strong>Benefit:</strong> ${escapeHtml(t.benefit)}</p>
          </div>`
        ).join("") || '<p class="muted small">No formatting improvements flagged.</p>'
      }
    </div>

    <div class="two-col">
      <div class="ai-section">
        <h3>Data &amp; Filter Pipeline</h3>
        ${(r.data_and_filter_tips || [])
          .map(
            (t) => `
            <div class="tip-item">
              <strong>${escapeHtml(t.target)}</strong> <span class="cat-pill">${escapeHtml(t.priority)}</span>
              <p class="small">${escapeHtml(t.issue)}</p>
              <p class="small"><strong>Action:</strong> ${escapeHtml(t.action)}</p>
            </div>`
          )
          .join("")}
      </div>
      <div class="ai-section">
        <h3>Dashboard Layout &amp; Server</h3>
        ${(r.dashboard_and_server_tips || [])
          .map(
            (t) => `
            <div class="tip-item">
              <strong>${escapeHtml(t.target)}</strong> <span class="cat-pill">${escapeHtml(t.priority)}</span>
              <p class="small">${escapeHtml(t.issue)}</p>
              <p class="small"><strong>Action:</strong> ${escapeHtml(t.action)}</p>
            </div>`
          )
          .join("")}
      </div>
    </div>

    <div class="two-col">
      <div class="ai-section">
        <h3>Quick Wins Checklist (&lt;10 min)</h3>
        <ul>${(r.quick_wins_checklist || []).map((q) => `<li class="small">${escapeHtml(q)}</li>`).join("")}</ul>
      </div>
      <div class="ai-section">
        <h3>Strategic Refactors</h3>
        <ul>${(r.strategic_refactors || []).map((s) => `<li class="small">${escapeHtml(s)}</li>`).join("")}</ul>
      </div>
    </div>`;
}

async function runAntigravityEvaluation() {
  if (!state.analysis) return;
  if (!state.config?.has_api_key) {
    openApiKeyModal("Please add your Gemini API key first to run the Antigravity SDK agent.");
    return;
  }

  const btn = document.getElementById("runAgentBtn");
  const traceBox = document.getElementById("liveTraceBox");
  const traceTitle = document.getElementById("liveTraceTitle");
  const traceSteps = document.getElementById("liveTraceSteps");
  const usageBadge = document.getElementById("liveUsageBadge");

  btn.disabled = true;
  btn.textContent = "Evaluating with Antigravity SDK...";
  traceBox.classList.remove("hidden");
  traceTitle.textContent = "Google Antigravity SDK Agent Inspecting Workbook...";
  traceSteps.innerHTML = "";
  usageBadge.textContent = "";

  const payload = {
    deep_multi_agent: document.getElementById("deepMultiAgentToggle").checked,
    focus_notes: document.getElementById("focusNotesInput").value,
  };

  try {
    const res = await fetch(`/api/evaluate/${encodeURIComponent(state.analysis.session_id)}/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop() || "";

      for (const block of lines) {
        if (!block.startsWith("data: ")) continue;
        const ev = JSON.parse(block.slice(6));
        if (ev.type === "tool_start") {
          const span = document.createElement("span");
          span.className = "trace-badge";
          span.textContent = `▶ ${ev.tool}`;
          traceSteps.appendChild(span);
        } else if (ev.type === "tool_end") {
          const span = document.createElement("span");
          span.className = "trace-badge";
          span.textContent = `✓ ${ev.tool}${ev.duration_ms ? ` (${ev.duration_ms}ms)` : ""}`;
          traceSteps.appendChild(span);
        } else if (ev.type === "error") {
          throw new Error(ev.detail || "Agent execution failed.");
        } else if (ev.type === "complete") {
          state.aiReport = ev.report;
          state.aiTelemetry = ev.telemetry;
          const totalTok = ev.telemetry?.usage?.total_token_count ?? 0;
          traceTitle.textContent = "Google Antigravity SDK Audit Complete";
          usageBadge.textContent = `Tokens used: ${totalTok}`;
          renderAIReport();
          activateTab("ai-report");
        }
      }
    }
  } catch (err) {
    traceTitle.textContent = `Evaluation Error: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Antigravity Evaluation";
  }
}

function renderChatHistory() {
  const box = document.getElementById("chatMessages");
  if (!state.chatHistory.length) {
    box.innerHTML = `<p class="muted small">Ask any question about the loaded workbook, or paste a Tableau calculated field / custom SQL block for instant optimization advice.</p>`;
    return;
  }
  box.innerHTML = state.chatHistory
    .map(
      (m) => `<div class="msg msg-${escapeHtml(m.role)}">${escapeHtml(m.content)}</div>`
    )
    .join("");
  box.scrollTop = box.scrollHeight;
}

async function sendChatMessage(message) {
  if (!state.analysis) return;
  if (!state.config?.has_api_key) {
    openApiKeyModal("Please add a Gemini API key to chat with the Antigravity Tableau Coach.");
    return;
  }

  const sendBtn = document.getElementById("chatSendBtn");
  sendBtn.disabled = true;
  sendBtn.textContent = "Thinking...";

  state.chatHistory.push({ role: "user", content: message });
  renderChatHistory();

  try {
    const data = await fetchJson(`/api/chat/${encodeURIComponent(state.analysis.session_id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    state.chatHistory = data.chat_history;
    renderChatHistory();
  } catch (err) {
    state.chatHistory.push({ role: "assistant", content: `Error: ${err.message}` });
    renderChatHistory();
  } finally {
    sendBtn.disabled = false;
    sendBtn.textContent = "Ask Coach";
  }
}

function activateTab(tabName) {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === tabName);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("active", p.id === `tab-${tabName}`);
  });
}

function openApiKeyModal(notice = "") {
  const modal = document.getElementById("apiKeyModal");
  const fb = document.getElementById("apiKeyFeedback");
  if (notice) {
    fb.textContent = notice;
    fb.classList.remove("hidden");
  } else {
    fb.classList.add("hidden");
  }
  modal.classList.remove("hidden");
}

function closeApiKeyModal() {
  document.getElementById("apiKeyModal").classList.add("hidden");
}

// Wire events
document.addEventListener("DOMContentLoaded", async () => {
  await Promise.all([loadConfig(), loadSamples()]);

  const dropZone = document.getElementById("dropZone");
  const fileInput = document.getElementById("fileInput");

  document.getElementById("browseBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    fileInput.click();
  });
  dropZone.addEventListener("click", () => fileInput.click());
  dropZone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener("change", (e) => uploadWorkbook(e.target.files[0]));
  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    if (e.dataTransfer.files.length) uploadWorkbook(e.dataTransfer.files[0]);
  });

  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });

  document.getElementById("severityFilter").addEventListener("change", renderFindings);
  document.getElementById("categoryFilter").addEventListener("change", renderFindings);
  document.getElementById("calcSearchInput").addEventListener("input", renderCalculations);
  document.getElementById("calcFlaggedOnlyToggle").addEventListener("change", renderCalculations);

  document.getElementById("runAgentBtn").addEventListener("click", runAntigravityEvaluation);

  document.querySelectorAll(".prompt-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById("chatInput").value = btn.dataset.prompt;
    });
  });

  document.getElementById("chatForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("chatInput");
    const msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    await sendChatMessage(msg);
  });

  document.getElementById("apiKeyStatusBtn").addEventListener("click", () => openApiKeyModal());
  document.getElementById("closeModalBtn").addEventListener("click", closeApiKeyModal);
  document.getElementById("modalBackdrop").addEventListener("click", closeApiKeyModal);

  document.getElementById("apiKeyForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = document.getElementById("apiKeyInput").value;
    const persist = document.getElementById("persistKeyCheckbox").checked;
    const fb = document.getElementById("apiKeyFeedback");
    try {
      await fetchJson("/api/config/api-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: key, persist_to_env: persist }),
      });
      document.getElementById("apiKeyInput").value = "";
      await loadConfig();
      closeApiKeyModal();
    } catch (err) {
      fb.textContent = err.message;
      fb.classList.remove("hidden");
    }
  });

  document.getElementById("clearKeyBtn").addEventListener("click", async () => {
    await fetchJson("/api/config/api-key?remove_from_env=true", { method: "DELETE" });
    await loadConfig();
    closeApiKeyModal();
  });
});
