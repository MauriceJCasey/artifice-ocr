# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-page temperature ladder on a repetition-guard rejection.

olmOCR 2 (arXiv:2510.19817 s4, "Dynamic temperature scaling"): resample the
SAME page at a rising temperature instead of discarding the whole document to
Tesseract on the first degenerate result.
"""

import pytest
from artifice_ocr.stages import ocr


def _cfg_from(mapping):
    return lambda key, default=None: mapping.get(key, default)


class _ScriptedClient:
    """Returns a different reply for each successive .chat() call."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.seen_temperatures = []

    def chat(self, *, model, messages, temperature, **kwargs):
        self.seen_temperatures.append(temperature)
        content = self._replies.pop(0)

        class _R:
            class message:
                pass

        _R.message.content = content
        return _R()


_LOOP_TEXT = "\n".join(["the same line over and over"] * 40)
_GOOD_TEXT = "\n".join([f"line {i} of real transcription" for i in range(40)])


def _patch_common(monkeypatch, cfg_overrides):
    monkeypatch.setattr(ocr, "cfg", _cfg_from(cfg_overrides))
    monkeypatch.setattr(ocr, "backend_for", lambda role: "ollama")
    monkeypatch.setattr(ocr, "model_for", lambda role: "richardyoung/olmocr2:7b-q8")
    monkeypatch.setattr(
        ocr,
        "_encode_image",
        lambda path, orientation=1, *, max_edge=None: ("YmFzZTY0", "image/png"),
    )


def test_ladder_disabled_keeps_temperature_zero_and_single_attempt(tmp_path, monkeypatch):
    client = _ScriptedClient([_GOOD_TEXT])
    _patch_common(monkeypatch, {"ocr_temperature_ladder_enabled": False})
    monkeypatch.setattr(ocr, "_get_backend_client", lambda backend: client)

    result = ocr._ocr_vision(tmp_path / "page.png")

    assert result == _GOOD_TEXT
    assert client.seen_temperatures == [0.0]


def test_ladder_escalates_temperature_on_repeated_rejection(tmp_path, monkeypatch):
    client = _ScriptedClient([_LOOP_TEXT, _LOOP_TEXT, _GOOD_TEXT])
    _patch_common(
        monkeypatch,
        {
            "ocr_temperature_ladder_enabled": True,
            "ocr_temperature_ladder_start": 0.1,
            "ocr_temperature_ladder_step": 0.1,
            "ocr_temperature_ladder_max": 0.8,
            "ocr_repetition_guard": True,
        },
    )
    monkeypatch.setattr(ocr, "_get_backend_client", lambda backend: client)

    result = ocr._ocr_vision(tmp_path / "page.png")

    assert result == _GOOD_TEXT
    assert client.seen_temperatures == pytest.approx([0.1, 0.2, 0.3])


def test_ladder_exhausted_raises_so_the_tesseract_fallback_can_catch_it(tmp_path, monkeypatch):
    replies = [_LOOP_TEXT] * 8  # more than enough rungs to exhaust 0.1..0.8
    client = _ScriptedClient(replies)
    _patch_common(
        monkeypatch,
        {
            "ocr_temperature_ladder_enabled": True,
            "ocr_temperature_ladder_start": 0.1,
            "ocr_temperature_ladder_step": 0.1,
            "ocr_temperature_ladder_max": 0.8,
            "ocr_repetition_guard": True,
        },
    )
    monkeypatch.setattr(ocr, "_get_backend_client", lambda backend: client)

    with pytest.raises(RuntimeError, match="repetition"):
        ocr._ocr_vision(tmp_path / "page.png")

    # 0.1, 0.2, ..., 0.8 inclusive = 8 rungs.
    assert len(client.seen_temperatures) == 8


class _AlwaysLoopsClient:
    """Answers every .chat() with repetition-loop text — an inexhaustible
    supply, so the only thing that can end a ladder run against it is the
    ladder logic itself, not a fixture running out of replies."""

    def __init__(self):
        self.seen_temperatures = []

    def chat(self, *, model, messages, temperature, **kwargs):
        self.seen_temperatures.append(temperature)
        if len(self.seen_temperatures) > 50:
            # Fail fast instead of hanging: a ladder that keeps calling past
            # this bound is the unbounded loop this test exists to catch.
            # AssertionError is not in @retry's retryable set, so it escapes
            # _ocr_vision immediately rather than being retried.
            raise AssertionError(
                f"ladder executed more than 50 rungs (last temperature "
                f"{temperature!r}) — the loop is unbounded"
            )

        class _R:
            class message:
                pass

        _R.message.content = _LOOP_TEXT
        return _R()


def test_negative_ladder_step_still_terminates(tmp_path, monkeypatch):
    """A malformed negative step must not become an infinite loop.

    With ``ocr_temperature_ladder_step < 0`` the temperature moves AWAY from
    the ceiling on every rung, so the ``temperature <= ceiling`` condition
    never becomes false. The ladder must be bounded by construction, not by
    the sanity of the configured step (Copilot review, PR #101).
    """
    client = _AlwaysLoopsClient()
    _patch_common(
        monkeypatch,
        {
            "ocr_temperature_ladder_enabled": True,
            "ocr_temperature_ladder_start": 0.1,
            "ocr_temperature_ladder_step": -0.1,
            "ocr_temperature_ladder_max": 0.8,
            "ocr_repetition_guard": True,
        },
    )
    monkeypatch.setattr(ocr, "_get_backend_client", lambda backend: client)

    with pytest.raises(RuntimeError, match="repetition"):
        ocr._ocr_vision(tmp_path / "page.png")

    assert len(client.seen_temperatures) <= 50


def test_single_image_falls_back_to_tesseract_when_ladder_exhausted(tmp_path, monkeypatch):
    """End-to-end through _ocr_single_image: exhausted ladder -> tesseract-fallback."""
    replies = [_LOOP_TEXT] * 8
    client = _ScriptedClient(replies)
    _patch_common(
        monkeypatch,
        {
            "ocr_temperature_ladder_enabled": True,
            "ocr_temperature_ladder_start": 0.1,
            "ocr_temperature_ladder_step": 0.1,
            "ocr_temperature_ladder_max": 0.8,
            "ocr_repetition_guard": True,
            "ocr_engine": "vision_model",
            "tesseract_fallback_on_failure": True,
        },
    )
    monkeypatch.setattr(ocr, "_get_backend_client", lambda backend: client)
    monkeypatch.setattr(ocr._tesseract, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "_tesseract_from_image", lambda path, orientation=1: "tesseract text")

    text, engine = ocr._ocr_single_image(tmp_path / "page.png")

    assert text == "tesseract text"
    assert engine == "tesseract-fallback"
