# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Configurable per-collection domain instruction, appended to OCR_PROMPT.
[CENT] (arXiv:2608.30616) Table 4: the single largest measured, zero-training
accuracy win in the source material for this plan."""

from artifice_ocr.stages import ocr


def _cfg_from(mapping):
    return lambda key, default=None: mapping.get(key, default)


def test_empty_instruction_leaves_prompt_unchanged():
    assert ocr._effective_prompt("") == ocr.OCR_PROMPT


def test_instruction_is_appended_not_replacing_the_base_prompt():
    prompt = ocr._effective_prompt(
        "This is a 19th-century field catalogue in German Kurrentschrift."
    )
    assert prompt.startswith(ocr.OCR_PROMPT)
    assert "19th-century field catalogue" in prompt


def test_vision_call_uses_the_effective_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ocr,
        "cfg",
        _cfg_from(
            {
                "ocr_prompt_instruction": "Expect handwritten German.",
                "ocr_temperature_ladder_enabled": False,
            }
        ),
    )
    monkeypatch.setattr(ocr, "backend_for", lambda role: "ollama")
    monkeypatch.setattr(ocr, "model_for", lambda role: "richardyoung/olmocr2:7b-q8")
    monkeypatch.setattr(
        ocr,
        "_encode_image",
        lambda path, orientation=1, *, max_edge=None: ("YmFzZTY0", "image/png"),
    )

    seen = {}

    class _Client:
        def chat(self, *, model, messages, temperature, **kwargs):
            seen["prompt_text"] = messages[0]["content"][0]["text"]

            class _R:
                class message:
                    content = "text"

            return _R()

    monkeypatch.setattr(ocr, "_get_backend_client", lambda backend: _Client())
    ocr._ocr_vision(tmp_path / "page.png")

    assert "Expect handwritten German." in seen["prompt_text"]
