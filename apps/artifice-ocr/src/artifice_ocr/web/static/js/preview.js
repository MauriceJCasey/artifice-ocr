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
  // Every Review re-render goes through here. The page picker already names
  // the file, so the heading carries the Tropy item title instead, or steps
  // aside when there isn't one.
  function renderItem(data) {
    renderCompare(container, data, { editableStages: new Set(["raw", "cleaned", "translated"]) });
    const heading = container.querySelector(".compare-title");
    heading.textContent = data.item_title || "";
    heading.hidden = !data.item_title;
    wireAllPanes();
    wireOriginalToggles(container);
    wireCrossHighlight(container);
  }

  async function savePaneText(key) {
    const textarea = getPaneTextarea(key);
    const btn = paneConfigs[key].btn;
    if (!textarea || !currentItemId || !btn) return;
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Saving…";
    try {
      const data = await api("POST", paneConfigs[key].endpoint(currentItemId), { text: textarea.value });
      renderItem(data);
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
      renderItem(data);
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

    // Reset to whole-page mode on every item open.
    setReviewMode("whole");

    try {
      const data = await api("GET", `/api/queue/${id}/preview`);
      if (request !== openRequest) return;
      renderItem(data);
      setPaneTab("raw");
      if (btnReprocess) btnReprocess.disabled = !data.raw;
      if (fabricatedToggle) {
        fabricatedToggle.checked = !!data.fabricated_result;
        fabricatedToggle.disabled = false;
      }
    } catch (err) {
      if (request !== openRequest) return;
      clearCompare(container);
      const heading = container.querySelector(".compare-title");
      heading.textContent = `Could not load: ${err.message}`;
      heading.hidden = false;
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

    // Try to load regions for this item. A 404 means no PAGE document exists
    // (segmentation was never enabled, or OCR hasn't run yet) — fall through
    // to the whole-page view in that case.
    const regionListEl = document.getElementById("preview-region-list");
    const comparePanes = container.querySelector(".compare-panes");
    const reviewModeTabs = document.getElementById("preview-review-mode");
    if (regionListEl && window.RegionReview) {
      try {
        const { hasRegions } = await window.RegionReview.load(id);
        if (request !== openRequest) return;
        reviewModeTabs.style.display = hasRegions ? "" : "none";
        // setReviewMode() is the single place that toggles both the pane
        // visibility and the tab buttons' active class — call it here too
        // instead of duplicating its logic, which previously left "Whole
        // page" marked active even while the regions pane was the one shown.
        // Segmentation is an explicit alternate view. Keep the full-page
        // reading surface as the default even when PAGE regions are present.
        setReviewMode("whole");
        if (hasRegions) {
          // Redraw crops after the image has loaded in PreviewImage.
          const imgEl = document.getElementById("preview-image");
          if (imgEl && imgEl.complete && imgEl.naturalWidth) {
            window.RegionReview.redrawCrops && window.RegionReview.redrawCrops();
          }
        }
      } catch {
        // Non-404 error loading regions — fall back to whole-page view.
        reviewModeTabs.style.display = "none";
        if (comparePanes) comparePanes.style.display = "";
        regionListEl.style.display = "none";
      }
    }

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

  // --------------------------------------------- pane stage tab switching (whole-page view)

  // Tracks which stage panes are currently visible in multi-pane comparison mode.
  const visiblePanes = new Set();

  function setPaneTab(key) {
    const tabs = container.querySelectorAll(".pane-stage-btn");
    const panes = container.querySelectorAll(".compare-pane");
    const comparePanes = container.querySelector(".compare-panes");

    // ── Drag-to-compare: add a pane alongside whatever is already showing.
    // Detected by setPaneTab being called from the drop handler (the drop
    // handler sets a flag that this call clears — see dragover/drop below).
    // `isDragAdd` short-circuits the "click an already-visible tab to close
    // it" check below it — without it, adding "translated" here made it
    // visible AND already-in-the-set by the time that check ran next, so a
    // drag-add immediately deleted the very pane it had just added.
    let isDragAdd = false;
    if (key === "__multi_add__") {
      // The dragover handler stashed the pane key in a private variable.
      key = _dragAddKey;
      _dragAddKey = null;
      if (!key) return;
      isDragAdd = true;
      visiblePanes.add(key);
    }

    // ── Close button: remove a specific pane from the comparison.
    if (key === "__close__") {
      // _closeKey is set by the close button click handler below.
      const k = _closeKey;
      _closeKey = null;
      if (!k) return;
      visiblePanes.delete(k);
      if (visiblePanes.size <= 1) {
        // Collapse back to single-pane mode.
        const last = [...visiblePanes][0] || "raw";
        visiblePanes.clear();
        comparePanes.classList.remove("compare-panes--multi");
        comparePanes.style.gridTemplateColumns = "";
        setPaneTab(last);
        return;
      }
    }

    // ── If the clicked/active tab is already in the visible set, remove it
    //    (this lets users click a visible tab's button to close it). Never
    //    true for a fresh drag-add — see the isDragAdd note above.
    if (!isDragAdd && visiblePanes.has(key) && visiblePanes.size > 1) {
      visiblePanes.delete(key);
      if (visiblePanes.size === 1) {
        // Back to single-pane.
        comparePanes.classList.remove("compare-panes--multi");
        comparePanes.style.gridTemplateColumns = "";
        const remaining = [...visiblePanes][0];
        visiblePanes.clear();
        setPaneTab(remaining);
        return;
      }
    }

    // ── Normal tab activation. A plain click while zero or exactly one pane
    // is showing REPLACES the comparison (ordinary tab-switch behaviour) —
    // it must not silently accumulate into a 2-pane comparison the user
    // never asked for. Only an explicit drag-add, or a plain click while
    // already comparing 2+ panes, extends the set instead of replacing it.
    if (isDragAdd) {
      // Already added above; nothing further to do here.
    } else if (visiblePanes.size <= 1) {
      visiblePanes.clear();
      visiblePanes.add(key);
    } else {
      // Already comparing 2+ panes and the user clicked a different,
      // not-yet-shown tab directly: extend the comparison rather than
      // collapsing it back to one pane.
      visiblePanes.add(key);
    }

    // ── Apply the visible state to tabs and panes.
    const isMulti = visiblePanes.size > 1;
    tabs.forEach(btn => {
      const paneKey = btn.dataset.pane;
      const isActive = paneKey === key;
      const isVisible = visiblePanes.has(paneKey);
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-pressed", isActive ? "true" : "false");
      // Add a subtle "in-comparison" ring to tabs that are open but not active.
      btn.classList.toggle("pane-stage-btn--visible", isVisible && !isActive);
    });

    panes.forEach(pane => {
      const paneKey = pane.dataset.pane;
      const isVisible = visiblePanes.has(paneKey);
      // In multi-pane mode the pane must be an explicit "flex" (matching
      // .compare-panes--multi .compare-pane's flex layout), not the bare ""
      // (browser default "block") single-pane mode uses — otherwise a
      // hidden pane's own inline display:none was being beaten by that
      // same CSS rule's !important, showing every pane regardless of
      // visiblePanes. Setting the shown value explicitly here means the
      // CSS rule no longer needs !important to force flex at all.
      pane.style.display = isVisible ? (isMulti ? "flex" : "") : "none";
    });

    // ── Toggle multi-pane CSS class and grid columns.
    _multiPaneMode = isMulti;
    if (isMulti) {
      comparePanes.classList.add("compare-panes--multi");
      comparePanes.style.gridTemplateColumns = "repeat(" + visiblePanes.size + ", 1fr)";
    } else {
      comparePanes.classList.remove("compare-panes--multi");
      comparePanes.style.gridTemplateColumns = "";
    }
  }

  // Private temp variables used by drag-to-compare and close-pane handlers.
  let _dragAddKey = null;
  let _closeKey = null;

  // Tracks whether multi-pane comparison mode is active, so it can be restored
  // when switching back from "Regions" review mode.
  let _multiPaneMode = false;

  // Wire pane-stage tab buttons.
  container.querySelectorAll(".pane-stage-btn").forEach(btn => {
    btn.setAttribute("aria-pressed", btn.classList.contains("active") ? "true" : "false");
    btn.addEventListener("click", () => setPaneTab(btn.dataset.pane));

    // Drag-to-compare: drag a tab into the pane area to open it alongside the
    // currently-visible pane(s).  Uses HTML5 drag-and-drop (same pattern as
    // the queue row reorder in app.js).
    btn.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", btn.dataset.pane);
      e.dataTransfer.effectAllowed = "copy";
      btn.classList.add("dragging");
    });
    btn.addEventListener("dragend", () => {
      btn.classList.remove("dragging");
    });
  });

  // Drag-over and drop on the .compare-panes region to activate drag-to-compare.
  const comparePanes = container.querySelector(".compare-panes");
  if (comparePanes) {
    comparePanes.addEventListener("dragover", (e) => {
      // Only accept drags from .pane-stage-btn (not the image or other things).
      if (!e.dataTransfer.types.includes("text/plain")) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      comparePanes.classList.add("drag-over");
      // Stash the dragged pane key so setPaneTab("__multi_add__") can use it.
      _dragAddKey = e.dataTransfer.getData("text/plain") || null;
    });
    comparePanes.addEventListener("dragleave", (e) => {
      // Remove drag-over only when leaving the pane area entirely (not a child).
      if (!comparePanes.contains(e.relatedTarget)) {
        comparePanes.classList.remove("drag-over");
        _dragAddKey = null;
      }
    });
    comparePanes.addEventListener("drop", (e) => {
      e.preventDefault();
      comparePanes.classList.remove("drag-over");
      const paneKey = e.dataTransfer.getData("text/plain");
      if (!paneKey) return;
      // Signal setPaneTab to add this pane to the comparison.
      _dragAddKey = paneKey;
      setPaneTab("__multi_add__");
    });
  }

  // Close button in each pane header: removes that pane from the comparison.
  container.querySelectorAll(".btn-close-pane").forEach(btn => {
    btn.addEventListener("click", (e) => {
      const pane = btn.closest(".compare-pane");
      if (!pane) return;
      _closeKey = pane.dataset.pane;
      setPaneTab("__close__");
    });
  });

  // ---------------------------------------------------- review mode switching

  // "Whole page" vs "Regions" mode tabs in the compare-bar.
  // Hidden when the open item has no regions (falls back to whole-page only).
  function setReviewMode(mode) {
    const comparePanes = container.querySelector(".compare-panes");
    const regionListEl = document.getElementById("preview-region-list");
    const reviewModeTabs = document.getElementById("preview-review-mode");

    reviewModeTabs?.querySelectorAll(".review-mode-btn").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.mode === mode);
    });

    if (mode === "whole") {
      if (comparePanes) {
        comparePanes.style.display = "";
        // Restore multi-pane state if it was active before entering regions mode.
        if (_multiPaneMode) {
          comparePanes.classList.add("compare-panes--multi");
          comparePanes.style.gridTemplateColumns = "repeat(" + visiblePanes.size + ", 1fr)";
        }
      }
      if (regionListEl) regionListEl.style.display = "none";
    } else if (mode === "regions") {
      if (comparePanes) {
        comparePanes.style.display = "none";
        // Multi-pane state is preserved in _multiPaneMode; no need to clear here.
      }
      if (regionListEl) regionListEl.style.display = "";
      if (window.RegionReview && currentItemId) {
        window.RegionReview.render(document.getElementById("preview-region-cards"), "raw");
      }
    }
  }

  // Wire mode tab buttons.
  document.querySelectorAll(".review-mode-btn").forEach(btn => {
    btn.addEventListener("click", () => setReviewMode(btn.dataset.mode));
  });

  // Refresh button in region list toolbar.
  document.getElementById("btn-region-refresh")?.addEventListener("click", async () => {
    if (currentItemId && window.RegionReview) {
      await window.RegionReview.reload(currentItemId);
    }
  });

  return { open, hasUnsavedEdits, setReviewMode };
})();

window.PreviewTab = PreviewTab;
