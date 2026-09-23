// SPDX-FileCopyrightText: 2026 Maurice Casey
// SPDX-License-Identifier: AGPL-3.0-or-later

// Review text is a local presentation preference, not a processing setting.
(function () {
  "use strict";
  const key = "artifice.ocr.review-text-size";
  const allowed = new Set(["16px", "18px", "20px", "24px"]);
  let value = "18px";
  try {
    const stored = localStorage.getItem(key);
    if (allowed.has(stored)) value = stored;
  } catch (_) {}

  function controls() {
    const found = Array.from(document.querySelectorAll("[data-reading-size]"));
    ["preview-reading-size", "history-reading-size"].forEach((id) => {
      const control = document.getElementById(id);
      if (control && !found.includes(control)) found.push(control);
    });
    return found;
  }

  function apply(next) {
    if (!allowed.has(next)) return;
    value = next;
    document.documentElement.style.setProperty("--review-text-size", value);
    controls().forEach((select) => {
      select.value = value;
    });
    try { localStorage.setItem(key, value); } catch (_) {}
  }

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-reading-size]")) apply(event.target.value);
  });
  apply(value);
})();
