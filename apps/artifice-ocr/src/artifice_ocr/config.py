# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import importlib.resources
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

# Resolved through importlib.resources, NOT a __file__-relative path.  This
# file is distributed as a frozen .exe/.dmg, where __file__ points inside a
# temporary extraction directory and any ``.parent.parent.parent`` walk lands
# somewhere meaningless.  The previous form was
# ``Path(__file__).resolve().parent.parent.parent / "configs"``, which also
# put configs under ``apps/artifice-ocr/configs/`` — outside the package —
# so the example config was excluded from the wheel entirely.
_CONFIG_DIR = importlib.resources.files("artifice_ocr") / "configs"

_DEFAULTS: dict[str, Any] = {
    "lm_studio_url": "http://localhost:1234/v1",
    # Model names are resolved at run time (see artifice_ocr._resolution):
    # an empty string means "no explicit choice — resolve from what the local
    # server actually serves".  Shipping a concrete name here was the bug: the
    # OCR default was a Hugging Face repo id, not an Ollama tag, so a default
    # install failed before the user did anything.
    "ocr_model": "",
    "cleanup_model": "",
    "translate_model": "",
    "output_dir": "output",
    "translate_enabled": True,
    "title_enabled": False,  # opt-in stage
    # Live Tropy project browsing (read-only .tpy). Enabled by default per the
    # maintainer's decision (2026-08-25): it restores the "select a project,
    # don't type a path" flow the JSON-LD rewrite dropped. Still gated by the
    # read-only connection guarantee; ARTIFICE_OCR_TROPY_LIVE_READ overrides it.
    "tropy_live_browse_enabled": True,
    # 0 discovers the port from Tropy's state and then tries the stable/beta
    # defaults (2019/2029). A non-zero value is an advanced override for a
    # Tropy instance launched with a custom ``--port``.
    "tropy_api_port": 0,
    "title_max_chars": 120,
    "resume": True,
    "max_ocr_workers": 2,
    # P4: Pipeline optimization & robustness
    "chunk_max_tokens": 3500,
    "chunk_overlap_tokens": 200,
    # Model context window, in tokens. 0 means "leave it to the backend" —
    # the model's own default — which is the behaviour before this setting
    # existed, so a config that predates it keeps working unchanged.
    #
    # This is NOT chunk_max_tokens. That splits *text* before sending it to
    # the cleanup/translate/structure stages. This is the size of the window
    # the model itself is loaded with, and it is what a page image overflows:
    # "request (4107 tokens) exceeds the available context size (4096)".
    #
    # Only Ollama honours it. LM Studio fixes context when it *loads* a model,
    # and hosted APIs set it server-side — for those the UI says where to
    # change it rather than sending a value that is silently ignored.
    # 8192 rather than 0. ``0`` means "leave it to the backend", and Ollama's
    # own default is 4096 — which a single page image overflows. A real
    # 4653x3445 archive scan needed 4145 tokens and failed on a stock install
    # before the user touched anything. 8192 leaves room for a page capped at
    # ``ocr_max_image_edge`` plus its transcription, at a modest VRAM cost.
    # Set 0 to restore the backend's own default.
    "context_size": 8192,
    "confidence_enabled": True,
    "document_type": "default",
    # Longest-edge cap, in pixels, for the image sent to the *vision* model.
    # olmOCR-2 is built on Qwen2.5-VL, which tiles at native resolution up to
    # its max_pixels: an unresized 4653x3445 scan becomes far more visual
    # tokens than the model ever saw in training, paid for twice — in latency
    # and in distribution mismatch. olmOCR 2 (arXiv:2510.19817 s4, "Image
    # Resizing") swept image sizes and picked 1288px on the longest edge.
    #
    # Only ever downscales; a smaller page is passed through untouched. ``0``
    # disables the cap and restores full-resolution behaviour. Deliberately
    # NOT applied on the Tesseract path, which benefits from more resolution
    # rather than less — see stages/ocr.py::_tesseract_from_image.
    "ocr_max_image_edge": 1288,
    # Free-text domain instruction, appended to OCR_PROMPT (not a replacement
    # — the "return only raw text" contract stays intact). [CENT]
    # (arXiv:2608.30616) Table 4: a zero-shot domain instruction prompt took
    # olmOCR2's SpACER-M error from 15.58% to 6.77% and field EMR from 30.55%
    # to 74.64%, with no training. Empty string (default) leaves OCR_PROMPT
    # exactly as it was. Already recorded per-run in the raw_ocr sidecar as
    # part of "ocr_prompt" — see stages/ocr.py::perform.
    "ocr_prompt_instruction": "",
    # Experimental. "raw" (default) is this app's original prompt contract;
    # "structured" asks for the YAML-front-matter + Markdown shape
    # olmOCR-2-7B-1025 was actually trained toward. See stages/ocr.py's
    # _STRUCTURED_PROMPT_ADDENDUM docstring — do not flip this default
    # without measured results and maintainer sign-off; it changes stage 1's
    # output contract with cleanup/structure/pdf_export.
    "ocr_prompt_style": "raw",
    # P7: throughput. Reasoning models burn ~17x the tokens they need on
    # mechanical cleanup; leaving this False keeps the cleanup stage fast.
    # Set True only if you swap in a model whose reasoning you actually want.
    "ollama_think": False,
    # Hard ceiling on generated tokens (None = no cap). A runaway-generation
    # guard; leave unset unless you have seen one, since a cap that bites
    # truncates the document silently.
    "max_output_tokens": None,
    # Degeneracy guard for the OCR stage. When a vision model is given an
    # image it fundamentally can't parse (confirmed cause: a scan with no
    # orientation metadata anywhere saying it was upside-down), greedy
    # decoding can hallucinate filler and then loop on it — one real page
    # produced 900+ lines of the same repeated sentence. Unlike the other
    # guards below, a rejected page has no source text to fall back to, so
    # this fails the item outright instead of silently writing the loop to
    # raw_ocr/ as if it were a real transcription.
    "ocr_repetition_guard": True,
    # Per-page temperature ladder: on a repetition-guard rejection, resample
    # the SAME page at a higher temperature instead of discarding the whole
    # document to Tesseract. olmOCR 2 (arXiv:2510.19817 s4, "Dynamic
    # temperature scaling") starts at 0.1 and steps to 0.2, 0.3, ... on each
    # rejection, up to 0.8, reporting ~0.3 accuracy points and a failure rate
    # drop to ~0.01% over fixed-temperature decoding. ``0.0`` (greedy) is
    # MORE loop-prone than the paper's own starting point, not less.
    #
    # ``ocr_temperature_ladder_enabled=False`` restores the exact previous
    # behaviour (temperature 0.0, no ladder) so before/after can be A/B'd
    # with scripts/measure_ocr_accuracy.py.
    "ocr_temperature_ladder_enabled": True,
    "ocr_temperature_ladder_start": 0.1,
    "ocr_temperature_ladder_step": 0.1,
    "ocr_temperature_ladder_max": 0.8,
    # Skip the OCR call entirely for a near-blank page (a verso, a flyleaf).
    # olmOCR 2 (arXiv:2510.19817 s4, "Handle blank pages"): a model never
    # trained on blank pages hallucinates rather than recognising there is
    # nothing there. See _blank.py.
    "ocr_blank_page_skip": True,
    # Probe a page with Tesseract OSD for rotation ONLY when Tropy's own
    # orientation metadata says "normal" (1) — an explicit non-1 value is
    # trusted as a deliberate correction and never second-guessed. Off by
    # default: it costs a Tesseract subprocess call per page and Tesseract
    # is an optional dependency. arXiv:2510.19817 s4, "automatic rotation
    # correction". See _rotation.py.
    "ocr_auto_rotation_detect": False,
    # Phase 1 deterministic image pre-processing, applied before the page is
    # sent to the vision model. Off by default: a clean scan needs none of it,
    # and it must never change behaviour for an existing user who has not asked
    # for it. The master toggle is the one UI control; the per-step keys are
    # advanced/config-only and used when the master is on. See
    # docs/OCR_PREPROCESSING_PLAN.md and stages/preprocess.py.
    "preprocess_enabled": False,
    "preprocess_grayscale": True,
    "preprocess_illumination": True,
    "preprocess_autocontrast": True,
    "preprocess_gamma": 1.0,  # 1.0 = no gamma; <1 lightens, >1 darkens mid-tones
    # OCR engine selection. "vision_model" (default) routes an image to a vision
    # LLM; "tesseract" runs the locally-installed Tesseract binary instead — a
    # fast, offline, deterministic transcriber. Tesseract is NOT bundled: the
    # binary is detected on PATH (or at tesseract_path). See docs/
    # OCR_TESSERACT_ENGINE_PLAN.md and _tesseract.py.
    "ocr_engine": "vision_model",
    "tesseract_lang": "eng",  # e.g. "eng", "deu", "deu+eng"; needs the traineddata
    "tesseract_path": "",  # explicit binary path when it is not on PATH
    # Independent safety net: when a vision-model page fails (repetition-guard
    # rejection or exhausted retries) and Tesseract is available, retry that page
    # with Tesseract rather than failing it. Detection is automatic; when the
    # binary or requested language data is unavailable the normal model failure
    # remains visible. Provenance is recorded as "tesseract-fallback".
    "tesseract_fallback_on_failure": True,
    # Content-preservation guard for cleanup. When the model's output looks
    # lossy or has altered a proper noun, the raw text is kept instead, so a
    # page is either cleaned or untouched — never quietly truncated.
    "cleanup_guard": True,
    "cleanup_guard_max_deleted_words": 2,
    "cleanup_guard_min_length_ratio": 0.97,  # letters, not characters
    "cleanup_guard_protect_nouns": True,
    # Content-preservation guard for the structure stage. When the model's
    # output has altered any word, the original text is kept instead, so a
    # page is either structured or untouched — never reworded.
    "structure_guard": True,
    # An LLM asked to "translate into English" text that is already English
    # has nothing to genuinely translate, and reliably "helps" by rewording,
    # dropping, or otherwise rewriting it instead — corrupting an
    # already-correct document. When the language-detection pass confidently
    # identifies English, skip the translate call entirely and pass the
    # cleaned text through untouched. Only ever skips on a confident "en"
    # result; an uncertain/failed detection still translates as before.
    "skip_translation_if_english": True,
    # P6: GUI persistence
    "history_db": None,  # defaults to ~/.artifice_ocr/history.db
    "gui_theme": "paper",  # "paper" (light) or "night" (dark)
    # ``"auto"`` means "use whichever local server is reachable and can serve
    # a suitable model".  The previous default presumed LM Studio for OCR and
    # Ollama for cleanup/translate, which broke a user running only Ollama
    # (the setup the Hub itself installs).  An explicit ``"ollama"`` /
    # ``"lm_studio"`` / ``"huggingface"`` / ``"api_key"`` is still honoured.
    "ocr_backend": "auto",
    "cleanup_backend": "auto",
    "translate_backend": "auto",
    "ollama_url": "http://localhost:11434",
    "huggingface_token": "",
    "api_key": "",
    "api_base_url": "https://api.openai.com/v1",
    # User-approved folders — an explicit, user-granted extension of the
    # allowed-roots list. Each entry is an absolute directory the user picked
    # through the native folder dialog (the consent step), so a Tropy project
    # on an external drive can be opened without the env var.
    "approved_folders": [],
}

