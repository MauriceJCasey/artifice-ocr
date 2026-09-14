# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Convert neutral segmentation regions into PAGE document objects.

This is the single place that decides a region's legal PAGE ``type``,
``@custom`` string, line baselines, confidence, and reading order. Providers
return neutral :class:`~artifice_ocr.segmentation.model.Region` objects;
everything PAGE-specific is resolved here so a later provider (Commit 4+)
never reimplements the mapping.

The neutral types are imported under ``TYPE_CHECKING`` only: the pipeline
imports this module eagerly (it is part of ``pagexml``), while the whole
``artifice_ocr.segmentation`` package must stay lazy behind the
``segmentation_enabled`` setting. The functions below work on any object with
the neutral region's attributes, so nothing here needs the types at runtime.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from .model import TextLine, TextRegion, artifice_custom, page_region_type

if TYPE_CHECKING:
    from artifice_ocr.segmentation.model import Line, Region


def _min_y(region: Region) -> int:
    return min((point[1] for point in region.polygon), default=0)


def _min_x(region: Region) -> int:
    return min((point[0] for point in region.polygon), default=0)


def order_regions(regions: Sequence[Region]) -> list[Region]:
    """Sort regions into a stable reading order.

    Uses the provider-supplied ``order`` when every region carries one,
    otherwise the geometric fallback: top-to-bottom, then left-to-right by
    each polygon's minimum corner. Advanced reading-order inference is out of
    scope; a provider that supplies no order gets the geometric fallback.
    """
    if not regions:
        return []
    if all(region.order is not None for region in regions):
        return sorted(regions, key=lambda r: (r.order, _min_y(r), _min_x(r)))
    return sorted(regions, key=lambda r: (_min_y(r), _min_x(r)))


def assign_region_ids(regions: Sequence[Region], *, prefix: str = "region") -> list[str]:
    """Return a unique, non-empty PAGE id for each region, preserving order.

    Keeps a provider's own id when it is non-empty and not already used;
    otherwise assigns ``prefix-NNNN``. A collision (two providers reusing
    ``region-0001``, or a provider whose id equals an auto-assigned one) is
    disambiguated with a numeric suffix. PAGE ids must be unique; they need
    not round-trip a provider's id exactly.
    """
    seen: set[str] = set()
    ids: list[str] = []
    for index, region in enumerate(regions, start=1):
        candidate = (region.id or "").strip()
        if not candidate:
            candidate = f"{prefix}-{index:04d}"
        base = candidate
        suffix = 1
        while candidate in seen:
            candidate = f"{base}-{suffix}"
            suffix += 1
        seen.add(candidate)
        ids.append(candidate)
    return ids


def to_text_region(region: Region, region_id: str) -> TextRegion:
    """Map one neutral region to a PAGE ``TextRegion``.

    ``region.type`` is an Artifice region type; :func:`page_region_type`
    picks the legal PAGE type and :func:`artifice_custom` the ``@custom``
    string, so both mappings stay in one place. Region-level ``baseline`` is
    intentionally not mapped — PAGE 2019 ``TextRegion`` has no ``Baseline``
    element; line-level baselines travel through ``region.lines``.
    """
    return TextRegion(
        id=region_id,
        type=page_region_type(region.type),
        custom=artifice_custom(region.type),
        polygon=region.polygon,
        confidence=region.confidence,
        lines=[_to_line(line) for line in region.lines],
    )


def _to_line(line: Line) -> TextLine:
    return TextLine(id=line.id, polygon=line.polygon, baseline=line.baseline)


def convert_regions(
    regions: Sequence[Region],
) -> tuple[list[TextRegion], list[str]]:
    """Order, id, and convert neutral regions into PAGE objects.

    Returns ``(text_regions, reading_order_ids)`` where ``reading_order_ids``
    are the final (possibly re-assigned) region ids in reading order — the
    value a ``PageDocument.reading_order`` needs.
    """
    ordered = order_regions(regions)
    ids = assign_region_ids(ordered)
    return [to_text_region(region, rid) for region, rid in zip(ordered, ids, strict=True)], ids
