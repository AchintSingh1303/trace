"use strict";

const ROLE_RANK = { viewer: 1, tester: 2, qa_lead: 3, admin: 4 };
const TYPE_INFO = {
  functional: ["Functional", "Smoke, happy path, negative and boundary cases from acceptance criteria"],
  api: ["API", "Auth, contract, error handling and not-found checks per endpoint"],
  security: ["Security", "Authentication, access control, input handling, sessions and data exposure"],
  performance: ["Performance", "Baseline, load, stress, spike and soak scenarios with targets"],
  integration: ["Integration", "Journeys and UI-to-API consistency across several requirements"],
};

const state = {
  token: localStorage.getItem("trace_token"),
  user: null,
  projects: [],
  projectId: null,
  cycleId: null,
  summary: null,
  reqs: [],
  cases: [],
  filters: { type: "", status: "", q: "", stale: false, req: "" },
  selected: new Set(),
  gen: { running: false, panels: {}, selected: [], info: null, gaps: null, error: null, complete: null },
  streamAbort: null,
  tab: "overview",
  providers: [],
  // The API key lives here, in memory only. It is never put in localStorage or the URL.
  llm: { provider: null, model: "", key: "", strict: false },
  knowledge: [],
  quality: {},
  qualitySummary: null,
  openRun: null,
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const can = (min) => state.user && ROLE_RANK[state.user.role] >= ROLE_RANK[min];
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.add("hidden"), 4200);
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (res.status === 401 && state.token && !path.includes("/auth/login")) {
    signOut();
    throw new Error("Your session expired. Please sign in again.");
  }
  if (!res.ok) {
    let message = res.statusText;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") message = data.detail;
      else if (Array.isArray(data.detail)) message = data.detail.map((d) => String(d.msg || "").replace(/^Value error, /, "")).filter(Boolean).join(". ") || message;
      else message = JSON.stringify(data.detail);
    } catch (_) {}
    throw new Error(message);
  }
  if (raw) return res;
  return res.status === 204 ? null : res.json();
}

// ------------------------------------------------------------------ auth

async function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  try {
    const cfg = await fetch("/api/config").then((r) => r.json());
    $(".demo-users").hidden = !cfg.demo;
    const users = cfg.demo ? await fetch("/api/auth/demo-users").then((r) => r.json()) : [];
    $("#demoUsers").innerHTML = users
      .map((u) => `<button type="button" class="chip" data-email="${esc(u.email)}">${esc(u.name)} (${esc(u.role)})</button>`)
      .join("");
  } catch (_) {}
}

async function signIn(email, password) {
  $("#loginError").textContent = "";
  try {
    const data = await api("/api/auth/login", { method: "POST", body: { email, password } });
    state.token = data.access_token;
    localStorage.setItem("trace_token", state.token);
    state.user = data.user;
    await startApp();
  } catch (err) {
    $("#loginError").textContent = err.message;
  }
}

function signOut() {
  stopStream();
  state.llm = { provider: null, model: "", key: "" };
  localStorage.removeItem("trace_token");
  state.token = null;
  state.user = null;
  showLogin();
}

// ------------------------------------------------------------------ boot

async function startApp() {
  if (!state.user) state.user = await api("/api/auth/me");
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#userLabel").textContent = `${state.user.name} · ${state.user.role}`;
  $$("[data-min]").forEach((el) => (el.hidden = !can(el.dataset.min)));
  const listing = await api("/api/llm/providers");
  state.providers = listing.providers;
  if (!state.llm.provider) {
    const saved = localStorage.getItem("trace_provider");
    state.llm.provider = state.providers.some((p) => p.id === saved) ? saved : listing.default;
  }
  await loadProjects();
}

function providerInfo(id) {
  return state.providers.find((p) => p.id === id) || state.providers[0];
}

function syncGeneratorBox() {
  const info = providerInfo(state.llm.provider);
  const live = info && info.id !== "rules";
  const box = $("#gKeyBox");
  if (!box) return;
  box.hidden = !live;
  if (live) {
    $("#gKey").value = state.llm.key; // set as a property so the key never appears in the page markup
    $("#gKey").placeholder = info.server_key ? "Using the key configured on the server (optional)" : info.key_hint;
    $("#gModel").placeholder = info.default_model;
  }
}

async function loadProjects() {
  state.projects = await api("/api/projects");
  const saved = Number(localStorage.getItem("trace_project"));
  const project = state.projects.find((p) => p.id === saved) || state.projects[0];
  state.projectId = project ? project.id : null;
  renderSelectors();
  if (project) await selectCycle(Number(localStorage.getItem("trace_cycle")) || project.cycles[0]?.id);
  else renderAll();
}

function renderSelectors() {
  $("#projectSelect").innerHTML = state.projects.map((p) => `<option value="${p.id}" ${p.id === state.projectId ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  const project = state.projects.find((p) => p.id === state.projectId);
  const cycles = project ? project.cycles : [];
  $("#cycleSelect").innerHTML = cycles.map((c) => `<option value="${c.id}" ${c.id === state.cycleId ? "selected" : ""}>${esc(c.name)}</option>`).join("");
}

async function selectCycle(cycleId) {
  const project = state.projects.find((p) => p.id === state.projectId);
  const cycle = project?.cycles.find((c) => c.id === cycleId) || project?.cycles[0];
  state.cycleId = cycle ? cycle.id : null;
  localStorage.setItem("trace_project", state.projectId);
  localStorage.setItem("trace_cycle", state.cycleId);
  state.selected.clear();
  state.filters = { type: "", status: "", q: "", stale: false, req: "" };
  renderSelectors();
  await refreshCycle();
  await restoreGeneration();
}

async function refreshCycle() {
  if (!state.cycleId) return renderAll();
  const [cycle, reqs, cases, quality] = await Promise.all([
    api(`/api/cycles/${state.cycleId}`),
    api(`/api/cycles/${state.cycleId}/requirements`),
    api(`/api/cycles/${state.cycleId}/test-cases`),
    api(`/api/cycles/${state.cycleId}/quality`),
  ]);
  state.quality = Object.fromEntries(quality.requirements.map((q) => [q.code, q]));
  state.qualitySummary = quality.summary;
  state.summary = cycle.summary;
  state.reqs = reqs;
  state.cases = cases;
  renderAll();
}

// ------------------------------------------------------------------ tabs

function setTab(tab) {
  state.tab = tab;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== `tab-${tab}`));
  renderTab(tab);
}

function renderAll() {
  renderTab(state.tab);
}

function renderTab(tab) {
  if (!state.cycleId && tab !== "roadmap" && tab !== "team") {
    $(`#tab-${tab}`).innerHTML = `<div class="card empty">No project yet. Create one to get started.</div>`;
    return;
  }
  ({ overview: renderOverview, requirements: renderRequirements, generate: renderGenerate, review: renderReview, execution: renderExecution, knowledge: renderKnowledge, team: renderTeam, trace: renderTrace, roadmap: renderRoadmap }[tab])();
}

// ------------------------------------------------------------------ overview

