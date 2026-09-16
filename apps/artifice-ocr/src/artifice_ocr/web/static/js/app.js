// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * Main-tab client. Talks to the FastAPI backend in server.py: queue mutation
 * over plain POSTs, live progress over one persistent EventSource (SSE).
 *
 * File paths: a browser <input type=file> or drag-and-drop never exposes a
 * real filesystem path (that's a deliberate browser security boundary).
 * The app falls back to `prompt()` for a typed path — crude, but honest
 * about what a browser can and cannot do on its own.
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

// -------------------------------------------------------------- adding files

async function pickFiles() {
  let res;
  try {
    res = await api("POST", "/api/native/pick-file");
  } catch {
    if (window.ArtificeToast) window.ArtificeToast.error("Could not reach the server to open the file picker.");
    return [];
  }
  if (res.state === "selected") return res.paths || [];
  if (res.state === "unavailable") {
    if (window.ArtificeToast) window.ArtificeToast.show(res.reason || "File picker unavailable", "warning");
    const raw = prompt("Enter a full file path (e.g. C:\\Users\\you\\Documents\\scan.jpg):");
    return raw ? [raw] : [];
  }
  return [];  // cancelled — the user closed the dialog on purpose
}

async function pickFolder(kind) {
  const label = kind === "output" ? "output directory"
             : kind === "tropy" ? "Tropy project" : "folder";
  let res;
  try {
    res = await api("POST", "/api/native/pick-folder");
  } catch {
    if (window.ArtificeToast) window.ArtificeToast.error("Could not reach the server to open the folder picker.");
    return null;
  }
  if (res.state === "selected") return res.paths[0] || null;
  if (res.state === "unavailable") {
    if (window.ArtificeToast) window.ArtificeToast.show(res.reason || "Folder picker unavailable", "warning");
    return prompt(`Enter a full ${label} path (e.g. C:\\Users\\you\\Documents):`);
  }
  return null;  // cancelled — the user closed the dialog on purpose
}

async function addPaths(paths) {
  if (!paths || !paths.length) return;
  const data = await api("POST", "/api/queue/add-paths", { paths });
  setQueue(data.items);
  log(`Added ${data.added} file(s)`, "accent");
}

// btn-browse-files: use the hidden file input in browser mode (where native
// pickers are unavailable), the native picker in desktop mode.
els["btn-browse-files"].onclick = async () => {
  if (isDesktop) {
    addPaths(await pickFiles());   // desktop: async native picker
  } else {
    ensureFileInput().click();     // browser: real OS file dialog
  }
};

// btn-add-folder: native folder picker is only available in desktop mode.
// In browser mode there is no API for folder access — disable rather than
// prompt for a typed path.
els["btn-add-folder"].onclick = () => {
  if (isDesktop) {
    pickFolder("folder").then(folder => { if (folder) addPaths([folder]); });
  } else {
    if (window.ArtificeToast) {
      window.ArtificeToast.show("Folder add is not available in browser mode.", "warning");
    }
  }
};

// -------------------------------------------------------------- dropzone

// Detect whether we are running inside a pywebview desktop window.
// A desktop window exposes window.pywebview; a browser does not.
const isDesktop = !!(window.pywebview);

// Hidden <input type=file> used for browser-mode file picking.
// Created once and reused; never exposed to the user directly.
let fileInput = null;

function ensureFileInput() {
  if (fileInput) return fileInput;
  fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.multiple = true;
  fileInput.accept = ".jpeg,.jpg,.pdf,.png,.tif,.tiff";
  fileInput.style.cssText = "position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;";
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) {
      uploadFiles(fileInput.files);
      fileInput.value = "";
    }
  });
  document.body.appendChild(fileInput);
  return fileInput;
}

// Show a named dropzone state sub-element, hide all others.
function setDropzoneState(state) {
  const states = ["idle", "uploading", "success", "error"];
  states.forEach(s => {
    const el = els["dropzone-" + s];
    if (el) el.classList.toggle("hidden", s !== state);
  });
}

// Announce a message to screen readers via aria-live.
function announceDropzone(message) {
  if (els["dropzone-live"]) els["dropzone-live"].textContent = message;
}

// Upload an array of File objects via POST /api/queue/upload.
async function uploadFiles(files) {
  if (!files || !files.length) return;
  setDropzoneState("uploading");
  announceDropzone("Uploading " + files.length + " file(s)…");

  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  let data;
  try {
    const res = await fetch("/api/queue/upload", { method: "POST", body: fd });
    data = await res.json();
  } catch (err) {
    setDropzoneState("error");
    els["dropzone-error-text"].textContent = "Upload failed — is the server running?";
    announceDropzone("Upload failed.");
    log("Upload failed: " + err.message, "error");
    setTimeout(() => setDropzoneState("idle"), 3000);
    return;
  }

  // Update queue with the server's response (same shape as add-paths).
  setQueue(data.items);

  // Report per-file results.
  const rejected = data.uploaded.filter(e => e.status === "rejected");
  const accepted = data.uploaded.filter(e => e.status === "ok");
  if (rejected.length === 0) {
    setDropzoneState("success");
    els["dropzone-success-text"].textContent =
      accepted.length === 1
        ? "Added: " + accepted[0].filename
        : "Added " + accepted.length + " file(s)";
    announceDropzone(accepted.length + " file(s) added to queue.");
    log("Uploaded " + accepted.length + " file(s)", "accent");
  } else {
    const reasons = rejected.map(e => e.filename + ": " + e.reason).join("; ");
    setDropzoneState("error");
    els["dropzone-error-text"].textContent = rejected.length + " rejected: " + reasons;
    announceDropzone(rejected.length + " file(s) rejected.");
    if (accepted.length > 0) {
      log("Uploaded " + accepted.length + " file(s); " + rejected.length + " rejected: " + reasons, "warning");
    } else {
      log("Upload rejected: " + reasons, "warning");
    }
  }
  setTimeout(() => setDropzoneState("idle"), 4000);
}

