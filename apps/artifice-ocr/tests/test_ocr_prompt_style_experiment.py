# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Experimental structured-prompt variant, for A/B measurement only via
scripts/measure_ocr_accuracy.py. See
docs/superpowers/plans/2026-09-09-olmocr2-optimisation.md's discussion of
§3 — olmOCR-2-7B-1025 was trained toward YAML-front-matter + Markdown output, not
the raw-text prompt this app uses by default. Whether raw or structured wins
for this app's users is an open, unmeasured question; this only makes the
comparison possible, it does not decide it."""

import json

import pytest
from artifice_ocr import config
from artifice_ocr.stages import ocr
from artifice_ocr.web import server
from fastapi.testclient import TestClient


def test_default_style_is_raw_and_unchanged():
    assert ocr._STRUCTURED_PROMPT_ADDENDUM not in ocr._effective_prompt("", style="raw")
    assert ocr._effective_prompt("", style="raw") == ocr.OCR_PROMPT


def test_structured_style_asks_for_markdown_and_front_matter():
    prompt = ocr._effective_prompt("", style="structured")
    assert "yaml" in prompt.lower() or "front matter" in prompt.lower()
    assert "markdown" in prompt.lower()


def test_instruction_and_structured_style_compose():
    prompt = ocr._effective_prompt("Expect handwritten German.", style="structured")
    assert "Expect handwritten German." in prompt
    assert "markdown" in prompt.lower()


def test_default_config_key_is_raw():
    from artifice_ocr.config import _DEFAULTS

    assert _DEFAULTS["ocr_prompt_style"] == "raw"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # POST /api/config reaches config.save_user_settings(), which always
    # targets ~/.artifice_ocr/settings.json by design — redirect the module
    # constant or this test overwrites the developer's real saved settings
    # (same pattern as the `client` fixture in test_web.py).
    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
    config.reset()
    config.load_config()
    with TestClient(server.app) as c:
        yield c


def test_prompt_style_is_not_settable_via_the_settings_api(client):
    """`ocr_prompt_style` is config-file/env-only by design — an experimental
    A/B flag whose "structured" value changes stage 1's output contract with
    cleanup/structure/pdf_export. It must not be accepted by the settings
    save path (set_config filters POST bodies against PERSISTED_KEYS), or a
    stray UI POST could silently flip the prompt contract (Copilot review,
    PR #101)."""
    res = client.post("/api/config", json={"ocr_prompt_style": "structured"})
    assert res.json() == {"ok": True}

    # Not applied at runtime by the save path ...
    assert config.get("ocr_prompt_style") == "raw"
    # ... not written to the persisted settings file ...
    if config._SETTINGS_PATH.exists():
        saved = json.loads(config._SETTINGS_PATH.read_text(encoding="utf-8"))
        assert "ocr_prompt_style" not in saved
    # ... and not in the whitelist the save path filters against at all.
    assert "ocr_prompt_style" not in config.PERSISTED_KEYS
