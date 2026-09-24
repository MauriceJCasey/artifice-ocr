// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * Region review: per-region crop + editable text + reorder + delete.
 *
 * Each region card shows:
 *   - its padded crop (drawn from source image + polygon via canvas clip)
 *   - a stage selector (raw / cleaned / translated)
 *   - the selected stage's text in an editable textarea
 *   - diffs between raw↔cleaned and cleaned↔translated (using the same
 *     highlightRanges() approach as the whole-page review in app.js)
 *   - an inline error flag if the region has an error field
 *   - up/down reorder buttons → POST .../regions/reorder with the full new
 *     region_ids list, then re-fetch from the server
 *   - a delete button → DELETE .../region/{id}, then re-fetch
 *
 * A correction (retype) fires on blur with a short debounce to avoid a
 * request on every keystroke — it saves the currently selected stage only,
 * never touching another stage or another region.
 *
 * Architecture note: this module is imported by preview.js which owns the
 * DOM and the source image. It does NOT load its own copy of the image —
 * callers pass the Image element they already have (PreviewImage.imgEl).
 */

/* ---------------------------------------------------------------------- crop */

// Draw the polygon region from `imgEl` onto `canvas`.
// `polygon` is [[x, y], ...] in original image pixel space.
// `padding` is a fraction (0–1) of the bounding box to add around the crop.
function drawCrop(canvas, imgEl, polygon, padding) {
  padding = padding || 0.08;

  if (!polygon || !polygon.length || !imgEl || !imgEl.naturalWidth) return;

  const ctx = canvas.getContext("2d");
  const nw = imgEl.naturalWidth;
  const nh = imgEl.naturalHeight;

  // Bounding box of the polygon in image pixel space.
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const [x, y] of polygon) {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }

  // Add padding around the crop.
  const pw = maxX - minX;
  const ph = maxY - minY;
  const padX = pw * padding;
  const padY = ph * padding;

  // Clamp to image bounds.
  const sx = Math.max(0, Math.floor(minX - padX));
  const sy = Math.max(0, Math.floor(minY - padY));
  const sw = Math.min(nw, Math.ceil(pw + padX * 2));
  const sh = Math.min(nh, Math.ceil(ph + padY * 2));

  canvas.width = sw;
  canvas.height = sh;

  ctx.clearRect(0, 0, sw, sh);

  // Build a Path2D from the polygon, translated so minX/minY is the new origin.
  const path = new Path2D();
  const [fx, fy] = polygon[0];
  path.moveTo(fx - sx, fy - sy);
  for (let i = 1; i < polygon.length; i++) {
    const [px, py] = polygon[i];
    path.lineTo(px - sx, py - sy);
  }
  path.closePath();

  ctx.save();
  ctx.clip(path);
  // Draw the image region that covers the canvas, offset so the right
  // portion of the image lines up with the canvas origin.
  ctx.drawImage(imgEl, -sx, -sy);
  ctx.restore();
}

/* ------------------------------------------------------------------ stage helpers */

const STAGES = ["raw", "cleaned", "translated"];

function stageLabel(s) {
  return { raw: "Raw OCR", cleaned: "Cleaned", translated: "Translated" }[s] || s;
}

// Text for a region at a given stage — null means "not run".
function regionStageText(region, stage) {
  if (stage === "raw") return region.raw;
  if (stage === "cleaned") return region.cleaned;
  if (stage === "translated") return region.translated;
  return null;
}

/* ------------------------------------------------------------------ render one card */

