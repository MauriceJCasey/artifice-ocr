// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// ---------------------------------------------------- keyboard shortcuts

// Shared by every modal in the app: what Tab should stop on. Used here for
// the Tab-trap and by each modal's own open() (via focusFirstIn) to move
// focus in when it opens. One definition so all six modals agree.
const MODAL_FOCUSABLE = 'button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])';

function focusFirstIn(modalEl) {
  const target = modalEl.querySelector(MODAL_FOCUSABLE);
  if (target) requestAnimationFrame(() => target.focus());
}

// A modal registers its own close() here (keyed by the modal element's id) so
// the generic Escape handler below can call it instead of just hiding the
// backdrop directly. close() is where each modal's real cleanup lives: focus
// restore, in some cases cancelling in-flight requests (Compile PDF's SSE
// connection). A modal that never registers (the two Tropy modals, which
// already close themselves correctly via their own capture-phase Escape
// handler in tropy.js) falls back to a plain hide, which is a harmless no-op
// if that modal is already closed by the time this handler runs.
const MODAL_CLOSERS = {};
function registerModalCloser(modalId, closeFn) {
  MODAL_CLOSERS[modalId] = closeFn;
}

document.addEventListener("keydown", (e) => {
  const tag = e.target.tagName;
  const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

  // Escape closes modals — via each modal's own close() when registered, so
  // its cleanup (focus restore, cancelling in-flight requests) actually runs.
  if (e.key === "Escape") {
    document.querySelectorAll(".modal-backdrop:not(.hidden)").forEach(m => {
      (MODAL_CLOSERS[m.id] || (() => m.classList.add("hidden")))();
    });
  }

  // Tab cycles within the open modal instead of escaping to the page behind it
  if (e.key === "Tab") {
    const openModal = document.querySelector(".modal-backdrop:not(.hidden)");
    if (openModal) {
      const focusable = [...openModal.querySelectorAll(MODAL_FOCUSABLE)];
      if (focusable.length) {
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
  }

  // Ctrl+Enter / Cmd+Enter runs pipeline
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    if (!els["btn-run"].disabled) els["btn-run"].click();
  }

  // Delete removes selected items (when not in a text input)
  if (e.key === "Delete" && !inInput) {
    if (selected.size && !running) els["btn-remove"].click();
  }

  // 1-5 switches tabs (when not in an input)
  if (!inInput && !e.ctrlKey && !e.metaKey) {
    const num = parseInt(e.key, 10);
    if (num >= 1 && num <= 5) {
      const tabs = document.querySelectorAll(".tab[data-tab]");
      if (tabs[num - 1]) tabs[num - 1].click();
    }
  }
});