// Dropzone click: desktop uses native picker, browser uses hidden file input.
els["dropzone"].addEventListener("click", () => {
  if (isDesktop) {
    els["btn-browse-files"].click();
  } else {
    ensureFileInput().click();
  }
});

els["dropzone"].addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    els["dropzone"].click();
  }
});

["dragenter", "dragover"].forEach(evt =>
  els["dropzone"].addEventListener(evt, e => {
    e.preventDefault();
    els["dropzone"].classList.add("drag");
  }));

["dragleave", "drop"].forEach(evt =>
  els["dropzone"].addEventListener(evt, e => {
    e.preventDefault();
    els["dropzone"].classList.remove("drag");
  }));

els["dropzone"].addEventListener("drop", async (e) => {
  e.preventDefault();
  els["dropzone"].classList.remove("drag");
  if (isDesktop) {
    // Desktop: native drag-drop paths are out of scope — fall back to Browse.
    log("Drag-and-drop is not available in desktop mode — use Browse Files.", "warning");
    return;
  }

  const entries = [];
  const items = e.dataTransfer.items;
  if (!items) {
    log("Could not read dropped items.", "warning");
    return;
  }

  let hasFolder = false;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item.kind === "directory") {
      hasFolder = true;
    } else if (item.kind === "file") {
      const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
      if (entry && entry.isDirectory) {
        hasFolder = true;
      } else {
        entries.push(item.getAsFile());
      }
    }
  }

  if (hasFolder) {
    setDropzoneState("error");
    els["dropzone-error-text"].textContent = "Folders cannot be uploaded — drop individual image files instead.";
    announceDropzone("Folders cannot be uploaded.");
    log("Folder drop ignored: folders cannot be uploaded via the browser.", "warning");
    setTimeout(() => setDropzoneState("idle"), 3500);
    return;
  }

  if (entries.length === 0) {
    log("No accepted files in drop.", "warning");
    return;
  }

  uploadFiles(entries);
});

els["btn-remove"].onclick = async () => {
  if (!selected.size) return;
  const data = await api("POST", "/api/queue/remove", { ids: [...selected] });
  setQueue(data.items);
};
els["btn-clear"].onclick = async () => {
  const data = await api("POST", "/api/queue/clear");
  setQueue(data.items);
};
els["btn-skip"].onclick = async () => {
  for (const id of selected) await api("POST", "/api/run/skip", { id });
  log(`Skip requested for ${selected.size} item(s)`, "warning");
};
els["btn-retry"].onclick = async () => {
  if (!selected.size) { log("Select items to retry", "warning"); return; }
  await api("POST", "/api/run/retry", [...selected]);
  await refreshQueue();
};

// ------------------------------------------------------------------- running

// Pause/Resume is one button whose icon and label swap with run state; both
// icons live in icons.js (Design_Philosophy.md §8.8) rather than as a
// Unicode dingbat mixed into the label — see the app.css note by the
// `.icon-btn` rule for why that used to inflate the button's line box.
function setPauseButtonLabel(paused) {
  els["btn-pause"].innerHTML = (paused ? Icons.play : Icons.pause) +
    `<span>${paused ? "Resume" : "Pause"}</span>`;
}