async function renderOverview() {
  const s = state.summary;
  const el = $("#tab-overview");
  const total = s.test_cases || 1;
  const typeBars = Object.keys(TYPE_INFO)
    .map((t) => {
      const n = s.by_type[t] || 0;
      return `<div class="bar-row"><span>${cap(t)}</span><div class="bar"><span style="width:${(100 * n) / total}%"></span></div><b>${n}</b></div>`;
    })
    .join("");
  const run = s.last_run;
  el.innerHTML = `
    <div class="page-head"><div><h2>Overview</h2><div class="muted">Where this cycle stands right now.</div></div>
      <button class="btn primary" data-goto="generate">Generate test cases</button></div>
    <div class="grid kpis">
      <div class="card kpi"><div class="value">${s.requirements}</div><div class="label">Requirements</div></div>
      <div class="card kpi"><div class="value">${s.test_cases}</div><div class="label">Test cases</div></div>
      <div class="card kpi"><div class="value">${s.by_status.approved}</div><div class="label">Approved</div></div>
      <div class="card kpi"><div class="value">${s.by_status.pending}</div><div class="label">Awaiting review</div></div>
      <div class="card kpi"><div class="value">${s.coverage_pct}%</div><div class="label">Requirements covered</div></div>
      <div class="card kpi"><div class="value">${s.stale_test_cases}</div><div class="label">Stale cases</div></div>
      <div class="card kpi"><div class="value">${s.readiness.ready_pct}%</div><div class="label">Requirements passing</div></div>
      <div class="card kpi"><div class="value">${s.readiness.failing}</div><div class="label">Requirements failing</div></div>
    </div>
    ${s.stale_test_cases ? `<div class="notice warn">${s.stale_test_cases} test case(s) were written against an older version of ${s.stale_requirements} requirement(s). Run an <b>incremental</b> generation to refresh only those.</div>` : ""}
    <div class="grid two">
      <div class="card"><h3>Test cases by type</h3>${s.test_cases ? typeBars : '<div class="muted">Nothing generated yet.</div>'}</div>
      <div class="card"><h3>Last generation run</h3>${
        run
          ? `<div>${statusPill(run.status)} <span class="muted">${esc(run.mode)} mode · ${esc(run.provider)} generator</span></div>
             <div class="muted small" style="margin-top:6px">Started ${esc(new Date(run.started_at + "Z").toLocaleString())}</div>
             ${run.summary && run.summary.total !== undefined ? `<div style="margin-top:8px">${run.summary.total} created, ${run.summary.deleted} replaced${run.summary.replaced_approved ? ` (${run.summary.replaced_approved} were approved)` : ""}</div>` : ""}
             ${run.error ? `<div class="error">${esc(run.error)}</div>` : ""}`
          : '<div class="muted">No runs yet.</div>'
      }</div>
    </div>
    <div class="card" style="margin-top:14px"><h3>Recent activity</h3><div id="activity" class="muted">Loading...</div></div>`;
  try {
    const rows = await api(`/api/cycles/${state.cycleId}/audit?limit=8`);
    $("#activity").innerHTML = rows.length
      ? `<table><tbody>${rows.map((r) => `<tr><td class="nowrap muted small">${esc(new Date(r.at + "Z").toLocaleString())}</td><td>${esc(r.actor)}</td><td><code>${esc(r.action)}</code></td><td>${esc(r.entity_id)}</td></tr>`).join("")}</tbody></table>`
      : "No activity yet.";
  } catch (_) {}
}

function statusPill(status) {
  const cls = { complete: "ok", approved: "ok", running: "info", failed: "bad", rejected: "bad", pending: "warn" }[status] || "";
  return `<span class="pill ${cls}">${esc(status)}</span>`;
}

// ------------------------------------------------------------------ requirements

function renderRequirements() {
  const editable = can("qa_lead");
  $("#tab-requirements").innerHTML = `
    <div class="page-head"><div><h2>Requirements</h2>
      <div class="muted">Editing a requirement bumps its version and marks linked test cases stale.</div>
      ${qualityLine()}</div>
      ${editable ? '<div class="actions" style="margin:0"><button class="btn" id="importReq">Import CSV</button><button class="btn primary" id="addReq">Add requirement</button></div>' : ""}</div>
    <div class="table-wrap"><table><thead><tr><th>Code</th><th>Type</th><th>Requirement</th><th>Criteria</th><th>Quality</th><th>Version</th><th>Test cases</th>${editable ? "<th></th>" : ""}</tr></thead>
    <tbody>${
      state.reqs.length
        ? state.reqs
            .map(
              (r) => `<tr>
        <td class="nowrap"><b>${esc(r.code)}</b></td>
        <td><span class="pill info">${esc(r.req_type)}</span></td>
        <td><div>${esc(r.title)}</div><div class="muted small">${esc(r.description)}</div></td>
        <td>${r.acceptance_criteria.length}</td>
        <td>${qualityPill(r.code)}</td>
        <td>v${r.version}</td>
        <td>${r.test_case_count}${r.stale_test_cases ? ` <span class="pill warn">${r.stale_test_cases} stale</span>` : ""}</td>
        ${editable ? `<td class="nowrap"><button class="btn small" data-edit="${r.id}">Edit</button> <button class="btn small bad" data-del="${r.id}">Delete</button></td>` : ""}
      </tr>`
            )
            .join("")
        : `<tr><td colspan="8" class="empty">No requirements yet.</td></tr>`
    }</tbody></table></div>`;
}

const GRADE_CLASS = { A: "ok", B: "info", C: "warn", D: "bad" };

function qualityPill(code) {
  const q = state.quality[code];
  if (!q) return "";
  return `<button class="pill ${GRADE_CLASS[q.grade]}" data-quality="${esc(code)}" title="${q.findings.length} finding(s). Click to see them.">${q.grade} · ${q.score}</button>`;
}

function qualityLine() {
  const q = state.qualitySummary;
  if (!q || !q.count) return "";
  return `<div class="muted small" style="margin-top:4px">Writing quality: average ${q.average_score}/100, ${q.with_errors} requirement(s) with errors. Advice only, it never blocks generation.</div>`;
}

function openQualityModal(code) {
  const q = state.quality[code];
  if (!q) return;
  const sev = { error: "bad", warning: "warn", info: "info" };
  openModal(`<h2>${esc(code)} · quality ${q.grade} (${q.score}/100)</h2>
    <div class="muted small" style="margin-bottom:8px">${esc(q.title)}</div>
    ${q.findings.length
      ? q.findings.map((f) => `<div class="finding"><div><span class="pill ${sev[f.severity]}">${esc(f.severity)}</span> <b>${esc(f.message)}</b></div><div class="muted small">${esc(f.field)} · ${esc(f.suggestion)}</div></div>`).join("")
      : '<div class="muted">Nothing to fix. This requirement reads as testable.</div>'}
    <div class="modal-actions"><button class="btn" data-close>Close</button></div>`);
}

