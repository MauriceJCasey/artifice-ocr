// SPDX-FileCopyrightText: 2026 Maurice Casey
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Live, read-only Tropy browsing and official Developer API note write-back. */
const tropy = {};
[
  "btn-add-tropy", "modal-tropy-add", "tropy-browse-path", "btn-tropy-browse-pick",
  "btn-tropy-browse-load", "tropy-browse-project-info", "tropy-browse-project-name",
  "tropy-browse-loading", "tropy-browse-error", "tropy-browse-error-text",
  "tropy-browse-recent-row", "tropy-browse-recent",
  "tropy-browse-picker", "tropy-browse-lists", "tropy-browse-tags",
  "tropy-browse-item-empty", "tropy-browse-item-list", "btn-tropy-browse-item-select-all",
  "tropy-browse-summary", "tropy-browse-summary-text", "btn-tropy-cancel-browse",
  "btn-tropy-browse-enqueue", "btn-send-tropy", "modal-tropy-send",
  "tropy-export-stat-items", "tropy-export-stat-photos", "tropy-export-stat-transcriptions",
  "tropy-export-stage", "tropy-writeback-preview", "btn-send-tropy-close-writeback",
  "btn-writeback-preview", "btn-writeback-commit",
].forEach((id) => { tropy[id] = document.getElementById(id); });

let project = null;
let lists = [];
let tags = [];
let visibleItems = [];
let selectedPhotos = new Map();
let filter = null;
let sendContext = null;
let notePreview = null;
let browserReturnFocus = null;
let sendReturnFocus = null;
let previewAbortController = null;
let previewTickTimer = null;

// Above this many pages, checking each one individually against Tropy's
// Developer API (one HTTP round trip per page, more for pages that already
// carry notes) can run for minutes if Tropy is busy with its own UI. Warn
// before starting rather than leaving the user staring at a status line
// with no sense of whether it is working or stuck.
const LARGE_BATCH_WARNING_THRESHOLD = 150;

function stopPreviewTicker() {
  if (previewTickTimer) { clearInterval(previewTickTimer); previewTickTimer = null; }
}

// A fresh check (or closing the modal) supersedes whatever Tropy request is
// still in flight — abort it instead of letting it finish unobserved and
// possibly race a newer response into the status line.
function cancelInFlightPreview() {
  stopPreviewTicker();
  if (previewAbortController) { previewAbortController.abort(); previewAbortController = null; }
}

function notify(kind, message) {
  if (!window.ArtificeToast) return;
  if (kind === "error") window.ArtificeToast.error(message);
  else if (kind === "warning") window.ArtificeToast.show(message, "warning");
  else window.ArtificeToast.success(message);
}

function setupProjectPickers() {
  const input = tropy["tropy-browse-path"];
  tropy["tropy-browse-recent"].onchange = () => {
    if (!tropy["tropy-browse-recent"].value) return;
    input.value = tropy["tropy-browse-recent"].value;
    loadProject();
  };
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    loadProject();
  });
}

function resetBrowser() {
  project = null;
  lists = [];
  tags = [];
  visibleItems = [];
  selectedPhotos = new Map();
  filter = null;
  ["tropy-browse-project-info", "tropy-browse-loading", "tropy-browse-error",
    "tropy-browse-picker", "tropy-browse-summary"].forEach((id) => tropy[id].classList.add("hidden"));
  tropy["btn-tropy-browse-enqueue"].disabled = true;
  tropy["btn-tropy-browse-enqueue"].textContent = "Add selected pages";
  tropy["tropy-browse-path"].removeAttribute("aria-invalid");
  tropy["tropy-browse-path"].disabled = false;
  tropy["btn-tropy-browse-pick"].disabled = false;
  tropy["btn-tropy-browse-load"].disabled = false;
  tropy["btn-tropy-browse-load"].removeAttribute("aria-busy");
}