function setRunning(isRunning) {
  running = isRunning;
  els["btn-run"].disabled = isRunning;
  els["btn-pause"].disabled = !isRunning;
  els["btn-stop"].disabled = !isRunning;
  if (!isRunning) {
    setPauseButtonLabel(false);
    els["progress-bar"].classList.remove("active");
  }
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

function applyRunStatus(status) {
  setRunning(!!status.running);
  if (status.paused) setPauseButtonLabel(true);
}

els["btn-run"].onclick = async () => {
  const stages = [];
  if (els["stage-ocr"].checked) stages.push("ocr");
  if (els["stage-cleanup"].checked) stages.push("cleanup");
  if (els["stage-title"].checked) stages.push("title");
  if (els["stage-translate"].checked) stages.push("translate");
  if (!items.size) { log("Add at least one document first.", "warning"); return; }
  if (!stages.includes("ocr")) { log("OCR is required for every new run.", "warning"); return; }

  const body = {
    stages, output_dir: els["output-dir"].value || "output",
    project: (els["output-dir"].value || "output") === "output" ? "OCR project" : null,
    force: els["stage-force"].checked,
  };
  // Only include segmentation_provider when the toggle is on and a provider is selected.
  if (els["stage-segmentation"] && els["stage-segmentation"].checked) {
    const provider = els["seg-provider"] ? els["seg-provider"].value : "";
    if (provider) body.segmentation_provider = provider;
    if (provider === "diff-residual") {
      const referenceImage = SegmentationToggle.getReferenceImage();
      if (!referenceImage) {
        log("diff-residual needs a reference scan — pick one before running.", "warning");
        return;
      }
      body.segmentation_reference_image = referenceImage;
    }
  }
  try {
    const result = await api("POST", "/api/run/start", body);
    if (result.output_dir) els["output-dir"].value = result.output_dir;
    setRunning(true);
    setWorkflowStep(2);
    els["progress-bar"].style.width = "0%";
    const pv = els["progress-value"];
    if (pv) pv.textContent = "0%";
  } catch (err) {
    log(`Could not start: ${friendlyError(err)}`, "error");
  }
};

els["btn-pause"].onclick = async () => {
  const paused = els["btn-pause"].textContent.includes("Resume");
  await api("POST", paused ? "/api/run/resume" : "/api/run/pause");
  setPauseButtonLabel(!paused);
};
els["btn-stop"].onclick = async () => {
  await api("POST", "/api/run/cancel");
    els["status-text"].textContent = "Stopping — finishing current work…";
};

// ---------------------------------------------------------------------- log

function log(message, tag) {
  // Remove the empty-state placeholder when the first real message arrives.
  const empty = els["log"].querySelector(".log-empty");
  if (empty) empty.remove();
  const line = document.createElement("div");
  line.className = `line ${tag || ""}`;
  line.textContent = message;
  els["log"].appendChild(line);
  els["log"].scrollTop = els["log"].scrollHeight;
  // Also show toasts for non-trivial messages
  if (window.ArtificeToast && tag && message.length > 3) {
    var tone = tag === "accent" ? "success" : (tag || "info");
    var duration = tone === "error" ? 0 : 3000;
    window.ArtificeToast.show(message, tone, { duration: duration });
  }
}

// ---------------------------------------------------------------------- SSE

let startTime = null;
let finishedCount = 0;

function updateProgress() {
  const total = items.size;
  const finished = [...items.values()]
    .filter(i => ["done", "failed", "skipped", "cancelled"].includes(i.state)).length;
  const pct = total ? Math.round((finished / total) * 100) : 0;
  els["progress-bar"].style.width = `${pct}%`;
  els["progress-bar"].classList.toggle("active", running && pct < 100 && pct > 0);
  const pv = els["progress-value"];
  if (pv) pv.textContent = running && pct < 100 ? `${pct}%` : pct === 100 ? "Done" : "0%";
  if (running) {
    let text = `Running — ${finished}/${total}`;
    if (startTime && finishedCount > 0) {
      const elapsed = (Date.now() - startTime) / 1000;
      const eta = elapsed / finishedCount * (total - finishedCount);
      if (eta > 60) {
        text += `  ~${Math.round(eta / 60)}m remaining`;
      } else {
        text += `  ~${Math.round(eta)}s remaining`;
      }
    }
    els["status-text"].textContent = text;
  } else {
    els["progress-bar"].classList.remove("active");
  }
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    const evt = JSON.parse(e.data);
    if (evt.message) log(evt.message, evt.tag);
    if (evt.item) updateRow(evt.item);

    switch (evt.kind) {
      case "run_started":
        startTime = Date.now();
        finishedCount = 0;
        setRunning(true);
        setWorkflowStep(2);
        els["stage-text"].textContent = "";
        break;
      case "stage_started":
        els["stage-text"].textContent = evt.item ? `${evt.stage} · ${evt.item.name}` : "";
        updateProgress();
        break;
      case "item_finished":
        finishedCount++;
        updateProgress();
        // Mirrors the desktop build: if Preview is the open tab, follow
        // whichever item just finished rather than making the user click it.
        if (window.PreviewTab && document.getElementById("panel-preview").classList.contains("active")) {
          window.PreviewTab.open(evt.item.id);
        }
        break;
      case "paused":
        els["status-text"].textContent = "Paused";
        break;
      case "resumed":
        els["status-text"].textContent = "Running";
        break;
      case "run_finished": {
        const p = evt.payload || {};
        setRunning(false);
        els["stage-text"].textContent = "";
        els["status-text"].textContent =
          `Done — ${p.done ?? 0} ok` + (p.failed ? `, ${p.failed} failed` : "");
        const pv = els["progress-value"];
        if (pv) pv.textContent = "Done";
        refreshQueue();
        break;
      }
    }
  };
  es.onerror = () => { /* EventSource retries on its own */ };
}

// -------------------------------------------------------------------- tabs

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(`panel-${tab.dataset.tab}`).classList.add("active");
    setWorkflowStep(workflowStepForTab(tab.dataset.tab));
    TAB_ACTIVATE[tab.dataset.tab]?.();
  });
});

// The simplified shell owns the visible navigation while the compact legacy
// tab buttons remain the panel controller. Bridge them in-page: a normal click
// must not reload the entire local app, discard queue selection, and restart
// endpoint detection merely to move from Source to Review.
document.querySelectorAll(".shell-nav a").forEach(link => {
  const url = new URL(link.href, window.location.href);
  const view = url.searchParams.get("view");
  const controller = view ? document.querySelector(`.tab[data-tab="${view}"]`) : null;
  if (!controller || url.pathname !== window.location.pathname) return;
  link.addEventListener("click", event => {
    event.preventDefault();
    controller.click();
    document.querySelectorAll(".shell-nav a").forEach(item => item.removeAttribute("aria-current"));
    link.setAttribute("aria-current", "page");
    window.history.replaceState(null, "", url.pathname + url.search);
  });
});
const requestedTabName = new URLSearchParams(window.location.search).get("view");
const requestedTab = document.querySelector(`.tab[data-tab="${requestedTabName}"]`);
if (requestedTab) requestedTab.click();

// ------------------------------------------------------- shared: comparison

/*
 * Renders the Raw / Cleaned / Translated three-pane comparison, used by both
 * the Preview tab (live queue items) and the History tab (past runs). Both
 * send the same shape of `data` — see `serialize_item_preview` and
 * `serialize_history_item_detail` in runtime.py, which is precisely the
 * point: one renderer, two sources, no drift between them.
 *
 * Diff/marker ranges arrive as (start, end, tag) triples computed in Python
 * (`_diff.py`) rather than recomputed here, so the highlight logic can never
 * quietly diverge from the desktop build's.
 */
