// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * Main-tab core: shared queue state, its rendering, and the fetch helpers the
 * rest of the client builds on. Talks to the FastAPI backend in server.py —
 * queue mutation over plain POSTs, live progress over one persistent
 * EventSource (SSE). File intake lives in intake.js, run controls and the log
 * in run_control.js, and live progress/tab wiring in events.js.
 */

const STATE_GLYPH = {
  pending: "·", running: "▸", done: "✓",
  skipped: "–", failed: "✕", cancelled: "—",
};

const STAGE_KEYS = ["ocr", "cleanup", "title", "translate"];

const els = {};
["queue-body", "queue-count", "dropzone", "log", "output-dir",
 "btn-browse-files", "btn-add-folder", "btn-remove",
 "btn-clear", "btn-skip", "btn-retry", "btn-browse-output",
 "btn-run", "btn-pause", "btn-stop", "progress-bar", "progress-value",
 "status-text", "stage-text", "stage-ocr", "stage-cleanup", "stage-title", "stage-translate", "stage-force",
 "stage-segmentation", "seg-provider",
 "dropzone-idle", "dropzone-uploading", "dropzone-success",
 "dropzone-error", "dropzone-hint", "dropzone-live",
 "dropzone-success-text", "dropzone-error-text",
].forEach(id => {
  els[id] = document.getElementById(id);
});

let items = new Map();   // id -> item dict, insertion order preserved
let selected = new Set();
let running = false;
let lastClickedId = null;  // for Shift+click range selection

// Header "select all" checkbox. Lives in static HTML (not re-rendered), so it
// is wired once at load; its checked/indeterminate state is refreshed on every
// queue render and on every per-row selection change.
const selectAllBox = document.getElementById("select-all-rows");

function updateSelectAllState() {
  if (!selectAllBox) return;
  const n = items.size;
  const s = selected.size;
  selectAllBox.disabled = n === 0;
  selectAllBox.checked = n > 0 && s === n;
  selectAllBox.indeterminate = s > 0 && s < n;
}

function selectedQueueIds() {
  return [...selected];
}

if (selectAllBox) {
  selectAllBox.addEventListener("change", () => {
    if (selectAllBox.checked) {
      for (const id of items.keys()) selected.add(id);
    } else {
      selected.clear();
    }
    for (const tr of els["queue-body"].querySelectorAll("tr[data-id]")) {
      const on = selected.has(tr.dataset.id);
      tr.classList.toggle("selected", on);
      const cb = tr.querySelector(".row-select");
      if (cb) cb.checked = on;
    }
    updateSelectAllState();
  });
}

// Tabs register a callback here (`TAB_ACTIVATE.history = fn`) to load or
// refresh their content only when the user actually switches to them.
const TAB_ACTIVATE = {};

// --------------------------------------------------------------- fetch helpers

