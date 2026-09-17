// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// ---------------------------------------------------- segmentation toggle

const SegmentationToggle = (function () {
  const toggle = document.getElementById("stage-segmentation");
  const controls = document.getElementById("segmentation-controls");
  const providerWrap = document.getElementById("seg-provider-wrap");
  const providerSelect = document.getElementById("seg-provider");
  const referenceWrap = document.getElementById("seg-reference-wrap");
  const referencePathInput = document.getElementById("seg-reference-path");
  const referenceBrowseBtn = document.getElementById("btn-seg-reference-browse");

  let cachedCapabilities = null;

  // Beginner-facing copy, keyed by provider name. This is a lookup, not
  // population logic: it never decides which providers exist (that stays
  // the API's job via populateProviders() below), and an unrecognized name
  // falls back to that provider's own `requirements` string so a future
  // fifth provider degrades gracefully instead of showing nothing.
  const HELP_COPY = {
    passthrough: "Whole page as one region. No setup needed, and it's the default.",
    "doclayout-yolo": "Finds text blocks, titles, and figures on a page automatically. Downloads a model on first use.",
    kraken: "Detects individual lines and reading order. Best for handwritten or complex historical pages, ideally black-and-white scans.",
    "diff-residual": "Finds hand-added marks by comparing against a separate clean reference scan. Not a general layout option.",
  };

  async function loadCapabilities() {
    if (cachedCapabilities) return cachedCapabilities;
    try {
      const data = await api("GET", "/api/segmentation/capabilities");
      cachedCapabilities = data.providers || [];
    } catch {
      cachedCapabilities = [{ name: "passthrough", requirements: "Built-in (always available)" }];
    }
    return cachedCapabilities;
  }

  function updateProviderHint() {
    const helpEl = document.getElementById("seg-provider-help");
    if (helpEl && providerSelect) {
      const name = providerSelect.value;
      if (!name) { helpEl.textContent = ""; }
      else if (HELP_COPY[name]) { helpEl.textContent = HELP_COPY[name]; }
      else {
        const match = (cachedCapabilities || []).find(p => p.name === name);
        helpEl.textContent = (match && match.requirements) || "";
      }
    }
    updateReferenceVisibility();
  }

  // diff-residual is the only provider that needs a second image (a clean
  // reference scan to diff against) — every other provider works from the
  // page alone, so the row stays hidden and irrelevant for them. Clearing
  // the stored path on hide (rather than just visually hiding it) means
  // switching away from diff-residual and back never resubmits a reference
  // picked for a *different* page.
  function updateReferenceVisibility() {
    if (!referenceWrap || !providerSelect) return;
    if (providerSelect.value === "diff-residual") {
      referenceWrap.style.display = "";
    } else {
      referenceWrap.style.display = "none";
      if (referencePathInput) referencePathInput.value = "";
    }
  }

  function getReferenceImage() {
    return referencePathInput ? referencePathInput.value : "";
  }

  async function onReferenceBrowse() {
    if (!referencePathInput) return;
    let res;
    try {
      res = await api("POST", "/api/native/pick-file", { preset: "images" });
    } catch {
      if (window.ArtificeToast) window.ArtificeToast.error("Could not reach the server to open the file picker.");
      return;
    }
    if (res.state === "selected" && res.paths && res.paths[0]) {
      referencePathInput.value = res.paths[0];
    } else if (res.state === "unavailable") {
      if (window.ArtificeToast) window.ArtificeToast.show(res.reason || "File picker unavailable", "warning");
      const raw = prompt("Enter the full path to the reference scan:");
      if (raw) referencePathInput.value = raw;
    }
    // "cancelled" — user closed the dialog on purpose, leave the field as-is.
  }

  function populateProviders(providers) {
    if (!providerSelect) return;
    providerSelect.innerHTML = "";
    if (!providers || !providers.length) {
      providerSelect.innerHTML = '<option value="">No providers available</option>';
      updateProviderHint();
      return;
    }
    for (const p of providers) {
      const opt = document.createElement("option");
      opt.value = p.name;
      // The brief: never hard-code a provider name, populate from the API only.
      opt.textContent = p.name + (p.requirements ? ` (${p.requirements})` : "");
      providerSelect.appendChild(opt);
    }
    // Select passthrough by default if it appears.
    const passthrough = providers.find(p => p.name === "passthrough");
    if (passthrough) providerSelect.value = "passthrough";
    updateProviderHint();
  }

  async function onToggle() {
    if (!toggle || !controls) return;
    const enabled = toggle.checked;
    // `controls` is the row containing this very checkbox — it must stay
    // visible regardless of checked state, or unchecking it would hide the
    // only control that can re-check it. Only the provider sub-controls are
    // conditional on the checkbox.
    if (enabled) {
      // Lazily load capabilities on first open.
      if (!cachedCapabilities) {
        const providers = await loadCapabilities();
        populateProviders(providers);
      }
      providerWrap.style.display = "";
      updateReferenceVisibility();
    } else {
      providerWrap.style.display = "none";
      if (referenceWrap) referenceWrap.style.display = "none";
      if (referencePathInput) referencePathInput.value = "";
    }
  }

  // Also expose the cached capabilities so preview.js can check them
  // without a second fetch when deciding whether to offer region review.
  function getCapabilities() { return cachedCapabilities; }

  if (toggle) {
    toggle.addEventListener("change", onToggle);
    // If the toggle is pre-checked (e.g. after a settings restore), show controls.
    if (toggle.checked) onToggle();
  }
  if (providerSelect) providerSelect.addEventListener("change", updateProviderHint);
  if (referenceBrowseBtn) referenceBrowseBtn.addEventListener("click", onReferenceBrowse);

  return { loadCapabilities, getCapabilities, updateProviderHint, getReferenceImage };
})();

window.SegmentationToggle = SegmentationToggle;

// --------------------------------------------------------- palette hint button

document.getElementById("btn-palette-hint")?.addEventListener("click", () => {
  window.Palette?.open();
});

const SegmentationHelp = (function () {
  const modal = document.getElementById("modal-segmentation-help");
  let returnFocus = null;

  async function open() {
    if (!modal) return;
    returnFocus = document.activeElement;
    // Loaded lazily and cached by SegmentationToggle; calling this here
    // does not re-fetch if the user already opened the provider dropdown,
    // and it fetches for the first time if they open help before that.
    const providers = await SegmentationToggle.loadCapabilities();
    const availableNames = new Set((providers || []).map(p => p.name));
    modal.querySelectorAll("[data-provider-unavailable]").forEach((el) => {
      const name = el.getAttribute("data-provider-unavailable");
      el.hidden = availableNames.has(name);
    });
    modal.classList.remove("hidden");
    focusFirstIn(modal);
  }

  function close() {
    if (modal) modal.classList.add("hidden");
    returnFocus?.focus?.();
  }

  modal?.querySelector("[data-modal-close]")?.addEventListener("click", close);
  // Close on backdrop click
  modal?.addEventListener("click", (e) => { if (e.target === modal) close(); });

  document.getElementById("btn-segmentation-help")?.addEventListener("click", open);
  registerModalCloser("modal-segmentation-help", close);

  return { open, close };
})();

window.SegmentationHelp = SegmentationHelp;
