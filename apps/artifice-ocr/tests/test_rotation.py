# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Auto-rotation detection via Tesseract OSD (arXiv:2510.19817 s4 and the ACL
2026 Demo paper s6 both list automatic rotation correction among the
deployment fixes). Only probed when Tropy's own orientation says "normal" —
see stages/ocr.py::_exif_orientation_matrix's docstring for the real failure
this closes: a page scanned upside-down with no metadata flagging it."""

from unittest.mock import MagicMock

from artifice_ocr import _rotation


def _osd_stdout(rotate_degrees: int) -> str:
    return (
        "Page number: 0\n"
        "Orientation in degrees: 0\n"
        f"Rotate: {rotate_degrees}\n"
        "Orientation confidence: 8.45\n"
        "Script: Latin\n"
        "Script confidence: 4.35\n"
    )


def test_no_rotation_needed_returns_none(monkeypatch):
    monkeypatch.setattr(_rotation._tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(
        _rotation.subprocess,
        "run",
        lambda *a, **k: MagicMock(returncode=0, stdout=_osd_stdout(0), stderr=""),
    )

    assert _rotation.detect_orientation(b"fake-png-bytes") is None


def test_upside_down_page_maps_to_orientation_3(monkeypatch):
    monkeypatch.setattr(_rotation._tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(
        _rotation.subprocess,
        "run",
        lambda *a, **k: MagicMock(returncode=0, stdout=_osd_stdout(180), stderr=""),
    )

    assert _rotation.detect_orientation(b"fake-png-bytes") == 3


def test_rotated_90_maps_to_orientation_6(monkeypatch):
    monkeypatch.setattr(_rotation._tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(
        _rotation.subprocess,
        "run",
        lambda *a, **k: MagicMock(returncode=0, stdout=_osd_stdout(90), stderr=""),
    )

    assert _rotation.detect_orientation(b"fake-png-bytes") == 6


def test_rotated_270_maps_to_orientation_8(monkeypatch):
    monkeypatch.setattr(_rotation._tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(
        _rotation.subprocess,
        "run",
        lambda *a, **k: MagicMock(returncode=0, stdout=_osd_stdout(270), stderr=""),
    )

    assert _rotation.detect_orientation(b"fake-png-bytes") == 8


def test_no_tesseract_returns_none(monkeypatch):
    monkeypatch.setattr(_rotation._tesseract, "resolve_binary", lambda: None)

    assert _rotation.detect_orientation(b"fake-png-bytes") is None


def test_osd_failure_returns_none_not_raise(monkeypatch):
    """Low-text pages make OSD fail (`Too few characters`) — must degrade, not crash a page."""
    monkeypatch.setattr(_rotation._tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(
        _rotation.subprocess,
        "run",
        lambda *a, **k: MagicMock(
            returncode=1, stdout="", stderr="Too few characters. Skipping this page"
        ),
    )

    assert _rotation.detect_orientation(b"fake-png-bytes") is None


def test_perform_probes_rotation_only_when_orientation_is_normal(tmp_path, monkeypatch):
    from artifice_ocr.stages import ocr

    calls = []
    monkeypatch.setattr(
        ocr,
        "cfg",
        lambda key, default=None: {
            "ocr_auto_rotation_detect": True,
            "ocr_repetition_guard": False,
        }.get(key, default),
    )
    monkeypatch.setattr(ocr, "_ocr_single_image", lambda path, orientation=1: ("text", "ollama"))
    monkeypatch.setattr(_rotation, "detect_orientation", lambda data: calls.append(data) or 3)

    path = tmp_path / "page.jpg"
    path.write_bytes(b"fake-jpeg-bytes")
    ocr.perform(str(path), output_dir=str(tmp_path / "out"), orientation=1)

    assert len(calls) == 1  # probed, because orientation was 1 (normal)


def test_perform_skips_the_probe_when_orientation_already_set(tmp_path, monkeypatch):
    from artifice_ocr.stages import ocr

    calls = []
    monkeypatch.setattr(
        ocr,
        "cfg",
        lambda key, default=None: {
            "ocr_auto_rotation_detect": True,
            "ocr_repetition_guard": False,
        }.get(key, default),
    )
    monkeypatch.setattr(ocr, "_ocr_single_image", lambda path, orientation=1: ("text", "ollama"))
    monkeypatch.setattr(_rotation, "detect_orientation", lambda data: calls.append(data) or 3)

    path = tmp_path / "page.jpg"
    path.write_bytes(b"fake-jpeg-bytes")
    ocr.perform(str(path), output_dir=str(tmp_path / "out"), orientation=6)

    assert calls == []  # not probed — orientation was already explicit


def test_perform_skips_the_rotation_probe_for_pdfs(tmp_path, monkeypatch):
    """detect_orientation() is an image-bytes probe — it writes whatever
    bytes it is given to a .png-suffixed temp file for Tesseract. Feeding it
    raw PDF bytes is wasted work and noisy (and a PDF's pages are rendered
    to proper images later anyway), so a .pdf path must never be probed,
    even with orientation=1 and detection enabled (Copilot review, PR #101).
    """
    from artifice_ocr.stages import ocr

    calls = []
    monkeypatch.setattr(
        ocr,
        "cfg",
        lambda key, default=None: {
            "ocr_auto_rotation_detect": True,
            "ocr_repetition_guard": False,
        }.get(key, default),
    )
    monkeypatch.setattr(ocr, "_ocr_single_image", lambda path, orientation=1: ("text", "ollama"))
    monkeypatch.setattr(
        ocr, "_pdf_to_page_images", lambda path, orientation=1: [tmp_path / "page_0001.png"]
    )
    monkeypatch.setattr(_rotation, "detect_orientation", lambda data: calls.append(data) or 3)

    path = tmp_path / "document.pdf"
    path.write_bytes(b"fake-pdf-bytes")
    ocr.perform(str(path), output_dir=str(tmp_path / "out"), orientation=1)

    assert calls == []  # not probed — PDF bytes are not image bytes
