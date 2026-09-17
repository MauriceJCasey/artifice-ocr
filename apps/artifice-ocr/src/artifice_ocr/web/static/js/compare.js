// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

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
