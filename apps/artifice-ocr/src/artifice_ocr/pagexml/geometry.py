# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Coordinate transforms shared by segmentation providers."""

from __future__ import annotations

from collections.abc import Iterable

from .model import Point


def restore_original_coordinates(
    points: Iterable[tuple[int | float, int | float]],
    *,
    model_size: tuple[int, int],
    original_size: tuple[int, int],
) -> list[Point]:
    """Map model-image points back to integer pixels in the original image.

    Separate horizontal and vertical scale factors deliberately support a
    provider that resizes to an exact, non-square input rather than preserving
    aspect ratio. Results are rounded to the nearest pixel and clamped to the
    inclusive image bounds.
    """
    model_width, model_height = model_size
    original_width, original_height = original_size
    if min(model_width, model_height, original_width, original_height) <= 0:
        raise ValueError("Image dimensions must be positive")

    scale_x = original_width / model_width
    scale_y = original_height / model_height
    return [
        (
            min(original_width, max(0, round(x * scale_x))),
            min(original_height, max(0, round(y * scale_y))),
        )
        for x, y in points
    ]


def padded_crop_bounds(
    points: Iterable[tuple[int | float, int | float]],
    *,
    padding: float,
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Axis-aligned crop bounds for a region's polygon, padded and clamped.

    ``padding`` is a fraction of the polygon's own extent added on each side
    (``0.02`` = 2%). Returns integer pixel ``(x0, y0, x1, y1)`` where
    ``x1``/``y1`` are *exclusive* upper bounds (the PIL crop convention),
    clamped to the inclusive image bounds ``(0, 0, width, height)`` — never
    negative and never past the far edge.

    Shares the rounding/clamping convention of
    :func:`restore_original_coordinates` (``round`` then ``min``/``max``) so
    there is exactly one rounding convention in this module, not two subtly
    different ones.
    """
    if not points:
        raise ValueError("A region with no points has no crop bounds")
    xs = [int(p[0]) for p in points]
    ys = [int(p[1]) for p in points]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    width, height = image_size
    pad_x = round((x1 - x0) * padding)
    pad_y = round((y1 - y0) * padding)
    return (
        max(0, x0 - pad_x),
        max(0, y0 - pad_y),
        min(width, x1 + pad_x),
        min(height, y1 + pad_y),
    )
