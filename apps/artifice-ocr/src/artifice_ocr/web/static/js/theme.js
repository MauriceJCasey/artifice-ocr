// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

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
