"use strict";

const api = (path, opts) => fetch("/api" + path, opts).then(async (r) => {
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.status === 204 ? null : r.json();
});

const el = (id) => document.getElementById(id);
const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

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
    suggestions.innerHTML = `<li class="empty">No matches in the local catalog.</li>`;
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

async function selectCompany(c) {
  if (!c) return;
  hideSuggestions();
  search.value = "";
  // Save + queue a full download.
  try {
    await api(`/companies/${c.id}/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ download: true }),
    });
  } catch (e) { alert("Could not queue download: " + e.message); }
  refreshAll();
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

async function loadSaved() {
  let rows = [];
  try { rows = await api("/saved"); } catch {}
  const box = el("saved");
  if (!rows.length) { box.innerHTML = `<div class="empty">No saved companies yet. Search above to add one.</div>`; return; }
  box.innerHTML = rows.map((c) => `
    <div class="card">
      <div>
        <div class="name" data-id="${c.id}" data-name="${esc(c.name)}">${esc(c.name)}</div>
        <div class="sub">${esc(c.type || "")} · ${esc(c.jurisdiction || "")} · #${esc(c.number)}</div>
        <div class="docs">${c.total_documents} document(s) downloaded</div>
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
}

async function loadQueue() {
  let jobs = [];
  try { jobs = await api("/queue?include_finished=true&limit=40"); } catch {}
  const box = el("queue");
  if (!jobs.length) { box.innerHTML = `<div class="empty">Queue is empty.</div>`; return; }
  box.innerHTML = jobs.map((j) => {
    const pct = j.total_documents ? Math.min(100, Math.round(100 * j.documents_done / j.total_documents)) : (j.status === "done" ? 100 : 0);
    const label = j.company_name || (j.kind === "enumerate_catalog" ? "Catalog enumeration" : "job #" + j.id);
    const cancel = j.status === "queued" ? `<button class="ghost small" data-cancel="${j.id}">cancel</button>` : "";
    return `<div class="job">
      <div class="row">
        <span class="title">${esc(label)}</span>
        <span>${cancel} <span class="status ${j.status}">${j.status}</span></span>
      </div>
      ${j.message ? `<div class="msg">${esc(j.message)}</div>` : ""}
      ${j.error ? `<div class="msg" style="color:var(--err)">${esc(j.error)}</div>` : ""}
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
  if (drawerCompanyId) { await api(`/companies/${drawerCompanyId}/download`, { method: "POST" }); loadQueue(); }
});
el("btn-recheck").addEventListener("click", async () => {
  if (drawerCompanyId) { await api(`/companies/${drawerCompanyId}/recheck`, { method: "POST" }); loadQueue(); }
});

// --------------------------------------------------------------------------
function refreshAll() { loadStats(); loadSaved(); loadQueue(); }
refreshAll();
setInterval(() => { loadQueue(); loadStats(); }, 3000);
setInterval(loadSaved, 8000);
