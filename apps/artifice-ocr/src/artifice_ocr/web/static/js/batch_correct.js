// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

const BatchCorrect = (function () {
  const modal = document.getElementById("modal-batch-correct");
  const findInput = document.getElementById("batch-find");
  const replaceInput = document.getElementById("batch-replace");
  const stageRaw = document.getElementById("batch-stage-raw");
  const stageCleaned = document.getElementById("batch-stage-cleaned");
  const stageTranslated = document.getElementById("batch-stage-translated");
  const applySelected = document.getElementById("batch-apply-selected");
  const statusEl = document.getElementById("batch-status");
  const btnApply = document.getElementById("btn-batch-apply");
  const btnClose = document.getElementById("btn-batch-cancel");
  let returnFocus = null;

  function open() {
    if (!modal) return;
    returnFocus = document.activeElement;
    modal.classList.remove("hidden");
    statusEl.textContent = "";
    focusFirstIn(modal);
  }

  function close() {
    if (modal) modal.classList.add("hidden");
    returnFocus?.focus?.();
  }

  async function apply() {
    const find = findInput.value.trim();
    const replace = replaceInput.value;
    if (!find) { statusEl.textContent = "Enter text to find."; return; }
    const stages = [];
    if (stageRaw.checked) stages.push("raw");
    if (stageCleaned.checked) stages.push("cleaned");
    if (stageTranslated.checked) stages.push("translated");
    if (!stages.length) { statusEl.textContent = "Select at least one stage."; return; }

    btnApply.disabled = true;
    const label = btnApply.textContent;
    btnApply.textContent = "Applying\u2026";
    statusEl.textContent = "";

    try {
      const body = { find, replace, stages };
      if (applySelected.checked) body.item_ids = [...selected];
      const result = await api("POST", "/api/queue/batch-replace", body);
      setQueue(result.items);
      statusEl.textContent = `Applied to ${result.updated} text(s) across ${result.items.length} item(s).`;
      log(`Batch correct applied: "${find}" -> "${replace}" (${result.updated} change(s))`, "accent");
    } catch (err) {
      statusEl.textContent = `Error: ${friendlyError(err)}`;
      log(`Batch correct failed: ${friendlyError(err)}`, "error");
    } finally {
      btnApply.textContent = label;
      btnApply.disabled = false;
    }
  }

  btnApply.addEventListener("click", apply);
  btnClose.addEventListener("click", close);
  modal?.querySelector("[data-modal-close]")?.addEventListener("click", close);
  // Close on backdrop click
  modal?.addEventListener("click", (e) => { if (e.target === modal) close(); });

  document.getElementById("btn-batch-correct")?.addEventListener("click", open);
  registerModalCloser("modal-batch-correct", close);

  return { open, close };
})();

window.BatchCorrect = BatchCorrect;