async function openBrowser() {
  browserReturnFocus = document.activeElement;
  resetBrowser();
  tropy["modal-tropy-add"].classList.remove("hidden");
  try {
    const config = await api("GET", "/api/config");
    if (config.tropy_last_path) tropy["tropy-browse-path"].value = config.tropy_last_path;
  } catch (_) { /* A saved path is optional. */ }
  try {
    const data = await api("GET", "/api/tropy/browse/recent");
    const projects = data.projects || [];
    tropy["tropy-browse-recent"].innerHTML = '<option value="">Choose a recent project…</option>' + projects.map(
      (path) => `<option value="${escapeHtml(path)}">${escapeHtml(path)}</option>`
    ).join("");
    tropy["tropy-browse-recent-row"].classList.toggle("hidden", !projects.length);
  } catch (error) {
    tropy["tropy-browse-recent-row"].classList.add("hidden");
    const disabled = error.message === "Live Tropy browse is not enabled";
    tropy["tropy-browse-error-text"].textContent = disabled
      ? "Enable Tropy project browsing in Settings before adding pages."
      : "Could not load recent Tropy projects: " + error.message;
    tropy["tropy-browse-error"].classList.remove("hidden");
    if (disabled) {
      tropy["tropy-browse-path"].disabled = true;
      tropy["btn-tropy-browse-pick"].disabled = true;
      tropy["btn-tropy-browse-load"].disabled = true;
    }
  }
  requestAnimationFrame(() => {
    const target = tropy["tropy-browse-path"].disabled
      ? tropy["modal-tropy-add"].querySelector("[data-modal-close]")
      : tropy["tropy-browse-path"];
    target.focus();
  });
}

async function pickProject(endpoint, body) {
  try {
    const data = await api("POST", endpoint, body);
    if (data.state === "selected" && data.paths?.length) {
      tropy["tropy-browse-path"].value = data.paths[0];
      return true;
    } else if (data.state === "unavailable") {
      notify("warning", data.reason || "Project picker unavailable");
    }
  } catch (error) {
    notify("error", "Could not open the project picker: " + error.message);
  }
  return false;
}

async function loadProject() {
  const path = tropy["tropy-browse-path"].value.trim();
  if (!path) {
    tropy["tropy-browse-path"].setAttribute("aria-invalid", "true");
    tropy["tropy-browse-error-text"].textContent = "Choose a .tropy project folder first.";
    tropy["tropy-browse-error"].classList.remove("hidden");
    tropy["tropy-browse-path"].focus();
    return;
  }
  resetBrowser();
  tropy["tropy-browse-loading"].classList.remove("hidden");
  tropy["btn-tropy-browse-load"].disabled = true;
  tropy["btn-tropy-browse-load"].setAttribute("aria-busy", "true");
  tropy["btn-tropy-browse-load"].textContent = "Opening…";
  try {
    const data = await api("POST", "/api/tropy/browse/projects", { path });
    const details = data.projects?.[0];
    if (!details) throw new Error("No Tropy project was found at that location");
    project = { ...details, path };
    tropy["tropy-browse-project-name"].textContent = details.name || path;
    tropy["tropy-browse-project-info"].classList.remove("hidden");
    await api("POST", "/api/config", { tropy_last_path: path });
    const [listData, tagData] = await Promise.all([
      api("POST", "/api/tropy/browse/lists", { path }),
      api("POST", "/api/tropy/browse/tags", { path }),
    ]);
    lists = listData.lists || [];
    tags = tagData.tags || [];
    renderSources();
    await loadItems();
    tropy["tropy-browse-picker"].classList.remove("hidden");
  } catch (error) {
    tropy["tropy-browse-path"].setAttribute("aria-invalid", "true");
    tropy["tropy-browse-error-text"].textContent = error.message;
    tropy["tropy-browse-error"].classList.remove("hidden");
  } finally {
    tropy["tropy-browse-loading"].classList.add("hidden");
    tropy["btn-tropy-browse-load"].disabled = false;
    tropy["btn-tropy-browse-load"].removeAttribute("aria-busy");
    tropy["btn-tropy-browse-load"].textContent = "Open project";
  }
}

