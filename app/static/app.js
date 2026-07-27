"use strict";

const api = (path, opts) => fetch("/api" + path, opts).then(async (r) => {
  if (r.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.status === 204 ? null : r.json();
});

const el = (id) => document.getElementById(id);
const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// Cap long status/error text so a huge unbroken string (e.g. a Radware block
// URL) can't blow out the queue layout.
const clip = (s, n = 280) => { s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n) + "…" : s; };

// --------------------------------------------------------------------------
// Autocomplete
// --------------------------------------------------------------------------
const search = el("search");
const suggestions = el("suggestions");
let activeIdx = -1;
let lastResults = [];
let debounce;

search.addEventListener("input", () => {
  clearTimeout(debounce);
  const q = search.value.trim();
  debounce = setTimeout(() => runSearch(q), 150);
});

search.addEventListener("keydown", (e) => {
  const items = [...suggestions.querySelectorAll("li")];
  if (e.key === "ArrowDown") { activeIdx = Math.min(activeIdx + 1, items.length - 1); paintActive(items); e.preventDefault(); }
  else if (e.key === "ArrowUp") { activeIdx = Math.max(activeIdx - 1, 0); paintActive(items); e.preventDefault(); }
  else if (e.key === "Enter" && activeIdx >= 0) { selectCompany(lastResults[activeIdx]); e.preventDefault(); }
  else if (e.key === "Escape") { hideSuggestions(); }
});

document.addEventListener("click", (e) => {
  if (!e.target.closest(".search-wrap")) hideSuggestions();
});

function paintActive(items) {
  items.forEach((li, i) => li.classList.toggle("active", i === activeIdx));
}
function hideSuggestions() { suggestions.hidden = true; activeIdx = -1; }

async function runSearch(q) {
  if (!q) { hideSuggestions(); return; }
  try {
    lastResults = await api(`/companies/search?q=${encodeURIComponent(q)}&limit=12`);
  } catch { return; }
  if (!lastResults.length) {
    const digits = q.replace(/\D/g, "");
    const addItem = digits
      ? `<li id="sugg-add" data-num="${esc(digits)}">➕ Use SEDAR #${esc(digits)} — add exchange &amp; ticker below</li>`
      : `<li class="empty">No local match. Add it by SEDAR number below.</li>`;
    suggestions.innerHTML = addItem;
    const add = document.getElementById("sugg-add");
    if (add) add.addEventListener("click", () => prefillAdd({ number: add.dataset.num }));
    suggestions.hidden = false;
    return;
  }
  suggestions.innerHTML = lastResults.map((c, i) => `
    <li data-i="${i}">
      <span>${esc(c.name)} ${c.saved ? '<span class="tag saved">saved</span>' : ""}</span>
      <span class="meta">${esc(c.type || "")} · ${esc(c.jurisdiction || "")} · #${esc(c.number)}</span>
    </li>`).join("");
  [...suggestions.querySelectorAll("li")].forEach((li) =>
    li.addEventListener("click", () => selectCompany(lastResults[+li.dataset.i])));
  activeIdx = -1;
  suggestions.hidden = false;
}

// Selecting a company prefills the add row (exchange + ticker still required)
// rather than downloading immediately, since they name the company's R2 folder.
function selectCompany(c) {
  if (!c) return;
  prefillAdd(c);
}

function prefillAdd(c) {
  hideSuggestions();
  search.value = "";
  el("add-number").value = c.number || "";
  el("add-name").value = c.name || "";
  el("add-exchange").value = c.exchange || "";
  el("add-ticker").value = c.ticker || "";
  (c.exchange ? el("add-ticker") : el("add-exchange")).focus();
}