function renderCompare(container, data, { editableStages = new Set() } = {}) {
  const confBits = [];
  if (data.language) confBits.push(`source: ${escapeHtml(data.language)}`);
  if (data.confidence != null) confBits.push(`confidence ${data.confidence}/100`);

  container.classList.remove("compare-empty");
  container.querySelector(".compare-title").textContent = data.title || "No document selected";
  const confEl = container.querySelector(".compare-conf");
  confEl.textContent = confBits.join("   ");
  confEl.className = `compare-conf dim conf-${data.confidence_tier || "none"}`;

  // Store original text for "Show original" toggle
  container.dataset.originalRaw = data.original_raw || "";
  container.dataset.originalCleaned = data.original_cleaned || "";
  container.dataset.originalTranslated = data.original_translated || "";

  const panes = {
    raw: { text: data.raw, ranges: data.diff?.raw_ranges || [], original: data.original_raw || "" },
    cleaned: { text: data.cleaned, ranges: data.diff?.cleaned_ranges || [], original: data.original_cleaned || "" },
    translated: { text: data.translated, ranges: data.diff?.translated_ranges || [], original: data.original_translated || "" },
  };
  for (const [key, { text, ranges, original }] of Object.entries(panes)) {
    const el = container.querySelector(`.compare-pane[data-pane="${key}"] .compare-text`);
    const meta = container.querySelector(`.compare-pane[data-pane="${key}"] .compare-meta`);
    // Store original text on the pane itself for toggle access
    const paneEl = container.querySelector(`.compare-pane[data-pane="${key}"]`);
    if (paneEl) paneEl.dataset.originalText = original;
    // Any stage in editableStages gets a plain-text textarea instead of highlighted innerHTML.
    if (editableStages.has(key)) {
      el.innerHTML = "";
      const textarea = document.createElement("textarea");
      textarea.className = "raw-edit";
      textarea.value = text || "";
      textarea.placeholder = "(not run)";
      el.appendChild(textarea);
      meta.textContent = text ? `${text.length.toLocaleString()} chars` : "";
      continue;
    }
    if (text) {
      el.innerHTML = highlightRanges(text, ranges);
      meta.textContent = `${text.length.toLocaleString()} chars`;
    } else {
      el.innerHTML = `<span class="empty">(not run)</span>`;
      meta.textContent = "";
    }
  }
}

function clearCompare(container) {
  // Three columns, each disabled and each repeating the same "select a
  // page" message, read as a wall of dead chrome. Collapsing to one
  // (CSS: .compare-empty hides the other two panes and every pane-head's
  // now-pointless Original/Diff/Save-correction buttons) keeps the same
  // markup — no restructuring of the fragile compare-card grid — while
  // showing that one message once, not three times.
  container.classList.add("compare-empty");
  container.querySelector(".compare-title").textContent = "No document selected";
  container.querySelector(".compare-conf").textContent = "";
  // "empty-no-selection" (not renderCompare()'s plain "empty") lets the CSS
  // tell this apart from a genuinely selected item whose stage just hasn't
  // run yet, and collapse the pane's height instead of leaving a 46vh gap.
  container.querySelectorAll(".compare-text").forEach(el => {
    el.innerHTML = `<span class="empty empty-no-selection">Select a page above to compare its stages.</span>`;
  });
  container.querySelectorAll(".compare-meta").forEach(el => { el.textContent = ""; });
  container.querySelectorAll(".compare-pane").forEach(el => { delete el.dataset.originalText; });
}

// Shared: toggle between current/edited text and original text.
// Called from both Preview and History tabs.
function wireOriginalToggles(container) {
  container.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-show-original");
    if (!btn) return;
    const pane = btn.closest(".compare-pane");
    if (!pane) return;
    const textEl = pane.querySelector(".compare-text");
    const textarea = textEl ? textEl.querySelector("textarea") : null;
    const currentText = textarea ? textarea.value : (textEl ? textEl.textContent || textEl.innerText : "");
    const originalText = pane.dataset.originalText || "";

    if (!originalText) return; // nothing to show

    const showingOriginal = btn.classList.toggle("showing-original");
    btn.textContent = showingOriginal ? "Back to Edit" : "View Original";

    if (textarea) {
      // Editable pane: swap textarea value
      if (showingOriginal) {
        textarea.dataset.editedText = currentText;
        textarea.value = originalText;
      } else {
        textarea.value = textarea.dataset.editedText || currentText;
        delete textarea.dataset.editedText;
      }
      // Trigger input event so save button state updates
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
}

// Shared: cross-highlight — when text is selected in one pane,
// find and select the same text in all other editable panes.
function wireCrossHighlight(container) {
  let lastSearch = "";
  let lastKey = "";
  let timer = null;

  function findInPane(key, query) {
    const pane = container.querySelector(`.compare-pane[data-pane="${key}"]`);
    if (!pane) return;
    const textarea = pane.querySelector("textarea.raw-edit");
    if (!textarea) return;
    const text = textarea.value;
    const idx = text.indexOf(query);
    if (idx !== -1) {
      textarea.focus();
      textarea.setSelectionRange(idx, idx + query.length);
      // Scroll into view roughly
      const linesBefore = text.slice(0, idx).split("\n").length - 1;
      const lineHeight = 20;
      textarea.scrollTop = Math.max(0, linesBefore * lineHeight - textarea.clientHeight / 3);
    }
  }

  container.addEventListener("mouseup", (e) => {
    const textarea = e.target.closest("textarea.raw-edit");
    if (!textarea) return;
    const pane = textarea.closest(".compare-pane");
    if (!pane) return;
    const key = pane.dataset.pane;
    if (!key) return;
    const selected = textarea.value.substring(
      textarea.selectionStart, textarea.selectionEnd
    ).trim();
    if (!selected || selected.length < 2) {
      lastSearch = "";
      return;
    }
    if (selected === lastSearch && key === lastKey) return;
    lastSearch = selected;
    lastKey = key;

    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      const others = ["raw", "cleaned", "translated"].filter((k) => k !== key);
      others.forEach((k) => findInPane(k, selected));
    }, 200);
  });
}

