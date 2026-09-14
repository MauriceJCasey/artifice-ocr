# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Auto-rotation detection via Tesseract's orientation-and-script-detection
(OSD) mode.

olmOCR 2 (arXiv:2510.19817 s4) and the ACL 2026 Demo paper (s6) both list
automatic rotation correction among the deployment fixes that "further
improved robustness without retraining." This app already corrects a known
Tropy `photos.orientation` value (see stages/ocr.py::_exif_orientation_matrix)
but has no detector of its own — it trusts upstream metadata entirely, and
that metadata has been wrong in a documented real case (a page scanned
upside-down with orientation left at 1/normal, which the model then
hallucinated over instead of transcribing).

OSD only detects axis-aligned rotation (0/90/180/270 degrees), not
mirroring — so only orientations 1, 6, 3, 8 (the pure-rotation subset of the
EXIF/Tropy 1-8 convention) are ever returned. That is also the only subset a
scanner or a phone camera actually produces; mirrored scans are not a real
failure mode this needs to cover.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from artifice_ocr import _tesseract
from artifice_ocr._logging import get_logger

log = get_logger("rotation")

# Tesseract's `Rotate: N` (degrees the image must be rotated clockwise to be
# upright) to the Tropy/EXIF orientation code _exif_orientation_matrix expects.
_ROTATE_TO_ORIENTATION = {0: None, 90: 6, 180: 3, 270: 8}

_ROTATE_RE = re.compile(r"^Rotate:\s*(\d+)", re.MULTILINE)


def detect_orientation(image_bytes: bytes) -> int | None:
    """Probe *image_bytes* with `tesseract --psm 0` and return a Tropy-style
    orientation code, or ``None`` when no correction is needed, Tesseract is
    unavailable, or OSD could not determine an angle (common on sparse-text
    or non-text pages — this must degrade silently, never fail the page).
    """
    binary = _tesseract.resolve_binary()
    if not binary:
        return None

    with tempfile.NamedTemporaryFile(prefix="ocr_osd_", suffix=".png", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            [binary, str(tmp_path), "stdout", "--psm", "0"],
            capture_output=True,
            text=True,
            **_tesseract._DECODE_AS_UTF8,
            timeout=30,
            **_tesseract._subprocess_window_options(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Rotation OSD failed to run: %s", exc)
        return None
    finally:
        tmp_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        log.debug("Rotation OSD declined (likely too little text): %s", (proc.stderr or "").strip())
        return None

    match = _ROTATE_RE.search(proc.stdout or "")
    if not match:
        return None
    degrees = int(match.group(1))
    return _ROTATE_TO_ORIENTATION.get(degrees)
