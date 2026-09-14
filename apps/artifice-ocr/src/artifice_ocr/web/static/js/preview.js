// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * Preview tab: Raw / Cleaned / Translated for one in-memory queue item.
 * Populated either by clicking a queue row's view arrow, by picking from the
 * dropdown here, or automatically as items finish while this tab is open
 * (wired from app.js's SSE handler, mirroring the desktop build's
 * `App.preview_item` auto-follow behaviour).
 */

const PreviewTab = (function () {
  const container = document.getElementById("panel-preview");
  const select = document.getElementById("preview-item-select");
  const btnSaveRaw = document.getElementById("btn-save-raw");
  const btnSaveCleaned = document.getElementById("btn-save-cleaned");
  const btnSaveTranslated = document.getElementById("btn-save-translated");
  const fabricatedToggle = document.getElementById("preview-fabricated-result");

  let currentItemId = null;
  let openRequest = 0;
  const originalText = { raw: "", cleaned: "", translated: "" };
  const btnReprocess = document.getElementById("btn-reprocess");

  const paneConfigs = {
    raw: {
      btn: btnSaveRaw,
      endpoint: (id) => `/api/queue/${id}/raw-text`,
    },
    cleaned: {
      btn: btnSaveCleaned,
      endpoint: (id) => `/api/queue/${id}/cleaned-text`,
    },
    translated: {
      btn: btnSaveTranslated,
      endpoint: (id) => `/api/queue/${id}/translated-text`,
    },
  };

  function refreshList(keepSelection) {
    const prev = keepSelection ? select.value : null;
    const eligible = [...items.values()].filter((i) => i.state !== "pending");
    select.innerHTML = eligible.length
      ? eligible.map((i) => `<option value="${i.id}">${escapeHtml(i.name)}</option>`).join("")
      : `<option value="">No documents yet</option>`;
    if (prev && eligible.some((i) => i.id === prev)) select.value = prev;
  }

  function getPaneTextarea(key) {
    return container.querySelector(`.compare-pane[data-pane="${key}"] textarea.raw-edit`);
  }

  function hasUnsavedEdits(stage) {
    const key = stage === "raw_ocr" ? "raw" : stage;
    const textarea = getPaneTextarea(key);
    return Boolean(
      currentItemId &&
      textarea &&
      Object.hasOwn(originalText, key) &&
      textarea.value !== originalText[key]
    );
  }

  // Re-run after every render (renderCompare rebuilds the textarea element
  // each time) so the dirty flag and Ctrl+S shortcut stay attached to
  // whichever textarea is currently in the DOM.
  function wirePaneEditing(key) {
    const textarea = getPaneTextarea(key);
    const btn = paneConfigs[key].btn;
    if (!textarea || !btn) return;
    originalText[key] = textarea.value;
    btn.disabled = true;
    textarea.addEventListener("input", () => {
      btn.disabled = textarea.value === originalText[key];
    });
    textarea.addEventListener("keydown", (e) => {
      if ((e.key === "s" || e.key === "S") && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        if (!btn.disabled) savePaneText(key);
      }
    });
  }

  // Not autosaved on every keystroke — a scan-correction session is a
  // deliberate, occasional action, not a live-typing document, so saving
  // only happens on the button click or Ctrl+S.
  async function savePaneText(key) {
    const textarea = getPaneTextarea(key);
    const btn = paneConfigs[key].btn;
    if (!textarea || !currentItemId || !btn) return;
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Saving…";
    try {
      const data = await api("POST", paneConfigs[key].endpoint(currentItemId), { text: textarea.value });
      renderCompare(container, data, { editableStages: new Set(["raw", "cleaned", "translated"]) });
      wireAllPanes();
      wireOriginalToggles(container);
      wireCrossHighlight(container);
      log(`${key.charAt(0).toUpperCase() + key.slice(1)} text corrected and saved.`, "accent");
    } catch (err) {
      log(`Could not save correction: ${err.message}`, "error");
      btn.disabled = false;
    } finally {
      btn.textContent = label;
    }
  }

  async function reprocessItem() {
    if (!currentItemId || !btnReprocess) return;
    btnReprocess.disabled = true;
    const label = btnReprocess.textContent;
    btnReprocess.textContent = "Processing\u2026";
    try {
      const data = await api("POST", `/api/queue/${currentItemId}/reprocess`, {
        from_stage: "raw", stages: ["cleanup", "translate"],
      });
      renderCompare(container, data, { editableStages: new Set(["raw", "cleaned", "translated"]) });
      wireAllPanes();
      wireOriginalToggles(container);
      wireCrossHighlight(container);
      log("Re-processing complete.", "accent");
    } catch (err) {
      log(`Re-processing failed: ${err.message}`, "error");
    } finally {
      btnReprocess.textContent = label;
      btnReprocess.disabled = false;
    }
  }

  function wireAllPanes() {
    ["raw", "cleaned", "translated"].forEach(wirePaneEditing);
  }

  if (btnSaveRaw) btnSaveRaw.addEventListener("click", () => savePaneText("raw"));
  if (btnSaveCleaned) btnSaveCleaned.addEventListener("click", () => savePaneText("cleaned"));
  if (btnSaveTranslated) btnSaveTranslated.addEventListener("click", () => savePaneText("translated"));
  if (btnReprocess) btnReprocess.addEventListener("click", reprocessItem);
  if (fabricatedToggle) fabricatedToggle.addEventListener("change", async () => {
    if (!currentItemId) return;
    const requested = fabricatedToggle.checked;
    fabricatedToggle.disabled = true;
    try {
      await api("POST", `/api/queue/${currentItemId}/fabricated-result`, { fabricated: requested });
      log(requested ? "Marked as a fabricated OCR result." : "Removed fabricated-result mark.", "accent");
    } catch (err) {
      fabricatedToggle.checked = !requested;
      log(`Could not save review flag: ${err.message}`, "error");
    } finally {
      fabricatedToggle.disabled = false;
    }
  });

  async function open(id) {
    const request = ++openRequest;
    refreshList(false);
    select.value = id;
    currentItemId = id;
    if (btnSaveRaw) btnSaveRaw.disabled = true;
    if (btnSaveCleaned) btnSaveCleaned.disabled = true;
    if (btnSaveTranslated) btnSaveTranslated.disabled = true;
    if (btnReprocess) btnReprocess.disabled = true;
    if (fabricatedToggle) fabricatedToggle.disabled = true;

    try {
      const data = await api("GET", `/api/queue/${id}/preview`);
      if (request !== openRequest) return;
      renderCompare(container, data, { editableStages: new Set(["raw", "cleaned", "translated"]) });
      wireAllPanes();
      wireOriginalToggles(container);
      wireCrossHighlight(container);
      if (btnReprocess) btnReprocess.disabled = !data.raw;
      if (fabricatedToggle) {
        fabricatedToggle.checked = !!data.fabricated_result;
        fabricatedToggle.disabled = false;
      }
    } catch (err) {
      if (request !== openRequest) return;
      clearCompare(container);
      container.querySelector(".compare-title").textContent = `Could not load: ${err.message}`;
      if (btnSaveRaw) btnSaveRaw.disabled = true;
      if (btnSaveCleaned) btnSaveCleaned.disabled = true;
      if (btnSaveTranslated) btnSaveTranslated.disabled = true;
      if (btnReprocess) btnReprocess.disabled = true;
      if (fabricatedToggle) {
        fabricatedToggle.checked = false;
        fabricatedToggle.disabled = true;
      }
    }

    if (window.PreviewImage) window.PreviewImage.load(`/api/queue/${id}/image`);

    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    document.querySelector('.tab[data-tab="preview"]').classList.add("active");
    container.classList.add("active");
  }

  select.addEventListener("change", () => { if (select.value) open(select.value); });

  TAB_ACTIVATE.preview = () => {
    refreshList(true);
    // A populated native select visually chooses its first option without
    // emitting a change event. Load that item explicitly so Review never
    // opens with a selected filename beside an empty comparison.
    if (select.value) open(select.value);
  };
  if (container.classList.contains("active")) TAB_ACTIVATE.preview();

  // Find & Replace
  if (container) {
    const previewFindReplace = new FindReplace(container);
    previewFindReplace.attach();
  }

  return { open, hasUnsavedEdits };
})();

window.PreviewTab = PreviewTab;