function renderRegionCard(region, allRegionIds, itemId, selectedStage) {
  const idx = allRegionIds.indexOf(region.id);
  const isFirst = idx === 0;
  const isLast = idx === allRegionIds.length - 1;
  const hasError = Boolean(region.error);
  const stageText = regionStageText(region, selectedStage);
  const placeholder = "(not run)";

  const card = document.createElement("div");
  card.className = "region-card" + (hasError ? " region-card--error" : "");
  card.dataset.regionId = region.id;

  // Build the diff HTML using the shared highlightRanges from app.js.
  // Note: diff ranges are per-item (raw_ranges, cleaned_ranges, translated_ranges in
  // the item-level preview payload), not per-region — the server has no
  // per-region diff-range endpoint yet, so this renders the plain escaped
  // text of each stage rather than a highlighted diff (an empty ranges
  // array makes highlightRanges fall back to plain escapeHtml). A future
  // commit that adds per-region diff ranges only needs to change what's
  // passed as the second argument here, not this card's structure.
  // highlightRanges safely handles empty ranges (returns escapeHtml of the text).
  const hl = window.highlightRanges || function(t) {
    const d = document.createElement("div");
    d.textContent = t || "";
    return d.innerHTML;
  };
  const cleanedDiffHtml = hl(region.cleaned || "", []);
  const rawDiffHtml = hl(region.raw || "", []);
  const translatedDiffHtml = hl(region.translated || "", []);

  card.innerHTML = `
    <div class="region-card-header">
      <span class="region-card-number">${idx + 1}</span>
      <span class="region-card-type">${escapeHtml(region.type || "text")}</span>
      ${region.confidence != null ? `<span class="region-card-conf" title="Region confidence">${Math.round(region.confidence)}%</span>` : ""}
      ${hasError ? `<span class="region-error-flag" title="${escapeHtml(region.error)}">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        ${escapeHtml(region.error)}
      </span>` : ""}
      <span class="spacer"></span>
      <button type="button" class="btn btn-small region-up" ${isFirst ? "disabled" : ""} title="Move up"
              data-action="up" aria-label="Move region up">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="18 15 12 9 6 15"/></svg>
      </button>
      <button type="button" class="btn btn-small region-down" ${isLast ? "disabled" : ""} title="Move down"
              data-action="down" aria-label="Move region down">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>
      </button>
      <button type="button" class="btn btn-small danger region-delete" title="Delete region"
              data-action="delete" aria-label="Delete region">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>
      </button>
    </div>
    <div class="region-card-body">
      <div class="region-crop-wrap">
        <canvas class="region-crop-canvas" aria-label="Region crop" data-region-id="${region.id}"></canvas>
      </div>
      <div class="region-text-col">
        <div class="region-stage-tabs" role="group" aria-label="Select text stage">
          ${STAGES.map(s => `
            <button type="button" class="btn btn-small region-stage-btn ${s === selectedStage ? "active" : ""}"
                    data-stage="${s}">${stageLabel(s)}</button>
          `).join("")}
        </div>
        <textarea class="region-textarea" placeholder="${placeholder}"
                  data-region-id="${region.id}" data-stage="${selectedStage}"
                  ${stageText == null ? "readonly" : ""}>${stageText != null ? escapeHtml(stageText) : ""}</textarea>
        ${selectedStage === "raw" && region.raw != null && region.cleaned != null ? `
          <div class="region-diff-label dim">Raw → Cleaned diff</div>
          <div class="region-diff-view">${cleanedDiffHtml}</div>` : ""}
        ${selectedStage === "cleaned" && region.cleaned != null && region.raw != null ? `
          <div class="region-diff-label dim">Previous: Raw</div>
          <div class="region-diff-view">${rawDiffHtml}</div>
          ${region.translated != null ? `<div class="region-diff-label dim">Next: Translated</div><div class="region-diff-view">${translatedDiffHtml}</div>` : ""}` : ""}
        ${selectedStage === "translated" && region.cleaned != null && region.translated != null ? `
          <div class="region-diff-label dim">Cleaned → Translated diff</div>
          <div class="region-diff-view">${translatedDiffHtml}</div>` : ""}
        <div class="region-card-actions">
          <button type="button" class="btn btn-small region-save" disabled
                  data-region-id="${region.id}">Save correction</button>
        </div>
      </div>
    </div>
    <!-- Polygon editing seam: future "drag corner handles" would attach here.
         v1 shows crops only; no editable geometry. -->
  `;

  return card;
}

