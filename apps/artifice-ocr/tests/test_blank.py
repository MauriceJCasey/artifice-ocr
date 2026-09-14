# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Near-blank page detection: olmOCR 2 (arXiv:2510.19817 s4, "Handle blank
pages") — a model never trained on blank pages hallucinates rather than
returning nothing. A cheap greyscale-variance check skips the call entirely
for a near-blank verso."""

import io

from artifice_ocr._blank import is_near_blank
from PIL import Image


def _solid_png(width, height, color) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _noisy_png(width, height) -> bytes:
    import numpy as np

    arr = (np.random.default_rng(0).random((height, width)) * 255).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def test_solid_white_page_is_blank():
    assert is_near_blank(_solid_png(600, 800, 255)) is True


def test_solid_black_page_is_blank():
    assert is_near_blank(_solid_png(600, 800, 0)) is True


def test_typescript_like_noise_is_not_blank():
    assert is_near_blank(_noisy_png(600, 800)) is False


def test_undecodable_bytes_are_not_treated_as_blank():
    """A decode failure must never silently skip a real page."""
    assert is_near_blank(b"not an image") is False


def test_ocr_single_image_skips_the_call_on_a_blank_page(tmp_path, monkeypatch):
    from artifice_ocr.stages import ocr

    path = tmp_path / "blank.png"
    path.write_bytes(_solid_png(600, 800, 255))

    monkeypatch.setattr(
        ocr,
        "cfg",
        lambda key, default=None: {"ocr_blank_page_skip": True}.get(key, default),
    )

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("OCR engine must not be called for a blank page")

    monkeypatch.setattr(ocr, "_ocr_vision", _fail_if_called)
    monkeypatch.setattr(ocr._tesseract, "ocr_bytes", _fail_if_called)

    text, engine = ocr._ocr_single_image(path)

    assert text == ""
    assert engine == "blank-skip"
