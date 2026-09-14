# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tesseract OCR engine — detect an installed binary and run it.

An alternative to the vision-model OCR path: a fast, offline, deterministic
transcriber. The Tesseract binary is **not bundled** (it is not a Python
package and cannot be pip/uv-installed into the frozen build) — it is detected
on ``PATH`` or at an explicit ``tesseract_path``. When it is absent, callers get
a clear ``TesseractUnavailable`` rather than a crash.

The engine is driven by image *bytes* (a PNG produced by ``ocr._encode_image``),
which means it automatically inherits the orientation correction and — when
enabled — the deterministic pre-processing that path already applies. See
docs/OCR_TESSERACT_ENGINE_PLAN.md.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from artifice_ocr._logging import get_logger
from artifice_ocr.config import get as cfg

log = get_logger("tesseract")

# Common Windows install locations for the UB Mannheim Tesseract build, checked
# after PATH so a user who installed it with defaults is found without config.
_WINDOWS_FALLBACK_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


# Tesseract writes UTF-8 on every platform. ``text=True`` alone decodes with
# ``locale.getpreferredencoding()``, which is cp1252 on a stock Windows install,
# and a single byte outside that codepage (0x9d, seen on a real archive scan)
# raises UnicodeDecodeError *inside subprocess's reader thread*. That exception
# is never propagated: ``run()`` returns normally with ``stdout=None``, and the
# caller crashes several frames later on ``.strip()`` with no trace of the real
# cause. ``errors="replace"`` keeps a stray byte from costing a whole page.
_DECODE_AS_UTF8: dict[str, str] = {"encoding": "utf-8", "errors": "replace"}


def _subprocess_window_options() -> dict[str, int]:
    """Keep command-line OCR helpers consoleless in the Windows desktop app."""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


class TesseractError(RuntimeError):
    """Tesseract ran but failed (non-zero exit, or a subprocess error)."""


class TesseractUnavailable(TesseractError):
    """The Tesseract binary could not be found."""


def resolve_binary() -> str | None:
    """Locate the tesseract binary: explicit config path → PATH → known
    install locations. Returns the path, or ``None`` if not found."""
    configured = (cfg("tesseract_path", "") or "").strip()
    if configured:
        p = Path(configured)
        if p.is_file():
            return str(p)
        # A bare name in tesseract_path is still worth resolving against PATH.
        on_path = shutil.which(configured)
        if on_path:
            return on_path

    on_path = shutil.which("tesseract")
    if on_path:
        return on_path

    for candidate in _WINDOWS_FALLBACK_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def is_available() -> bool:
    return resolve_binary() is not None


def version(binary: str | None = None) -> str | None:
    """First line of ``tesseract --version`` (e.g. "tesseract 5.3.3"), or None."""
    binary = binary or resolve_binary()
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            **_DECODE_AS_UTF8,
            timeout=10,
            **_subprocess_window_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout or proc.stderr or ""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[0] if lines else None


def status() -> dict:
    """Detection status for the UI: whether it is available, where, which
    version, and the configured language."""
    binary = resolve_binary()
    return {
        "available": binary is not None,
        "path": binary,
        "version": version(binary) if binary else None,
        "lang": (cfg("tesseract_lang", "eng") or "eng"),
    }


def ocr_bytes(data: bytes, *, lang: str | None = None, binary: str | None = None) -> str:
    """Run Tesseract on PNG (or any Tesseract-readable) image *bytes* and return
    the recognised text.

    Raises ``TesseractUnavailable`` if the binary is missing, ``TesseractError``
    if it runs but fails.
    """
    binary = binary or resolve_binary()
    if not binary:
        raise TesseractUnavailable(
            "Tesseract is not installed or could not be found. Install it, or "
            "set its path in Settings."
        )
    lang = (lang or cfg("tesseract_lang", "eng") or "eng").strip() or "eng"

    # delete=False + a with-block: the file must be closed (not just flushed)
    # before Tesseract opens it by path, which a Windows lock would otherwise
    # prevent; we unlink it ourselves afterwards.
    with tempfile.NamedTemporaryFile(prefix="ocr_tess_", suffix=".png", delete=False) as tmp:
        tmp.write(data)
        tmp_name = tmp.name
    try:
        # `tesseract <image> stdout -l <lang>` writes recognised text to stdout.
        proc = subprocess.run(
            [binary, tmp_name, "stdout", "-l", lang],
            capture_output=True,
            text=True,
            **_DECODE_AS_UTF8,
            timeout=300,
            **_subprocess_window_options(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TesseractError(f"Tesseract failed to run: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)

    if proc.returncode != 0:
        message = (proc.stderr or "").strip() or f"tesseract exited {proc.returncode}"
        raise TesseractError(message)
    # ``stdout`` can be None even on a zero exit code (see _DECODE_AS_UTF8).
    # The annotated contract is ``-> str``; a None here became the opaque
    # "'NoneType' object has no attribute 'strip'" the fallback used to fail with.
    return proc.stdout or ""