async function addByNumber() {
  const number = el("add-number").value.trim();
  const name = el("add-name").value.trim();
  const exchange = el("add-exchange").value.trim();
  const ticker = el("add-ticker").value.trim();
  if (!number) { el("add-number").focus(); return; }
  if (!exchange || !ticker) {
    alert("Enter an exchange and ticker — together they name the company's R2 folder.");
    (exchange ? el("add-ticker") : el("add-exchange")).focus();
    return;
  }
  const maxBatches = el("test-mode").checked ? 1 : null;
  try {
    await api(`/companies/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ number, name: name || null, exchange, ticker, download: true, max_batches: maxBatches }),
    });
  } catch (e) { alert("Could not add company: " + e.message); return; }
  hideSuggestions();
  search.value = "";
  ["add-number", "add-name", "add-exchange", "add-ticker"].forEach((id) => (el(id).value = ""));
  refreshAll();
}

el("add-btn").addEventListener("click", addByNumber);

el("enumerate-btn").addEventListener("click", async () => {
  if (!confirm("Build/refresh the full company catalog? This is a long browser job that pages through every issuer. It may pause for a CAPTCHA — solve it in the live view and it keeps going.")) return;
  try {
    await api("/catalog/enumerate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_type: "Company" }),
    });
  } catch (e) { alert("Could not queue enumeration: " + e.message); return; }
  loadQueue();
});

el("clear-queue-btn").addEventListener("click", async () => {
  try { await api("/queue/clear", { method: "POST" }); }
  catch (e) { alert("Could not clear: " + e.message); return; }
  loadQueue();
});

el("retry-failed-btn").addEventListener("click", async () => {
  try {
    const r = await api("/queue/retry-failed", { method: "POST" });
    if (!r.requeued) alert("Nothing to retry.");
  } catch (e) { alert("Could not retry: " + e.message); return; }
  loadQueue();
});

el("enumerate-pause-btn").addEventListener("click", async (e) => {
  const id = e.currentTarget.dataset.jobId;
  if (!id) return;
  e.currentTarget.disabled = true;
  try { await api(`/jobs/${id}/pause`, { method: "POST" }); }
  catch (err) { alert("Could not pause: " + err.message); }
  loadQueue();
});

// Reflect enumerate state on the controls: running -> Pause; paused -> Resume;
// otherwise -> Build/refresh. Prevents double-queueing a second full run.
function updateEnumerateControls(jobs) {
  const running = jobs.find((j) => j.kind === "enumerate_catalog" && j.status === "running");
  const queued = jobs.find((j) => j.kind === "enumerate_catalog" && j.status === "queued");
  const paused = jobs.find((j) => j.kind === "enumerate_catalog" && j.status === "paused");
  const btn = el("enumerate-btn");
  const pauseBtn = el("enumerate-pause-btn");
  const hint = el("enumerate-hint");
  if (running || queued) {
    btn.hidden = true;
    pauseBtn.hidden = !running;
    if (running) {
      pauseBtn.dataset.jobId = running.id;
      pauseBtn.disabled = !!running.pause_requested;
    }
    hint.textContent = running ? "running — " + clip(running.message || "…", 60) : "queued…";
  } else if (paused) {
    btn.hidden = false;
    btn.textContent = "▶ Resume enumeration";
    pauseBtn.hidden = true;
    hint.textContent = "paused — " + clip(paused.message || "", 60);
  } else {
    btn.hidden = false;
    btn.textContent = "↻ Build / refresh company catalog";
    pauseBtn.hidden = true;
    hint.textContent = "";
  }
}

// --------------------------------------------------------------------------
// Saved companies + queue + stats
// --------------------------------------------------------------------------
async function loadStats() {
  try {
    const s = await api("/catalog/stats");
    el("stats").innerHTML =
      `<span><b>${s.companies}</b> in catalog</span>
       <span><b>${s.saved}</b> saved</span>
       <span><b>${s.documents}</b> documents</span>`;
    el("catalog-hint").textContent = s.companies
      ? `Autocomplete searches ${s.companies} locally-stored companies.`
      : "Catalog is empty — run an enumerate job to populate autocomplete (see README).";
  } catch {}
}

// Held vs. what SEDAR+ reports, so an incomplete company is obvious rather than
// looking finished just because the job said "done".
function coverageLabel(c) {
  const held = c.total_documents || 0;
  if (!c.reported_total) return `${held} document(s) downloaded`;
  const missing = Math.max(0, c.reported_total - held);
  return c.is_complete
    ? `<span class="cov ok">✓ ${held}/${c.reported_total} complete</span>`
    : `<span class="cov warn">${held}/${c.reported_total} — ${missing} missing</span>`;
}

async function loadSaved() {
  let rows = [];
  try { rows = await api("/saved"); } catch {}
  const box = el("saved");
  if (!rows.length) { box.innerHTML = `<div class="empty">No saved companies yet. Search above to add one.</div>`; return; }
  box.innerHTML = rows.map((c) => `
    <div class="card">
      <div class="card-main">
        <div class="name" data-id="${c.id}" data-name="${esc(c.name)}">${esc(c.name)}</div>
        <div class="sub">${esc(c.type || "")} · ${esc(c.jurisdiction || "")} · #${esc(c.number)}</div>
        <div class="docs">${coverageLabel(c)}</div>
        <div class="slug" data-slug="${c.id}">
          ${c.folder_slug
            ? `<span class="slug-tag">${esc(c.folder_slug)}/</span>`
            : `<span class="slug-warn">⚠ no exchange/ticker set</span>`}
          <button class="link-btn" data-edit="${c.id}">edit</button>
        </div>
        <div class="slug-edit" data-editrow="${c.id}" hidden>
          <input class="mini" data-ex="${c.id}" placeholder="exchange" value="${esc(c.exchange || "")}" />
          <input class="mini" data-tk="${c.id}" placeholder="ticker" value="${esc(c.ticker || "")}" />
          <button class="small" data-savemeta="${c.id}">save</button>
          <button class="link-btn" data-canceledit="${c.id}">cancel</button>
        </div>
      </div>
      <button class="ghost small" data-recheck="${c.id}">Check new</button>
    </div>`).join("");
  box.querySelectorAll(".name").forEach((n) =>
    n.addEventListener("click", () => openDrawer(+n.dataset.id, n.dataset.name)));
  box.querySelectorAll("[data-recheck]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/companies/${b.dataset.recheck}/recheck`, { method: "POST" });
      loadQueue();
    }));
  const toggleEdit = (id, editing) => {
    box.querySelector(`[data-slug="${id}"]`).hidden = editing;
    box.querySelector(`[data-editrow="${id}"]`).hidden = !editing;
  };
  box.querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => toggleEdit(b.dataset.edit, true)));
  box.querySelectorAll("[data-canceledit]").forEach((b) =>
    b.addEventListener("click", () => toggleEdit(b.dataset.canceledit, false)));
  box.querySelectorAll("[data-savemeta]").forEach((b) =>
    b.addEventListener("click", async () => {
      const id = b.dataset.savemeta;
      const exchange = box.querySelector(`[data-ex="${id}"]`).value.trim();
      const ticker = box.querySelector(`[data-tk="${id}"]`).value.trim();
      try {
        // download:false -> just persist exchange/ticker without queueing a job.
        await api(`/companies/${id}/save`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ download: false, exchange, ticker }),
        });
      } catch (e) { alert("Could not save: " + e.message); return; }
      loadSaved();
    }));
}