/* ------------------------------------------------------------------ render list */

function renderRegionList(regions, allRegionIds, itemId, selectedStage, container) {
  if (!container) return;
  container.innerHTML = "";

  if (!regions || !regions.length) {
    container.innerHTML = '<p class="dim" style="padding:1rem;">No regions found for this item.</p>';
    return;
  }

  const countLabel = document.getElementById("region-count-label");
  if (countLabel) countLabel.textContent = `${regions.length} region${regions.length !== 1 ? "s" : ""} in reading order`;

  for (const region of regions) {
    const card = renderRegionCard(region, allRegionIds, itemId, selectedStage);
    container.appendChild(card);
  }

  // Wire crop drawing after cards are in the DOM.
  wireCropDrawing(container, regions);
  // Wire all card interactions.
  wireRegionCards(container, regions, allRegionIds, itemId);
}

/* ------------------------------------------------------------------ wire crops */

function wireCropDrawing(container, regions) {
  // The source image is loaded in PreviewImage. We need a reference to the
  // img element to draw crops from. PreviewImage is exposed globally.
  const imgEl = document.getElementById("preview-image");
  if (!imgEl || !imgEl.complete || !imgEl.naturalWidth) {
    // Image not yet loaded — defer until it is.
    if (imgEl) {
      imgEl.addEventListener("load", () => wireCropDrawing(container, regions));
    }
    return;
  }

  for (const region of regions) {
    const card = container.querySelector(`[data-region-id="${region.id}"]`);
    const c = card ? card.querySelector(".region-crop-canvas") : null;
    if (c && region.polygon) {
      drawCrop(c, imgEl, region.polygon, 0.08);
    }
  }
}

/* ------------------------------------------------------------------ wire card interactions */

