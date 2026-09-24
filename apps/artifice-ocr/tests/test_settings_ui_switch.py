# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Browser-free DOM contract for switching OCR model endpoint rows."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_SETTINGS_JS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "artifice_ocr"
    / "web"
    / "static"
    / "js"
    / "settings.js"
)


_HARNESS = r"""
class FakeElement {
  constructor(id) {
    this.id = id;
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.style = {};
    this.textContent = "";
    this.innerHTML = "";
    this.listeners = {};
    this.row = { style: {} };
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  dispatch(name) { for (const fn of this.listeners[name] || []) fn({ target: this }); }
  closest() { return this.row; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
}

const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, new FakeElement(id));
  return elements.get(id);
};

globalThis.document = { getElementById: element };
globalThis.window = { ArtificeToast: null };
globalThis.TAB_ACTIVATE = {};
globalThis.escapeHtml = (value) => String(value);
globalThis.pickFolder = async () => null;
globalThis.confirm = () => true;
globalThis.setTimeout = () => 0;

const fields = {
  ocr_backend: "lm_studio", cleanup_backend: "lm_studio", translate_backend: "lm_studio",
  ocr_model: "vision", cleanup_model: "cleanup", translate_model: "translate",
  lm_studio_url: "http://192.168.1.50:1234/v1",
  ollama_url: "http://172.21.176.1:11434",
  huggingface_token: "", api_key: "", api_base_url: "https://api.openai.com/v1",
  document_type: "default",
  ocr_prompt_instruction: "A 19th-century field catalogue in German Kurrentschrift",
  max_ocr_workers: 2, chunk_max_tokens: 3500,
  context_size: 0, resume: true, confidence_enabled: true, preprocess_enabled: false,
  ollama_think: false, tropy_live_browse_enabled: true, tropy_writeback_enabled: false,
  tropy_api_port: 0, ocr_engine: "vision_model", tesseract_lang: "eng",
  tesseract_path: "", tesseract_fallback_on_failure: false,
  output_dir: "output", approved_folders: []
};
for (const [key, value] of Object.entries(fields)) {
  const target = element("set-" + key);
  if (typeof value === "boolean") target.checked = value;
  else target.value = value;
}
element("output-dir").value = fields.output_dir;

const calls = [];
globalThis.api = async (method, path, body) => {
  calls.push({ method, path, body });
  if (path === "/api/config" && method === "GET") return fields;
  if (path === "/api/document-types") return { types: { default: "General documents" } };
  if (path === "/api/tesseract/status") return { available: false };
  if (path.startsWith("/api/local-models")) {
    const backend = new URL(path, "http://artifice.test").searchParams.get("backend");
    return backend === "lm_studio"
      ? { ok: true, backend, url: globalThis.discoveredLmUrl || fields.lm_studio_url,
          models: ["vision-live", "text-live"] }
      : { ok: true, backend, url: fields.ollama_url, models: ["ollama-live"] };
  }
  return { ok: true };
};
"""


def _run_settings_js(assertions: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "-e", _HARNESS + _SETTINGS_JS.read_text(encoding="utf-8") + assertions],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_switching_all_roles_from_ollama_to_lm_studio_changes_row_and_payload():
    assertions = r"""
(async () => {
  for (const key of ["ocr_backend", "cleanup_backend", "translate_backend"]) {
    element("set-" + key).value = "ollama";
  }
  element("set-ocr_backend").dispatch("change");
  if (element("set-ollama_url").row.style.display !== "") throw new Error("Ollama row hidden");
  if (element("set-lm_studio_url").row.style.display !== "none") throw new Error("LM row visible");

  for (const key of ["ocr_backend", "cleanup_backend", "translate_backend"]) {
    element("set-" + key).value = "lm_studio";
  }
  element("set-ocr_backend").dispatch("change");
  await element("btn-refresh-models").onclick();
  if (element("set-lm_studio_url").row.style.display !== "") throw new Error("LM row hidden");
  if (element("set-ollama_url").row.style.display !== "none") throw new Error("Ollama row visible");
  if (element("pick-ocr_model").hidden) throw new Error("LM model picker hidden");
  if (!element("pick-ocr_model").innerHTML.includes("vision-live")) {
    throw new Error("LM models absent");
  }

  element("pick-ocr_model").value = "vision-live";
  element("pick-ocr_model").dispatch("change");

  await element("btn-settings-save").onclick();
  const post = calls.find((call) => call.method === "POST" && call.path === "/api/config");
  if (!post) throw new Error("Settings were not posted");
  if (post.body.lm_studio_url !== fields.lm_studio_url) throw new Error("LM URL omitted");
  if (Object.hasOwn(post.body, "ollama_url")) throw new Error("inactive Ollama URL posted");
  if (post.body.ocr_backend !== "lm_studio") throw new Error("backend switch omitted");
  if (post.body.ocr_model !== "vision-live") throw new Error("discovered model choice omitted");
  console.log("settings-switch-ok");
})().catch((error) => { console.error(error.stack); process.exit(1); });
"""
    proc = _run_settings_js(assertions)
    assert proc.returncode == 0, proc.stderr
    assert "settings-switch-ok" in proc.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_undoing_an_edit_returns_settings_to_no_changes():
    # The dirty baseline was the raw config, a different shape from the form
    # values it was compared with, so once anything fired a change event the
    # page stayed "Unsaved changes" (and prompted on leave) even after undoing.
    proc = _run_settings_js(r"""
(async () => {
  const status = element("settings-saved");
  const expect = (want, when) => {
    if (status.textContent !== want) throw new Error(when + ": " + status.textContent);
  };
  await SettingsTab.load();
  expect("No changes", "after load");
  element("set-max_ocr_workers").value = "5";
  element("set-max_ocr_workers").dispatch("input");
  expect("Unsaved changes", "after edit");
  element("set-max_ocr_workers").value = "2";
  element("set-max_ocr_workers").dispatch("input");
  expect("No changes", "after undo");
  console.log("settings-dirty-ok");
})().catch((error) => { console.error(error.stack); process.exit(1); });
""")
    assert proc.returncode == 0, proc.stderr
    assert "settings-dirty-ok" in proc.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_a_newly_discovered_address_says_why_settings_need_saving():
    proc = _run_settings_js(r"""
(async () => {
  await SettingsTab.load();
  // Guard: a failed load leaves no baseline, which would also look dirty.
  const loaded = element("settings-saved").textContent;
  if (loaded !== "No changes") throw new Error("load failed: " + loaded);
  globalThis.discoveredLmUrl = "http://172.21.176.1:1234/v1";
  await element("btn-refresh-models").onclick();
  const status = element("settings-saved").textContent;
  if (!status.startsWith("Found LM Studio at a new address")) throw new Error("status: " + status);
  if (element("set-lm_studio_url").value !== globalThis.discoveredLmUrl) {
    throw new Error("discovered address not shown");
  }
  console.log("settings-discovery-ok");
})().catch((error) => { console.error(error.stack); process.exit(1); });
""")
    assert proc.returncode == 0, proc.stderr
    assert "settings-discovery-ok" in proc.stdout
