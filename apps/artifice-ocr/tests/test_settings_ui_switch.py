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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_switching_all_roles_from_ollama_to_lm_studio_changes_row_and_payload():
    harness = r"""
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
  if (path === "/api/tesseract/status") return { available: false };
  if (path.startsWith("/api/local-models")) {
    const backend = new URL(path, "http://artifice.test").searchParams.get("backend");
    return backend === "lm_studio"
      ? { ok: true, backend, url: fields.lm_studio_url, models: ["vision-live", "text-live"] }
      : { ok: true, backend, url: fields.ollama_url, models: ["ollama-live"] };
  }
  return { ok: true };
};
"""
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
    proc = subprocess.run(
        ["node", "-e", harness + _SETTINGS_JS.read_text(encoding="utf-8") + assertions],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "settings-switch-ok" in proc.stdout