// A tag maps 1:1 to a CSS class (delete_/insert_/replace_/marker); markers
// and diff ranges are applied to the same text in independent passes on the
// Python side, so overlaps are already resolved before they reach here.
function highlightRanges(text, ranges) {
  if (!ranges.length) return escapeHtml(text);
  const sorted = [...ranges].sort((a, b) => a[0] - b[0]);
  let out = "";
  let pos = 0;
  for (const [start, end, tag] of sorted) {
    if (start < pos) continue; // ignore any accidental overlap defensively
    out += escapeHtml(text.slice(pos, start));
    out += `<mark class="hl-${tag}">${escapeHtml(text.slice(start, end))}</mark>`;
    pos = end;
  }
  out += escapeHtml(text.slice(pos));
  return out;
}

// Expose for use by regions.js (loaded after app.js, before preview.js).
window.highlightRanges = highlightRanges;

// ------------------------------------------------------------------- output

els["btn-browse-output"].onclick = async () => {
  const dir = await pickFolder("output");
  if (dir) els["output-dir"].value = dir;
};

// Tropy (both "Add from…" and "Send to…") lives in tropy.js, loaded after
// this file — it reuses api(), escapeHtml(), pickFolder(), setQueue() and log()
// from here, the same way the tk build's TropyPicker/TropySendDialog reuse
// helpers from the app it's attached to.

// -------------------------------------------------------------------- init

(async function init() {
  try {
    const cfg = await api("GET", "/api/config");
    if (cfg.output_dir) els["output-dir"].value = cfg.output_dir;
  } catch { /* config is optional at startup */ }
  await refreshQueue();
  connectEvents();
})();

// ----------------------------------------------------------------- theme toggle

const ThemeToggle = (function () {
  const STORAGE_KEY = "ocr_theme";
  const btn = document.getElementById("themeToggle");

  function apply(theme) {
    document.documentElement.dataset.theme = theme;
  }

  function init() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      apply(saved);
    } else if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
      apply("dark");
    }
  }

  function toggle() {
    const current = document.documentElement.dataset.theme;
    const next = current === "dark" ? "light" : "dark";
    apply(next);
    localStorage.setItem(STORAGE_KEY, next);
  }

  if (btn) btn.addEventListener("click", toggle);
  init();
  return { toggle };
})();

window.ThemeToggle = ThemeToggle;

// ---------------------------------------------------- segmentation toggle

