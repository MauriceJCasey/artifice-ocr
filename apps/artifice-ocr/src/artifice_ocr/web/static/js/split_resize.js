// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

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