_USER_DIR = Path.home() / ".artifice_ocr"
_SETTINGS_PATH = _USER_DIR / "settings.json"

# Keys the GUI is allowed to persist between sessions.
PERSISTED_KEYS = (
    "lm_studio_url",
    "ocr_model",
    "cleanup_model",
    "translate_model",
    "output_dir",
    "max_ocr_workers",
    "resume",
    "document_type",
    "confidence_enabled",
    "chunk_max_tokens",
    "context_size",
    "ocr_max_image_edge",
    "ocr_prompt_instruction",
    # NOTE: "ocr_prompt_style" is deliberately NOT here. It stays in
    # _DEFAULTS (so ARTIFICE_OCR_CONFIG YAML and direct config-file edits
    # work) but is config-file/env-only: the settings-save API path filters
    # POST bodies against PERSISTED_KEYS, so listing it here made a raw API
    # POST able to flip this experimental stage-1 output-contract flag even
    # though it is invisible in GET /api/config and the UI (Copilot review,
    # PR #101).
    "ocr_temperature_ladder_enabled",
    "ocr_temperature_ladder_start",
    "ocr_temperature_ladder_step",
    "ocr_temperature_ladder_max",
    "ocr_blank_page_skip",
    "ocr_auto_rotation_detect",
    "preprocess_enabled",
    "preprocess_grayscale",
    "preprocess_illumination",
    "preprocess_autocontrast",
    "preprocess_gamma",
    "ocr_engine",
    "tesseract_lang",
    "tesseract_path",
    "tesseract_fallback_on_failure",
    "gui_theme",
    "ollama_think",
    "onboarding_dismissed",
    "ocr_backend",
    "title_enabled",
    "title_max_chars",
    "cleanup_backend",
    "translate_backend",
    "ollama_url",
    "huggingface_token",
    "api_key",
    "api_base_url",
    "tropy_last_path",
    "tropy_live_browse_enabled",
    "tropy_api_port",
    "approved_folders",
)

