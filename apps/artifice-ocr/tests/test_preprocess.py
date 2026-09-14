# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the deterministic OCR image pre-processing stage (Phase 1).

The whole point of this stage being model-free is that it is testable without a
model: given a synthetic washed-out image, the pipeline should measurably darken
the ink and widen the tonal range, and it must be a strict no-op when disabled.
"""

import io

import numpy as np
from artifice_ocr.stages import preprocess
from PIL import Image


def _washed_page() -> bytes:
    """A pale-grey 'page' with faint, only-slightly-darker 'text' bars — the
    low-contrast, over-exposed case that defeats a vision model."""
    arr = np.full((200, 200), 235, dtype=np.uint8)  # bright near-white paper
    arr[40:60, 20:180] = 200  # faint text rows, only 35 levels below paper
    arr[90:110, 20:180] = 200
    arr[140:160, 20:180] = 200
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def _decode(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as im:
        return np.asarray(im.convert("L"), dtype=np.uint8)


def test_maybe_process_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(preprocess, "is_enabled", lambda: False)
    assert preprocess.maybe_process(_washed_page()) is None


def test_maybe_process_returns_png_when_enabled(monkeypatch):
    monkeypatch.setattr(preprocess, "is_enabled", lambda: True)
    out = preprocess.maybe_process(_washed_page())
    assert out is not None
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "PNG"


def test_processing_widens_contrast(monkeypatch):
    """Auto-contrast + illumination normalisation should stretch the tonal
    range so faint text separates from paper far more than in the original."""
    monkeypatch.setattr(preprocess, "is_enabled", lambda: True)
    # Default config: grayscale + illumination + autocontrast on, gamma 1.0.
    monkeypatch.setattr(
        preprocess,
        "cfg",
        lambda key, default=None: {
            "preprocess_grayscale": True,
            "preprocess_illumination": True,
            "preprocess_autocontrast": True,
            "preprocess_gamma": 1.0,
        }.get(key, default),
    )

    original = _decode(_washed_page())
    processed = _decode(preprocess.maybe_process(_washed_page()))

    assert np.ptp(processed) > np.ptp(original)  # wider min..max spread
    assert processed.std() > original.std()  # more separation overall


def test_gamma_out_of_range_is_ignored(monkeypatch):
    """A nonsensical gamma must not raise into the OCR run — it is dropped."""
    monkeypatch.setattr(
        preprocess,
        "cfg",
        lambda key, default=None: {
            "preprocess_gamma": 999.0,
        }.get(key, default),
    )
    assert preprocess._gamma_value() is None


def test_decode_failure_falls_back_to_original(monkeypatch):
    """Undecodable bytes must degrade to the original image, never fail the
    page."""
    monkeypatch.setattr(preprocess, "is_enabled", lambda: True)
    assert preprocess.maybe_process(b"not an image") is None


def test_process_pil_returns_single_channel(monkeypatch):
    monkeypatch.setattr(preprocess, "cfg", lambda key, default=None: default)
    colour = Image.new("RGB", (32, 32), (180, 190, 200))
    out = preprocess.process_pil(colour)
    assert out.mode == "L"
