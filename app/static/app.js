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
// Add a company (by exchange + ticker + SEDAR number)
// --------------------------------------------------------------------------
async function addByNumber(download) {
  const number = el("add-number").value.trim();
  const name = el("add-name").value.trim();
  const exchange = el("add-exchange").value.trim();
  const ticker = el("add-ticker").value.trim();
  if (!number) { el("add-number").focus(); return; }
  if (!exchange || !ticker) {
    alert("Enter an exchange and ticker.");
    (exchange ? el("add-ticker") : el("add-exchange")).focus();
    return;
  }
  try {
    await api(`/companies/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ number, name: name || null, exchange, ticker, download }),
    });
  } catch (e) { alert("Could not add company: " + e.message); return; }
  ["add-number", "add-name", "add-exchange", "add-ticker"].forEach((id) => (el(id).value = ""));
  refreshAll();
}

el("add-btn").addEventListener("click", () => addByNumber(true));
// Catalog it now, download later from the manager row.
el("add-only-btn").addEventListener("click", () => addByNumber(false));

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

// Surface an enumerate that is already running (queued via the API) so it can
// still be paused. The catalog is maintained by CSV import and the add row now,
// so there is no button here to start one.
function updateEnumerateControls(jobs) {
  const running = jobs.find((j) => j.kind === "enumerate_catalog" && j.status === "running");
  const queued = jobs.find((j) => j.kind === "enumerate_catalog" && j.status === "queued");
  const paused = jobs.find((j) => j.kind === "enumerate_catalog" && j.status === "paused");
  const pauseBtn = el("enumerate-pause-btn");
  const hint = el("enumerate-hint");
  pauseBtn.hidden = !running;
  if (running) {
    pauseBtn.dataset.jobId = running.id;
    pauseBtn.disabled = !!running.pause_requested;
    hint.textContent = "catalog enumeration running — " + clip(running.message || "…", 60);
  } else if (queued) {
    hint.textContent = "catalog enumeration queued…";
  } else if (paused) {
    hint.textContent = "catalog enumeration paused — " + clip(paused.message || "", 60);
  } else {
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
      ? `${s.companies} companies in the catalog — manage them below.`
      : "Catalog is empty — import a CSV or run an enumerate job.";
  } catch {}
}

// Held vs. what SEDAR+ reports. A gap of a few documents is normally SEDAR
// counting duplicate rows as separate filings, so anything within the tolerance
// reads as in sync; a real shortfall stays loud.
const SYNC_TOLERANCE = 5;
function coverageLabel(c) {
  const held = c.total_documents || 0;
  const paused = c.paused ? `<span class="cov paused">⏸ PAUSED</span> ` : "";
  if (!c.reported_total) return `${paused}${held} document(s) downloaded`;
  if (c.paused) return `${paused}${held}/${c.reported_total} held`;
  const missing = Math.max(0, c.reported_total - held);
  return (c.is_complete || missing <= SYNC_TOLERANCE)
    ? `<span class="cov ok">✓ In Sync</span>`
    : `<span class="cov warn">${held}/${c.reported_total} — ${missing} missing</span>`;
}

// Drop the INCOMPLETE caveat from a finished job that is within the sync
// tolerance, so a company that is effectively in sync reads as plainly done.
// Also covers jobs recorded before the tolerance existed.
function jobMessage(j) {
  const m = j.message || "";
  const held = m.match(/(\d+)\s*\/\s*(\d+)\s+held/);
  if (j.status === "done" && held) {
    const missing = Math.max(0, (+held[2]) - (+held[1]));
    if (missing <= SYNC_TOLERANCE) return m.replace(/\s*—\s*INCOMPLETE.*$/, "");
  }
  return m;
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
      ${j.message ? `<div class="msg">${esc(clip(jobMessage(j)))}</div>` : ""}
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
    await api(`/companies/${drawerCompanyId}/download`, { method: "POST" });
    loadQueue();
  }
});
el("btn-recheck").addEventListener("click", async () => {
  if (drawerCompanyId) { await api(`/companies/${drawerCompanyId}/recheck`, { method: "POST" }); loadQueue(); }
});

// --------------------------------------------------------------------------
// Company manager: bulk edit the catalog, import a CSV, resolve missing numbers
// --------------------------------------------------------------------------
const MGR = { offset: 0, limit: 100, total: 0, selected: new Set(), rows: [] };

async function loadManager() {
  const q = el("mgr-search").value.trim();
  const f = el("mgr-filter").value;
  let data;
  try {
    data = await api(`/companies?q=${encodeURIComponent(q)}&filter=${f}` +
                     `&limit=${MGR.limit}&offset=${MGR.offset}`);
  } catch { return; }
  MGR.total = data.total; MGR.rows = data.companies;
  el("mgr-count").textContent = `${data.total} compan${data.total === 1 ? "y" : "ies"}`;
  const from = data.total ? MGR.offset + 1 : 0;
  el("mgr-page").textContent = `${from}–${Math.min(MGR.offset + MGR.limit, data.total)} of ${data.total}`;
  el("mgr-prev").disabled = MGR.offset <= 0;
  el("mgr-next").disabled = MGR.offset + MGR.limit >= data.total;

  el("mgr-table").innerHTML = `
    <div class="mgr-row mgr-head">
      <span><input type="checkbox" id="mgr-all" /></span>
      <span>Name</span><span>SEDAR #</span><span>Exch</span><span>Ticker</span>
      <span>Coverage</span><span>Actions</span><span></span>
    </div>` + data.companies.map((c) => {
      const flags = [];
      if (c.saved) flags.push(`<span class="tag saved">saved</span>`);
      return `<div class="mgr-row">
        <span><input type="checkbox" data-sel="${c.id}" ${MGR.selected.has(c.id) ? "checked" : ""} /></span>
        <span class="nm" data-open="${c.id}" data-name="${esc(c.name)}"
              title="${esc(c.name)}">${esc(c.name)}</span>
        <span><input class="cell-edit ${c.number ? "" : "warn-cell"}" data-num="${c.id}"
                     value="${esc(c.number || "")}" placeholder="— none —"
                     inputmode="numeric" title="Type a SEDAR number and press Enter" /></span>
        <span><input class="cell-edit" data-ex="${c.id}" value="${esc(c.exchange || "")}"
                     placeholder="—" title="Exchange" /></span>
        <span><input class="cell-edit" data-tk="${c.id}" value="${esc(c.ticker || "")}"
                     placeholder="—" title="Ticker" /></span>
        <span class="cov-cell">${coverageLabel(c)} ${flags.join(" ")}</span>
        <span class="row-actions">
          <button class="ghost tiny" data-download="${c.id}" ${c.paused ? "disabled" : ""}>Download</button>
          <button class="ghost tiny" data-recheck="${c.id}" ${c.paused ? "disabled" : ""}>Check new</button>
          <button class="ghost tiny ${c.paused ? "unpause" : ""}" data-pause="${c.id}"
                  data-on="${c.paused ? 1 : 0}">${c.paused ? "▶ Unpause" : "⏸ Pause"}</button>
        </span>
        <span><button class="link-danger" data-del="${c.id}"
                      data-name="${esc(c.name)}" data-held="${c.total_documents || 0}"
                      title="Delete this company">✕</button></span>
      </div>`;
    }).join("");

  el("mgr-table").querySelectorAll("[data-sel]").forEach((cb) =>
    cb.addEventListener("change", () => {
      const id = +cb.dataset.sel;
      cb.checked ? MGR.selected.add(id) : MGR.selected.delete(id);
      paintBulk();
    }));

  // Inline editing for number / exchange / ticker: commit on Enter or blur,
  // revert on Escape, ignore a no-op edit.
  [["data-num", "num", "number"], ["data-ex", "ex", "exchange"],
   ["data-tk", "tk", "ticker"]].forEach(([attr, key, field]) => {
    el("mgr-table").querySelectorAll(`[${attr}]`).forEach((inp) => {
      const original = inp.value;
      const commit = async () => {
        const val = inp.value.trim();
        if (val === original) return;
        try {
          await api("/companies/bulk-update", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids: [+inp.dataset[key]], set: { [field]: val } }),
          });
        } catch (e) {
          alert(`Could not set ${field}: ` + e.message);
          inp.value = original;
          return;
        }
        loadManager(); loadStats();
      };
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
        if (e.key === "Escape") { inp.value = original; inp.blur(); }
      });
      inp.addEventListener("blur", commit);
    });
  });

  el("mgr-table").querySelectorAll("[data-open]").forEach((n) =>
    n.addEventListener("click", () => openDrawer(+n.dataset.open, n.dataset.name)));

  el("mgr-table").querySelectorAll("[data-download]").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await api(`/companies/${b.dataset.download}/download`, { method: "POST" }); }
      catch (e) { alert("Could not queue: " + e.message); b.disabled = false; return; }
      loadQueue();
    }));

  el("mgr-table").querySelectorAll("[data-recheck]").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await api(`/companies/${b.dataset.recheck}/recheck`, { method: "POST" }); }
      catch (e) { alert("Could not queue: " + e.message); b.disabled = false; return; }
      loadQueue();
    }));

  // Hard pause/unpause. The flag lives on the company, so it survives queue
  // clears, restarts and cron rechecks until explicitly lifted.
  el("mgr-table").querySelectorAll("[data-pause]").forEach((b) =>
    b.addEventListener("click", async () => {
      const on = b.dataset.on === "1";
      b.disabled = true;
      try {
        await api(`/companies/${b.dataset.pause}/${on ? "unpause" : "pause"}`, { method: "POST" });
      } catch (e) { alert("Could not change pause: " + e.message); }
      loadManager(); loadQueue();
    }));

  el("mgr-table").querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", async () => {
      const held = +b.dataset.held;
      const warn = held
        ? `\n\nIt holds ${held} document record(s). Its files in R2 are NOT deleted, but this app will forget them and a re-scrape would start from page 1.`
        : "";
      if (!confirm(`Delete "${b.dataset.name}"?${warn}`)) return;
      try {
        const r = await api("/companies/bulk-delete", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: [+b.dataset.del], force: true }),
        });
        if (!r.deleted) {
          alert("Not deleted: " + ((r.refused && r.refused[0] && r.refused[0].reason) || "refused"));
        }
      } catch (e) { alert("Could not delete: " + e.message); return; }
      MGR.selected.delete(+b.dataset.del);
      loadManager(); loadStats();
    }));
  const all = el("mgr-all");
  if (all) all.addEventListener("change", () => {
    data.companies.forEach((c) => all.checked ? MGR.selected.add(c.id) : MGR.selected.delete(c.id));
    loadManager();
  });
  paintBulk();
}

function paintBulk() {
  const n = MGR.selected.size;
  el("mgr-bulk").hidden = n === 0;
  el("mgr-selected").textContent = `${n} selected`;
}

async function bulkAction(kind) {
  const ids = [...MGR.selected];
  if (!ids.length) return;
  try {
    if (kind === "download") {
      // Queued one at a time on purpose: the endpoint refuses paused companies
      // individually, so a paused one in the selection can't sink the rest.
      let ok = 0, refused = 0;
      for (const id of ids) {
        try { await api(`/companies/${id}/download`, { method: "POST" }); ok++; }
        catch { refused++; }
      }
      alert(`Queued ${ok} download(s)` + (refused ? `; ${refused} refused (paused?)` : ""));
    } else {
      const set = { save: { saved: true }, unsave: { saved: false },
                    pause: { paused: true }, unpause: { paused: false } }[kind]
        || { exchange: el("mgr-set-exchange").value, ticker: el("mgr-set-ticker").value };
      if (kind === "setslug" && !set.exchange && !set.ticker) {
        alert("Enter an exchange and/or ticker to apply."); return;
      }
      const r = await api("/companies/bulk-update", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids, set }),
      });
      alert(`Updated ${r.updated} compan${r.updated === 1 ? "y" : "ies"}`);
    }
  } catch (e) { alert("Bulk action failed: " + e.message); return; }
  MGR.selected.clear();
  loadManager(); loadQueue();
}

document.querySelectorAll("[data-bulk]").forEach((b) =>
  b.addEventListener("click", () => bulkAction(b.dataset.bulk)));
el("mgr-search").addEventListener("input", () => {
  clearTimeout(MGR._t); MGR._t = setTimeout(() => { MGR.offset = 0; loadManager(); }, 250);
});
el("mgr-filter").addEventListener("change", () => { MGR.offset = 0; loadManager(); });
el("mgr-prev").addEventListener("click", () => { MGR.offset = Math.max(0, MGR.offset - MGR.limit); loadManager(); });
el("mgr-next").addEventListener("click", () => { MGR.offset += MGR.limit; loadManager(); });

el("mgr-resolve-btn").addEventListener("click", async () => {
  const ids = [...MGR.selected];
  const scope = ids.length ? `${ids.length} selected` : "every company missing a number";
  if (!confirm(`Look up SEDAR numbers for ${scope}? This drives the browser and only writes exact name matches; anything uncertain comes back for review.`)) return;
  try {
    const j = await api("/companies/resolve-numbers", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    alert(`Queued resolve job #${j.id}. Watch the queue for results.`);
  } catch (e) { alert("Could not queue: " + e.message); }
  loadQueue();
});

