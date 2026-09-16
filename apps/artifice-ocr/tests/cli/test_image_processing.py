# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""PDF OCR, orientation correction, and OCR degeneracy-guard integration tests."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .test_stages import _mock_backend_response

# ---------------------------------------------------------------------------
# PDF OCR tests
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.ocr._ocr_single_image")
@patch("artifice_ocr.stages.ocr._pdf_to_page_images")
def test_pdf_ocr_concatenates_pages(mock_pages, mock_ocr, tmp_path):
    mock_pages.return_value = [tmp_path / "p1.png", tmp_path / "p2.png"]
    # _ocr_single_image now returns (text, engine).
    mock_ocr.side_effect = [("Text from page 1", "ollama"), ("Text from page 2", "ollama")]

    from artifice_ocr.stages import ocr

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"fake pdf")

    out_dir = tmp_path / "output"
    result = ocr.perform(str(pdf_path), output_dir=str(out_dir))

    assert "Page Break" in result["extracted_text"]
    assert "Text from page 1" in result["extracted_text"]
    assert "Text from page 2" in result["extracted_text"]
    assert result["total_pages"] == 2
    assert mock_ocr.call_count == 2


@patch("artifice_ocr.stages.ocr._ocr_single_image")
def test_single_image_ocr_sets_total_pages_1(mock_ocr, tmp_path):
    mock_ocr.return_value = ("Single page text", "ollama")

    from artifice_ocr.stages import ocr

    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG fake")

    result = ocr.perform(str(img), output_dir=str(tmp_path / "out"))
    assert result["total_pages"] == 1


# ---------------------------------------------------------------------------
# Tropy orientation correction
# ---------------------------------------------------------------------------
#
# Confirmed on a real archive page: Tropy's own `photos.orientation` column
# said 1 (normal — nobody had flagged the scan), and the file's own EXIF had
# no orientation tag either, yet the page was genuinely scanned upside-down.
# Once a caller (jobs.py, from `item.source["orientation"]`) does know the
# correct value, `perform()` must actually apply it before the model ever
# sees the image.


def _make_test_image(path: Path, width=60, height=90) -> None:
    """A small, real, fitz-openable PNG — not a fake byte stub — since these
    tests exercise the actual rotation matrix, not just a mocked pipeline."""
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((5, 15), "T", fontsize=12)  # asymmetric mark
    pix = page.get_pixmap()
    pix.save(str(path))
    doc.close()


def test_exif_orientation_matrix_returns_none_for_normal():
    from artifice_ocr.stages import ocr

    assert ocr._exif_orientation_matrix(1, 100, 150) is None


def test_exif_orientation_matrix_returns_none_for_unrecognised_value():
    from artifice_ocr.stages import ocr

    assert ocr._exif_orientation_matrix(99, 100, 150) is None


def test_exif_orientation_matrix_covers_every_valid_exif_value():
    from artifice_ocr.stages import ocr

    for orientation in range(2, 9):
        assert ocr._exif_orientation_matrix(orientation, 100, 150) is not None


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_applies_orientation_correction_before_encoding(mock_get_client, tmp_path):
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("text")
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    img = tmp_path / "scan.png"
    _make_test_image(img)
    out_dir = tmp_path / "out"

    ocr.perform(str(img), output_dir=str(out_dir), orientation=3, stem="rotated")
    rotated_url = mock_client.chat.call_args.kwargs["messages"][0]["content"][1]["image_url"]["url"]

    mock_client.chat.reset_mock()
    ocr.perform(str(img), output_dir=str(out_dir), orientation=1, stem="normal")
    normal_url = mock_client.chat.call_args.kwargs["messages"][0]["content"][1]["image_url"]["url"]

    assert rotated_url.startswith("data:image/png;base64,")
    assert rotated_url != normal_url  # the bytes actually changed, not just relabelled