function openRequirementModal(req) {
  const types = ["Functional", "UI", "API", "Security", "Performance", "Journey"];
  openModal(`
    <h2>${req ? `Edit ${esc(req.code)}` : "Add requirement"}</h2>
    <label>Type<select id="rType">${types.map((t) => `<option ${req && req.req_type === t ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    <label>Title<input id="rTitle" value="${esc(req?.title || "")}" /></label>
    <label>Description<textarea id="rDesc" rows="2">${esc(req?.description || "")}</textarea></label>
    <label>Acceptance criteria (one per line)<textarea id="rAc" rows="6">${esc((req?.acceptance_criteria || []).join("\n"))}</textarea></label>
    <p id="modalError" class="error"></p>
    <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="saveReq">${req ? "Save changes" : "Add"}</button></div>`);
  $("#saveReq").onclick = async () => {
    const body = {
      req_type: $("#rType").value,
      title: $("#rTitle").value.trim(),
      description: $("#rDesc").value,
      acceptance_criteria: $("#rAc").value.split("\n"),
    };
    try {
      if (req) {
        const updated = await api(`/api/cycles/${state.cycleId}/requirements/${req.id}`, { method: "PATCH", body });
        toast(updated.version > req.version ? `${updated.code} is now v${updated.version}. ${updated.stale_test_cases} test case(s) are stale.` : "No changes to save.");
      } else {
        await api(`/api/cycles/${state.cycleId}/requirements`, { method: "POST", body });
        toast("Requirement added.");
      }
      closeModal();
      await refreshCycle();
    } catch (err) {
      $("#modalError").textContent = err.message;
    }
  };
}

// ------------------------------------------------------------------ generate

function renderGenerate() {
  const g = state.gen;
  const s = state.summary;
  const hasChanges = s.stale_test_cases > 0 || state.reqs.some((r) => r.test_case_count === 0);
  const prior = s.test_cases > 0 && hasChanges ? "incremental" : "full";
  const canRun = can("qa_lead");
  $("#tab-generate").innerHTML = `
    <div class="page-head"><div><h2>Generate test cases</h2>
      <div class="muted">Agents run in parallel and stream each case as it is written.</div></div></div>
    <div class="gen-layout">
      <div class="card">
        <h3>Generator</h3>
        <label>Who writes the test cases
          <select id="gProvider">${state.providers.map((p) => `<option value="${p.id}" ${p.id === state.llm.provider ? "selected" : ""}>${esc(p.label)}</option>`).join("")}</select>
        </label>
        <div id="gKeyBox" class="keybox" hidden>
          <label>API key<input id="gKey" type="password" autocomplete="off" spellcheck="false" /></label>
          <label>Model <span class="muted">(edit if your account uses another)</span><input id="gModel" autocomplete="off" spellcheck="false" value="${esc(state.llm.model)}" /></label>
          <div class="keyrow"><button class="btn small" id="gCheck" type="button" ${canRun ? "" : "disabled"}>Test key</button><span id="gCheckMsg" class="small"></span></div>
          <label class="type-option"><input type="checkbox" id="gStrict" ${state.llm.strict ? "checked" : ""} /><span><b>Strict grounding</b><span class="muted">Also drop cases that mention endpoints or fields the requirement never names. Cases without a real quote from the requirement are always dropped.</span></span></label>
          <p class="muted small">Sent with the Generate request, used for that run only, and never stored, logged or shown again. It is forgotten when you sign out or reload.</p>
        </div>
        <h3 style="margin-top:14px">Test types</h3>
        ${Object.entries(TYPE_INFO).map(([k, [name, desc]]) => `<label class="type-option"><input type="checkbox" name="gtype" value="${k}" checked /><span><b>${name}</b><span class="muted">${desc}</span></span></label>`).join("")}
        <h3 style="margin-top:14px">Mode</h3>
        <label class="radio-card ${prior === "full" ? "on" : ""}"><input type="radio" name="gmode" value="full" ${prior === "full" ? "checked" : ""}/><span><b>Full</b><br><span class="muted">Replace every case of the selected types. Approvals are lost.</span></span></label>
        <label class="radio-card ${prior === "incremental" ? "on" : ""}"><input type="radio" name="gmode" value="incremental" ${prior === "incremental" ? "checked" : ""}/><span><b>Incremental</b><br><span class="muted">Only new or changed requirements (${s.stale_requirements} changed, ${state.reqs.filter((r) => r.test_case_count === 0).length} new). Other cases and approvals stay.</span></span></label>
        <label>Max test cases (optional)<input id="gMax" type="number" min="1" placeholder="no limit" /></label>
        <button class="btn primary" id="runGen" style="margin-top:12px;width:100%" ${g.running || !canRun ? "disabled" : ""}>${g.running ? "Generating..." : "Generate"}</button>
        ${canRun ? "" : '<div class="muted small" style="margin-top:6px">QA lead role or higher is needed to generate.</div>'}
      </div>
      <div id="genOut"></div>
    </div>`;
  syncGeneratorBox();
  renderGenOutput();
}

function renderGenOutput() {
  const out = $("#genOut");
  if (!out) return;
  const g = state.gen;
  if (!g.info && !g.error) {
    out.innerHTML = `<div class="card empty">Choose test types and press Generate. Progress appears here live.</div>`;
    return;
  }
  const head = g.info
    ? `<div class="status-line">${g.running ? '<div class="spinner"></div><b>Generating</b>' : g.error ? '<span class="pill bad">failed</span>' : '<span class="pill ok">complete</span>'}
        <span class="muted">${esc(g.info.app)} · ${esc(g.info.mode)} mode · ${esc(g.info.generator || g.info.provider)}${g.info.model ? ` (${esc(g.info.model)})` : ""} · ${g.info.requirements} requirements${g.info.mode === "incremental" ? ` (${g.info.new_requirements} new, ${g.info.stale_requirements} changed)` : ""}</span></div>`
    : "";
  const panels = Object.keys(TYPE_INFO)
    .map((t) => {
      const p = g.panels[t] || { state: "off", count: 0, feed: [], subtypes: [], total: 0, generator: "" };
      const label = { off: "not selected", queued: "queued", running: "running", done: "done" }[p.state];
      const cls = { off: "", queued: "", running: "info", done: "ok" }[p.state];
      return `<div class="agent ${p.state}">
        <div class="agent-head"><b>${TYPE_INFO[t][0]}</b><span class="pill ${cls}">${label}${p.state === "running" || p.state === "done" ? ` · ${p.state === "done" ? p.total : p.count}` : ""}</span></div>
        ${p.state === "done" && p.generator && p.generator !== "rules" ? `<div class="small ${p.generator.includes("fallback") ? "" : "muted"}">Written by ${esc(p.generator)}</div>` : ""}
        ${p.subtypes.length ? `<div class="chips">${p.subtypes.map((x) => `<span class="pill">${esc(x.label)} ${x.count}</span>`).join("")}</div>` : ""}
        ${p.feed.length ? `<ul class="feed">${p.feed.map((f) => `<li title="${esc(f)}">${esc(f)}</li>`).join("")}</ul>` : ""}
      </div>`;
    })
    .join("");
  let done = "";
  if (g.error) done = `<div class="notice bad">Generation failed: ${esc(g.error)}</div>`;
  if (g.complete) {
    const c = g.complete;
    const gaps = c.coverage_gaps || [];
    done = `<div class="notice ok"><b>${c.total} test cases created</b> in ${c.elapsed_s}s${c.deleted ? `, replacing ${c.deleted}` : ""}${c.replaced_approved ? ` (${c.replaced_approved} of them were already approved and need re-review)` : ""}.
      <a href="#" data-goto="review">Review them</a></div>
      ${c.dropped ? `<div class="notice warn"><b>${c.dropped} generated case(s) were dropped</b> because they were not backed by a quote from your requirements. Nothing invented reached the review queue.</div>` : ""}
      ${c.warned ? `<div class="notice warn">${c.warned} case(s) mention terms that are not in the requirements or project knowledge. They are flagged in Review.</div>` : ""}
      ${(c.clarifications || []).length ? `<div class="notice"><b>The model asked ${c.clarifications.length} question(s) instead of guessing:</b><ul style="margin:6px 0 0 18px;padding:0">${c.clarifications.map((q) => `<li>${esc(q.req_code)}: ${esc(q.question)}</li>`).join("")}</ul><div class="muted small" style="margin-top:6px">Answer them in the requirement text or add a rule in the Knowledge tab, then generate again.</div></div>` : ""}
      ${gaps.length ? `<div class="notice warn"><b>${gaps.length} requirement(s) have no test cases:</b><ul style="margin:6px 0 0 18px;padding:0">${gaps.map((x) => `<li>${esc(x.code)} ${esc(x.title)} <span class="muted">(${x.reason === "agent_not_selected" ? "needs the " + x.missing_agents.join(" or ") + " agent" : "no cases generated"})</span></li>`).join("")}</ul></div>` : ""}`;
  }
  out.innerHTML = `${head}${done}<div class="agents">${panels}</div>`;
}

function resetGen() {
  state.gen = { running: false, panels: {}, selected: [], info: null, gaps: null, error: null, complete: null, lastMode: state.gen.lastMode };
}

function handleEvent(e) {
  const g = state.gen;
  const d = e.data;
  switch (e.type) {
    case "pipeline_start":
      g.running = true;
      g.info = d;
      g.selected = d.selected_types;
      g.lastMode = d.mode;
      Object.keys(TYPE_INFO).forEach((t) => (g.panels[t] = { state: d.selected_types.includes(t) ? "queued" : "off", count: 0, feed: [], subtypes: [], total: 0 }));
      break;
    case "agent_start":
      if (g.panels[d.test_type]) g.panels[d.test_type].state = "running";
      break;
    case "tc_created": {
      const p = g.panels[d.test_type];
      if (p) {
        p.count += 1;
        p.feed.push(`${cap(d.priority)} · ${d.title}`);
        if (p.feed.length > 5) p.feed.shift();
      }
      break;
    }
    case "agent_warning":
      toast(d.message, true);
      break;
    case "agent_complete": {
      const p = g.panels[d.test_type];
      if (p) Object.assign(p, { state: "done", total: d.total, subtypes: d.subtypes, generator: d.generator });
      break;
    }
    case "generation_complete":
      g.running = false;
      g.complete = d;
      break;
    case "generation_error":
      g.running = false;
      g.error = d.error;
      break;
  }
}

function paintGen() {
  if (state.tab === "generate") {
    const btn = $("#runGen");
    if (btn) {
      btn.disabled = state.gen.running || !can("qa_lead");
      btn.textContent = state.gen.running ? "Generating..." : "Generate";
    }
    renderGenOutput();
  }
}

async function startGeneration() {
  const selected = $$('input[name="gtype"]:checked').map((i) => i.value);
  if (!selected.length) return toast("Pick at least one test type.", true);
  const mode = $('input[name="gmode"]:checked').value;
  const max = $("#gMax").value ? Number($("#gMax").value) : null;
  const info = providerInfo(state.llm.provider);
  const live = info.id !== "rules";
  if (live && !state.llm.key.trim() && !info.server_key) return toast(`Paste your ${info.label} API key first, or switch to the built-in rules.`, true);
  const body = { selected_test_types: selected, mode, max_tcs: max, provider: info.id };
  if (live) {
    body.strict_grounding = !!state.llm.strict;
    if (state.llm.key.trim()) body.api_key = state.llm.key.trim();
    if (state.llm.model.trim()) body.model = state.llm.model.trim();
  }
  resetGen();
  try {
    await api(`/api/cycles/${state.cycleId}/generate`, { method: "POST", body });
  } catch (err) {
    return toast(err.message, true);
  }
  state.gen.running = true;
  paintGen();
  await openStream();
}

async function openStream() {
  stopStream();
  const abort = new AbortController();
  state.streamAbort = abort;
  const cycleId = state.cycleId;
  try {
    const res = await fetch(`/api/cycles/${cycleId}/generate/stream`, { headers: { Authorization: `Bearer ${state.token}` }, signal: abort.signal });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    resetGen();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        handleEvent(JSON.parse(line.slice(6)));
        paintGen();
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") toast("Lost the live connection: " + err.message, true);
  } finally {
    if (state.streamAbort === abort) state.streamAbort = null;
    if (state.gen.complete || state.gen.error) await refreshCycle();
  }
}

function stopStream() {
  if (state.streamAbort) state.streamAbort.abort();
  state.streamAbort = null;
}

async function restoreGeneration() {
  stopStream();
  resetGen();
  if (!state.cycleId) return;
  const status = await api(`/api/cycles/${state.cycleId}/generate/status`);
  if (!status.events.length) return;
  if (status.active) {
    openStream();
  } else {
    status.events.forEach(handleEvent);
    paintGen();
  }
}

async function checkKey() {
  const info = providerInfo(state.llm.provider);
  const msg = $("#gCheckMsg");
  if (!state.llm.key.trim()) {
    msg.className = "small error";
    msg.textContent = info.server_key ? "The server key is used when this is empty." : "Paste a key first.";
    return;
  }
  msg.className = "small muted";
  msg.textContent = "Checking...";
  try {
    const body = { provider: info.id, api_key: state.llm.key.trim() };
    if (state.llm.model.trim()) body.model = state.llm.model.trim();
    const res = await api("/api/llm/check", { method: "POST", body });
    msg.className = "small ok-text";
    msg.textContent = `Works. ${res.model} answered in ${res.latency_ms} ms.`;
  } catch (err) {
    msg.className = "small error";
    msg.textContent = err.message;
  }
}

// ------------------------------------------------------------------ review

function filteredCases() {
  const f = state.filters;
  return state.cases.filter(
    (c) =>
      (!f.type || c.test_type === f.type) &&
      (!f.status || c.status === f.status) &&
      (!f.stale || c.stale) &&
      (!f.req || c.req_links.some((l) => l.code === f.req)) &&
      (!f.q || c.title.toLowerCase().includes(f.q.toLowerCase()) || c.tc_code.toLowerCase().includes(f.q.toLowerCase()))
  );
}

function renderReview() {
  const f = state.filters;
  const reviewer = can("tester");
  const rows = filteredCases();
  const visible = new Set(rows.map((c) => c.id));
  [...state.selected].forEach((id) => !visible.has(id) && state.selected.delete(id));
  const allSelected = rows.length > 0 && rows.every((c) => state.selected.has(c.id));
  $("#tab-review").innerHTML = `
    <div class="page-head"><div><h2>Review</h2><div class="muted">${state.cases.length} test cases. Nothing moves on until a person approves it.</div></div>
      ${can("qa_lead") ? '<div class="toolbar"><button class="btn" data-export="approved">Export approved (CSV)</button><button class="btn" data-export="">Export all</button><button class="btn" data-exportpy>pytest scaffold (API)</button></div>' : ""}</div>
    <div class="toolbar">
      <select id="fType"><option value="">All types</option>${Object.keys(TYPE_INFO).map((t) => `<option value="${t}" ${f.type === t ? "selected" : ""}>${cap(t)}</option>`).join("")}</select>
      <select id="fStatus"><option value="">All statuses</option>${["pending", "approved", "rejected"].map((t) => `<option value="${t}" ${f.status === t ? "selected" : ""}>${cap(t)}</option>`).join("")}</select>
      <input id="fQ" class="grow" placeholder="Search title or code" value="${esc(f.q)}" />
      <label class="type-option" style="grid-template-columns:auto auto;align-items:center;padding:0"><input type="checkbox" id="fStale" ${f.stale ? "checked" : ""}/> Stale only</label>
      ${f.req ? `<span class="pill info">Requirement ${esc(f.req)} <a href="#" data-clearreq>clear</a></span>` : ""}
    </div>
    <div class="table-wrap"><table><thead><tr>${reviewer ? `<th><input type="checkbox" id="selAll" aria-label="Select all" ${allSelected ? "checked" : ""}/></th>` : ""}<th>ID</th><th>Test case</th><th>Type</th><th>Priority</th><th>Status</th><th>Requirements</th></tr></thead>
    <tbody>${
      rows.length
        ? rows
            .map(
              (c) => `<tr class="clickable" data-open="${c.id}">
        ${reviewer ? `<td><input type="checkbox" data-sel="${c.id}" ${state.selected.has(c.id) ? "checked" : ""} aria-label="Select ${esc(c.tc_code)}"/></td>` : ""}
        <td class="nowrap"><b>${esc(c.tc_code)}</b></td>
        <td>${esc(c.title)}${c.stale ? ' <span class="pill warn">stale</span>' : ""}</td>
        <td><span class="pill">${esc(c.test_type)}</span></td>
        <td><span class="pill ${c.priority === "critical" ? "bad" : c.priority === "high" ? "warn" : ""}">${esc(c.priority)}</span></td>
        <td>${statusPill(c.status)}</td>
        <td class="nowrap">${c.req_links.map((l) => esc(l.code)).join(", ")}</td></tr>`
            )
            .join("")
        : `<tr><td colspan="7" class="empty">${state.cases.length ? "No test cases match these filters." : "No test cases yet. Generate some first."}</td></tr>`
    }</tbody></table></div>
    ${state.selected.size ? `<div class="bulkbar"><b>${state.selected.size} selected</b><span style="flex:1"></span><button class="btn ok" data-bulk="approved">Approve</button><button class="btn bad" data-bulk="rejected">Reject</button><button class="btn" data-bulk="pending">Reset</button><button class="btn" data-bulk-clear>Clear</button></div>` : ""}`;
}

function openDrawer(caseId, editing = false) {
  api(`/api/cycles/${state.cycleId}/test-cases/${caseId}`).then((c) => {
    const reviewer = can("tester");
    const d = $("#drawer");
    state.drawerCase = c;
    if (editing) return renderDrawerEditor(c, d);
    d.innerHTML = `
      <div style="display:flex;justify-content:space-between;gap:12px"><div class="muted">${esc(c.tc_code)}</div><button class="btn small" data-close-drawer>Close</button></div>
      <h2>${esc(c.title)}</h2>
      <div class="meta"><span class="pill ${c.source === "rules" ? "" : "info"}">${esc(c.source)}</span>${c.edited ? '<span class="pill ok">corrected by a person</span>' : ""}<span class="pill">${esc(c.test_type)}</span><span class="pill">${esc(c.subtype)}</span><span class="pill ${c.priority === "critical" ? "bad" : c.priority === "high" ? "warn" : ""}">${esc(c.priority)}</span>${statusPill(c.status)}${c.stale ? '<span class="pill warn">stale</span>' : ""}</div>
      ${c.req_links.map((l) => `<div class="small ${l.stale ? "" : "muted"}">Covers <b>${esc(l.code)}</b> ${esc(l.title)} · written against v${l.written_against}${l.stale ? ` <span class="pill warn">requirement is now v${l.current_version}</span>` : ""}</div>`).join("")}
      ${c.evidence ? `<div class="evidence"><div class="muted small">Backed by this part of the requirement</div>${esc(c.evidence)}</div>` : ""}
      ${(c.warnings || []).map((w) => `<div class="notice warn">${esc(w)}</div>`).join("")}
      ${c.preconditions.length ? `<h3 style="margin-top:14px">Preconditions</h3><ul>${c.preconditions.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
      <h3 style="margin-top:14px">Steps</h3>
      <div class="table-wrap"><table><thead><tr><th>#</th><th>Action</th><th>Expected result</th></tr></thead><tbody>${c.steps.map((s) => `<tr><td>${s.seq}</td><td>${esc(s.action)}</td><td>${esc(s.expected)}</td></tr>`).join("")}</tbody></table></div>
      ${c.test_data.length ? `<h3 style="margin-top:14px">Test data</h3><div class="table-wrap"><table><thead><tr><th>Field</th><th>Value</th><th>Step</th></tr></thead><tbody>${c.test_data.map((t) => `<tr><td>${esc(t.field_name)}</td><td><code>${esc(t.value)}</code></td><td>${t.step_seq ?? ""}</td></tr>`).join("")}</tbody></table></div>` : ""}
      ${c.reviewed_by ? `<p class="muted small">Last reviewed by ${esc(c.reviewed_by)}${c.review_notes ? `: ${esc(c.review_notes)}` : ""}</p>` : ""}
      ${reviewer ? `<h3 style="margin-top:14px">Decision</h3><textarea id="reviewNotes" rows="2" placeholder="Notes (optional)">${esc(c.review_notes || "")}</textarea>
        <div class="actions"><button class="btn" data-editcase="${c.id}">Edit</button><button class="btn ok" data-decide="approved" data-id="${c.id}">Approve</button><button class="btn bad" data-decide="rejected" data-id="${c.id}">Reject</button><button class="btn" data-decide="pending" data-id="${c.id}">Reset</button></div>` : '<p class="muted small">Your role can read but not review.</p>'}`;
    d.classList.remove("hidden");
  }).catch((err) => toast(err.message, true));
}

async function decide(ids, status, notes) {
  try {
    if (ids.length === 1 && notes !== undefined) {
      await api(`/api/cycles/${state.cycleId}/test-cases/${ids[0]}`, { method: "PATCH", body: { status, review_notes: notes } });
    } else {
      await api(`/api/cycles/${state.cycleId}/test-cases`, { method: "PATCH", body: { tc_ids: ids, status } });
    }
    toast(`${ids.length} test case(s) ${status === "pending" ? "reset" : status}.`);
    state.selected.clear();
    $("#drawer").classList.add("hidden");
    await refreshCycle();
  } catch (err) {
    toast(err.message, true);
  }
}

async function exportCsv(status) {
  try {
    const res = await api(`/api/cycles/${state.cycleId}/export/csv${status ? `?status=${status}` : ""}`, { raw: true });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `cycle-${state.cycleId}-test-cases.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } catch (err) {
    toast(err.message, true);
  }
}

async function exportPytest() {
  try {
    const res = await api(`/api/cycles/${state.cycleId}/export/pytest`, { raw: true });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `test_cycle_${state.cycleId}_api.py`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    const skipped = Number(res.headers.get("X-Trace-Skipped") || 0);
    toast(skipped ? `Exported. ${skipped} case(s) had no endpoint and were left out.` : "Exported. Fill in the assertions before running it.");
  } catch (err) {
    toast(err.message, true);
  }
}

// ------------------------------------------------------------------ execution

const RESULT_CLASS = { passing: "ok", failing: "bad", blocked: "warn", not_run: "", no_approved_cases: "info", passed: "ok", failed: "bad", skipped: "info" };
const RESULT_LABEL = { passing: "passing", failing: "failing", blocked: "blocked", not_run: "not run", no_approved_cases: "no approved cases", passed: "passed", failed: "failed", skipped: "skipped" };

function runStatePill(status) {
  return `<span class="pill ${status === "open" ? "info" : "ok"}">${status}</span>`;
}

async function renderExecution() {
  const el = $("#tab-execution");
  const runs = await api(`/api/cycles/${state.cycleId}/runs`);
  if (state.openRun && !runs.some((r) => r.id === state.openRun)) state.openRun = null;
  if (state.openRun) return renderRunDetail();
  el.innerHTML = `
    <div class="page-head"><div><h2>Execution</h2><div class="muted">A run is a fixed list of the cases approved when it starts. Record what happened to each one.</div></div>
      ${can("qa_lead") ? '<button class="btn primary" data-newrun>New run</button>' : ""}</div>
    <div class="table-wrap"><table><thead><tr><th>Run</th><th>Status</th><th>Progress</th><th>Passed</th><th>Failed</th><th>Blocked</th><th>Pass rate</th><th>Started</th></tr></thead><tbody>${
      runs.length
        ? runs.map((r) => `<tr class="clickable" data-openrun="${r.id}"><td><b>${esc(r.name)}</b></td><td>${runStatePill(r.status)}</td>
          <td>${r.executed}/${r.total} (${r.progress_pct}%)</td><td>${r.passed}</td><td>${r.failed}</td><td>${r.blocked}</td>
          <td>${r.pass_rate === null ? '<span class="muted">none yet</span>' : r.pass_rate + "%"}</td><td class="muted small nowrap">${esc(new Date(r.created_at + "Z").toLocaleString())}</td></tr>`).join("")
        : '<tr><td colspan="8" class="empty">No runs yet. Approve some cases in Review, then start a run.</td></tr>'
    }</tbody></table></div>`;
}

async function renderRunDetail() {
  const el = $("#tab-execution");
  const run = await api(`/api/cycles/${state.cycleId}/runs/${state.openRun}`);
  const open = run.status === "open";
  const canRecord = open && can("tester");
  const btn = (status, label, cls, tc) => `<button class="btn small ${cls}" data-setresult="${status}" data-tc="${tc}">${label}</button>`;
  el.innerHTML = `
    <div class="page-head"><div><h2>${esc(run.name)} ${runStatePill(run.status)}</h2>
      <div class="muted">Started by ${esc(run.created_by)}. ${open ? "Results can change until the run is closed." : "Closed, so this is now a record."}</div></div>
      <div class="toolbar"><button class="btn" data-backruns>All runs</button>${open && can("qa_lead") ? '<button class="btn" data-closerun>Close run</button>' : ""}</div></div>
    <div class="grid kpis">
      <div class="card kpi"><div class="value">${run.progress_pct}%</div><div class="label">Executed (${run.executed}/${run.total})</div></div>
      <div class="card kpi"><div class="value">${run.pass_rate === null ? "-" : run.pass_rate + "%"}</div><div class="label">Pass rate</div></div>
      <div class="card kpi"><div class="value">${run.passed}</div><div class="label">Passed</div></div>
      <div class="card kpi"><div class="value">${run.failed}</div><div class="label">Failed</div></div>
      <div class="card kpi"><div class="value">${run.blocked}</div><div class="label">Blocked</div></div>
      <div class="card kpi"><div class="value">${run.skipped}</div><div class="label">Skipped</div></div>
    </div>
    ${run.results.some((r) => r.stale_in_run) ? '<div class="notice warn">A requirement behind some of these cases was edited after the run started. Those results are marked, since they may describe an older meaning.</div>' : ""}
    <div class="table-wrap"><table><thead><tr><th>ID</th><th>Test case</th><th>Priority</th><th>Result</th>${canRecord ? "<th>Notes and defect</th><th>Record</th>" : "<th>Notes</th>"}</tr></thead><tbody>${run.results.map((r) => `<tr>
      <td class="nowrap"><b>${esc(r.tc_code)}</b></td>
      <td>${esc(r.title)}${r.stale_in_run ? ' <span class="pill warn">requirement changed</span>' : ""}<div class="muted small">${esc(r.requirements.join(", "))}</div></td>
      <td><span class="pill ${r.priority === "critical" ? "bad" : r.priority === "high" ? "warn" : ""}">${esc(r.priority)}</span></td>
      <td><span class="pill ${RESULT_CLASS[r.status]}">${RESULT_LABEL[r.status]}</span>${r.executed_by ? `<div class="muted small">${esc(r.executed_by)}</div>` : ""}</td>
      ${canRecord
        ? `<td><input id="note-${r.tc_id}" placeholder="Notes (required to fail)" value="${esc(r.notes)}" style="min-width:230px" /><input id="defect-${r.tc_id}" placeholder="Defect key or link" value="${esc(r.defect_ref)}" style="margin-top:4px;min-width:230px" /></td>
           <td class="nowrap">${btn("passed", "Pass", "ok", r.tc_id)} ${btn("failed", "Fail", "bad", r.tc_id)} ${btn("blocked", "Block", "", r.tc_id)} ${btn("skipped", "Skip", "", r.tc_id)} ${btn("not_run", "Reset", "", r.tc_id)}</td>`
        : `<td>${esc(r.notes)}${r.defect_ref ? `<div class="muted small">${esc(r.defect_ref)}</div>` : ""}</td>`}
    </tr>`).join("")}</tbody></table></div>`;
}

async function refreshSummary() {
  try { state.summary = (await api(`/api/cycles/${state.cycleId}`)).summary; } catch (_) {}
}

async function recordResult(tcId, status) {
  const notes = $(`#note-${tcId}`)?.value || "";
  const defect = $(`#defect-${tcId}`)?.value || "";
  const y = window.scrollY;
  try {
    await api(`/api/cycles/${state.cycleId}/runs/${state.openRun}/results/${tcId}`, { method: "PUT", body: { status, notes, defect_ref: defect } });
    await renderRunDetail();
    window.scrollTo(0, y);
    refreshSummary();
  } catch (err) {
    toast(err.message, true);
  }
}

function openRunModal() {
  openModal(`<h2>New run</h2><div class="muted small" style="margin-bottom:8px">Includes every case that is approved right now.</div>
    <label>Name<input id="runName" placeholder="Release 1.4 regression" /></label><p id="modalError" class="error"></p>
    <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="saveRun">Start run</button></div>`);
  $("#saveRun").onclick = async () => {
    try {
      const run = await api(`/api/cycles/${state.cycleId}/runs`, { method: "POST", body: { name: $("#runName").value } });
      closeModal();
      state.openRun = run.id;
      await refreshSummary();
      renderExecution();
      toast(`Run started with ${run.total} cases.`);
    } catch (err) { $("#modalError").textContent = err.message; }
  };
}

// ------------------------------------------------------------------ traceability

async function renderTrace() {
  const el = $("#tab-trace");
  el.innerHTML = '<div class="muted">Loading...</div>';
  const m = await api(`/api/cycles/${state.cycleId}/traceability`);
  const s = m.summary;
  const stateLabel = { covered: ["ok", "covered"], pending_review: ["warn", "needs review"], uncovered: ["bad", "no cases"] };
  el.innerHTML = `
    <div class="page-head"><div><h2>Traceability</h2><div class="muted">A requirement counts as covered once at least one linked case is approved.</div></div></div>
    <div class="grid kpis">
      <div class="card kpi"><div class="value">${s.coverage_pct}%</div><div class="label">Covered</div></div>
      <div class="card kpi"><div class="value">${s.covered}</div><div class="label">Covered requirements</div></div>
      <div class="card kpi"><div class="value">${s.pending_review}</div><div class="label">Need review</div></div>
      <div class="card kpi"><div class="value">${s.uncovered}</div><div class="label">No cases</div></div>
    </div>
    <div class="table-wrap"><table><thead><tr><th>Requirement</th><th>Type</th><th>Version</th><th>Cases by type</th><th>Review progress</th><th>State</th><th>Last result</th></tr></thead><tbody>${m.rows
      .map((r) => {
        const t = r.total || 1;
        const [cls, label] = stateLabel[r.state];
        return `<tr class="clickable" data-reqfilter="${esc(r.code)}">
          <td><b>${esc(r.code)}</b> ${esc(r.title)}</td><td><span class="pill info">${esc(r.req_type)}</span></td><td>v${r.version}</td>
          <td>${Object.entries(r.by_type).map(([k, v]) => `<span class="pill">${k} ${v}</span>`).join(" ") || '<span class="muted">none</span>'}</td>
          <td><div class="stack" title="${r.statuses.approved} approved, ${r.statuses.pending} pending, ${r.statuses.rejected} rejected"><span class="a" style="width:${(100 * r.statuses.approved) / t}%"></span><span class="p" style="width:${(100 * r.statuses.pending) / t}%"></span><span class="r" style="width:${(100 * r.statuses.rejected) / t}%"></span></div>
            <div class="muted small">${r.statuses.approved}/${r.total} approved${r.stale ? ` · <span class="pill warn">${r.stale} stale</span>` : ""}</div></td>
          <td><span class="pill ${cls}">${label}</span></td>
          <td><span class="pill ${RESULT_CLASS[r.result]}">${RESULT_LABEL[r.result]}</span></td></tr>`;
      })
      .join("")}</tbody></table></div>
    <p class="muted small">Click a row to see its test cases in Review.</p>`;
}

// ------------------------------------------------------------------ roadmap

async function renderRoadmap() {
  const el = $("#tab-roadmap");
  if (!renderRoadmap.data) {
    el.innerHTML = '<div class="muted">Loading...</div>';
    renderRoadmap.data = await fetch("/api/roadmap").then((r) => r.json());
  }
  const r = renderRoadmap.data;
  const cols = [["shipped", "Built in this demo", "ok"], ["next", "Considering next", "info"], ["later", "Later", ""]];
  el.innerHTML = `
    <div class="page-head"><div><h2>Roadmap</h2><div class="muted" style="max-width:760px">${esc(r.intro)}</div></div></div>
    <div class="road-cols">${cols
      .map(
        ([status, title, cls]) => `<div class="road-col"><h3>${title} <span class="pill ${cls}">${r.items.filter((i) => i.status === status).length}</span></h3>
        ${r.items
          .filter((i) => i.status === status)
          .map((i) => `<div class="card road-item"><span class="pill">${esc(i.area)}</span>${i.effort ? ` <span class="pill">${esc(i.effort)}</span>` : ""}<h4>${esc(i.title)}</h4><p>${esc(i.summary)}</p><p class="why"><b>Why:</b> ${esc(i.why)}</p></div>`)
          .join("")}</div>`
      )
      .join("")}</div>
    <div class="card" style="margin-top:18px"><h3>Principles behind the list</h3><ul class="lessons">${r.principles.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>`;
}

// ------------------------------------------------------------------ modal helpers

function openModal(html) {
  $("#modalCard").innerHTML = html;
  $("#modal").classList.remove("hidden");
  $("#modalCard input, #modalCard textarea")?.focus();
}
function closeModal() {
  $("#modal").classList.add("hidden");
}

// ------------------------------------------------------------------ case editor

function renderDrawerEditor(c, d) {
  d.innerHTML = `
    <div style="display:flex;justify-content:space-between;gap:12px"><div class="muted">${esc(c.tc_code)} · editing</div><button class="btn small" data-canceledit>Cancel</button></div>
    <p class="muted small">Corrections are kept. Approved and corrected cases are shown to the model as examples the next time you generate for this project.</p>
    <label>Title<input id="eTitle" value="${esc(c.title)}" /></label>
    <label>Priority<select id="ePriority">${["critical", "high", "medium", "low"].map((p) => `<option ${p === c.priority ? "selected" : ""}>${p}</option>`).join("")}</select></label>
    <label>Preconditions (one per line)<textarea id="ePre" rows="2">${esc((c.preconditions || []).join("\n"))}</textarea></label>
    <h3 style="margin-top:12px">Steps</h3>
    <div id="eSteps">${c.steps.map(stepRow).join("")}</div>
    <div class="actions"><button class="btn small" data-addstep>Add step</button></div>
    <p id="editError" class="error"></p>
    <div class="actions"><button class="btn primary" id="saveCase">Save correction</button></div>`;
  d.classList.remove("hidden");
}

function stepRow(s = { action: "", expected: "" }) {
  return `<div class="step-row"><input class="s-action" placeholder="Action" value="${esc(s.action)}" /><input class="s-expected" placeholder="Expected result" value="${esc(s.expected)}" /><button class="btn small bad" data-rmstep type="button" aria-label="Remove step">x</button></div>`;
}

function addEditorStep() {
  $("#eSteps").insertAdjacentHTML("beforeend", stepRow());
}

async function saveCaseEdit() {
  const c = state.drawerCase;
  const steps = $$(".step-row").map((r) => ({ action: $(".s-action", r).value, expected: $(".s-expected", r).value }));
  try {
    await api(`/api/cycles/${state.cycleId}/test-cases/${c.id}`, {
      method: "PUT",
      body: { title: $("#eTitle").value, priority: $("#ePriority").value, preconditions: $("#ePre").value.split("\n"), steps },
    });
    toast("Correction saved.");
    await refreshCycle();
    openDrawer(c.id);
  } catch (err) {
    $("#editError").textContent = err.message;
  }
}

// ------------------------------------------------------------------ requirement import

function openImportModal() {
  openModal(`
    <h2>Import requirements from CSV</h2>
    <p class="muted small">Columns: <code>title</code> (required), <code>type</code>, <code>description</code>, <code>acceptance_criteria</code> (separate several with <code>|</code>), <code>code</code> (optional).
      <a href="#" id="dlTemplate">Download a template</a></p>
    <label>Choose a CSV file<input id="impFile" type="file" accept=".csv,text/csv" /></label>
    <label>or paste the rows<textarea id="impText" rows="6" placeholder="title,type,description,acceptance_criteria"></textarea></label>
    <div id="impResult"></div>
    <div class="modal-actions"><button class="btn" data-close>Close</button><button class="btn primary" id="runImport">Import</button></div>`);
  $("#impFile").onchange = async (e) => { const f = e.target.files[0]; if (f) $("#impText").value = await f.text(); };
  $("#dlTemplate").onclick = async (e) => {
    e.preventDefault();
    const res = await api(`/api/cycles/${state.cycleId}/requirements/template.csv`, { raw: true });
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: "requirements-template.csv" });
    a.click();
    URL.revokeObjectURL(url);
  };
  $("#runImport").onclick = async () => {
    const text = $("#impText").value.trim();
    if (!text) return ($("#impResult").innerHTML = '<p class="error">Choose a file or paste some rows first.</p>');
    try {
      const r = await api(`/api/cycles/${state.cycleId}/requirements/import`, { method: "POST", body: { csv: text } });
      $("#impResult").innerHTML = `<div class="notice ${r.created ? "ok" : "warn"}"><b>${r.created} requirement(s) imported.</b></div>${r.errors.length ? `<div class="notice warn"><b>${r.errors.length} row(s) skipped</b><ul style="margin:6px 0 0 18px;padding:0">${r.errors.map((x) => `<li>${x.line ? "Line " + x.line + ": " : ""}${esc(x.error)}</li>`).join("")}</ul></div>` : ""}`;
      if (r.created) await refreshCycle();
    } catch (err) {
      $("#impResult").innerHTML = `<p class="error">${esc(err.message)}</p>`;
    }
  };
}

