# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the "already English" translation skip and the multi_lang
language-detection parsing fix.

An LLM asked to "translate into English" text that is already English has
nothing to genuinely translate, and reliably "helps" instead — rewording,
dropping, or otherwise rewriting an already-correct document. These tests
pin down that a confident "en" detection short-circuits the real translate
call, and that an uncertain detection still translates as before.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from artifice_ocr import config


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_skips_translation_when_source_confidently_english(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="en"))

    from artifice_ocr.stages import translate

    out_dir = tmp_path / "output"
    result = translate.perform(
        "This document is already in English.",
        source_file="doc.txt",
        output_dir=str(out_dir),
    )

    assert result["translated_text"] == "This document is already in English."
    assert result["source_language"] == "en"
    assert result["skipped_translation"] is True
    assert result["skip_reason"] == "source_already_english"

    # Only the language-detection call happened — no translate call, no
    # confidence self-assessment call (both would be wasted/misleading here).
    assert mock_chat.call_count == 1

    text_file = out_dir / "translated" / "text" / "doc.txt"
    assert text_file.read_text(encoding="utf-8") == "This document is already in English."

    data = json.loads((out_dir / "translated" / "json" / "doc.json").read_text(encoding="utf-8"))
    assert data["skipped_translation"] is True
    assert data["skip_reason"] == "source_already_english"
    assert "confidence" not in data


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_still_translates_when_detection_is_uncertain(mock_chat, tmp_path):
    # Garbled response -> detect_language() falls back to "unknown", which
    # must NOT skip translation — only a *confident* "en" does.
    mock_chat = mock_chat.return_value.chat
    mock_chat.side_effect = [
        MagicMock(message=MagicMock(content="not a language code")),
        MagicMock(message=MagicMock(content="Translated text")),
    ]

    from artifice_ocr.stages import translate

    result = translate.perform("Some text", output_dir=str(tmp_path))

    assert result["source_language"] == "unknown"
    assert result["translated_text"] == "Translated text"
    assert "skipped_translation" not in result
    # 2 chat calls (language detection + translation). Confidence
    # self-assessment now routes through model_harness.run_structured.
    assert mock_chat.call_count == 2


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_skip_behavior_can_be_disabled_via_config(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.side_effect = [
        MagicMock(message=MagicMock(content="en")),
        MagicMock(message=MagicMock(content="Rewritten by the model")),
    ]

    from artifice_ocr.stages import translate

    config.apply_overrides({"skip_translation_if_english": False})
    try:
        result = translate.perform("English source text", output_dir=str(tmp_path))
    finally:
        config.apply_overrides({"skip_translation_if_english": True})

    assert result["source_language"] == "en"
    assert result["translated_text"] == "Rewritten by the model"
    assert "skipped_translation" not in result
    # 2 chat calls (language detection + translation). Confidence
    # self-assessment now routes through model_harness.run_structured.
    assert mock_chat.call_count == 2


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_multi_lang_detection_parses_comma_separated_response(mock_chat):
    # multi_lang's own prompt asks for several comma-separated codes in
    # prevalence order; previously the comma made isalpha() fail and this
    # always fell back to "unknown".
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="de,en,fr"))

    from artifice_ocr.stages.translate import detect_language

    lang = detect_language("Some multilingual text", doc_type="multi_lang")
    assert lang == "de"


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_multi_lang_single_code_still_works(mock_chat):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="en"))

    from artifice_ocr.stages.translate import detect_language

    assert detect_language("English text", doc_type="multi_lang") == "en"
