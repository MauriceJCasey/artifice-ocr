# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Logging must survive the frozen Windows build.

The app ships as a windowed executable (``console=False`` in
``artifice-ocr.spec``), so ``sys.stderr`` goes nowhere. Every diagnostic the
app emitted was therefore invisible to the user: a real context-window
overflow was reported to them only as an opaque ``AttributeError``, and
diagnosing it required reproducing the run with stderr redirected. A log on
disk is what makes the next one self-service.
"""

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

import pytest
from artifice_ocr import _logging


def _read_log() -> str:
    path = _logging.log_path()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_records_are_written_to_a_file_on_disk():
    log = _logging.get_logger("ocr")
    log.info("Starting OCR for %s", "page.jpg")
    logging.shutdown()

    assert _logging.log_path().exists()
    assert "Starting OCR for page.jpg" in _read_log()


def test_log_dir_is_overridable_by_environment(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("ARTIFICE_OCR_LOG_DIR", str(target))
    _logging.reset()

    _logging.get_logger("ocr").warning("redirected")
    logging.shutdown()

    assert (target / "artifice-ocr.log").exists()
    assert "redirected" in (target / "artifice-ocr.log").read_text(encoding="utf-8")


def test_log_file_is_utf8_not_the_windows_locale():
    """The handler must not reintroduce the cp1252 bug it exists to record.

    A log that cannot write "über" is worse than no log: the failing page is
    exactly the one whose text is non-ASCII, and a UnicodeEncodeError inside a
    handler is swallowed by logging's own error handling.
    """
    log = _logging.get_logger("ocr")
    log.info("Transkription: über Angeschuldigte — 'quoted' \u2014 \u201cfancy\u201d")
    logging.shutdown()

    body = _read_log()
    assert "über Angeschuldigte" in body
    assert "\u201cfancy\u201d" in body


def test_file_handler_rotates_rather_than_growing_without_bound():
    _logging.setup_logging()
    handlers = [
        h for h in logging.getLogger("artifice_ocr").handlers if isinstance(h, RotatingFileHandler)
    ]
    assert len(handlers) == 1
    assert handlers[0].maxBytes > 0
    assert handlers[0].backupCount >= 1


def test_unwritable_log_dir_does_not_stop_the_app(monkeypatch, tmp_path):
    """A log that cannot be opened must never be the reason a run fails."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARTIFICE_OCR_LOG_DIR", str(blocker / "logs"))
    _logging.reset()

    _logging.get_logger("ocr").info("still works")  # must not raise

    handlers = logging.getLogger("artifice_ocr").handlers
    assert not any(isinstance(h, RotatingFileHandler) for h in handlers)


def test_no_stderr_handler_when_stderr_is_none(monkeypatch):
    """``pythonw.exe`` sets both std streams to None before anything runs.

    ``logging.StreamHandler(None)`` then falls back to whatever ``sys.stderr``
    is at emit time, so guard it explicitly rather than relying on that.
    """
    monkeypatch.setattr(sys, "stderr", None)
    _logging.reset()
    _logging.setup_logging()

    handlers = logging.getLogger("artifice_ocr").handlers
    assert not any(type(h) is logging.StreamHandler for h in handlers)
    # The file handler is the whole point in this configuration.
    assert any(isinstance(h, RotatingFileHandler) for h in handlers)


def test_repeated_setup_does_not_duplicate_handlers():
    _logging.setup_logging()
    first = len(logging.getLogger("artifice_ocr").handlers)
    _logging.setup_logging()
    assert len(logging.getLogger("artifice_ocr").handlers) == first


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_unhandled_thread_exception_reaches_the_log():
    """The failure that started all this died in a subprocess reader thread.

    ``threading``'s default excepthook prints to stderr, which the frozen build
    discards — so the UnicodeDecodeError that broke a real run left no trace.
    """
    _logging.setup_logging()

    def boom():
        raise ValueError("reader thread died")

    thread = threading.Thread(target=boom)
    thread.start()
    thread.join()
    logging.shutdown()

    body = _read_log()
    assert "reader thread died" in body
    assert "ValueError" in body