// ---- CSV import with column mapping ----
let CSV = { header: [], rows: [] };

function parseCSV(text) {
  // Small RFC4180-ish parser: quoted fields, escaped quotes, embedded commas
  // and newlines. Company names routinely contain commas, so splitting on ","
  // would silently corrupt the import.
  const rows = []; let row = [], field = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else q = false; }
      else field += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (c !== "\r") field += c;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((v) => (v || "").trim()));
}

el("mgr-import-btn").addEventListener("click", () => { el("import-modal").hidden = false; });
el("import-close").addEventListener("click", () => { el("import-modal").hidden = true; });

el("import-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const rows = parseCSV(reader.result);
    if (rows.length < 2) { alert("That CSV has no data rows."); return; }
    CSV.header = rows[0].map((h) => h.trim());
    CSV.rows = rows.slice(1);
    const opts = `<option value="">— not mapped —</option>` +
      CSV.header.map((h, i) => `<option value="${i}">${esc(h)}</option>`).join("");
    document.querySelectorAll("[data-map]").forEach((sel) => {
      sel.innerHTML = opts;
      // Pre-select the obvious column so the common case needs no clicks.
      const want = sel.dataset.map;
      const guess = CSV.header.findIndex((h) => {
        const k = h.toLowerCase().replace(/[^a-z]/g, "");
        if (want === "name") return k.includes("name") || k.includes("company") || k.includes("issuer");
        if (want === "number") return k.includes("sedar") || k === "number" || k.includes("profilenumber");
        if (want === "exchange") return k.includes("exchange") || k === "exch" || k.includes("venue");
        if (want === "ticker") return k.includes("ticker") || k.includes("symbol");
        return false;
      });
      if (guess >= 0) sel.value = String(guess);
      sel.addEventListener("change", previewImport);
    });
    el("import-map").hidden = false;
    previewImport();
  };
  reader.readAsText(file);
});