// ------------------------------------------------------------------ knowledge

const KIND_INFO = {
  glossary: ["Term", "What a word means in this product, e.g. 'Hold: a 24 hour reservation on a book'."],
  rule: ["Rule", "A fact that is always true, e.g. 'Cards lock after 5 failed sign-ins and unlock after 30 minutes'."],
  avoid: ["Avoid", "Something generated cases must never assume, e.g. 'There is no guest checkout'."],
};

async function renderKnowledge() {
  const el = $("#tab-knowledge");
  const editable = can("qa_lead");
  try {
    state.knowledge = await api(`/api/projects/${state.projectId}/knowledge`);
  } catch (err) {
    el.innerHTML = `<div class="card empty">${esc(err.message)}</div>`;
    return;
  }
  const kind = state.kindDraft || "rule";
  state.kindDraft = kind;
  const group = (k) => state.knowledge.filter((i) => i.kind === k);
  el.innerHTML = `
    <div class="page-head"><div><h2>Project knowledge</h2>
      <div class="muted">What the model must know about this product. It is added to every Claude or ChatGPT run for this project, so generated cases use your terms and respect your rules instead of guessing.</div></div></div>
    <div class="gen-layout">
      <div class="card">
        <h3>Teach the generator</h3>
        ${editable ? `
        <div class="actions" style="margin:0 0 8px">${Object.entries(KIND_INFO).map(([k, [name]]) => `<button class="btn small ${k === kind ? "primary" : ""}" data-kind="${k}" type="button">${name}</button>`).join("")}</div>
        <p class="muted small" id="kindHelp">${esc(KIND_INFO[kind][1])}</p>
        <label>Short title<input id="kTitle" maxlength="200" /></label>
        <label>Details<textarea id="kContent" rows="4" maxlength="2000"></textarea></label>
        <p id="kError" class="error"></p>
        <button class="btn primary" id="addKnowledge" style="width:100%">Add</button>` : '<p class="muted">QA lead role or higher is needed to add knowledge.</p>'}
        <h3 style="margin-top:16px">It also learns from review</h3>
        <p class="muted small">Cases you approve or correct are shown to the model as examples. Cases you reject with a note are shown as mistakes not to repeat. The built-in rules generator does not use any of this.</p>
      </div>
      <div>
        ${state.knowledge.length ? Object.entries(KIND_INFO).map(([k, [name]]) => group(k).length ? `<div class="card" style="margin-bottom:12px"><h3>${name}s <span class="pill">${group(k).length}</span></h3>
          ${group(k).map((i) => `<div class="k-item"><div><b>${esc(i.title)}</b><div class="small">${esc(i.content)}</div><div class="muted small">${esc(i.created_by)}</div></div>${editable ? `<button class="btn small bad" data-delknowledge="${i.id}">Delete</button>` : ""}</div>`).join("")}</div>` : "").join("") : '<div class="card empty">Nothing here yet. Add a term, a rule or something to avoid.</div>'}
      </div>
    </div>`;
}