const SegmentationToggle = (function () {
  const toggle = document.getElementById("stage-segmentation");
  const controls = document.getElementById("segmentation-controls");
  const providerWrap = document.getElementById("seg-provider-wrap");
  const providerSelect = document.getElementById("seg-provider");
  const referenceWrap = document.getElementById("seg-reference-wrap");
  const referencePathInput = document.getElementById("seg-reference-path");
  const referenceBrowseBtn = document.getElementById("btn-seg-reference-browse");

  let cachedCapabilities = null;

  // Beginner-facing copy, keyed by provider name. This is a lookup, not
  // population logic: it never decides which providers exist (that stays
  // the API's job via populateProviders() below), and an unrecognized name
  // falls back to that provider's own `requirements` string so a future
  // fifth provider degrades gracefully instead of showing nothing.
  const HELP_COPY = {
    passthrough: "Whole page as one region. No setup needed, and it's the default.",
    "doclayout-yolo": "Finds text blocks, titles, and figures on a page automatically. Downloads a model on first use.",
    kraken: "Detects individual lines and reading order. Best for handwritten or complex historical pages, ideally black-and-white scans.",
    "diff-residual": "Finds hand-added marks by comparing against a separate clean reference scan. Not a general layout option.",
  };

  async function loadCapabilities() {
    if (cachedCapabilities) return cachedCapabilities;
    try {
      const data = await api("GET", "/api/segmentation/capabilities");
      cachedCapabilities = data.providers || [];
    } catch {
      cachedCapabilities = [{ name: "passthrough", requirements: "Built-in (always available)" }];
    }
    return cachedCapabilities;
  }

  function updateProviderHint() {
    const helpEl = document.getElementById("seg-provider-help");
    if (helpEl && providerSelect) {
      const name = providerSelect.value;
      if (!name) { helpEl.textContent = ""; }
      else if (HELP_COPY[name]) { helpEl.textContent = HELP_COPY[name]; }
      else {
        const match = (cachedCapabilities || []).find(p => p.name === name);
        helpEl.textContent = (match && match.requirements) || "";
      }
    }
    updateReferenceVisibility();
  }

  // diff-residual is the only provider that needs a second image (a clean
  // reference scan to diff against) — every other provider works from the
  // page alone, so the row stays hidden and irrelevant for them. Clearing
  // the stored path on hide (rather than just visually hiding it) means
  // switching away from diff-residual and back never resubmits a reference
  // picked for a *different* page.
  function updateReferenceVisibility() {
    if (!referenceWrap || !providerSelect) return;
    if (providerSelect.value === "diff-residual") {
      referenceWrap.style.display = "";
    } else {
      referenceWrap.style.display = "none";
      if (referencePathInput) referencePathInput.value = "";
    }
  }

  function getReferenceImage() {
    return referencePathInput ? referencePathInput.value : "";
  }

  async function onReferenceBrowse() {
    if (!referencePathInput) return;
    let res;
    try {
      res = await api("POST", "/api/native/pick-file", { preset: "images" });
    } catch {
      if (window.ArtificeToast) window.ArtificeToast.error("Could not reach the server to open the file picker.");
      return;
    }
    if (res.state === "selected" && res.paths && res.paths[0]) {
      referencePathInput.value = res.paths[0];
    } else if (res.state === "unavailable") {
      if (window.ArtificeToast) window.ArtificeToast.show(res.reason || "File picker unavailable", "warning");
      const raw = prompt("Enter the full path to the reference scan:");
      if (raw) referencePathInput.value = raw;
    }
    // "cancelled" — user closed the dialog on purpose, leave the field as-is.
  }

  function populateProviders(providers) {
    if (!providerSelect) return;
    providerSelect.innerHTML = "";
    if (!providers || !providers.length) {
      providerSelect.innerHTML = '<option value="">No providers available</option>';
      updateProviderHint();
      return;
    }
    for (const p of providers) {
      const opt = document.createElement("option");
      opt.value = p.name;
      // The brief: never hard-code a provider name, populate from the API only.
      opt.textContent = p.name + (p.requirements ? ` (${p.requirements})` : "");
      providerSelect.appendChild(opt);
    }
    // Select passthrough by default if it appears.
    const passthrough = providers.find(p => p.name === "passthrough");
    if (passthrough) providerSelect.value = "passthrough";
    updateProviderHint();
  }

  async function onToggle() {
    if (!toggle || !controls) return;
    const enabled = toggle.checked;
    // `controls` is the row containing this very checkbox — it must stay
    // visible regardless of checked state, or unchecking it would hide the
    // only control that can re-check it. Only the provider sub-controls are
    // conditional on the checkbox.
    if (enabled) {
      // Lazily load capabilities on first open.
      if (!cachedCapabilities) {
        const providers = await loadCapabilities();
        populateProviders(providers);
      }
      providerWrap.style.display = "";
      updateReferenceVisibility();
    } else {
      providerWrap.style.display = "none";
      if (referenceWrap) referenceWrap.style.display = "none";
      if (referencePathInput) referencePathInput.value = "";
    }
  }

  // Also expose the cached capabilities so preview.js can check them
  // without a second fetch when deciding whether to offer region review.
  function getCapabilities() { return cachedCapabilities; }

  if (toggle) {
    toggle.addEventListener("change", onToggle);
    // If the toggle is pre-checked (e.g. after a settings restore), show controls.
    if (toggle.checked) onToggle();
  }
  if (providerSelect) providerSelect.addEventListener("change", updateProviderHint);
  if (referenceBrowseBtn) referenceBrowseBtn.addEventListener("click", onReferenceBrowse);

  return { loadCapabilities, getCapabilities, updateProviderHint, getReferenceImage };
})();

window.SegmentationToggle = SegmentationToggle;

// --------------------------------------------------------- palette hint button

document.getElementById("btn-palette-hint")?.addEventListener("click", () => {
  window.Palette?.open();
});

// ---------------------------------------------------- keyboard shortcuts

// Shared by every modal in the app: what Tab should stop on. Used here for
// the Tab-trap and by each modal's own open() (via focusFirstIn) to move
// focus in when it opens. One definition so all six modals agree.
const MODAL_FOCUSABLE = 'button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])';

function focusFirstIn(modalEl) {
  const target = modalEl.querySelector(MODAL_FOCUSABLE);
  if (target) requestAnimationFrame(() => target.focus());
}

// A modal registers its own close() here (keyed by the modal element's id) so
// the generic Escape handler below can call it instead of just hiding the
// backdrop directly. close() is where each modal's real cleanup lives: focus
// restore, in some cases cancelling in-flight requests (Compile PDF's SSE
// connection). A modal that never registers (the two Tropy modals, which
// already close themselves correctly via their own capture-phase Escape
// handler in tropy.js) falls back to a plain hide, which is a harmless no-op
// if that modal is already closed by the time this handler runs.
const MODAL_CLOSERS = {};
function registerModalCloser(modalId, closeFn) {
  MODAL_CLOSERS[modalId] = closeFn;
}