async function api(method, path, body, { signal } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

// Trigger a native "Save As" for server-generated content (an export, a
// compiled PDF). `window.open(url, "_blank")` is a browser-only trick — this
// app also runs inside a frameless pywebview desktop window, and nothing
// here creates a second native window for a script-triggered popup to open
// into, so window.open silently goes nowhere in that mode. Fetching the
// bytes and driving a Blob download works identically in both.
async function downloadFile(path, fallbackFilename) {
  const res = await fetch(path);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || res.statusText);
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const filename = (match && decodeURIComponent(match[1])) || fallbackFilename;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// -------------------------------------------------------------------- queue

// Strip Python exception class names and other technical prefixes from error
// messages so non-technical users see something they can act on.
function friendlyError(err) {
  let msg = (err && err.message) || String(err);
  msg = msg.replace(/^(ConnectionError|TimeoutError|FileNotFoundError|OSError|RuntimeError|ValueError|JSONDecodeError|HTTPError|RequestException):\s*/i, "");
  return msg;
}

function setQueue(list) {
  items = new Map(list.map(it => [it.id, it]));
  selected = new Set([...selected].filter(id => items.has(id)));
  renderAll();
  // A deep link can activate Review before the initial queue request returns.
  // Refresh it again once the data exists instead of leaving its first item
  // visibly selected beside an empty comparison.
  if (document.getElementById("panel-preview")?.classList.contains("active")) {
    TAB_ACTIVATE.preview?.();
  }
}

function renderAll() {
  els["queue-body"].innerHTML = "";
  if (items.size === 0) {
    const hint = isDesktop
      ? "Use Browse Files to add documents."
      : "Drop image files above, or use Browse Files to add documents.";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="10" class="table-empty-cell">
      <p class="panel-empty-title">No documents queued</p>
      <p class="panel-empty-desc">${hint}</p>
    </td>`;
    els["queue-body"].appendChild(tr);
  } else {
    for (const item of items.values()) els["queue-body"].appendChild(rowFor(item));
  }
  updateCount();
  updateSelectAllState();
  updateLogEmptyState();
}

function updateLogEmptyState() {
  if (els["log"].children.length === 0) {
    els["log"].innerHTML = '<p class="log-empty">Activity will appear here as the pipeline runs.</p>';
  }
}

function updateCount() {
  const n = items.size;
  const done = [...items.values()].filter(i => i.state === "done").length;
  const failed = [...items.values()].filter(i => i.state === "failed").length;
  let text = `${n} file${n === 1 ? "" : "s"}`;
  if (done || failed) {
    text += `  ·  ${done} done`;
    if (failed) text += `  ·  ${failed} failed`;
  }
  els["queue-count"].textContent = text;
}

// Skip-reason codes mirror pipeline.py's SKIP_NOT_SELECTED / SKIP_ALREADY_EXISTS.
function skipReasonLabel(s) {
  if (s.skip_reason === "already_exists") {
    return s.skip_key ? `Already transcribed (${s.skip_key})` : "Already transcribed";
  }
  if (s.skip_reason === "not_selected") return "Not selected";
  return "";
}

function stageCell(item, key) {
  const s = item.stages[key];
  const glyph = STATE_GLYPH[s.state] || "·";
  const text = (s.state === "done" && s.chars) ? `${glyph} ${s.chars}` : glyph;
  const reasonLabel = s.state === "skipped" ? skipReasonLabel(s) : "";
  const titleAttr = reasonLabel ? ` title="${escapeHtml(reasonLabel)}"` : "";
  return `<td class="c stage-${s.state}"${titleAttr}>${text}</td>`;
}

function statusText(item) {
  if (item.state === "failed") {
    const head = (item.error || "").split(":")[0];
    return head ? `failed (${head})` : "failed";
  }
  const running = STAGE_KEYS.find(k => item.stages[k].state === "running");
  if (running) return `${running[0].toUpperCase()}${running.slice(1)}…`;
  // A "done" item can still have quietly reused a prior run's output on one
  // or more stages — say so on the pill rather than let it read identically
  // to a page that was freshly transcribed this run.
  if (item.state === "done" && STAGE_KEYS.some(k => item.stages[k].skip_reason === "already_exists")) {
    return "done — reused";
  }
  return item.state;
}

function rowFor(item) {
  const tr = document.createElement("tr");
  tr.dataset.id = item.id;
  tr.className = `state-${item.state}` + (selected.has(item.id) ? " selected" : "");
  tr.title = item.path;

  const checked = selected.has(item.id) ? "checked" : "";
  const canPreview = item.state !== "pending";
  tr.innerHTML = `
    <td><input type="checkbox" class="row-select" ${checked}></td>
    <td>${escapeHtml(item.name)}</td>
    ${stageCell(item, "ocr")}
    ${stageCell(item, "cleanup")}
    ${stageCell(item, "title")}
    ${stageCell(item, "translate")}
    <td class="c">${item.confidence ?? "—"}</td>
    <td class="c">${item.elapsed ? item.elapsed.toFixed(1) + "s" : "—"}</td>
    <td><span class="status-pill ${item.state}">${escapeHtml(statusText(item))}</span></td>
    <td class="c">${canPreview ? `<button class="btn row-view" title="Open in Preview">&rarr;</button>` : ""}</td>
  `;
  tr.querySelector(".row-select").addEventListener("change", (e) => {
    if (e.target.checked) selected.add(item.id); else selected.delete(item.id);
    tr.classList.toggle("selected", e.target.checked);
    lastClickedId = item.id;
    updateSelectAllState();
  });

  // Shift+click range selection
  tr.addEventListener("click", (e) => {
    if (e.target.closest("button") || e.target.tagName === "INPUT") return;
    if (e.shiftKey && lastClickedId) {
      const allIds = [...items.keys()];
      const start = allIds.indexOf(lastClickedId);
      const end = allIds.indexOf(item.id);
      if (start !== -1 && end !== -1) {
        const [lo, hi] = start < end ? [start, end] : [end, start];
        for (let i = lo; i <= hi; i++) {
          selected.add(allIds[i]);
          const row = els["queue-body"].querySelector(`tr[data-id="${allIds[i]}"]`);
          if (row) {
            row.classList.add("selected");
            const cb = row.querySelector(".row-select");
            if (cb) cb.checked = true;
          }
        }
      }
    } else if (e.ctrlKey || e.metaKey) {
      // Ctrl/Cmd+click: toggle individual
      if (selected.has(item.id)) {
        selected.delete(item.id);
        tr.classList.remove("selected");
        const cb = tr.querySelector(".row-select");
        if (cb) cb.checked = false;
      } else {
        selected.add(item.id);
        tr.classList.add("selected");
        const cb = tr.querySelector(".row-select");
        if (cb) cb.checked = true;
      }
    }
    lastClickedId = item.id;
    updateSelectAllState();
  });

  // Drag-drop reorder
  tr.draggable = true;
  tr.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", item.id);
    tr.classList.add("dragging");
  });
  tr.addEventListener("dragend", () => tr.classList.remove("dragging"));
  tr.addEventListener("dragover", (e) => {
    e.preventDefault();
    const rect = tr.getBoundingClientRect();
    const mid = rect.top + rect.height / 2;
    tr.classList.toggle("drag-over-top", e.clientY < mid);
    tr.classList.toggle("drag-over-bottom", e.clientY >= mid);
  });
  tr.addEventListener("dragleave", () => {
    tr.classList.remove("drag-over-top", "drag-over-bottom");
  });
  tr.addEventListener("drop", async (e) => {
    e.preventDefault();
    tr.classList.remove("drag-over-top", "drag-over-bottom");
    const dragId = e.dataTransfer.getData("text/plain");
    const dropId = item.id;
    if (dragId === dropId) return;
    const allIds = [...items.keys()];
    const dragIdx = allIds.indexOf(dragId);
    const dropIdx = allIds.indexOf(dropId);
    if (dragIdx === -1 || dropIdx === -1) return;
    // Reorder via API: remove and re-add at position
    const rect = tr.getBoundingClientRect();
    const insertBefore = e.clientY < rect.top + rect.height / 2;
    await api("POST", "/api/queue/reorder", {
      drag_id: dragId, drop_id: dropId, before: insertBefore,
    }).catch(function(err) {
      window.ArtificeToast.error("Could not reorder: " + err.message);
    });
    await refreshQueue();
  });
  const viewBtn = tr.querySelector(".row-view");
  if (viewBtn) {
    viewBtn.addEventListener("click", () => {
      if (window.PreviewTab) window.PreviewTab.open(item.id);
    });
  }
  return tr;
}

function updateRow(item) {
  items.set(item.id, item);
  const tr = els["queue-body"].querySelector(`tr[data-id="${item.id}"]`);
  if (tr) tr.replaceWith(rowFor(item));
  else els["queue-body"].appendChild(rowFor(item));
  updateCount();
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

async function refreshQueue() {
  const data = await api("GET", "/api/queue");
  setQueue(data.items);
  applyRunStatus(data.status);
}

function setWorkflowStep(step) {
  document.querySelectorAll(".workflow-rail [data-workflow-step]").forEach(item => {
    const active = Number(item.dataset.workflowStep) === Number(step);
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
}

function workflowStepForTab(tabName) {
  if (tabName === "preview" || tabName === "history") return 3;
  return running ? 2 : 1;
}

window.setWorkflowStep = setWorkflowStep;
window.workflowStepForTab = workflowStepForTab;

// Call sites moved out with their owning concerns: events.js calls
// setWorkflowStep(workflowStepForTab(tab.dataset.tab)) on tab navigation, and
// both events.js and run_control.js call setWorkflowStep(2) when a run starts.
// Kept here (and on window) so one file owns the name and tropy.js can reach it.

// Tropy (both "Add from…" and "Send to…") lives in tropy.js, loaded after
// this file — it reuses api(), escapeHtml(), pickFolder(), setQueue() and log()
// from here, the same way the tk build's TropyPicker/TropySendDialog reuse
// helpers from the app it's attached to.

window.QueueTab = {
  selectedIds: selectedQueueIds,
  outputDirectory: () => els["output-dir"]?.value || "output",
  // Pipeline output stems for the current selection — what a PAGE file is
  // keyed by (see serializers.py::serialize_item's "stem" field), not the
  // ephemeral in-memory id() the row is keyed by in this Map.
  selectedStems: () => selectedQueueIds().map((id) => items.get(id)?.stem).filter(Boolean),
  preferredStage: () => {
    const selected = selectedQueueIds().map((id) => items.get(id)).filter(Boolean);
    if (selected.length && selected.every((item) => item.stages?.translate?.state === "done")) return "translated";
    if (selected.length && selected.every((item) => item.stages?.cleanup?.state === "done")) return "cleaned";
    return "raw_ocr";
  },
};
