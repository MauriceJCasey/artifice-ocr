# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The always-available full-page segmentation provider.

``PassthroughProvider`` returns exactly one region covering the whole page in
original-image space. It exists to prove the segmentation pipeline end to end
— crop, per-region OCR, PAGE assembly, metadata, and per-region error
handling — without installing any real segmentation model (Commit 4+).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import Region, SegmentationProvider

_FULL_PAGE_REGION_ID = "region-0001"


class PassthroughProvider(SegmentationProvider):
    name = "passthrough"
    requirements = "No external dependencies; always available."

    def is_available(self) -> bool:
        return True

    def segment(self, image_path: Path, options: dict[str, Any]) -> list[Region]:
        """Return one full-page region in original-image space.

        The pipeline hands this provider the original-space page image (a
        rendered PDF page or an orientation-corrected render when needed), so
        probing its pixel size yields exactly the region the PAGE document
        must cover. ``options`` is part of the protocol contract; this
        provider has no tunable options.
        """
        del options  # accepted for protocol conformance; nothing to tune
        from PIL import Image

        with Image.open(image_path) as img:
            width, height = img.size
        return [
            Region(
                id=_FULL_PAGE_REGION_ID,
                type="unclassified",
                polygon=[(0, 0), (width, 0), (width, height), (0, height)],
                order=0,
            )
        ]