document.addEventListener("keydown", (e) => {
  const tag = e.target.tagName;
  const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

  // Escape closes modals — via each modal's own close() when registered, so
  // its cleanup (focus restore, cancelling in-flight requests) actually runs.
  if (e.key === "Escape") {
    document.querySelectorAll(".modal-backdrop:not(.hidden)").forEach(m => {
      (MODAL_CLOSERS[m.id] || (() => m.classList.add("hidden")))();
    });
  }

  // Tab cycles within the open modal instead of escaping to the page behind it
  if (e.key === "Tab") {
    const openModal = document.querySelector(".modal-backdrop:not(.hidden)");
    if (openModal) {
      const focusable = [...openModal.querySelectorAll(MODAL_FOCUSABLE)];
      if (focusable.length) {
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
  }

  // Ctrl+Enter / Cmd+Enter runs pipeline
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    if (!els["btn-run"].disabled) els["btn-run"].click();
  }

  // Delete removes selected items (when not in a text input)
  if (e.key === "Delete" && !inInput) {
    if (selected.size && !running) els["btn-remove"].click();
  }

  // 1-5 switches tabs (when not in an input)
  if (!inInput && !e.ctrlKey && !e.metaKey) {
    const num = parseInt(e.key, 10);
    if (num >= 1 && num <= 5) {
      const tabs = document.querySelectorAll(".tab[data-tab]");
      if (tabs[num - 1]) tabs[num - 1].click();
    }
  }
});

// -------------------------------------------------------- column visibility

const ColVis = (function () {
  const STORAGE_KEY = "ocr_col_vis";
  let menuEl = null;
  const COLS = [
    { key: "ocr", label: "OCR", default: true },
    { key: "cleanup", label: "Cleanup", default: true },
    { key: "translate", label: "Translate", default: true },
    { key: "confidence", label: "Conf", default: true },
    { key: "elapsed", label: "Time", default: true },
  ];

  function loadState() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; } catch { return {}; }
  }

  function saveState(state) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  }

  function isVisible(key) {
    const state = loadState();
    if (key in state) return state[key];
    return COLS.find(c => c.key === key)?.default ?? true;
  }

  function toggle(key) {
    const state = loadState();
    state[key] = !isVisible(key);
    saveState(state);
    apply();
  }

  function apply() {
    const header = els["queue-body"]?.closest("table")?.querySelector("thead tr");
    const rows = els["queue-body"]?.querySelectorAll("tr");
    if (!header) return;
    const ths = header.querySelectorAll("th");
    // th indices: 0=checkbox, 1=File, 2=OCR, 3=Cleanup, 4=Translate, 5=Conf, 6=Time, 7=Status, 8=view
    const colMap = { ocr: 2, cleanup: 3, translate: 4, confidence: 5, elapsed: 6 };
    for (const [key, idx] of Object.entries(colMap)) {
      const vis = isVisible(key);
      if (ths[idx]) ths[idx].style.display = vis ? "" : "none";
      rows?.forEach(tr => {
        const td = tr.children[idx];
        if (td) td.style.display = vis ? "" : "none";
      });
    }
  }

  function showMenu(e) {
    if (!menuEl) {
      menuEl = document.createElement("div");
      menuEl.className = "col-vis-menu hidden";
      menuEl.innerHTML = COLS.map(c => `
        <div class="col-vis-item" data-col="${c.key}">
          <input type="checkbox" ${isVisible(c.key) ? "checked" : ""}>
          <span>${c.label}</span>
        </div>`).join("");
      document.body.appendChild(menuEl);
      menuEl.querySelectorAll(".col-vis-item").forEach(el => {
        el.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const cb = el.querySelector("input");
          cb.checked = !cb.checked;
          toggle(el.dataset.col);
        });
      });
    }
    menuEl.classList.toggle("hidden");
    if (!menuEl.classList.contains("hidden")) {
      const rect = e.target.getBoundingClientRect();
      menuEl.style.top = `${rect.bottom + 4}px`;
      menuEl.style.left = `${rect.left}px`;
      // Sync checkboxes
      menuEl.querySelectorAll(".col-vis-item").forEach(el => {
        el.querySelector("input").checked = isVisible(el.dataset.col);
      });
      const close = (ev) => {
        if (!menuEl.contains(ev.target)) {
          menuEl.classList.add("hidden");
          document.removeEventListener("click", close);
        }
      };
      setTimeout(() => document.addEventListener("click", close), 0);
    }
  }

  apply();
  return { showMenu, apply };
})();

window.ColVis = ColVis;

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

// -------------------------------------------------------- batch correct

const SplitResize = (function () {
  const STORAGE_KEY = "ocr_split_frac";
  const HANDLES = ["preview-resize-handle", "history-resize-handle"];

  // Reasonable bounds: image column must stay between 20% and 70% of the card.
  var MIN_FRAC = 0.20;
  var MAX_FRAC = 0.70;
  var DEFAULT_FRAC = 0.333;

  function loadState() {
    try {
      var v = parseFloat(localStorage.getItem(STORAGE_KEY));
      if (!isNaN(v) && v >= MIN_FRAC && v <= MAX_FRAC) return v;
    } catch (_) {}
    return DEFAULT_FRAC;
  }

  function saveState(frac) {
    try { localStorage.setItem(STORAGE_KEY, String(frac)); } catch (_) {}
  }

  function apply(frac) {
    // Push the value as a CSS custom property onto every compare-card instance.
    document.querySelectorAll(".compare-card--with-image").forEach(function (card) {
      card.style.setProperty("--split-frac", String(frac));
    });
  }

  // ── Drag interaction ──────────────────────────────────────────────────────

  function initHandle(handleId) {
    var handle = document.getElementById(handleId);
    if (!handle) return;
    var dragging = false;
    var startX = 0;
    var startFrac = DEFAULT_FRAC;

    handle.addEventListener("mousedown", function (e) {
      if (e.button !== 0) return; // left button only
      dragging = true;
      startX = e.clientX;
      startFrac = loadState();
      e.preventDefault();
      document.body.style.cursor = "col-resize";
      handle.setAttribute("aria-label", "Dragging — release to set position");
    });

    document.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      var cards = document.querySelectorAll(".compare-card--with-image");
      if (!cards.length) return;
      // Use the first card's width as reference (both cards are independent
      // but share the same CSS variable, so either works).
      var cardWidth = cards[0].offsetWidth;
      if (!cardWidth) return;
      // Total columns = image(frac) + handle(8px) + text(1fr = remaining)
      // handle width in px
      var handleW = 8;
      // Delta in fraction: (deltaX / cardWidth)
      var delta = (e.clientX - startX) / cardWidth;
      var next = Math.min(MAX_FRAC, Math.max(MIN_FRAC, startFrac + delta));
      apply(next);
    });

    document.addEventListener("mouseup", function () {
      if (!dragging) return;
      dragging = false;
      document.body.style.cursor = "";
      // Read the currently-applied CSS variable value back from the card.
      var card = document.querySelector(".compare-card--with-image");
      if (card) {
        var v = parseFloat(card.style.getPropertyValue("--split-frac"));
        if (!isNaN(v)) saveState(v);
      }
      handle.setAttribute("aria-label", "Drag to resize");
    });

    // Keyboard accessibility: left/right arrow keys move the split in 1% steps.
    handle.addEventListener("keydown", function (e) {
      var step = e.shiftKey ? 0.05 : 0.01;
      var card = handle.closest(".compare-card--with-image") || document.querySelector(".compare-card--with-image");
      var cur = parseFloat((card && card.style.getPropertyValue("--split-frac")) || String(DEFAULT_FRAC)) || DEFAULT_FRAC;
      var next;
      if (e.key === "ArrowLeft") { next = Math.max(MIN_FRAC, cur - step); e.preventDefault(); }
      else if (e.key === "ArrowRight") { next = Math.min(MAX_FRAC, cur + step); e.preventDefault(); }
      else return;
      apply(next);
      saveState(next);
    });
  }

  HANDLES.forEach(initHandle);
  apply(loadState());

  return { apply: apply };
})();

