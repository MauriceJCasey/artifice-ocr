# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The image budget sent to the vision model, and the context window it needs.

Regression cover for a real Windows failure: a 4653x3445 archive scan was sent
to ``richardyoung/olmocr2:7b-q8`` at full native resolution, needed 4145 tokens,
and overflowed Ollama's default 4096-token window. The run then failed with an
unrelated ``AttributeError`` from the Tesseract fallback, hiding the cause.

olmOCR-2 is built on Qwen2.5-VL, which tiles at native resolution up to its
``max_pixels`` — so an unresized scan is both slower and off the distribution
the model was trained on. olmOCR 2 (arXiv:2510.19817) section 4 records 1288px
on the longest edge as the swept optimum.
"""

import base64
import io

from artifice_ocr.stages import ocr
from PIL import Image


def _cfg_from(mapping):
    return lambda key, default=None: mapping.get(key, default)


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _decode(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.standard_b64decode(b64)))


def test_oversized_page_is_capped_to_the_configured_longest_edge(tmp_path, monkeypatch):
    """A 4653x3445 scan — the real failing page — must be capped at 1288px."""
    path = tmp_path / "scan.png"
    path.write_bytes(_png(4653, 3445))
    monkeypatch.setattr(ocr, "cfg", _cfg_from({"ocr_max_image_edge": 1288}))

    b64, mime = ocr._encode_image(path, max_edge=ocr._configured_max_image_edge())
    img = _decode(b64)

    assert max(img.size) == 1288
    # Aspect ratio preserved: 3445 * 1288 / 4653 = 953.61 -> 954.
    assert img.size == (1288, 954)
    assert mime == "image/png"


def test_small_page_is_never_upscaled(tmp_path, monkeypatch):
    """Enlarging a small scan invents detail and costs tokens for nothing."""
    path = tmp_path / "small.png"
    path.write_bytes(_png(800, 600))
    monkeypatch.setattr(ocr, "cfg", _cfg_from({"ocr_max_image_edge": 1288}))

    b64, _mime = ocr._encode_image(path, max_edge=ocr._configured_max_image_edge())
    assert _decode(b64).size == (800, 600)


def test_zero_disables_the_cap(tmp_path, monkeypatch):
    """``0`` must restore the previous full-resolution behaviour exactly."""
    path = tmp_path / "scan.png"
    path.write_bytes(_png(4653, 3445))
    monkeypatch.setattr(ocr, "cfg", _cfg_from({"ocr_max_image_edge": 0}))

    assert ocr._configured_max_image_edge() is None
    b64, _mime = ocr._encode_image(path, max_edge=None)
    assert _decode(b64).size == (4653, 3445)


def test_malformed_setting_falls_back_to_no_cap(tmp_path, monkeypatch):
    """A bad setting must not fail a page; it degrades to the old behaviour."""
    monkeypatch.setattr(ocr, "cfg", _cfg_from({"ocr_max_image_edge": "not-a-number"}))
    assert ocr._configured_max_image_edge() is None


def test_tesseract_path_is_not_capped(tmp_path, monkeypatch):
    """1288px is a Qwen2.5-VL constraint, not a general one.

    Tesseract benefits from *more* resolution, so the fallback must keep
    sending full-resolution bytes even while the vision path is capped.
    """
    path = tmp_path / "scan.png"
    path.write_bytes(_png(4653, 3445))
    monkeypatch.setattr(ocr, "cfg", _cfg_from({"ocr_max_image_edge": 1288}))

    seen = {}

    def fake_ocr_bytes(data, **kwargs):
        seen["size"] = Image.open(io.BytesIO(data)).size
        return "text"

    monkeypatch.setattr(ocr._tesseract, "ocr_bytes", fake_ocr_bytes)
    ocr._tesseract_from_image(path)
    assert seen["size"] == (4653, 3445)


def test_vision_call_applies_the_cap(tmp_path, monkeypatch):
    """The cap has to be wired into the actual vision request, not just available."""
    path = tmp_path / "scan.png"
    path.write_bytes(_png(4653, 3445))
    monkeypatch.setattr(ocr, "cfg", _cfg_from({"ocr_max_image_edge": 1288}))
    monkeypatch.setattr(ocr, "backend_for", lambda role: "ollama")
    monkeypatch.setattr(ocr, "model_for", lambda role: "richardyoung/olmocr2:7b-q8")

    seen = {}

    class _Client:
        def chat(self, *, model, messages, **kwargs):
            url = messages[0]["content"][1]["image_url"]["url"]
            seen["size"] = _decode(url.split("base64,", 1)[1]).size

            class _R:
                class message:
                    content = "text"

            return _R()

    monkeypatch.setattr(ocr, "_get_backend_client", lambda backend: _Client())
    ocr._ocr_vision(path)
    assert max(seen["size"]) == 1288


def test_default_context_size_leaves_room_for_a_capped_page():
    """The shipped default must not put Ollama back on its 4096 default.

    A 1288px page still costs well over 1000 visual tokens; 4096 left only
    ~4145 of headroom on the page that triggered this work, i.e. none.
    """
    from artifice_ocr.config import _DEFAULTS

    assert _DEFAULTS["context_size"] >= 8192


# --------------------------------------------------------------------------- #
# The live gate's own page — why this bug reached a user through a green gate
# --------------------------------------------------------------------------- #


def test_live_gate_page_is_large_enough_to_have_failed(
    tmp_path, archive_resolution_page, live_gate_page_spec
):
    """The live gate must send a page in the failing size class.

    This is the guard the gate was missing. Both live interop tests now build
    their page through ``make_archive_resolution_page``; if someone shrinks it
    back — or points the gate at the raw fixture again — this fails in the fast
    suite rather than silently narrowing the release gate to pages that cannot
    overflow a context window.
    """
    page = archive_resolution_page(tmp_path / "page.jpg")
    with Image.open(page) as img:
        assert max(img.size) >= live_gate_page_spec["longest_edge"], (
            f"longest edge too small: {img.size}"
        )
        assert img.width * img.height >= live_gate_page_spec["min_pixels"], (
            f"too few pixels: {img.size}"
        )


def test_committed_fixture_alone_could_not_have_caught_the_overflow(live_gate_page_spec):
    """Documents the gap, so the reasoning is not lost with this conversation.

    ``proceedings_usnm_173.jpg`` is a perfectly good OCR fixture and stays the
    source image. It is simply far too small — by roughly 7.5x in pixel count —
    to reach the token budget that broke a real archive scan. A gate built only
    on it reports green for a bug that is live in the field.
    """
    with Image.open(live_gate_page_spec["fixture"]) as img:
        fixture_pixels = img.width * img.height

    assert fixture_pixels < live_gate_page_spec["min_pixels"] / 5, (
        "the committed fixture is now large enough to overflow on its own — "
        "if that is deliberate, this test and the upscaling helper can go"
    )