function wireRegionCards(container, regions, allRegionIds, itemId) {
  // ---- stage switching ----
  container.querySelectorAll(".region-stage-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".region-card");
      if (!card) return;
      const regionId = card.dataset.regionId;
      const newStage = btn.dataset.stage;
      // Update active button state.
      card.querySelectorAll(".region-stage-btn").forEach(b => b.classList.toggle("active", b === btn));
      // Update textarea.
      const textarea = card.querySelector(".region-textarea");
      const region = regions.find(r => r.id === regionId);
      if (!region) return;
      const text = regionStageText(region, newStage);
      textarea.value = text != null ? text : "";
      textarea.dataset.stage = newStage;
      textarea.readOnly = text == null;
      textarea.placeholder = text == null ? "(not run)" : "";
      // Show/hide diffs.
      card.querySelectorAll(".region-diff-label, .region-diff-view").forEach(el => el.remove());
      const hl = window.highlightRanges || function(t) {
        const d = document.createElement("div");
        d.textContent = t || "";
        return d.innerHTML;
      };
      const addDiffs = (label, aHtml, bHtml, bText) => {
        if (!bText) return;
        const diffLabel = document.createElement("div");
        diffLabel.className = "region-diff-label dim";
        diffLabel.textContent = label;
        card.querySelector(".region-textarea").after(diffLabel);
        const diffView = document.createElement("div");
        diffView.className = "region-diff-view";
        diffView.innerHTML = aHtml;
        diffLabel.after(diffView);
      };
      if (newStage === "raw" && region.raw && region.cleaned) {
        const cleanedRanges = window._regionDiffRanges?.[region.id]?.cleaned_ranges || [];
        addDiffs("Cleaned", hl(region.cleaned || "", cleanedRanges), hl(region.raw || "", []), region.cleaned);
      } else if (newStage === "cleaned" && region.cleaned) {
        if (region.raw) {
          const rawRanges = window._regionDiffRanges?.[region.id]?.raw_ranges || [];
          addDiffs("Previous: Raw", hl(region.raw || "", rawRanges), null, region.raw);
        }
        if (region.translated) {
          const translatedRanges = window._regionDiffRanges?.[region.id]?.translated_ranges || [];
          addDiffs("Next: Translated", hl(region.translated || "", translatedRanges), null, region.translated);
        }
      } else if (newStage === "translated" && region.cleaned && region.translated) {
        const translatedRanges = window._regionDiffRanges?.[region.id]?.translated_ranges || [];
        addDiffs("Cleaned → Translated", hl(region.translated || "", translatedRanges), null, region.translated);
      }
      // Reset save button.
      const saveBtn = card.querySelector(".region-save");
      if (saveBtn) saveBtn.disabled = true;
    });
  });

  // ---- edit / save with debounce ----
  let saveTimers = {};

  container.querySelectorAll(".region-textarea").forEach(textarea => {
    const regionId = textarea.dataset.regionId;
    const card = textarea.closest(".region-card");

    function getSaveBtn() { return card ? card.querySelector(".region-save") : null; }

    textarea.addEventListener("input", () => {
      const saveBtn = getSaveBtn();
      if (!saveBtn) return;
      const region = regions.find(r => r.id === regionId);
      if (!region) return;
      const stage = textarea.dataset.stage;
      const originalText = regionStageText(region, stage);
      saveBtn.disabled = textarea.value === (originalText || "");
    });

    textarea.addEventListener("blur", () => {
      if (saveTimers[regionId]) clearTimeout(saveTimers[regionId]);
      saveTimers[regionId] = setTimeout(() => {
        const region = regions.find(r => r.id === regionId);
        if (!region) return;
        const stage = textarea.dataset.stage;
        const originalText = regionStageText(region, stage);
        const currentText = textarea.value;
        if (currentText === (originalText || "")) return;
        saveRegionText(itemId, regionId, stage, currentText).then(updated => {
          if (updated) {
            region[stage] = currentText;
            const saveBtn = getSaveBtn();
            if (saveBtn) saveBtn.disabled = true;
          }
        }).catch(err => {
          if (window.ArtificeToast) window.ArtificeToast.error("Save failed: " + err.message);
        });
      }, 600);
    });

    // Ctrl+S / Cmd+S to save immediately.
    textarea.addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        if (saveTimers[regionId]) clearTimeout(saveTimers[regionId]);
        const region = regions.find(r => r.id === regionId);
        if (!region) return;
        const stage = textarea.dataset.stage;
        saveRegionText(itemId, regionId, stage, textarea.value).then(updated => {
          if (updated) {
            region[stage] = textarea.value;
            const saveBtn = getSaveBtn();
            if (saveBtn) saveBtn.disabled = true;
          }
        }).catch(err => {
          if (window.ArtificeToast) window.ArtificeToast.error("Save failed: " + err.message);
        });
      }
    });
  });

  // ---- save button ----
  container.querySelectorAll(".region-save").forEach(btn => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".region-card");
      if (!card) return;
      const regionId = card.dataset.regionId;
      const textarea = card.querySelector(".region-textarea");
      if (!textarea) return;
      const stage = textarea.dataset.stage;
      const region = regions.find(r => r.id === regionId);
      if (!region) return;
      btn.disabled = true;
      btn.textContent = "Saving…";
      try {
        const updated = await saveRegionText(itemId, regionId, stage, textarea.value);
        if (updated) {
          region[stage] = textarea.value;
          btn.textContent = "Saved";
          setTimeout(() => { btn.textContent = "Save correction"; }, 1500);
        } else {
          btn.textContent = "Save correction";
        }
      } catch (err) {
        btn.textContent = "Save correction";
        if (window.ArtificeToast) window.ArtificeToast.error("Save failed: " + err.message);
      }
    });
  });

  // ---- reorder up/down ----
  container.querySelectorAll(".region-up, .region-down").forEach(btn => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".region-card");
      if (!card) return;
      const regionId = card.dataset.regionId;
      const direction = btn.dataset.action; // "up" or "down"
      const currentIdx = allRegionIds.indexOf(regionId);
      if (currentIdx === -1) return;
      const newIdx = direction === "up" ? currentIdx - 1 : currentIdx + 1;
      if (newIdx < 0 || newIdx >= allRegionIds.length) return;
      // Build new order.
      const newOrder = [...allRegionIds];
      newOrder.splice(currentIdx, 1);
      newOrder.splice(newIdx, 0, regionId);
      btn.disabled = true;
      try {
        await api("POST", `/api/queue/${itemId}/regions/reorder`, { region_ids: newOrder });
        // Refresh the whole region list from the server.
        if (window.RegionReview) {
          await window.RegionReview.reload(itemId);
        }
      } catch (err) {
        btn.disabled = false;
        if (window.ArtificeToast) window.ArtificeToast.error("Reorder failed: " + err.message);
      }
    });
  });

  // ---- delete ----
  container.querySelectorAll(".region-delete").forEach(btn => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".region-card");
      if (!card) return;
      const regionId = card.dataset.regionId;
      if (!confirm("Delete this region? This cannot be undone.")) return;
      btn.disabled = true;
      try {
        await api("DELETE", `/api/queue/${itemId}/region/${regionId}`);
        if (window.RegionReview) {
          await window.RegionReview.reload(itemId);
        }
      } catch (err) {
        btn.disabled = false;
        if (window.ArtificeToast) window.ArtificeToast.error("Delete failed: " + err.message);
      }
    });
  });
}