window.SplitResize = SplitResize;

const BatchCorrect = (function () {
  const modal = document.getElementById("modal-batch-correct");
  const findInput = document.getElementById("batch-find");
  const replaceInput = document.getElementById("batch-replace");
  const stageRaw = document.getElementById("batch-stage-raw");
  const stageCleaned = document.getElementById("batch-stage-cleaned");
  const stageTranslated = document.getElementById("batch-stage-translated");
  const applySelected = document.getElementById("batch-apply-selected");
  const statusEl = document.getElementById("batch-status");
  const btnApply = document.getElementById("btn-batch-apply");
  const btnClose = document.getElementById("btn-batch-cancel");
  let returnFocus = null;

  function open() {
    if (!modal) return;
    returnFocus = document.activeElement;
    modal.classList.remove("hidden");
    statusEl.textContent = "";
    focusFirstIn(modal);
  }

  function close() {
    if (modal) modal.classList.add("hidden");
    returnFocus?.focus?.();
  }

  async function apply() {
    const find = findInput.value.trim();
    const replace = replaceInput.value;
    if (!find) { statusEl.textContent = "Enter text to find."; return; }
    const stages = [];
    if (stageRaw.checked) stages.push("raw");
    if (stageCleaned.checked) stages.push("cleaned");
    if (stageTranslated.checked) stages.push("translated");
    if (!stages.length) { statusEl.textContent = "Select at least one stage."; return; }

    btnApply.disabled = true;
    const label = btnApply.textContent;
    btnApply.textContent = "Applying\u2026";
    statusEl.textContent = "";

    try {
      const body = { find, replace, stages };
      if (applySelected.checked) body.item_ids = [...selected];
      const result = await api("POST", "/api/queue/batch-replace", body);
      setQueue(result.items);
      statusEl.textContent = `Applied to ${result.updated} text(s) across ${result.items.length} item(s).`;
      log(`Batch correct applied: "${find}" -> "${replace}" (${result.updated} change(s))`, "accent");
    } catch (err) {
      statusEl.textContent = `Error: ${friendlyError(err)}`;
      log(`Batch correct failed: ${friendlyError(err)}`, "error");
    } finally {
      btnApply.textContent = label;
      btnApply.disabled = false;
    }
  }

  btnApply.addEventListener("click", apply);
  btnClose.addEventListener("click", close);
  modal?.querySelector("[data-modal-close]")?.addEventListener("click", close);
  // Close on backdrop click
  modal?.addEventListener("click", (e) => { if (e.target === modal) close(); });

  document.getElementById("btn-batch-correct")?.addEventListener("click", open);
  registerModalCloser("modal-batch-correct", close);

  return { open, close };
})();

const SegmentationHelp = (function () {
  const modal = document.getElementById("modal-segmentation-help");
  let returnFocus = null;

  async function open() {
    if (!modal) return;
    returnFocus = document.activeElement;
    // Loaded lazily and cached by SegmentationToggle; calling this here
    // does not re-fetch if the user already opened the provider dropdown,
    // and it fetches for the first time if they open help before that.
    const providers = await SegmentationToggle.loadCapabilities();
    const availableNames = new Set((providers || []).map(p => p.name));
    modal.querySelectorAll("[data-provider-unavailable]").forEach((el) => {
      const name = el.getAttribute("data-provider-unavailable");
      el.hidden = availableNames.has(name);
    });
    modal.classList.remove("hidden");
    focusFirstIn(modal);
  }

  function close() {
    if (modal) modal.classList.add("hidden");
    returnFocus?.focus?.();
  }

  modal?.querySelector("[data-modal-close]")?.addEventListener("click", close);
  // Close on backdrop click
  modal?.addEventListener("click", (e) => { if (e.target === modal) close(); });

  document.getElementById("btn-segmentation-help")?.addEventListener("click", open);
  registerModalCloser("modal-segmentation-help", close);

  return { open, close };
})();

window.SegmentationHelp = SegmentationHelp;

window.BatchCorrect = BatchCorrect;
