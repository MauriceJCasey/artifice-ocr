// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

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
    log("Drag-and-drop is not available in desktop mode — use Add files.", "warning");
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
