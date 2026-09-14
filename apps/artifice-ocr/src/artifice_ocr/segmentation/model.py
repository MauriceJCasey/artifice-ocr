# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The neutral, provider-agnostic segmentation contract.

A provider returns :class:`Region` objects in the original-image pixel space
(the plan's compatibility invariant: coordinates are always integer pixels in
the original image). Converting those into a PAGE document is the job of
``artifice_ocr.pagexml.convert``, never the provider — providers know nothing
about PAGE's legal types, ``@custom`` syntax, or reading order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from artifice_ocr.pagexml.model import Point


@dataclass
class Line:
    """A text line inside a region (a PAGE ``TextLine``).

    ``baseline`` is the two-point PAGE baseline; a provider that does not
    produce one leaves it ``None``.
    """

    id: str
    polygon: list[Point]
    baseline: list[Point] | None = None


@dataclass
class Region:
    """One segmented region in original-image pixel space.

    ``type`` is an Artifice region type (see ``ARTIFICE_REGION_TYPES``);
    unknown labels normalise to ``"unclassified"`` by
    :func:`~artifice_ocr.pagexml.artifice_custom`. ``order`` is an optional
    reading-order position (lower is earlier); when absent the pipeline falls
    back to top-to-bottom/left-to-right. ``error`` records a provider- or
    OCR-side failure on this region without failing the page.
    """

    id: str
    type: str
    polygon: list[Point]
    order: int | None = None
    baseline: list[Point] | None = None
    confidence: float | None = None
    lines: list[Line] = field(default_factory=list)
    error: str | None = None


@runtime_checkable
class SegmentationProvider(Protocol):
    """Minimal contract for a page-segmentation backend.

    ``name`` is the stable registry key; ``requirements`` is a short,
    human-readable note about what must be installed for ``is_available``
    to be true — surfaced verbatim in the "unavailable" error the pipeline
    raises. A provider with an optional dependency imports it lazily inside
    ``segment``/``is_available`` so importing this module stays free.
    """

    name: str
    requirements: str

    def is_available(self) -> bool: ...

    def segment(self, image_path: Path, options: dict[str, Any]) -> list[Region]: ...
