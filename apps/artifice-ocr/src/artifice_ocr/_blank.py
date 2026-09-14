# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Near-blank page detection.

olmOCR 2 (arXiv:2510.19817 s4, "Handle blank pages"): a model never trained
on blank pages hallucinates plausible-looking filler instead of recognising
there is nothing to transcribe. Cheapest fix: never send it one. A blank or
near-blank page (a verso, an inserted flyleaf) has near-zero greyscale
variance regardless of exposure — unlike text detection, this needs no
model and is safe to run unconditionally, independent of the opt-in
``preprocess_enabled`` pipeline.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from artifice_ocr._logging import get_logger

log = get_logger("blank")


def is_near_blank(data: bytes, *, std_threshold: float = 6.0) -> bool:
    """True if *data* decodes to an image with near-zero greyscale variance.

    ``std_threshold`` is a standard deviation on a 0-255 greyscale scale — a
    genuinely blank scan (paper grain, faint scanner shadow) sits under 3;
    typescript or handwriting of any density sits well over 20. 6.0 leaves a
    wide margin on both sides. Never raises: an undecodable image returns
    ``False`` so a corrupt file is never mistaken for blank and silently
    skipped.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            grey = img.convert("L")
            # Variance survives downsampling — a blank page is near-zero-std
            # at ANY resolution — so probe a <=256x256 thumbnail instead of
            # allocating a full-resolution float32 array (tens of MB for a
            # 4653x3445 scan) on every page. This check runs by default
            # (ocr_blank_page_skip=True), so the cost mattered. In-place,
            # aspect-preserving, and a no-op for images already within the
            # box.
            grey.thumbnail((256, 256))
            arr = np.asarray(grey, dtype=np.float32)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not decode image for blank-page check: %s", exc)
        return False
    return bool(arr.std() <= std_threshold)