function renderSources() {
  const listContainer = tropy["tropy-browse-lists"];
  function tree(parentId) {
    return lists.filter((entry) => entry.parent_list_id === parentId).map((entry) =>
      '<div class="tropy-browse-list-node">' +
      `<button type="button" class="tropy-browse-list-link" aria-pressed="false" data-list-id="${entry.list_id}">${escapeHtml(entry.name || "")}</button>` +
      tree(entry.list_id) + "</div>"
    ).join("");
  }
  listContainer.innerHTML = '<button type="button" class="tropy-browse-list-link active" aria-pressed="true" data-list-id="all"><strong>All items</strong></button>' + tree(0);
  listContainer.querySelectorAll(".tropy-browse-list-link").forEach((element) => {
    element.onclick = () => {
      setActiveSource(element);
      filter = element.dataset.listId === "all" ? null : { list_id: Number(element.dataset.listId) };
      loadItems();
    };
  });
  const tagContainer = tropy["tropy-browse-tags"];
  tagContainer.innerHTML = tags.map((tag) =>
    `<button type="button" class="tropy-browse-tag-link" aria-pressed="false" data-tag="${escapeHtml(tag.name)}">${escapeHtml(tag.name)}</button>`
  ).join("") || '<span class="dim tropy-source-empty">No tags</span>';
  tagContainer.querySelectorAll(".tropy-browse-tag-link").forEach((element) => {
    element.onclick = () => { setActiveSource(element); filter = { tag: element.dataset.tag }; loadItems(); };
  });
}

function setActiveSource(activeElement) {
  tropy["tropy-browse-picker"].querySelectorAll(".tropy-browse-list-link, .tropy-browse-tag-link").forEach((element) => {
    const active = element === activeElement;
    element.classList.toggle("active", active);
    element.setAttribute("aria-pressed", String(active));
  });
}

async function loadItems() {
  const params = new URLSearchParams();
  if (filter?.list_id !== undefined) params.set("list_id", filter.list_id);
  if (filter?.tag !== undefined) params.set("tag", filter.tag);
  try {
    const data = await api("POST", "/api/tropy/browse/items" + (params.size ? "?" + params : ""), { path: project.path });
    visibleItems = data.items || [];
    renderItems();
  } catch (error) {
    notify("error", error.message);
  }
}

function renderItems() {
  const visiblePhotos = visibleItems.flatMap((item) => item.photos || []).filter((photo) => !photo.missing);
  const allSelected = visiblePhotos.length > 0 && visiblePhotos.every((photo) => selectedPhotos.has(photo.photo_id));
  tropy["tropy-browse-item-empty"].style.display = visibleItems.length ? "none" : "";
  tropy["btn-tropy-browse-item-select-all"].disabled = !visiblePhotos.length;
  tropy["btn-tropy-browse-item-select-all"].textContent = allSelected ? "Deselect all" : "Select all";
  tropy["tropy-browse-item-list"].innerHTML = visibleItems.map((item) => {
    const available = (item.photos || []).filter((photo) => !photo.missing);
    const itemSelected = available.length > 0 && available.every((photo) => selectedPhotos.has(photo.photo_id));
    const someSelected = available.some((photo) => selectedPhotos.has(photo.photo_id));
    const pages = (item.photos || []).map((photo, index) => {
      const label = photo.page == null ? `Photo ${index + 1}` : `Page ${photo.page + 1}`;
      const unavailable = photo.missing ? '<span class="tropy-result-missing-badge">Unavailable</span>' : "";
      return `<label class="tropy-browse-page-row${photo.missing ? " unavailable" : ""}">` +
        `<input type="checkbox" class="tropy-browse-page-check" data-item-id="${item.item_id}" ` +
        `data-photo-id="${photo.photo_id}" ${selectedPhotos.has(photo.photo_id) ? "checked" : ""} ${photo.missing ? "disabled" : ""}>` +
        `<span>${escapeHtml(label)}</span><span class="page-file">${escapeHtml((photo.path || "").split(/[\\/]/).pop())}</span>${unavailable}</label>`;
    }).join("");
    return `<section class="tropy-browse-item-group" data-item-id="${item.item_id}">` +
      `<label class="tropy-browse-item-row"><input type="checkbox" class="tropy-browse-item-check" ` +
      `data-item-id="${item.item_id}" ${itemSelected ? "checked" : ""} data-partial="${someSelected && !itemSelected}">` +
      `<span class="item-title">${escapeHtml(item.title || "Untitled item")}</span>` +
      `<span class="item-meta">${item.photo_count} ${item.photo_count === 1 ? "page" : "pages"}</span></label>` +
      `<div class="tropy-browse-pages" aria-label="Pages in ${escapeHtml(item.title || "Untitled item")}">${pages}</div></section>`;
  }).join("");
  tropy["tropy-browse-item-list"].querySelectorAll(".tropy-browse-item-check").forEach((checkbox) => {
    checkbox.indeterminate = checkbox.dataset.partial === "true";
    checkbox.onchange = () => {
      const id = Number(checkbox.dataset.itemId);
      const item = visibleItems.find((entry) => entry.item_id === id);
      (item?.photos || []).filter((photo) => !photo.missing).forEach((photo) => {
        if (checkbox.checked) selectedPhotos.set(photo.photo_id, { ...photo, item_id: id });
        else selectedPhotos.delete(photo.photo_id);
      });
      renderItems();
    };
  });
  tropy["tropy-browse-item-list"].querySelectorAll(".tropy-browse-page-check").forEach((checkbox) => {
    checkbox.onchange = () => {
      const itemId = Number(checkbox.dataset.itemId);
      const photoId = Number(checkbox.dataset.photoId);
      const item = visibleItems.find((entry) => entry.item_id === itemId);
      const photo = item?.photos?.find((entry) => entry.photo_id === photoId);
      if (checkbox.checked && photo) selectedPhotos.set(photoId, { ...photo, item_id: itemId });
      else selectedPhotos.delete(photoId);
      renderItems();
    };
  });
  updateSummary();
}

