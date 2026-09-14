// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * Command palette (Ctrl+K / Cmd+K) — search tabs, jump to actions, run
 * core workflow actions. Lightweight fuzzy-ish matcher over a static command list.
 */

const Palette = (function () {
  let backdrop = null;
  let inputEl = null;
  let resultsEl = null;
  let activeIndex = 0;
  let commands = [];

  function buildCommands() {
    commands = [
      // Tabs
      { label: "Main", icon: Icons.chevron, action: () => activateTab("main"), shortcut: "1" },
      { label: "Preview", icon: Icons.chevron, action: () => activateTab("preview"), shortcut: "2" },
      { label: "History", icon: Icons.chevron, action: () => activateTab("history"), shortcut: "3" },
      { label: "Settings", icon: Icons.chevron, action: () => activateTab("settings"), shortcut: "4" },
      // Queue actions
      { label: "Browse Files", icon: Icons.file, action: () => document.getElementById("btn-browse-files")?.click() },
      { label: "Add Folder", icon: Icons.folderPlus, action: () => document.getElementById("btn-add-folder")?.click() },
      { label: "Add from Tropy", icon: Icons.file, action: () => document.getElementById("btn-add-tropy")?.click() },
      { label: "Clear Queue", icon: Icons.trash, action: () => document.getElementById("btn-clear")?.click() },
      { label: "Remove Selected", icon: Icons.trash, action: () => document.getElementById("btn-remove")?.click() },
      { label: "Skip Selected", icon: Icons.skipForward, action: () => document.getElementById("btn-skip")?.click() },
      { label: "Retry Selected", icon: Icons.refreshCw, action: () => document.getElementById("btn-retry")?.click() },
      // Run actions
      { label: "Start Pipeline", icon: Icons.play, action: () => document.getElementById("btn-run")?.click(), shortcut: "Enter" },
      { label: "Pause / Resume", icon: Icons.pause, action: () => document.getElementById("btn-pause")?.click() },
      { label: "Stop Pipeline", icon: Icons.stop, action: () => document.getElementById("btn-stop")?.click() },
      // Compile / Export
      { label: "Compile PDF", icon: Icons.fileText, action: () => document.getElementById("btn-compile-pdf")?.click() },
      { label: "Send to Tropy", icon: Icons.file, action: () => document.getElementById("btn-send-tropy")?.click() },
      // Settings
      { label: "Save Settings", icon: Icons.save, action: () => document.getElementById("btn-settings-save")?.click() },
      { label: "Reset Settings", icon: Icons.refreshCcw, action: () => document.getElementById("btn-settings-reset")?.click() },
      { label: "Check Connections", icon: Icons.link, action: () => document.getElementById("btn-preflight")?.click() },
      // Toggle theme
      { label: "Toggle Dark Mode", icon: Icons.moon, action: () => window.ThemeToggle?.toggle(), shortcut: "D" },
    ];

  }

  function activateTab(tabName) {
    const tab = document.querySelector(`.tab[data-tab="${tabName}"]`);
    if (tab) tab.click();
  }

  function ensureBackdrop() {
    if (backdrop) return;
    backdrop = document.createElement("div");
    backdrop.className = "palette-backdrop hidden";

    const paletteEl = document.createElement("div");
    paletteEl.className = "palette";

    inputEl = document.createElement("input");
    inputEl.type = "text";
    inputEl.placeholder = "Type a command…";
    paletteEl.appendChild(inputEl);

    resultsEl = document.createElement("div");
    resultsEl.className = "palette-results";
    paletteEl.appendChild(resultsEl);

    backdrop.appendChild(paletteEl);
    document.body.appendChild(backdrop);

    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) close();
    });

    inputEl.addEventListener("input", () => renderResults(inputEl.value));
    inputEl.addEventListener("keydown", (e) => {
      const items = resultsEl.querySelectorAll(".palette-item");
      if (e.key === "Escape") { close(); return; }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        activeIndex = Math.min(activeIndex + 1, items.length - 1);
        updateActive(items);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        activeIndex = Math.max(activeIndex - 1, 0);
        updateActive(items);
      } else if (e.key === "Enter") {
        e.preventDefault();
        const active = items[activeIndex];
        if (active) active.click();
      }
    });
  }

  function updateActive(items) {
    items.forEach((item, i) => item.classList.toggle("active", i === activeIndex));
    const active = items[activeIndex];
    if (active) active.scrollIntoView({ block: "nearest" });
  }

  function renderResults(query) {
    const q = query.toLowerCase().trim();
    const matches = q
      ? commands.filter(c => c.label.toLowerCase().includes(q))
      : commands;

    activeIndex = 0;

    if (!matches.length) {
      resultsEl.innerHTML = `<div class="palette-empty">No matching commands</div>`;
      return;
    }

    resultsEl.innerHTML = matches.map((c, i) => `
      <div class="palette-item${i === 0 ? " active" : ""}" data-idx="${i}">
        <span class="palette-icon">${c.icon}</span>
        <span>${c.label}</span>
        ${c.shortcut ? `<span class="palette-shortcut">${c.shortcut}</span>` : ""}
      </div>`).join("");

    resultsEl.querySelectorAll(".palette-item").forEach((el, i) => {
      el.addEventListener("click", () => {
        close();
        matches[i].action();
      });
    });
  }

  function open() {
    buildCommands();
    ensureBackdrop();
    backdrop.classList.remove("hidden");
    inputEl.value = "";
    renderResults("");
    requestAnimationFrame(() => inputEl.focus());
  }

  function close() {
    if (backdrop) backdrop.classList.add("hidden");
  }

  function toggle() {
    if (backdrop && !backdrop.classList.contains("hidden")) close();
    else open();
  }

  // Global keyboard shortcut
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "k") {
      e.preventDefault();
      toggle();
    }
  });

  return { open, close, toggle };
})();

window.Palette = Palette;