/* ------------------------------------------------------------------ API calls */

async function saveRegionText(itemId, regionId, stage, text) {
  const data = await api("POST", `/api/queue/${itemId}/region/${regionId}/text`, {
    stage, text,
  });
  return data.ok === true;
}

/* ------------------------------------------------------------------ public API */

const RegionReview = (function () {
  let _currentItemId = null;
  let _currentRegions = [];
  let _currentRegionIds = [];
  let _selectedStage = "raw";

  async function load(itemId) {
    _currentItemId = itemId;
    // An item with no PAGE document (no segmentation) comes back as an empty
    // list; any error that reaches here is a real one for the caller.
    const data = await api("GET", `/api/queue/${itemId}/regions`);
    _currentRegions = data.regions || [];
    _currentRegionIds = data.reading_order || _currentRegions.map(r => r.id);
    return {
      regions: _currentRegions,
      regionIds: _currentRegionIds,
      hasRegions: _currentRegions.length > 0,
    };
  }

  function render(container, selectedStage) {
    _selectedStage = selectedStage || "raw";
    renderRegionList(_currentRegions, _currentRegionIds, _currentItemId, _selectedStage, container);
  }

  async function reload(itemId) {
    const item = itemId || _currentItemId;
    if (!item) return;
    await load(item);
    const container = document.getElementById("preview-region-cards");
    render(container, _selectedStage);
  }

  // Redraw all crops after the source image has been loaded/loaded-anew.
  // Called from preview.js when switching to region mode after the image is ready.
  function redrawCrops() {
    if (!_currentRegions.length) return;
    const container = document.getElementById("preview-region-cards");
    if (!container) return;
    const imgEl = document.getElementById("preview-image");
    if (!imgEl || !imgEl.complete || !imgEl.naturalWidth) return;
    for (const region of _currentRegions) {
      const card = container.querySelector(`[data-region-id="${region.id}"]`);
      const c = card ? card.querySelector(".region-crop-canvas") : null;
      if (c && region.polygon) {
        drawCrop(c, imgEl, region.polygon, 0.08);
      }
    }
  }

  return { load, render, reload, redrawCrops, get currentRegions() { return _currentRegions; } };
})();

window.RegionReview = RegionReview;
window.drawCrop = drawCrop;
