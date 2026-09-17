// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// -------------------------------------------------------- column visibility

const ColVis = (function () {
  const STORAGE_KEY = "ocr_col_vis";
  let menuEl = null;
  const COLS = [
    { key: "ocr", label: "OCR", default: true },
    { key: "cleanup", label: "Cleanup", default: true },
    { key: "translate", label: "Translate", default: true },
    { key: "confidence", label: "Conf", default: true },
    { key: "elapsed", label: "Time", default: true },
  ];

  function loadState() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; } catch { return {}; }
  }

  function saveState(state) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  }

  function isVisible(key) {
    const state = loadState();
    if (key in state) return state[key];
    return COLS.find(c => c.key === key)?.default ?? true;
  }

  function toggle(key) {
    const state = loadState();
    state[key] = !isVisible(key);
    saveState(state);
    apply();
  }

  function apply() {
    const header = els["queue-body"]?.closest("table")?.querySelector("thead tr");
    const rows = els["queue-body"]?.querySelectorAll("tr");
    if (!header) return;
    const ths = header.querySelectorAll("th");
    // th indices: 0=checkbox, 1=File, 2=OCR, 3=Cleanup, 4=Translate, 5=Conf, 6=Time, 7=Status, 8=view
    const colMap = { ocr: 2, cleanup: 3, translate: 4, confidence: 5, elapsed: 6 };
    for (const [key, idx] of Object.entries(colMap)) {
      const vis = isVisible(key);
      if (ths[idx]) ths[idx].style.display = vis ? "" : "none";
      rows?.forEach(tr => {
        const td = tr.children[idx];
        if (td) td.style.display = vis ? "" : "none";
      });
    }
  }

  function showMenu(e) {
    if (!menuEl) {
      menuEl = document.createElement("div");
      menuEl.className = "col-vis-menu hidden";
      menuEl.innerHTML = COLS.map(c => `
        <div class="col-vis-item" data-col="${c.key}">
          <input type="checkbox" ${isVisible(c.key) ? "checked" : ""}>
          <span>${c.label}</span>
        </div>`).join("");
      document.body.appendChild(menuEl);
      menuEl.querySelectorAll(".col-vis-item").forEach(el => {
        el.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const cb = el.querySelector("input");
          cb.checked = !cb.checked;
          toggle(el.dataset.col);
        });
      });
    }
    menuEl.classList.toggle("hidden");
    if (!menuEl.classList.contains("hidden")) {
      const rect = e.target.getBoundingClientRect();
      menuEl.style.top = `${rect.bottom + 4}px`;
      menuEl.style.left = `${rect.left}px`;
      // Sync checkboxes
      menuEl.querySelectorAll(".col-vis-item").forEach(el => {
        el.querySelector("input").checked = isVisible(el.dataset.col);
      });
      const close = (ev) => {
        if (!menuEl.contains(ev.target)) {
          menuEl.classList.add("hidden");
          document.removeEventListener("click", close);
        }
      };
      setTimeout(() => document.addEventListener("click", close), 0);
    }
  }

  apply();
  return { showMenu, apply };
})();

window.ColVis = ColVis;
