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
