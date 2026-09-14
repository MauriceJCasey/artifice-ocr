# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the Tesseract engine, its detection, and the vision→Tesseract
fallback in the OCR stage. Everything is mocked — no real Tesseract binary is
required in CI."""

from pathlib import Path

import pytest
from artifice_ocr import _tesseract


def _cfg_from(mapping):
    return lambda key, default=None: mapping.get(key, default)


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_resolve_binary_prefers_configured_path(monkeypatch, tmp_path):
    fake = tmp_path / "tesseract"
    fake.write_text("")
    monkeypatch.setattr(_tesseract, "cfg", _cfg_from({"tesseract_path": str(fake)}))
    assert _tesseract.resolve_binary() == str(fake)


def test_resolve_binary_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(_tesseract, "cfg", _cfg_from({}))
    monkeypatch.setattr(
        _tesseract.shutil,
        "which",
        lambda name: "/usr/bin/tesseract" if name == "tesseract" else None,
    )
    assert _tesseract.resolve_binary() == "/usr/bin/tesseract"


def test_resolve_binary_not_found(monkeypatch):
    monkeypatch.setattr(_tesseract, "cfg", _cfg_from({}))
    monkeypatch.setattr(_tesseract.shutil, "which", lambda name: None)
    # Neutralise the Windows fallback locations so this is deterministic on any OS.
    monkeypatch.setattr(_tesseract, "_WINDOWS_FALLBACK_PATHS", ())
    assert _tesseract.resolve_binary() is None
    assert _tesseract.is_available() is False


def test_status_reports_availability_and_lang(monkeypatch):
    monkeypatch.setattr(_tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(_tesseract, "version", lambda binary=None: "tesseract 5.3.3")
    monkeypatch.setattr(_tesseract, "cfg", _cfg_from({"tesseract_lang": "deu"}))
    s = _tesseract.status()
    assert s == {
        "available": True,
        "path": "/usr/bin/tesseract",
        "version": "tesseract 5.3.3",
        "lang": "deu",
    }


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


def test_ocr_bytes_returns_stdout(monkeypatch):
    monkeypatch.setattr(_tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(_tesseract, "cfg", _cfg_from({"tesseract_lang": "eng"}))
    monkeypatch.setattr(_tesseract.subprocess, "run", lambda *a, **k: _Proc(0, "hello world\n"))
    assert _tesseract.ocr_bytes(b"pngbytes") == "hello world\n"


def test_ocr_bytes_unavailable_raises(monkeypatch):
    monkeypatch.setattr(_tesseract, "resolve_binary", lambda: None)
    with pytest.raises(_tesseract.TesseractUnavailable):
        _tesseract.ocr_bytes(b"pngbytes")


def test_ocr_bytes_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(_tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(_tesseract, "cfg", _cfg_from({}))
    monkeypatch.setattr(_tesseract.subprocess, "run", lambda *a, **k: _Proc(1, "", "bad langpack"))
    with pytest.raises(_tesseract.TesseractError, match="bad langpack"):
        _tesseract.ocr_bytes(b"pngbytes")


def test_ocr_bytes_passes_configured_lang(monkeypatch):
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _Proc(0, "text")

    monkeypatch.setattr(_tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(_tesseract, "cfg", _cfg_from({"tesseract_lang": "deu+eng"}))
    monkeypatch.setattr(_tesseract.subprocess, "run", fake_run)
    _tesseract.ocr_bytes(b"pngbytes")
    assert "-l" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-l") + 1] == "deu+eng"


def test_ocr_bytes_decodes_as_utf8_not_the_windows_locale(monkeypatch):
    """Tesseract writes UTF-8; the subprocess must be told so explicitly.

    Regression: ``subprocess.run(..., text=True)`` with no ``encoding`` decodes
    with ``locale.getpreferredencoding()``, which is cp1252 on a stock Windows
    install. A single byte outside cp1252 (0x9d) raised ``UnicodeDecodeError``
    *inside the subprocess reader thread*, where it is not propagated —
    ``proc.stdout`` silently became ``None`` and the caller crashed on
    ``.strip()``. Observed on a real frozen Windows build.
    """
    captured = {}

    def fake_run(cmd, *a, **k):
        captured.update(k)
        return _Proc(0, "text")

    monkeypatch.setattr(_tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(_tesseract, "cfg", _cfg_from({}))
    monkeypatch.setattr(_tesseract.subprocess, "run", fake_run)
    _tesseract.ocr_bytes(b"pngbytes")
    assert captured.get("encoding") == "utf-8"
    # An undecodable byte must degrade to a replacement character, never kill
    # the reader thread.
    assert captured.get("errors") == "replace"


def test_version_decodes_as_utf8_not_the_windows_locale(monkeypatch):
    """``version()`` shares the same trap and the same fix."""
    captured = {}

    def fake_run(cmd, *a, **k):
        captured.update(k)
        return _Proc(0, "tesseract 5.3.3\n")

    monkeypatch.setattr(_tesseract.subprocess, "run", fake_run)
    assert _tesseract.version("/usr/bin/tesseract") == "tesseract 5.3.3"
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"


def test_ocr_bytes_returns_empty_string_when_stdout_is_none(monkeypatch):
    """``proc.stdout`` can be ``None`` even on a zero exit code.

    Returning it unguarded is what turned a legible context-overflow error into
    ``'NoneType' object has no attribute 'strip'`` three frames later. The
    annotated contract is ``-> str``; honour it.
    """
    monkeypatch.setattr(_tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(_tesseract, "cfg", _cfg_from({}))
    monkeypatch.setattr(_tesseract.subprocess, "run", lambda *a, **k: _Proc(0, None))
    assert _tesseract.ocr_bytes(b"pngbytes") == ""


def test_dispatch_reraises_original_error_when_tesseract_returns_none(monkeypatch):
    """The fallback must not mask the vision failure with an AttributeError.

    End-to-end shape of the observed Windows bug: the vision call fails with an
    actionable context-overflow message, Tesseract then yields nothing usable,
    and the user must still be shown the *vision* error.
    """
    from artifice_ocr.stages import ocr

    monkeypatch.setattr(ocr, "cfg", _cfg_from({"tesseract_fallback_on_failure": True}))
    monkeypatch.setattr(ocr._tesseract, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "_tesseract_from_image", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("The page needs 4145 tokens but the context window is 4096.")

    monkeypatch.setattr(ocr, "_ocr_vision", boom)
    with pytest.raises(RuntimeError, match="4145 tokens"):
        ocr._ocr_single_image(Path("page.png"))


def test_tesseract_subprocesses_hide_windows_console(monkeypatch):
    monkeypatch.setattr(_tesseract.os, "name", "nt")
    monkeypatch.setattr(_tesseract.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert _tesseract._subprocess_window_options() == {"creationflags": 0x08000000}


def test_tesseract_subprocesses_need_no_flags_off_windows(monkeypatch):
    monkeypatch.setattr(_tesseract.os, "name", "posix")
    assert _tesseract._subprocess_window_options() == {}


# --------------------------------------------------------------------------- #
# Engine dispatch + fallback in the OCR stage
# --------------------------------------------------------------------------- #


def test_dispatch_primary_tesseract(monkeypatch):
    from artifice_ocr.stages import ocr

    monkeypatch.setattr(ocr, "cfg", _cfg_from({"ocr_engine": "tesseract"}))
    monkeypatch.setattr(ocr, "_tesseract_from_image", lambda p, o=1: "tess text")
    assert ocr._ocr_single_image(Path("x.png")) == ("tess text", "tesseract")


def test_dispatch_vision_success(monkeypatch):
    from artifice_ocr.stages import ocr

    monkeypatch.setattr(ocr, "cfg", _cfg_from({"ocr_engine": "vision_model"}))
    monkeypatch.setattr(ocr, "_ocr_vision", lambda p, o=1: "vision text")
    monkeypatch.setattr(ocr, "backend_for", lambda kind: "ollama")
    assert ocr._ocr_single_image(Path("x.png")) == ("vision text", "ollama")


def test_dispatch_falls_back_to_tesseract_on_vision_failure(monkeypatch):
    from artifice_ocr.stages import ocr

    monkeypatch.setattr(
        ocr,
        "cfg",
        _cfg_from({"ocr_engine": "vision_model", "tesseract_fallback_on_failure": True}),
    )

    def boom(p, o=1):
        raise RuntimeError("vision down")

    monkeypatch.setattr(ocr, "_ocr_vision", boom)
    monkeypatch.setattr(ocr._tesseract, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "_tesseract_from_image", lambda p, o=1: "recovered")
    assert ocr._ocr_single_image(Path("x.png")) == ("recovered", "tesseract-fallback")


def test_dispatch_reraises_when_fallback_disabled(monkeypatch):
    from artifice_ocr.stages import ocr

    monkeypatch.setattr(
        ocr,
        "cfg",
        _cfg_from({"ocr_engine": "vision_model", "tesseract_fallback_on_failure": False}),
    )

    def boom(p, o=1):
        raise RuntimeError("vision down")

    monkeypatch.setattr(ocr, "_ocr_vision", boom)
    with pytest.raises(RuntimeError, match="vision down"):
        ocr._ocr_single_image(Path("x.png"))


def test_dispatch_reraises_when_tesseract_yields_nothing(monkeypatch):
    from artifice_ocr.stages import ocr

    monkeypatch.setattr(
        ocr,
        "cfg",
        _cfg_from({"ocr_engine": "vision_model", "tesseract_fallback_on_failure": True}),
    )

    def boom(p, o=1):
        raise RuntimeError("vision down")

    monkeypatch.setattr(ocr, "_ocr_vision", boom)
    monkeypatch.setattr(ocr._tesseract, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "_tesseract_from_image", lambda p, o=1: "   ")
    with pytest.raises(RuntimeError, match="vision down"):
        ocr._ocr_single_image(Path("x.png"))


def test_summarise_engines(monkeypatch):
    from artifice_ocr.stages import ocr

    assert ocr._summarise_engines(["ollama", "ollama"]) == "ollama"
    assert ocr._summarise_engines(["ollama", "tesseract-fallback"]) == "ollama+tesseract-fallback"
    monkeypatch.setattr(ocr, "backend_for", lambda kind: "lm_studio")
    assert ocr._summarise_engines([]) == "lm_studio"


def test_perform_recovers_rejected_page_via_tesseract(monkeypatch, tmp_path):
    """A vision result the repetition guard rejects is recovered by the
    Tesseract fallback, and the page provenance says so."""
    from artifice_ocr.stages import ocr

    monkeypatch.setattr(ocr, "_ocr_single_image", lambda p, o=1: ("loop loop loop", "ollama"))
    monkeypatch.setattr(
        ocr,
        "cfg",
        _cfg_from(
            {
                "ocr_repetition_guard": True,
                "tesseract_fallback_on_failure": True,
                "confidence_enabled": False,
            }
        ),
    )
    monkeypatch.setattr(ocr._tesseract, "is_available", lambda: True)
    monkeypatch.setattr(
        ocr,
        "_ocr_document_via_tesseract",
        lambda path, page, is_pdf, orientation: "clean recovered text",
    )

    class _G:
        def __init__(self, ok):
            self.ok = ok
            self.reasons = ["repetition loop"]

        def to_dict(self):
            return {"ok": self.ok}

    monkeypatch.setattr(ocr._guard, "check_no_repetition_loop", lambda text: _G("clean" in text))

    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG fake")
    result = ocr.perform(str(img), output_dir=str(tmp_path / "out"))

    assert result["engine"] == "tesseract-fallback"
    assert "clean recovered text" in result["extracted_text"]