_config_cache: dict[str, Any] | None = None


def load_config(
    config_path: str | Path | None = None, *, include_user_settings: bool = True
) -> dict[str, Any]:
    """Load configuration from YAML file, merged over defaults.

    Resolution order:
      1. Explicit config_path argument
      2. ARTIFICE_OCR_CONFIG env var
      3. configs/default.yaml (if it exists)
      4. Built-in _DEFAULTS only
    """
    global _config_cache

    if _config_cache is not None and config_path is None:
        return _config_cache

    merged = dict(_DEFAULTS)

    if config_path is None:
        config_path = os.environ.get("ARTIFICE_OCR_CONFIG")
    if config_path is None:
        default_path = _CONFIG_DIR / "default.yaml"
        if default_path.exists():
            config_path = default_path

    if config_path is not None:
        p = Path(config_path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                file_cfg = yaml.safe_load(f) or {}
            merged.update(file_cfg)

    # Durable UI settings override shipped/YAML defaults on every process
    # start. The save path has always written this file, but until now the
    # load path never merged it, so settings appeared to vanish on restart.
    if include_user_settings:
        merged.update(load_user_settings())

    env_overrides = {
        "ocr_model": "OCR_MODEL",
        "cleanup_model": "CLEANUP_MODEL",
        "translate_model": "TRANSLATE_MODEL",
        "lm_studio_url": "LM_STUDIO_URL",
        "ollama_url": "OLLAMA_URL",
        "output_dir": "OUTPUT_DIR",
    }
    for key, env_var in env_overrides.items():
        val = os.environ.get(env_var)
        if val is not None:
            merged[key] = val

    _config_cache = merged
    return merged


def get(key: str, default: Any = None) -> Any:
    """Shorthand: get a single config value."""
    return load_config().get(key, default)


def apply_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply runtime overrides to the current config cache.

    Returns the updated config dict. Useful for GUI settings.
    """
    global _config_cache
    if _config_cache is None:
        load_config()
    _config_cache.update(overrides)
    return _config_cache


def load_user_settings() -> dict[str, Any]:
    """Read GUI-persisted settings from ~/.artifice_ocr/settings.json."""
    if not _SETTINGS_PATH.exists():
        return {}
    try:
        from secure_io import ensure_restricted

        ensure_restricted(_SETTINGS_PATH)
    except Exception:
        import logging

        logging.warning(
            "Could not restrict permissions on %s — continuing anyway",
            _SETTINGS_PATH,
        )
    try:
        import json

        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if k in PERSISTED_KEYS}
    except Exception:
        return {}


def save_user_settings(settings: dict[str, Any]) -> None:
    """Persist GUI settings. Only whitelisted keys are written.

    Merges onto whatever is already saved rather than replacing the file
    outright. The desktop Settings tab always saves its full field set, so it
    never noticed, but the web build persists single fields in isolation
    (`output_dir` alone, right after starting a run) — a plain overwrite would
    silently discard every other saved setting each time that happened.
    """
    from secure_io import write_private_json_verified

    _USER_DIR.mkdir(parents=True, exist_ok=True)
    merged = load_user_settings()
    merged.update({k: v for k, v in settings.items() if k in PERSISTED_KEYS})
    write_private_json_verified(_SETTINGS_PATH, merged, label="settings file")


def reset():
    """Clear cached config (useful in tests)."""
    global _config_cache
    _config_cache = None