async function addKnowledge() {
  try {
    await api(`/api/projects/${state.projectId}/knowledge`, { method: "POST", body: { kind: state.kindDraft, title: $("#kTitle").value, content: $("#kContent").value } });
    toast("Added. It will be used the next time you generate.");
    renderKnowledge();
  } catch (err) {
    $("#kError").textContent = err.message.includes("[") ? "Fill in both a title and the details." : err.message;
  }
}

async function deleteKnowledge(id) {
  try {
    await api(`/api/projects/${state.projectId}/knowledge/${id}`, { method: "DELETE" });
    renderKnowledge();
  } catch (err) {
    toast(err.message, true);
  }
}

// ------------------------------------------------------------------ team (admin)

const ROLES = ["viewer", "tester", "qa_lead", "admin"];
const ds_isRole = (t) => t.dataset && t.dataset.roleuser !== undefined;

async function renderTeam() {
  const el = $("#tab-team");
  let users;
  try {
    users = await api("/api/users");
  } catch (err) {
    el.innerHTML = `<div class="card empty">${esc(err.message)}</div>`;
    return;
  }
  el.innerHTML = `
    <div class="page-head"><div><h2>Team</h2><div class="muted">Viewers read, testers review, QA leads manage requirements and generate, admins manage people.</div></div></div>
    <div class="gen-layout">
      <div class="card"><h3>Add a person</h3>
        <label>Name<input id="uName" /></label><label>Email<input id="uEmail" type="email" /></label>
        <label>Role<select id="uRole">${ROLES.map((r) => `<option ${r === "tester" ? "selected" : ""}>${r}</option>`).join("")}</select></label>
        <label>Temporary password (8+ characters)<input id="uPass" type="password" autocomplete="new-password" /></label>
        <p id="uError" class="error"></p><button class="btn primary" id="addUser" style="width:100%">Add</button></div>
      <div class="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th></th></tr></thead><tbody>
        ${users.map((u) => `<tr><td>${esc(u.name)}</td><td>${esc(u.email)}</td>
          <td><select data-roleuser="${u.id}">${ROLES.map((r) => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}</select></td>
          <td>${u.id === state.user.id ? '<span class="muted small">you</span>' : `<button class="btn small bad" data-deluser="${u.id}">Remove</button>`}</td></tr>`).join("")}
      </tbody></table></div></div>`;
}

async function addUser() {
  try {
    await api("/api/users", { method: "POST", body: { name: $("#uName").value, email: $("#uEmail").value, role: $("#uRole").value, password: $("#uPass").value } });
    toast("Person added.");
    renderTeam();
  } catch (err) {
    $("#uError").textContent = err.message.includes("[") ? "Enter a name, an email and a password of 8 or more characters." : err.message;
  }
}

async function changeRole(id, role) {
  try { await api(`/api/users/${id}`, { method: "PATCH", body: { role } }); toast("Role updated."); }
  catch (err) { toast(err.message, true); }
  renderTeam();
}

async function deleteUser(id) {
  if (!confirm("Remove this person? They will no longer be able to sign in.")) return;
  try { await api(`/api/users/${id}`, { method: "DELETE" }); toast("Person removed."); renderTeam(); }
  catch (err) { toast(err.message, true); }
}

// ------------------------------------------------------------------ events

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, a, tr[data-open], tr[data-reqfilter], tr[data-openrun], .radio-card, [data-goto]");
  if (!t) return;
  const ds = t.dataset;

  if (ds.email) return signIn(ds.email, "demo123");
  if (t.closest("#tabs") && ds.tab) return setTab(ds.tab);
  if (ds.goto) { ev.preventDefault(); return setTab(ds.goto); }
  if (t.id === "logoutBtn") return signOut();
  if (t.id === "newProjectBtn") {
    openModal(`<h2>New project</h2><label>Name<input id="pName" /></label><label>Description<textarea id="pDesc" rows="2"></textarea></label><p id="modalError" class="error"></p>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="savePrj">Create</button></div>`);
    $("#savePrj").onclick = async () => {
      try {
        const p = await api("/api/projects", { method: "POST", body: { name: $("#pName").value, description: $("#pDesc").value } });
        closeModal();
        state.projectId = p.id;
        await loadProjects();
        localStorage.setItem("trace_project", p.id);
        toast("Project created. Add requirements to get started.");
        setTab("requirements");
      } catch (err) { $("#modalError").textContent = err.message; }
    };
    return;
  }
  if (t.id === "newCycleBtn") {
    openModal(`<h2>New cycle</h2><label>Name<input id="cName" placeholder="Sprint 2" /></label><p id="modalError" class="error"></p>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="saveCyc">Create</button></div>`);
    $("#saveCyc").onclick = async () => {
      try {
        const c = await api(`/api/projects/${state.projectId}/cycles`, { method: "POST", body: { name: $("#cName").value } });
        closeModal();
        await loadProjects();
        await selectCycle(c.id);
        toast("Cycle created.");
      } catch (err) { $("#modalError").textContent = err.message; }
    };
    return;
  }
  if (ds.close !== undefined) return closeModal();
  if (ds.closeDrawer !== undefined) return $("#drawer").classList.add("hidden");
  if (ds.quality) return openQualityModal(ds.quality);
  if (ds.exportpy !== undefined) return exportPytest();
  if (ds.newrun !== undefined) return openRunModal();
  if (ds.openrun) { state.openRun = Number(ds.openrun); return renderExecution(); }
  if (ds.backruns !== undefined) { state.openRun = null; return renderExecution(); }
  if (ds.setresult) return recordResult(Number(ds.tc), ds.setresult);
  if (ds.closerun !== undefined) {
    if (!confirm("Close this run? Results can no longer be changed.")) return;
    try { await api(`/api/cycles/${state.cycleId}/runs/${state.openRun}/close`, { method: "POST" }); toast("Run closed."); renderRunDetail(); }
    catch (err) { toast(err.message, true); }
    return;
  }
  if (t.id === "addReq") return openRequirementModal(null);
  if (t.id === "importReq") return openImportModal();
  if (ds.editcase) return openDrawer(Number(ds.editcase), true);
  if (ds.canceledit !== undefined) return openDrawer(state.drawerCase.id);
  if (ds.addstep !== undefined) return addEditorStep();
  if (ds.rmstep !== undefined) { t.closest(".step-row").remove(); return; }
  if (t.id === "saveCase") return saveCaseEdit();
  if (t.id === "addKnowledge") return addKnowledge();
  if (ds.delknowledge) return deleteKnowledge(Number(ds.delknowledge));
  if (ds.kind) { state.kindDraft = ds.kind; $$("[data-kind]").forEach((b) => b.classList.toggle("primary", b === t)); $("#kindHelp").textContent = KIND_INFO[ds.kind][1]; return; }
  if (t.id === "addUser") return addUser();
  if (ds.deluser) return deleteUser(Number(ds.deluser));
  if (ds.edit) return openRequirementModal(state.reqs.find((r) => r.id === Number(ds.edit)));
  if (ds.del) {
    const req = state.reqs.find((r) => r.id === Number(ds.del));
    if (!confirm(`Delete ${req.code}? Test cases that only cover it are deleted too.`)) return;
    try { await api(`/api/cycles/${state.cycleId}/requirements/${req.id}`, { method: "DELETE" }); toast("Requirement deleted."); await refreshCycle(); }
    catch (err) { toast(err.message, true); }
    return;
  }
  if (t.id === "runGen") return startGeneration();
  if (t.id === "gCheck") return checkKey();
  if (t.classList.contains("radio-card")) {
    $$(".radio-card").forEach((c) => c.classList.toggle("on", c === t));
    return;
  }
  if (ds.export !== undefined) return exportCsv(ds.export);
  if (ds.bulk) return decide([...state.selected], ds.bulk);
  if (ds.bulkClear !== undefined) { state.selected.clear(); return renderReview(); }
  if (ds.clearreq !== undefined) { ev.preventDefault(); state.filters.req = ""; return renderReview(); }
  if (ds.decide) {
    const notes = $("#reviewNotes") ? $("#reviewNotes").value : undefined;
    return decide([Number(ds.id)], ds.decide, notes);
  }
  if (ds.reqfilter) {
    state.filters = { type: "", status: "", q: "", stale: false, req: ds.reqfilter };
    return setTab("review");
  }
  if (ds.open && !ev.target.closest("input")) return openDrawer(Number(ds.open));
});