function updateSummary() {
  const itemCount = new Set([...selectedPhotos.values()].map((photo) => photo.item_id)).size;
  const pageCount = selectedPhotos.size;
  const text = `${pageCount} ${pageCount === 1 ? "page" : "pages"} selected from ${itemCount} ${itemCount === 1 ? "item" : "items"}`;
  tropy["tropy-browse-summary-text"].textContent = text;
  tropy["tropy-browse-summary-text"].classList.remove("warning");
  tropy["tropy-browse-summary"].classList.toggle("hidden", pageCount === 0);
  tropy["btn-tropy-browse-enqueue"].disabled = pageCount === 0;
}

async function enqueueSelection() {
  const button = tropy["btn-tropy-browse-enqueue"];
  button.disabled = true;
  button.textContent = "Adding…";
  button.setAttribute("aria-busy", "true");
  try {
    const data = await api("POST", "/api/tropy/browse/enqueue", {
      path: project.path,
      output_dir: document.getElementById("output-dir")?.value || "output",
      item_ids: [...new Set([...selectedPhotos.values()].map((photo) => photo.item_id))],
      photo_ids: [...selectedPhotos.keys()],
    });
    setQueue(data.items);
    tropy["modal-tropy-add"].classList.add("hidden");
    const suffix = data.missing ? `; ${data.missing} of ${data.total} page(s) unavailable` : "";
    notify(data.missing ? "warning" : "success", `Added ${data.added} page(s) from Tropy${suffix}`);
  } catch (error) {
    notify("error", "Could not add Tropy pages: " + error.message);
    button.disabled = false;
  } finally {
    button.textContent = "Add selected pages";
    button.removeAttribute("aria-busy");
  }
}

function sendBody() {
  return {
    source: sendContext?.isHistory ? "history" : "queue",
    item_ids: sendContext?.itemIds || [],
    stage: tropy["tropy-export-stage"].value,
  };
}

function hasUnsavedText() {
  const editor = sendContext?.isHistory ? window.HistoryTab : window.PreviewTab;
  return Boolean(editor?.hasUnsavedEdits?.(tropy["tropy-export-stage"].value));
}

function showNoteStatus(message, state = "default") {
  const target = tropy["tropy-writeback-preview"];
  target.textContent = message;
  target.classList.remove("hidden");
  target.classList.toggle("error", state === "error");
  target.classList.toggle("success", state === "success");
}