function mappedRows() {
  const map = {};
  document.querySelectorAll("[data-map]").forEach((s) => { map[s.dataset.map] = s.value === "" ? -1 : +s.value; });
  return CSV.rows.map((r) => ({
    name: map.name >= 0 ? (r[map.name] || "").trim() : "",
    number: map.number >= 0 ? (r[map.number] || "").trim() : "",
    exchange: map.exchange >= 0 ? (r[map.exchange] || "").trim() : "",
    ticker: map.ticker >= 0 ? (r[map.ticker] || "").trim() : "",
  }));
}

function previewImport() {
  const rows = mappedRows();
  const noNumber = rows.filter((r) => !r.number).length;
  const noName = rows.filter((r) => !r.name && !r.number).length;
  el("import-preview").innerHTML = `
    <div class="hint">${rows.length} row(s). ${noNumber} without a SEDAR number` +
    (noNumber ? ` — resolvable from SEDAR+ after import` : "") +
    (noName ? `. <b>${noName} row(s) have neither name nor number and will be skipped.</b>` : ".") + `</div>` +
    `<table class="preview"><tr><th>Name</th><th>SEDAR #</th><th>Exch</th><th>Ticker</th></tr>` +
    rows.slice(0, 5).map((r) => `<tr><td>${esc(r.name)}</td><td>${esc(r.number)}</td><td>${esc(r.exchange)}</td><td>${esc(r.ticker)}</td></tr>`).join("") +
    `</table>`;
}

el("import-run").addEventListener("click", async () => {
  const rows = mappedRows();
  if (!rows.length) return;
  el("import-run").disabled = true;
  el("import-status").textContent = "importing…";
  try {
    const r = await api("/companies/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows, save: el("import-save").checked }),
    });
    el("import-status").textContent =
      `${r.created} created, ${r.matched} matched, ${r.skipped.length} skipped` +
      (r.missing_numbers ? `, ${r.missing_numbers} still missing a SEDAR #` : "");
    loadManager();
  } catch (e) { el("import-status").textContent = "failed: " + e.message; }
  el("import-run").disabled = false;
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
function refreshAll() { loadStats(); loadQueue(); loadManager(); }
refreshAll();
loadR2();
setInterval(() => { loadQueue(); loadStats(); }, 3000);
// The manager now carries coverage + actions, so it is what needs refreshing.
setInterval(loadManager, 8000);