document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (t.id === "projectSelect") { state.projectId = Number(t.value); const p = state.projects.find((x) => x.id === state.projectId); selectCycle(p.cycles[0]?.id); }
  else if (t.id === "cycleSelect") selectCycle(Number(t.value));
  else if (t.id === "gProvider") {
    state.llm.provider = t.value;
    state.llm.model = "";
    localStorage.setItem("trace_provider", t.value);
    $("#gCheckMsg").textContent = "";
    $("#gModel").value = "";
    syncGeneratorBox();
  }
  else if (t.id === "gStrict") state.llm.strict = t.checked;
  else if (ds_isRole(t)) changeRole(Number(t.dataset.roleuser), t.value);
  else if (t.id === "fType") { state.filters.type = t.value; renderReview(); }
  else if (t.id === "fStatus") { state.filters.status = t.value; renderReview(); }
  else if (t.id === "fStale") { state.filters.stale = t.checked; renderReview(); }
  else if (t.id === "selAll") { filteredCases().forEach((c) => (t.checked ? state.selected.add(c.id) : state.selected.delete(c.id))); renderReview(); }
  else if (t.dataset.sel) { t.checked ? state.selected.add(Number(t.dataset.sel)) : state.selected.delete(Number(t.dataset.sel)); renderReview(); }
});

document.addEventListener("input", (ev) => {
  if (ev.target.id === "gKey") state.llm.key = ev.target.value;
  else if (ev.target.id === "gModel") state.llm.model = ev.target.value;
  if (ev.target.id === "fQ") {
    state.filters.q = ev.target.value;
    const pos = ev.target.selectionStart;
    renderReview();
    const box = $("#fQ");
    box.focus();
    box.setSelectionRange(pos, pos);
  }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { closeModal(); $("#drawer").classList.add("hidden"); }
});

$("#loginForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  signIn($("#loginEmail").value.trim().toLowerCase(), $("#loginPassword").value);
});

(async function boot() {
  if (!state.token) return showLogin();
  try { await startApp(); } catch (_) { signOut(); }
})();