async function previewNotes() {
  cancelInFlightPreview();
  notePreview = null;
  tropy["btn-writeback-commit"].disabled = true;
  if (hasUnsavedText()) return showNoteStatus("Save the current edits before sending this text to Tropy.", "error");

  const controller = new AbortController();
  previewAbortController = controller;
  // A newer previewNotes() call replaces previewAbortController before this
  // one's fetch settles (cancelInFlightPreview() runs at the top of every
  // call). Without this guard, the stale call's success/catch/finally would
  // still fire and clobber the newer call's ticker, buttons, and cancel
  // handle once its own await resolves.
  const isCurrent = () => previewAbortController === controller;
  const startedAt = Date.now();
  const itemCount = sendContext?.itemIds?.length || 0;
  const tick = () => {
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    const of = itemCount > 1 ? ` of up to ${itemCount} pages` : "";
    showNoteStatus(`Checking the open Tropy project… (${elapsed}s${of} — close this dialog to cancel)`);
  };
  tick();
  previewTickTimer = setInterval(tick, 1000);

  tropy["tropy-export-stage"].disabled = true;
  tropy["btn-writeback-preview"].disabled = true;
  tropy["btn-writeback-preview"].setAttribute("aria-busy", "true");
  try {
    const data = await api("POST", "/api/tropy/notes/preview", sendBody(), { signal: controller.signal });
    if (!isCurrent()) return;
    stopPreviewTicker();
    notePreview = data;
    const count = data.counts || {};
    tropy["tropy-export-stat-items"].textContent = String(count.selected || 0);
    tropy["tropy-export-stat-photos"].textContent = String((count.ready || 0) + (count.duplicate || 0));
    tropy["tropy-export-stat-transcriptions"].textContent = String(count.ready || 0);
    const blockers = data.blockers || [];
    const itemErrors = data.item_errors || [];
    const message = `${count.ready || 0} ready · ${count.duplicate || 0} duplicate · ${count.empty || 0} empty · ${(count.foreign || 0) + (count.ineligible || 0)} blocked` +
      (count.error ? ` · ${count.error} could not be checked` : "") +
      (blockers.length ? "\n" + blockers.join("\n") : "") +
      (itemErrors.length ? "\n" + itemErrors.map((entry) => `${entry.label}: ${entry.message}`).join("\n") : "");
    showNoteStatus(message, blockers.length > 0 || itemErrors.length > 0 ? "error" : "success");
    tropy["btn-writeback-commit"].disabled = blockers.length > 0 || data.write_count < 1;
    tropy["btn-writeback-commit"].textContent = `Add ${data.write_count || 0} note${data.write_count === 1 ? "" : "s"}`;
  } catch (error) {
    if (!isCurrent()) return;
    stopPreviewTicker();
    if (error.name === "AbortError") return;
    showNoteStatus("Could not check Tropy: " + error.message, "error");
  } finally {
    if (isCurrent()) {
      previewAbortController = null;
      tropy["tropy-export-stage"].disabled = false;
      tropy["btn-writeback-preview"].disabled = false;
      tropy["btn-writeback-preview"].removeAttribute("aria-busy");
    }
  }
}

async function commitNotes() {
  if (!notePreview || hasUnsavedText()) return previewNotes();
  tropy["btn-writeback-preview"].disabled = true;
  tropy["btn-writeback-commit"].disabled = true;
  tropy["btn-writeback-commit"].setAttribute("aria-busy", "true");
  showNoteStatus("Adding notes to Tropy…");
  try {
    const data = await api("POST", "/api/tropy/notes/commit", {
      ...sendBody(), expected_write_count: notePreview.write_count,
    });
    const blocked = data.remaining || data.errors?.length || 0;
    const errors = data.errors?.length ? "\n" + data.errors.map((entry) => `${entry.label}: ${entry.message}`).join("\n") : "";
    showNoteStatus(`${data.written} added · ${data.skipped} duplicates skipped · ${blocked} blocked${errors}`, data.status === "partial" ? "error" : "success");
    notify(data.status === "partial" ? "warning" : "success", `${data.written} note(s) added to Tropy`);
    notePreview = null;
  } catch (error) {
    showNoteStatus("Could not add notes: " + error.message + "\nCheck again before retrying.", "error");
  } finally {
    tropy["btn-writeback-preview"].disabled = false;
    tropy["btn-writeback-commit"].removeAttribute("aria-busy");
  }
}