def test_encode_image_is_untouched_when_preprocess_disabled(tmp_path, monkeypatch):
    """With pre-processing off (the default), _encode_image returns the raw
    file bytes and original mime — no behaviour change for existing users."""
    from artifice_ocr.stages import ocr, preprocess

    monkeypatch.setattr(preprocess, "is_enabled", lambda: False)
    img = tmp_path / "scan.png"
    _make_test_image(img)

    b64, mime = ocr._encode_image(img)
    assert mime == "image/png"
    import base64

    with open(img, "rb") as f:
        raw = f.read()
    assert base64.standard_b64decode(b64) == raw  # bytes passed through unchanged


def test_encode_image_applies_preprocessing_when_enabled(tmp_path, monkeypatch):
    """With pre-processing on, _encode_image funnels the image through the
    stage and the emitted bytes differ from the raw file."""
    from artifice_ocr.stages import ocr, preprocess

    monkeypatch.setattr(preprocess, "is_enabled", lambda: True)
    img = tmp_path / "scan.png"
    _make_test_image(img)

    b64, mime = ocr._encode_image(img)
    assert mime == "image/png"
    import base64

    with open(img, "rb") as f:
        raw = f.read()
    assert base64.standard_b64decode(b64) != raw  # the stage actually ran


# ---------------------------------------------------------------------------
# OCR degeneracy guard integration
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_rejects_a_repetition_loop(mock_get_client, tmp_path):
    from artifice_ocr import config

    mock_client = MagicMock()
    looped_text = "\n\n".join(["Same hallucinated sentence over and over."] * 40)
    mock_client.chat.return_value = _mock_backend_response(looped_text)
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    # The per-page temperature ladder (on by default) owns repetition-guard
    # rejections before perform() ever sees the text. This test exercises
    # perform()'s document-level guard path, so disable the ladder first:
    # the mocked client's single call at temperature=0.0 returns the looped
    # text and perform()'s own check_no_repetition_loop rejects it.
    # Restore the CAPTURED prior value, not a hardcoded True — otherwise this
    # test forces the ladder on for every test that runs after it, making the
    # suite order-dependent.
    prior = config.get("ocr_temperature_ladder_enabled")
    config.apply_overrides({"ocr_temperature_ladder_enabled": False})
    try:
        img = tmp_path / "bad_scan.png"
        img.write_bytes(b"\x89PNG fake")
        out_dir = tmp_path / "output"

        with pytest.raises(RuntimeError, match="OCR rejected"):
            ocr.perform(str(img), output_dir=str(out_dir), stem="bad_scan")

        # A rejected page has nothing safe to write as "the" text.
        assert not (out_dir / "raw_ocr" / "text" / "bad_scan.txt").exists()

        # But the JSON is kept for forensic review, with the verdict + the text
        # that was rejected, mirroring structure.py's rejected_structured_text.
        data = json.loads(
            (out_dir / "raw_ocr" / "json" / "bad_scan.json").read_text(encoding="utf-8")
        )
        assert data["guard"]["ok"] is False
        assert data["rejected_extracted_text"] == looped_text
    finally:
        config.apply_overrides({"ocr_temperature_ladder_enabled": prior})


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_repetition_guard_can_be_disabled(mock_get_client, tmp_path):
    from artifice_ocr import config

    mock_client = MagicMock()
    looped_text = "\n\n".join(["Same hallucinated sentence over and over."] * 40)
    mock_client.chat.return_value = _mock_backend_response(looped_text)
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    config.apply_overrides({"ocr_repetition_guard": False})
    try:
        img = tmp_path / "bad_scan.png"
        img.write_bytes(b"\x89PNG fake")
        result = ocr.perform(str(img), output_dir=str(tmp_path / "output"), stem="bad_scan")
        assert result["extracted_text"] == looped_text
    finally:
        config.apply_overrides({"ocr_repetition_guard": True})


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_accepts_real_varied_text(mock_get_client, tmp_path):
    mock_client = MagicMock()
    real_text = "\n\n".join(f"Genuinely distinct sentence number {i}." for i in range(40))
    mock_client.chat.return_value = _mock_backend_response(real_text)
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    img = tmp_path / "good_scan.png"
    img.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    result = ocr.perform(str(img), output_dir=str(out_dir), stem="good_scan")

    assert result["extracted_text"] == real_text
    assert result["guard"]["ok"] is True
    assert (out_dir / "raw_ocr" / "text" / "good_scan.txt").exists()