async function loadQueue() {
  let jobs = [];
  try { jobs = await api("/queue?include_finished=true&limit=40"); } catch {}
  updateEnumerateControls(jobs);
  el("clear-queue-btn").hidden = !jobs.some((j) => ["done", "failed", "cancelled"].includes(j.status));
  el("retry-failed-btn").hidden = !jobs.some((j) => j.status === "failed" && ["download_company", "recheck_company"].includes(j.kind));
  const box = el("queue");
  if (!jobs.length) { box.innerHTML = `<div class="empty">Queue is empty.</div>`; return; }
  const vncUrl = vncViewUrl();
  box.innerHTML = jobs.map((j) => {
    // A finished job shows a full bar (a capped/test download is "done" even
    // though documents_done < total_documents); otherwise show real progress.
    const pct = (j.status === "done")
      ? 100
      : (j.total_documents ? Math.min(100, Math.round(100 * j.documents_done / j.total_documents)) : 0);
    const label = j.company_name || (j.kind === "enumerate_catalog" ? "Catalog enumeration" : "job #" + j.id);
    const cancel = j.status === "queued" ? `<button class="ghost small" data-cancel="${j.id}">cancel</button>` : "";
    const statusLabel = j.blocked ? "blocked" : j.status;
    const statusCls = j.blocked ? "blocked" : j.status;
    const solve = j.blocked
      ? `<a class="solve-btn" href="${vncUrl}" target="_blank" rel="noopener">🔓 Solve CAPTCHA in live browser view</a>`
      : "";
    return `<div class="job${j.blocked ? " is-blocked" : ""}">
      <div class="row">
        <span class="title">${esc(label)}</span>
        <span>${cancel} <span class="status ${statusCls}">${statusLabel}</span></span>
      </div>
      ${j.message ? `<div class="msg">${esc(clip(j.message))}</div>` : ""}
      ${solve}
      ${j.error ? `<div class="msg" style="color:var(--err)" title="${esc(j.error)}">${esc(clip(j.error))}</div>` : ""}
      <div class="bar"><span style="width:${pct}%"></span></div>
    </div>`;
  }).join("");
  box.querySelectorAll("[data-cancel]").forEach((b) =>
    b.addEventListener("click", async () => {
      try { await api(`/jobs/${b.dataset.cancel}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
      loadQueue();
    }));
}

// --------------------------------------------------------------------------
// Documents drawer
// --------------------------------------------------------------------------
let drawerCompanyId = null;
async function openDrawer(id, name) {
  drawerCompanyId = id;
  el("drawer-title").textContent = name;
  el("drawer-meta").textContent = "Loading documents…";
  el("drawer-docs").innerHTML = "";
  el("drawer").hidden = false;
  let docs = [];
  try { docs = await api(`/companies/${id}/documents`); } catch {}
  el("drawer-meta").textContent = `${docs.length} document(s) downloaded`;
  el("drawer-docs").innerHTML = docs.length ? docs.map((d) => `
    <div class="doc">
      <div class="t">${esc(d.title || "(untitled)")}</div>
      <div class="d">
        <span>${esc(d.submitted || "")}</span>
        <span>${esc(d.jurisdiction || "")}</span>
        <span>${esc(d.file_size || "")}</span>
        ${d.batch_zip ? `<a href="/api/files/download?path=${encodeURIComponent(d.batch_zip)}">download batch zip</a>` : ""}
      </div>
    </div>`).join("") : `<div class="empty">No documents downloaded yet.</div>`;
}

el("drawer-close").addEventListener("click", () => { el("drawer").hidden = true; });
el("drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") el("drawer").hidden = true; });
el("btn-redownload").addEventListener("click", async () => {
  if (drawerCompanyId) {
    const qs = el("test-mode").checked ? "?max_batches=1" : "";
    await api(`/companies/${drawerCompanyId}/download${qs}`, { method: "POST" });
    loadQueue();
  }
});
el("btn-recheck").addEventListener("click", async () => {
  if (drawerCompanyId) { await api(`/companies/${drawerCompanyId}/recheck`, { method: "POST" }); loadQueue(); }
});

// --------------------------------------------------------------------------
// R2 viewer (read-only browse of the object store, rooted at <bucket>/<prefix>)
// --------------------------------------------------------------------------
function fmtSize(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

async function loadR2() {
  let st;
  try { st = await api("/r2/status"); } catch { return; }
  if (!st.enabled) {
    el("r2-status").textContent =
      "R2 is not configured — set R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.";
    el("r2-breadcrumb").innerHTML = "";
    el("r2-listing").innerHTML = "";
    return;
  }
  el("r2-status").innerHTML =
    `Bucket <code>${esc(st.bucket)}</code>, rooted at <code>${esc(st.prefix)}</code>.`;
  r2Navigate("");
}

async function r2Navigate(path) {
  let data;
  try { data = await api(`/r2/list?path=${encodeURIComponent(path)}`); }
  catch (e) { el("r2-listing").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  renderR2Breadcrumb(data.prefix);
  const folders = data.folders.map((f) => `
    <div class="r2-row folder" data-path="${esc(f.path)}">
      <span class="ic">📁</span><span class="nm">${esc(f.name)}/</span>
    </div>`).join("");
  const files = data.files.map((f) => `
    <div class="r2-row file">
      <span class="ic">📄</span>
      <a class="nm" href="/api/r2/object?path=${encodeURIComponent(f.path)}" target="_blank" rel="noopener">${esc(f.name)}</a>
      <span class="sz">${fmtSize(f.size)}</span>
      <span class="dt">${esc((f.last_modified || "").slice(0, 19).replace("T", " "))}</span>
    </div>`).join("");
  el("r2-listing").innerHTML = (folders + files) || `<div class="empty">Empty folder.</div>`;
  el("r2-listing").querySelectorAll(".r2-row.folder").forEach((r) =>
    r.addEventListener("click", () => r2Navigate(r.dataset.path)));
}

function renderR2Breadcrumb(path) {
  const parts = path ? path.split("/").filter(Boolean) : [];
  const crumbs = [`<a data-path="">root</a>`];
  let acc = "";
  parts.forEach((p) => {
    acc += (acc ? "/" : "") + p;
    crumbs.push(`<span class="sep">/</span><a data-path="${esc(acc)}">${esc(p)}</a>`);
  });
  const bc = el("r2-breadcrumb");
  bc.innerHTML = crumbs.join(" ");
  bc.querySelectorAll("a").forEach((a) =>
    a.addEventListener("click", () => r2Navigate(a.dataset.path)));
}

el("r2-refresh").addEventListener("click", loadR2);

// --------------------------------------------------------------------------
// Live browser view (noVNC). Pin host/port/encrypt explicitly — noVNC's own
// guess picks the wrong port on 443 and fails to connect. Computed from the page
// so it works on any host.
function vncViewUrl() {
  const host = location.hostname;
  const port = location.port || (location.protocol === "https:" ? "443" : "80");
  const enc = location.protocol === "https:" ? "1" : "0";
  return `/novnc/vnc.html?host=${host}&port=${port}&encrypt=${enc}&path=novnc/websockify&autoconnect=true&resize=remote`;
}
// Surface an always-available "Live view" button in the header when noVNC is up,
// so the worker's Chrome can be watched anytime — not only during a CAPTCHA.
(async () => {
  try {
    const v = await api("/vnc/status");
    if (v.available) {
      const b = el("live-view-btn");
      b.href = vncViewUrl();
      b.hidden = false;
    }
  } catch {}
})();

// --------------------------------------------------------------------------
// Auth (login gate): show Sign out when enabled; log out clears the session.
el("logout-btn").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST" }); } catch {}
  location.href = "/login";
});
(async () => {
  try {
    const s = await api("/auth/status");
    el("logout-btn").hidden = !s.enabled;
    if (s.enabled && s.user) el("logout-btn").title = "Signed in as " + s.user;
  } catch {}
})();

// --------------------------------------------------------------------------
function refreshAll() { loadStats(); loadSaved(); loadQueue(); }
refreshAll();
loadR2();
setInterval(() => { loadQueue(); loadStats(); }, 3000);
setInterval(loadSaved, 8000);
