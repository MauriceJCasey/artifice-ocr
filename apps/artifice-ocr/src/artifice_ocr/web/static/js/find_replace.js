// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * Find & Replace widget for editable text panes.
 *
 * Attaches to a container element. When Ctrl+F is pressed while a textarea
 * inside the container is focused, a find bar slides open at the top of the
 * pane. Supports find-next / find-prev / replace-all.
 *
 * Usage:
 *   const fr = new FindReplace(containerEl);
 *   fr.attach();   // listen for Ctrl+F on textareas
 *   fr.detach();   // clean up listeners
 */

class FindReplace {
  constructor(container) {
    this.container = container;
    this.activePane = null;
    this._keyHandler = this._onKeyDown.bind(this);
    this._bars = new Map(); // paneKey -> { bar, input, replaceInput, ... }
  }

  attach() {
    document.addEventListener("keydown", this._keyHandler);
  }

  detach() {
    document.removeEventListener("keydown", this._keyHandler);
    this._bars.forEach((b) => b.bar.remove());
    this._bars.clear();
  }

  _onKeyDown(e) {
    if ((e.key === "f" || e.key === "F") && (e.ctrlKey || e.metaKey)) {
      // Find which textarea is focused, if any
      const textarea = e.target.closest
        ? e.target.closest("textarea.raw-edit")
        : null;
      if (!textarea) return;
      const pane = textarea.closest(".compare-pane");
      if (!pane) return;
      e.preventDefault();
      const key = pane.dataset.pane;
      if (!key) return;
      this._ensureBar(key, textarea);
      this._toggleBar(key);
    }
  }

  _ensureBar(key, textarea) {
    if (this._bars.has(key)) return;
    const pane = this.container.querySelector(`.compare-pane[data-pane="${key}"]`);
    if (!pane) return;

    const bar = document.createElement("div");
    bar.className = "find-bar hidden";
    bar.innerHTML = `
      <input type="text" class="find-input" placeholder="Find\u2026" spellcheck="false">
      <span class="find-count dim"></span>
      <button class="btn find-prev" type="button" title="Previous (\u21e7+F3)">&uarr;</button>
      <button class="btn find-next" type="button" title="Next (F3)">&darr;</button>
      <button class="btn find-replace-toggle" type="button" title="Replace">R</button>
      <div class="find-replace-row hidden">
        <input type="text" class="find-replace-input" placeholder="Replace with\u2026" spellcheck="false">
        <button class="btn find-replace-all" type="button">Replace All</button>
      </div>
      <button class="btn find-close" type="button" title="Close (Esc)">&times;</button>
    `;

    // Insert bar at the top of the pane content (after the header)
    const textWrap = pane.querySelector(".compare-text");
    if (textWrap) pane.insertBefore(bar, textWrap);

    const input = bar.querySelector(".find-input");
    const count = bar.querySelector(".find-count");
    const prevBtn = bar.querySelector(".find-prev");
    const nextBtn = bar.querySelector(".find-next");
    const replaceToggle = bar.querySelector(".find-replace-toggle");
    const replaceRow = bar.querySelector(".find-replace-row");
    const replaceInput = bar.querySelector(".find-replace-input");
    const replaceAllBtn = bar.querySelector(".find-replace-all");
    const closeBtn = bar.querySelector(".find-close");

    let currentMatch = -1;
    let matches = [];

    function getText() { return textarea.value; }

    function findMatches(query) {
      if (!query) { matches = []; currentMatch = -1; count.textContent = ""; return; }
      const text = getText();
      const indices = [];
      let idx = 0;
      while (true) {
        const pos = text.indexOf(query, idx);
        if (pos === -1) break;
        indices.push(pos);
        idx = pos + query.length;
      }
      matches = indices;
      currentMatch = matches.length > 0 ? 0 : -1;
      updateCount();
      selectCurrent();
    }

    function selectCurrent() {
      if (currentMatch < 0 || currentMatch >= matches.length) {
        textarea.setSelectionRange(0, 0);
        return;
      }
      const start = matches[currentMatch];
      const q = input.value;
      textarea.focus();
      textarea.setSelectionRange(start, start + q.length);
      textarea.scrollTop = textarea.scrollTop; // keep scroll stable
    }

    function updateCount() {
      count.textContent = matches.length
        ? `${currentMatch + 1}/${matches.length}` : "0/0";
    }

    input.addEventListener("input", () => {
      currentMatch = -1;
      findMatches(input.value);
    });

    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        if (ev.shiftKey) {
          navigate(-1);
        } else {
          navigate(1);
        }
      }
      if (ev.key === "Escape") {
        hide();
      }
    });

    function navigate(dir) {
      if (matches.length === 0) return;
      currentMatch = (currentMatch + dir + matches.length) % matches.length;
      updateCount();
      selectCurrent();
    }

    prevBtn.addEventListener("click", () => navigate(-1));
    nextBtn.addEventListener("click", () => navigate(1));

    replaceToggle.addEventListener("click", () => {
      replaceRow.classList.toggle("hidden");
    });

    replaceAllBtn.addEventListener("click", () => {
      const find = input.value;
      const replace = replaceInput.value;
      if (!find) return;
      const oldText = getText();
      const newText = oldText.split(find).join(replace);
      if (newText !== oldText) {
        textarea.value = newText;
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
        // Re-run find
        matches = [];
        currentMatch = -1;
        findMatches(input.value);
      }
    });

    closeBtn.addEventListener("click", hide);

    function hide() {
      bar.classList.add("hidden");
      replaceRow.classList.add("hidden");
      textarea.focus();
    }

    // Esc while bar is shown closes it
    bar.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") hide();
    });

    this._bars.set(key, { bar, input, replaceInput, replaceRow, textarea });
  }

  _toggleBar(key) {
    const entry = this._bars.get(key);
    if (!entry) return;
    const hidden = entry.bar.classList.contains("hidden");
    // Hide all other bars first
    this._bars.forEach((b) => b.bar.classList.add("hidden"));
    if (hidden) {
      entry.bar.classList.remove("hidden");
      entry.input.focus();
      entry.input.select();
      // Auto-find any selected text
      const selected = entry.textarea.value.substring(
        entry.textarea.selectionStart, entry.textarea.selectionEnd
      );
      if (selected) {
        entry.input.value = selected;
        entry.input.dispatchEvent(new Event("input"));
      }
    } else {
      entry.textarea.focus();
    }
  }

  // External: open find bar for a specific pane key
  open(key) {
    const textarea = this.container.querySelector(
      `.compare-pane[data-pane="${key}"] textarea.raw-edit`
    );
    if (!textarea) return;
    this._ensureBar(key, textarea);
    this._toggleBar(key);
  }

  // External: close all find bars
  closeAll() {
    this._bars.forEach((b) => {
      b.bar.classList.add("hidden");
      b.replaceRow.classList.add("hidden");
    });
  }
}