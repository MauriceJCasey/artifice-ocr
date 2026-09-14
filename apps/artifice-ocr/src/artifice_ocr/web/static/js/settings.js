// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * Settings tab: models/endpoints/processing options bound to /api/config,
 * plus a pre-flight service-health check. Mirrors gui/views/settings_view.py
 * field-for-field; the persistence rules are identical too — Save writes to
 * ~/.artifice_ocr/settings.json via the same config.save_user_settings() the
 * desktop build calls, Reset only clears the in-memory/session override until
 * Save is pressed again.
 */

const SettingsTab = (function () {
  const FIELDS = {
    ocr_backend: "select", cleanup_backend: "select", translate_backend: "select",
    ocr_model: "text", cleanup_model: "text", translate_model: "text",
    lm_studio_url: "text", ollama_url: "text", huggingface_token: "text",
    api_key: "text", api_base_url: "text",
    document_type: "select", ocr_prompt_instruction: "text",
    max_ocr_workers: "int", chunk_max_tokens: "int", context_size: "int",
    resume: "bool", confidence_enabled: "bool", preprocess_enabled: "bool", ollama_think: "bool", tropy_live_browse_enabled: "bool",
    tropy_api_port: "int",
    ocr_engine: "select", tesseract_lang: "text", tesseract_path: "text", tesseract_fallback_on_failure: "bool",
  };

  // Only Ollama honours a per-request context window. LM Studio fixes it when
  // it *loads* a model and hosted APIs set it server-side, so on those backends
  // the field is disabled and says where the limit really lives — a control
  // that looks live and is ignored is worse than no control.
  const CONTEXT_SIZE_HINTS = {
    ollama: null,   // null = field stays enabled, default hint shown
    lm_studio:
      "LM Studio sets the context window when it loads a model, so this cannot " +
      "be changed from here. Raise it in LM Studio, or run: " +
      "lms load <model> --context-length 8192",
    api_key:
      "This backend sets its context window server-side. Choose a model with a " +
      "larger context window instead.",
    huggingface:
      "This backend sets its context window server-side. Choose a model with a " +
      "larger context window instead.",
  };

  const DEFAULT_CONTEXT_HINT =
    "0 uses the model’s own default. Raise this if a page fails with " +
    "“exceeds the available context size”.";

  // The OCR stage is what overflows on a page image, so the *vision* backend
  // decides whether this field can do anything — not cleanup or translate.
  function updateContextSizeState() {
    const input = document.getElementById("set-context_size");
    const hint = document.getElementById("context-size-hint");
    if (!input || !hint) return;

    const backendSel = document.getElementById("set-ocr_backend");
    const backend = backendSel ? backendSel.value : "";

    // "auto" resolves at run time, so it cannot be pinned to one answer here.
    if (!backend || backend === "auto") {
      input.disabled = false;
      hint.textContent =
        DEFAULT_CONTEXT_HINT +
        " With OCR backend set to Auto, this applies only when Ollama is chosen.";
      return;
    }

    const hintText = CONTEXT_SIZE_HINTS[backend];
    if (hintText) {
      input.disabled = true;
      hint.textContent = hintText;
    } else {
      input.disabled = false;
      hint.textContent = DEFAULT_CONTEXT_HINT;
    }
  }

  // Connection fields are only meaningful — and only visible — when a backend
  // that uses them is selected.  Mirrors the server's URL→backend mapping plus
  // the two credential rows ``updateConnectionVisibility`` also hides.  collect()
  // skips any field whose backend is inactive, so a hidden row's stale value is
  // never posted back (e.g. the shipped api_base_url default on an Ollama-only
  // install, which would otherwise 400 on the server's endpoint policy).
  const CONNECTION_FIELDS = {
    lm_studio_url: "lm_studio",
    ollama_url: "ollama",
    huggingface_token: "huggingface",
    api_key: "api_key",
    api_base_url: "api_key",
  };

  const docTypeSelect = document.getElementById("set-document_type");
  const docTypeHint = document.getElementById("doc-type-hint");
  const savedLabel = document.getElementById("settings-saved");
  const healthPanel = document.getElementById("health-panel");
  const detectedModels = document.getElementById("detected-local-models");
  const localModelStatus = document.getElementById("local-model-status");

  const MODEL_BACKENDS = {
    ocr_model: "ocr_backend",
    cleanup_model: "cleanup_backend",
    translate_model: "translate_backend",
  };
  const discoveredModels = new Map();
  let discoveryGeneration = 0;
  let statusRevertTimer = null;

  const approvedFoldersList = document.getElementById("approved-folders-list");
  const approvedFoldersStatus = document.getElementById("approved-folders-status");
  let approvedFolders = [];
  let settingsBusy = false;
  let savedSnapshot = "";
  let dirty = false;

  let docTypes = {};

  function el(key) {
    return document.getElementById(`set-${key}`);
  }

  async function ensureDocTypes() {
    if (Object.keys(docTypes).length) return;
    const data = await api("GET", "/api/document-types");
    docTypes = data.types;
    docTypeSelect.innerHTML = Object.keys(docTypes)
      .map(k => `<option value="${escapeHtml(k)}">${escapeHtml(k)}</option>`).join("");
  }

  function updateDocTypeHint() {
    docTypeHint.textContent = docTypes[docTypeSelect.value] || "";
  }

  function apply(values) {
    for (const [key, kind] of Object.entries(FIELDS)) {
      const value = values[key];
      if (kind === "bool") el(key).checked = !!value;
      else el(key).value = value ?? "";
    }
    approvedFolders = Array.isArray(values.approved_folders) ? values.approved_folders.slice() : [];
    const outputDir = document.getElementById("output-dir");
    if (outputDir && values.output_dir) outputDir.value = values.output_dir;
    renderApprovedFolders();
    updateDocTypeHint();
    updateConnectionVisibility();
    updateContextSizeState();
    renderModelControls();
    refreshTesseractStatus();
  }

  function modelsForBackend(backend) {
    if (backend === "auto") {
      return [...new Set([
        ...(discoveredModels.get("ollama") || []),
        ...(discoveredModels.get("lm_studio") || []),
      ])].sort();
    }
    return (discoveredModels.get(backend) || []).slice().sort();
  }

  function renderModelControl(modelKey) {
    const backend = el(MODEL_BACKENDS[modelKey]).value;
    const input = el(modelKey);
    const picker = document.getElementById(`pick-${modelKey}`);
    if (!input || !picker) return;

    const local = backend === "auto" || backend === "ollama" || backend === "lm_studio";
    picker.hidden = !local;
    input.hidden = local;
    if (!local) return;

    const current = input.value || "";
    const models = modelsForBackend(backend);
    const options = [{ value: "", label: "Automatic — choose a suitable installed model" }];
    if (current && !models.includes(current)) {
      options.push({ value: current, label: `${current} (currently selected)` });
    }
    options.push(...models.map(name => ({ value: name, label: name })));
    picker.innerHTML = options.map(option =>
      `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`
    ).join("");
    picker.value = current;
  }

  function renderModelControls() {
    Object.keys(MODEL_BACKENDS).forEach(renderModelControl);
  }

  function renderDiscoveredModelList() {
    if (!detectedModels) return;
    const names = new Set();
    for (const models of discoveredModels.values()) {
      for (const name of models) names.add(name);
    }
    detectedModels.innerHTML = [...names].sort()
      .map(name => `<option value="${escapeHtml(name)}"></option>`).join("");
  }

  async function refreshLocalModels() {
    const generation = ++discoveryGeneration;
    const backends = activeBackends();
    const wanted = ["ollama", "lm_studio"].filter(backend => backends.has(backend));
    if (!wanted.length) {
      if (localModelStatus) localModelStatus.textContent =
        "Model names for hosted services are entered manually.";
      renderModelControls();
      return;
    }

    if (localModelStatus) localModelStatus.textContent = "Finding local services and models…";
    const results = await Promise.all(wanted.map(async backend => {
      const urlKey = backend === "ollama" ? "ollama_url" : "lm_studio_url";
      const url = el(urlKey).value || "";
      try {
        return await api("GET", `/api/local-models?backend=${encodeURIComponent(backend)}&url=${encodeURIComponent(url)}`);
      } catch (error) {
        return { ok: false, backend, models: [], detail: error.message };
      }
    }));
    if (generation !== discoveryGeneration) return;

    const summaries = [];
    for (const result of results) {
      discoveredModels.set(result.backend, result.ok ? result.models || [] : []);
      if (result.ok) {
        const urlKey = result.backend === "ollama" ? "ollama_url" : "lm_studio_url";
        const urlInput = el(urlKey);
        if (urlInput && result.url && urlInput.value !== result.url) {
          urlInput.value = result.url;
          markChanged();
        }
        summaries.push(`${result.backend === "ollama" ? "Ollama" : "LM Studio"}: ${(result.models || []).length} model${(result.models || []).length === 1 ? "" : "s"}`);
      } else {
        summaries.push(`${result.backend === "ollama" ? "Ollama" : "LM Studio"}: not found`);
      }
    }
    renderDiscoveredModelList();
    renderModelControls();
    if (localModelStatus) localModelStatus.textContent = summaries.join(" · ");
  }

  // Report whether the Tesseract binary is actually detected. A control that
  // silently no-ops when the binary is missing is worse than none — so the UI
  // says plainly whether it was found, and where.
  async function refreshTesseractStatus() {
    const elStatus = document.getElementById("tesseract-status");
    if (!elStatus) return;
    try {
      const s = await api("GET", "/api/tesseract/status");
      if (s.available) {
        const ver = s.version ? ` — ${escapeHtml(s.version)}` : "";
        elStatus.textContent = `Tesseract found${ver} (${escapeHtml(s.path || "on PATH")}).`;
      } else {
        elStatus.textContent =
          "Tesseract not found. Install it and/or set its path below to use it as an engine or fallback.";
      }
    } catch (_) {
      elStatus.textContent = "Could not check for Tesseract.";
    }
  }

  function collect() {
    const out = {};
    const backends = activeBackends();
    for (const [key, kind] of Object.entries(FIELDS)) {
      const backend = CONNECTION_FIELDS[key];
      if (backend && !backends.has(backend)) continue;
      const field = el(key);
      if (kind === "bool") out[key] = field.checked;
      else if (kind === "int") out[key] = parseInt(field.value, 10) || 0;
      else out[key] = field.value;
    }
    const outputDir = document.getElementById("output-dir");
    if (outputDir) out.output_dir = outputDir.value || "output";
    return out;
  }

  function setStatus(message, tone) {
    savedLabel.textContent = message;
    savedLabel.style.color = tone === "error" ? "var(--gold)" :
      tone === "success" ? "var(--accent)" : "";
  }

  function snapshot(values) {
    return JSON.stringify(values || collect());
  }

  function setDirty(value) {
    dirty = value;
    if (dirty) setStatus("Unsaved changes", "warning");
    else if (!settingsBusy) setStatus("No changes");
  }

  function markChanged() {
    if (!settingsBusy) setDirty(snapshot() !== savedSnapshot);
  }

  function validate() {
    const rules = [
      ["max_ocr_workers", 1, Infinity, "Enter at least 1 OCR reader."],
      ["chunk_max_tokens", 100, Infinity, "Enter at least 100 tokens."],
      ["context_size", 0, Infinity, "Context size cannot be negative."],
      ["tropy_api_port", 0, 65535, "Enter a port from 0 to 65535."],
    ];
    for (const [key, min, max, message] of rules) {
      const field = el(key);
      const value = Number(field.value);
      const raw = String(field.value ?? "").trim();
      const invalid = raw === "" || !Number.isFinite(value) || value < min || value > max;
      if (field.disabled) continue;
      if (field.setAttribute) field.setAttribute("aria-invalid", String(invalid));
      if (invalid) {
        setStatus(message, "error");
        field.focus();
        return false;
      }
      if (field.removeAttribute) field.removeAttribute("aria-invalid");
    }
    return true;
  }

  function activeBackends() {
    const backends = new Set([
      el("ocr_backend").value,
      el("cleanup_backend").value,
      el("translate_backend").value,
    ]);
    // "auto" probes both local servers and picks whichever is reachable
    // (see _resolution._resolve_auto) -- never a cloud backend. So with any
    // role set to Auto, both connection rows must stay visible/collectable,
    // never the huggingface/api_key ones auto can't select.
    if (backends.has("auto")) {
      backends.add("lm_studio");
      backends.add("ollama");
    }
    return backends;
  }

  function updateConnectionVisibility() {
    const backends = activeBackends();
    const lmRow = el("lm_studio_url")?.closest("label");
    const olRow = el("ollama_url")?.closest("label");
    const hfRow = el("huggingface_token")?.closest("label");
    const akRow = el("api_key")?.closest("label");
    const buRow = el("api_base_url")?.closest("label");
    if (lmRow) lmRow.style.display = backends.has("lm_studio") ? "" : "none";
    if (olRow) olRow.style.display = backends.has("ollama") ? "" : "none";
    if (hfRow) hfRow.style.display = backends.has("huggingface") ? "" : "none";
    if (akRow) akRow.style.display = backends.has("api_key") ? "" : "none";
    if (buRow) buRow.style.display = backends.has("api_key") ? "" : "none";
  }

  function setApprovedStatus(msg, isError) {
    approvedFoldersStatus.textContent = msg;
    approvedFoldersStatus.style.color = isError ? "var(--gold)" : "";
  }

  function renderApprovedFolders() {
    approvedFoldersList.innerHTML = approvedFolders.length
      ? approvedFolders.map((folder, i) =>
          `<li class="approved-folder-row">
             <span class="dim">${escapeHtml(folder)}</span>
             <button class="btn btn-small" data-remove-folder="${i}" type="button">Remove</button>
           </li>`).join("")
      : `<li class="dim">No folders approved yet.</li>`;
    approvedFoldersList.querySelectorAll("[data-remove-folder]").forEach(btn => {
      btn.addEventListener("click", () => removeApprovedFolder(Number(btn.dataset.removeFolder)));
    });
  }

  async function persistApprovedFolders() {
    await api("POST", "/api/config", { approved_folders: approvedFolders });
  }

  async function addApprovedFolder() {
    let folder = null;
    try {
      folder = await pickFolder();
    } catch {
      setApprovedStatus("Could not open the folder picker.", true);
      return;
    }
    if (!folder) return; // cancelled or unavailable without a typed path
    if (approvedFolders.includes(folder)) {
      setApprovedStatus("That folder is already approved.", true);
      return;
    }
    approvedFolders.push(folder);
    try {
      await persistApprovedFolders();
    } catch (err) {
      approvedFolders.pop();
      setApprovedStatus("Could not approve folder: " + err.message, true);
      return;
    }
    renderApprovedFolders();
    setApprovedStatus("", false);
  }

  async function removeApprovedFolder(index) {
    const removed = approvedFolders.splice(index, 1)[0];
    try {
      await persistApprovedFolders();
    } catch (err) {
      approvedFolders.splice(index, 0, removed);
      setApprovedStatus("Could not remove folder: " + err.message, true);
      return;
    }
    renderApprovedFolders();
    setApprovedStatus("", false);
  }

  async function load() {
    try {
      await ensureDocTypes();
      const cfg = await api("GET", "/api/config");
      // Do not let a slow initial request wipe edits made while the page was
      // loading. The user can reload explicitly if they want to discard them.
      if (dirty) {
        setStatus("Settings changed while loading; reload to discard edits.", "warning");
        return;
      }
      apply(cfg);
      savedSnapshot = snapshot(cfg);
      setDirty(false);
    } catch (err) {
      setStatus("Could not load settings: " + err.message, "error");
    }
  }

  function setSettingsBusy(busy) {
    settingsBusy = busy;
    ["btn-settings-save", "btn-settings-reset"].forEach(id => {
      const button = document.getElementById(id);
      if (button) button.disabled = busy;
    });
  }

  async function save() {
    if (settingsBusy) return;
    if (!validate()) return;
    setSettingsBusy(true);
    setStatus("Saving…");
    try {
      await api("POST", "/api/config", collect());
      const cfg = await api("GET", "/api/config");
      apply(cfg);
      savedSnapshot = snapshot(cfg);
      setDirty(false);
      setStatus("Saved.", "success");
      // Cancel any revert timer still pending from an earlier save, or the
      // stale timer will later overwrite this save's "Saved." with "No changes".
      clearTimeout(statusRevertTimer);
      statusRevertTimer = setTimeout(() => { if (!dirty) setStatus("No changes"); }, 2500);
    } catch (err) {
      if (window.ArtificeToast) window.ArtificeToast.error("Could not save settings: " + err.message);
      setStatus("Could not save settings: " + err.message, "error");
    } finally {
      setSettingsBusy(false);
    }
  }

  async function resetDefaults() {
    if (settingsBusy) return;
    if (!confirm("Reset the settings on this page to their defaults? Save afterwards to keep them.")) return;
    setSettingsBusy(true);
    setStatus("Resetting…");
    try {
      const cfg = await api("POST", "/api/config/reset");
      apply(cfg);
      setDirty(snapshot(cfg) !== savedSnapshot);
      setStatus("Defaults loaded. Save to keep them.", "warning");
    } catch (err) {
      if (window.ArtificeToast) window.ArtificeToast.error("Could not reset settings: " + err.message);
      setStatus("Could not reset settings: " + err.message, "error");
    } finally {
      setSettingsBusy(false);
    }
  }

  function healthLine(label, ok, detail, models) {
    const cls = ok ? "health-ok" : "health-fail";
    const status = ok ? "OK" : "FAIL";
    const count = ok && Array.isArray(models) ? `   ${models.length} model${models.length === 1 ? "" : "s"}` : "";
    return `<div class="health-row ${cls}"><strong>${escapeHtml(label)}</strong><span>${status}${count}${detail ? `   ${escapeHtml(detail)}` : ""}</span></div>`;
  }

  function updateDetectedModels(health) {
    for (const key of ["ollama", "lm_studio"]) {
      if (health[key]?.ok) discoveredModels.set(key, health[key].models || []);
    }
    renderDiscoveredModelList();
    renderModelControls();
  }

  async function runPreflight() {
    const button = document.getElementById("btn-preflight");
    if (button?.disabled) return;
    if (button) button.disabled = true;
    healthPanel.innerHTML = `<p class="dim">Checking connections&hellip;</p>`;
    try {
      const health = await api("GET", "/api/health");
      updateDetectedModels(health);
      const lines = [];
      if (health.lm_studio) {
        lines.push(healthLine("LM Studio", health.lm_studio.ok,
                   health.lm_studio.ok ? health.lm_studio.url : health.lm_studio.detail,
                   health.lm_studio.models));
      }
      if (health.ollama) {
        lines.push(healthLine("Ollama", health.ollama.ok,
                   health.ollama.ok ? health.ollama.url : health.ollama.detail,
                   health.ollama.models));
      }
      if (health.models) {
        // Each model was checked against the backend its own role is
        // configured to use (see settings.py's per-role ROLE_KEYS loop) —
        // show which server was actually consulted, not just Ollama's.
        // A cloud-backend model (checkable === false) has no local model
        // list to grade against, so it gets a neutral "not checkable" line
        // (reusing the existing `dim` class) rather than a false FAIL.
        lines.push(...health.models.map(m => m.checkable === false
          ? `<div class="dim">  ${escapeHtml(m.name)} (${escapeHtml(m.backend)})   not checkable${m.detail ? `   ${escapeHtml(m.detail)}` : ""}</div>`
          : healthLine(`  ${m.name} (${m.backend})`, m.ok)
        ));
      }
      if (health.huggingface) {
        lines.push(healthLine("Hugging Face", health.huggingface.ok, health.huggingface.detail));
      }
      if (health.api_key) {
        lines.push(healthLine("API Key", health.api_key.ok,
                   health.api_key.ok ? health.api_key.url : health.api_key.detail));
      }
      healthPanel.innerHTML = lines.join("");
    } catch (err) {
      healthPanel.innerHTML = `<p class="health-fail">Could not check connections &mdash; is the server running?</p>`;
    } finally {
      if (button) button.disabled = false;
    }
  }

  // Wire up backend dropdown change events to update connection visibility
  ["ocr_backend", "cleanup_backend", "translate_backend"].forEach(key => {
    el(key).addEventListener("change", () => {
      updateConnectionVisibility();
      renderModelControls();
      refreshLocalModels();
    });
  });

  Object.keys(MODEL_BACKENDS).forEach(modelKey => {
    document.getElementById(`pick-${modelKey}`)?.addEventListener("change", event => {
      el(modelKey).value = event.target.value;
      markChanged();
    });
  });

  // Context size follows the *vision* backend specifically: the OCR stage is
  // what overflows on a page image, and it is the only stage whose backend
  // decides whether a per-request context window is honoured at all.
  el("ocr_backend").addEventListener("change", updateContextSizeState);

  docTypeSelect.addEventListener("change", updateDocTypeHint);
  document.getElementById("btn-settings-save").onclick = save;
  document.getElementById("btn-settings-reset").onclick = resetDefaults;
  document.getElementById("btn-preflight").onclick = runPreflight;
  document.getElementById("btn-refresh-models").onclick = refreshLocalModels;
  document.getElementById("btn-approved-folder-add").onclick = addApprovedFolder;
  const settingsSectionButtons = document.querySelectorAll
    ? document.querySelectorAll("[data-settings-target]") : [];
  settingsSectionButtons.forEach(button => {
    button.addEventListener("click", () => {
      settingsSectionButtons.forEach(item => {
        item.classList.toggle("active", item === button);
      });
      document.getElementById(button.dataset.settingsTarget)?.scrollIntoView({
        behavior: "smooth", block: "start",
      });
    });
  });

  Object.keys(FIELDS).forEach(key => {
    const field = el(key);
    if (field) field.addEventListener("input", markChanged);
    if (field) field.addEventListener("change", markChanged);
  });
  document.getElementById("output-dir")?.addEventListener("input", markChanged);
  if (window.addEventListener) {
    window.addEventListener("beforeunload", event => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "You have unsaved OCR settings.";
      }
    });
  }

  let loaded = false;
  TAB_ACTIVATE.settings = () => {
    if (!loaded) {
      loaded = true;
      load().then(refreshLocalModels).then(runPreflight);
    }
  };
  // app.js applies ?view=settings before this deferred tab module loads.
  // Honour that initial activation now that its callback exists.
  if (document.getElementById("panel-settings")?.classList?.contains("active")) {
    TAB_ACTIVATE.settings();
  }

  return { load };
})();