async function openTropyExport(context) {
  const itemIds = context?.itemIds || [];
  if (itemIds.length > LARGE_BATCH_WARNING_THRESHOLD) {
    const proceed = confirm(
      `Sending ${itemIds.length} pages to Tropy checks each one individually and can take ` +
      "several minutes if Tropy is busy with its own window open. Continue?"
    );
    if (!proceed) return;
  }
  sendReturnFocus = document.activeElement;
  sendContext = context || null;
  notePreview = null;
  ["tropy-export-stat-items", "tropy-export-stat-photos", "tropy-export-stat-transcriptions"].forEach((id) => {
    tropy[id].textContent = "0";
  });
  tropy["modal-tropy-send"].classList.remove("hidden");
  window.setWorkflowStep?.(4);
  tropy["tropy-writeback-preview"].classList.add("hidden");
  requestAnimationFrame(() => tropy["tropy-export-stage"].focus());
  await previewNotes();
}

function closeSend() {
  cancelInFlightPreview();
  tropy["modal-tropy-send"].classList.add("hidden");
  sendContext = null;
  notePreview = null;
  const activeTab = document.querySelector(".tab.active")?.dataset.tab;
  const step = typeof window.workflowStepForTab === "function"
    ? window.workflowStepForTab(activeTab)
    : (activeTab === "preview" || activeTab === "history" ? 3 : 1);
  window.setWorkflowStep?.(step);
  sendReturnFocus?.focus?.();
}

function closeBrowser() {
  tropy["modal-tropy-add"].classList.add("hidden");
  browserReturnFocus?.focus?.();
}

setupProjectPickers();
tropy["btn-add-tropy"].onclick = openBrowser;
tropy["btn-tropy-browse-pick"].onclick = async () => {
  if (await pickProject("/api/native/pick-folder", {})) await loadProject();
};
tropy["btn-tropy-browse-load"].onclick = loadProject;
tropy["btn-tropy-browse-item-select-all"].onclick = () => {
  const photos = visibleItems.flatMap((item) => (item.photos || []).map((photo) => ({ ...photo, item_id: item.item_id }))).filter((photo) => !photo.missing);
  const all = photos.length && photos.every((photo) => selectedPhotos.has(photo.photo_id));
  photos.forEach((photo) => all ? selectedPhotos.delete(photo.photo_id) : selectedPhotos.set(photo.photo_id, photo));
  renderItems();
};
tropy["btn-tropy-browse-enqueue"].onclick = enqueueSelection;
tropy["btn-tropy-cancel-browse"].onclick = closeBrowser;
tropy["modal-tropy-add"].querySelector("[data-modal-close]").onclick = closeBrowser;
tropy["btn-send-tropy"].onclick = () => {
  const itemIds = window.QueueTab?.selectedIds?.() || [];
  if (!itemIds.length) {
    notify("warning", "Select one or more completed Tropy pages in the queue first.");
    return;
  }
  openTropyExport({ itemIds, isHistory: false });
};
tropy["btn-send-tropy-close-writeback"].onclick = closeSend;
tropy["modal-tropy-send"].querySelector("[data-modal-close]").onclick = closeSend;
tropy["btn-writeback-preview"].onclick = previewNotes;
tropy["btn-writeback-commit"].onclick = commitNotes;
tropy["tropy-export-stage"].onchange = previewNotes;
tropy["modal-tropy-add"].addEventListener("click", (event) => {
  if (event.target === tropy["modal-tropy-add"]) closeBrowser();
});
tropy["modal-tropy-send"].addEventListener("click", (event) => {
  if (event.target === tropy["modal-tropy-send"]) closeSend();
});
document.addEventListener("keydown", (event) => {
  const openModal = [tropy["modal-tropy-send"], tropy["modal-tropy-add"]].find((modal) => !modal.classList.contains("hidden"));
  if (!openModal) return;
  if (event.key === "Escape") {
    event.preventDefault();
    openModal === tropy["modal-tropy-send"] ? closeSend() : closeBrowser();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...openModal.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])')];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}, { capture: true });
window.openTropyExport = openTropyExport;
