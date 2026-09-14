# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Kraken BLLA segmentation provider (Commit 5).

``KrakenProvider`` runs Kraken's BLLA (baseline + region) segmenter
(``kraken.blla.segment``) over a page and maps its native output — regions,
line polygons, baselines, and reading order — into the neutral
:class:`~artifice_ocr.segmentation.model.Region` / ``Line`` contract.

It is segmentation only: this module imports nothing from Kraken's
recognition/transcription API, and produces geometry, never text.

The optional package (``kraken``) is imported lazily inside ``segment`` /
``is_available``, so importing this module — and the registry that merely lists
it — never forces Kraken or its heavyweight dependencies (torch) at startup
(the plan's compatibility invariant #3). Kraken's own ``__init__.py`` is an
empty docstring, so the availability probe is cheap; ``kraken.blla`` (and thus
torch) is only imported when a page is actually segmented.

Coordinate convention: Kraken's ``blla.segment`` scales its vectorised output
back to the input image's own pixel space before returning (see
``kraken.lib.segmentation.scale_polygonal_lines`` / ``scale_regions``, called
from ``kraken.blla.segment``). The returned baselines, line polygons, and
region polygons are therefore already original-image pixels. This provider
passes a unit ``model_size == original_size`` through
:func:`~artifice_ocr.pagexml.geometry.restore_original_coordinates` so the
mapping rounds and clamps them to integer pixels (compatibility invariant #6)
without a second scaling — exactly the convention Commit 4 established for
DocLayout-YOLO, and it holds here because the underlying library does its own
restoration.

Reading order: Kraken supplies line reading order (``blla.segment`` reorders
its ``lines`` list via ``polygonal_reading_order`` before returning). It does
not emit a scalar per-region order, so this provider derives each region's
order from the reading-order position of the first line it contains, then falls
back to a synthetic "unclassified" region for lines no detected region covers.

The bundled default model (``kraken/blla.mlmodel``) is a *binary* segmentation
model; Kraken logs a warning and proceeds when handed a non-bitonal image, and
its own CLI does not binarize before the segment step either. Binarization is
deliberately out of scope here (this is segmentation only); a caller that wants
it can pre-binarize, or point ``model`` at a segmentation checkpoint trained on
grayscale input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..pagexml.geometry import restore_original_coordinates
from .model import Line, Point, Region, SegmentationProvider

# Kraken's region vocabulary is model-dependent, not a closed set: the bundled
# ``blla.mlmodel`` emits exactly one region class (``"text"``), while a custom
# checkpoint may emit any set of document-layout classes. None of them overlaps
# ``ARTIFICE_REGION_TYPES`` — an *annotation* vocabulary (marginalia,
# interlinear, underline, ...) with no text/image/table category — so every
# Kraken region class maps to ``"unclassified"`` on purpose. The dict records
# the one class the default model produces so a future commit that extends
# ``ARTIFICE_REGION_TYPES`` has exactly one place to edit; anything else falls
# back to ``"unclassified"`` (``_kraken_region_type``).
_KRAKEN_REGION_TYPE_MAP: Final[Mapping[str, str]] = {
    "text": "unclassified",
}

_BUNDLED_MODEL_IDENTITY = "bundled:blla.mlmodel"

_DEFAULT_DEVICE = "cpu"
_DEFAULT_TEXT_DIRECTION = "horizontal-lr"
_DEFAULT_AUTOCAST = False

# The four ``text_direction`` values Kraken's ``blla.segment`` accepts.
_TEXT_DIRECTIONS: Final[frozenset[str]] = frozenset(
    {"horizontal-lr", "horizontal-rl", "vertical-lr", "vertical-rl"}
)


@dataclass(frozen=True)
class KrakenLine:
    """One raw Kraken BLLA line, in original-image pixel space.

    ``baseline`` is the baseline polyline (two or more ``(x, y)`` points).
    ``boundary`` is the line's bounding polygon (``None`` when Kraken could
    not polygonise the line). ``region_ids`` are the Kraken region ids the
    line was assigned to (empty when the model detected no regions). This is
    the stable seam the mapping function consumes, so tests can feed synthetic
    Kraken output without importing Kraken.
    """

    baseline: tuple[tuple[float, float], ...]
    boundary: tuple[tuple[float, float], ...] | None
    region_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class KrakenRegion:
    """One raw Kraken BLLA region, in original-image pixel space."""

    id: str
    boundary: tuple[tuple[float, float], ...]


def _kraken_region_type(region_type: str) -> str:
    """Map a Kraken region class to an Artifice region type (see module map)."""
    return _KRAKEN_REGION_TYPE_MAP.get(region_type, "unclassified")


def _restore(
    points: Sequence[tuple[float, float] | tuple[int, int] | list[float]],
    *,
    model_size: tuple[int, int],
    original_size: tuple[int, int],
) -> list[Point]:
    """Coerce a raw point sequence to floats and restore it to original space.

    Kraken returns lists of ``[x, y]`` (possibly numpy integers); the shared
    helper unpacks 2-tuples and needs plain Python numbers so ``round`` returns
    an ``int`` (numpy scalars would leak ``float`` strings into the XSD
    ``PointsType`` pattern, which requires integer digits).
    """
    coords = [(float(x), float(y)) for x, y in points]
    return restore_original_coordinates(coords, model_size=model_size, original_size=original_size)


def _bbox_polygon(lines: Sequence[Line], image_size: tuple[int, int]) -> list[Point]:
    """The axis-aligned bounding polygon of a set of neutral lines."""
    xs = [p[0] for line in lines for p in line.polygon]
    ys = [p[1] for line in lines for p in line.polygon]
    if not xs:
        width, height = image_size
        return [(0, 0), (width, 0), (width, height), (0, height)]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def kraken_segmentation_to_regions(
    lines: Sequence[KrakenLine],
    regions: Mapping[str, Sequence[KrakenRegion]],
    *,
    model_size: tuple[int, int],
    original_size: tuple[int, int],
) -> list[Region]:
    """Map Kraken BLLA output (lines in reading order + regions) to neutral regions.

    Each Kraken region becomes one :class:`Region` (type ``"unclassified"``,
    no confidence — BLLA emits none). Each line becomes one ``Line`` nested
    inside the region its ``region_ids`` reference, preserving Kraken's line
    reading order within the region and carrying Kraken's baseline polyline.

    Lines no detected region covers are collected into a single synthetic
    ``"unclassified"`` region whose polygon is their combined bounding box —
    this covers both a line-only model (no regions at all) and a region
    detection gap.

    Region reading order is derived from the reading-order position of each
    region's first line; regions with no lines sort after every line-bearing
    region. Every returned region carries an explicit ``order`` so the
    pipeline's centralised ``order_regions`` respects it rather than falling
    back to geometric sorting.
    """
    # Neutral regions are keyed by their *index* into ``neutral`` (a plain
    # list), not by the ``Region`` objects themselves: ``Region`` is a mutable
    # dataclass and therefore unhashable.
    neutral: list[Region] = []
    region_for: dict[str, int] = {}
    for region_type, kraken_regions in regions.items():
        for kreg in kraken_regions:
            index = len(neutral)
            neutral.append(
                Region(
                    id=f"kraken-{index + 1:04d}",
                    type=_kraken_region_type(region_type),
                    polygon=_restore(
                        kreg.boundary, model_size=model_size, original_size=original_size
                    ),
                    order=None,
                )
            )
            region_for[kreg.id] = index

    first_seen: dict[int, int] = {}
    orphan_lines: list[Line] = []
    orphan_position: int | None = None
    line_index = 0
    for reading_index, kline in enumerate(lines):
        if not kline.baseline and not kline.boundary:
            continue  # nothing to represent
        owner_index = next(
            (region_for[rid] for rid in kline.region_ids if rid in region_for),
            None,
        )
        polygon = kline.boundary if kline.boundary else kline.baseline
        line = Line(
            id=f"kraken-line-{line_index + 1:04d}",
            polygon=_restore(polygon, model_size=model_size, original_size=original_size),
            baseline=_restore(kline.baseline, model_size=model_size, original_size=original_size)
            if kline.baseline
            else None,
        )
        line_index += 1
        if owner_index is not None:
            neutral[owner_index].lines.append(line)
            first_seen.setdefault(owner_index, reading_index)
        else:
            orphan_lines.append(line)
            if orphan_position is None:
                orphan_position = reading_index

    if orphan_lines:
        orphan_index = len(neutral)
        neutral.append(
            Region(
                id=f"kraken-{orphan_index + 1:04d}",
                type="unclassified",
                polygon=_bbox_polygon(orphan_lines, original_size),
                order=None,
                lines=orphan_lines,
            )
        )
        if orphan_position is not None:
            first_seen[orphan_index] = orphan_position

    ordered = sorted(
        range(len(neutral)),
        key=lambda index: (first_seen.get(index, len(first_seen)), index),
    )
    result = [neutral[index] for index in ordered]
    for position, region in enumerate(result):
        region.order = position
    return result


def _as_points(raw: Any) -> tuple[tuple[float, float], ...]:
    """Coerce a Kraken point sequence (lists of ``[x, y]``) to float 2-tuples."""
    if not raw:
        return ()
    return tuple((float(x), float(y)) for x, y in raw)


class KrakenProvider(SegmentationProvider):
    name = "kraken"
    requirements = (
        "Kraken is an optional dependency. Install it with "
        'pip install "artifice-ocr[segmentation-kraken]".'
    )

    def __init__(self) -> None:
        # Set during ``segment``: the resolved segmentation-model identity
        # (``bundled:blla.mlmodel`` or ``file:<path>``) surfaced into PAGE
        # metadata by the caller.
        self.resolved_weights: str | None = None
        self.model_path: Path | None = None

    def is_available(self) -> bool:
        """True only if the ``kraken`` package imports; never raises."""
        try:
            import kraken  # noqa: F401
        except Exception:
            return False
        return True

    def segment(self, image_path: Path, options: dict[str, Any]) -> list[Region]:
        """Run Kraken BLLA segmentation and map the output to neutral regions.

        Provider options (all optional, sane defaults when absent):

        - ``device`` (str) — default ``"cpu"``; a GPU device is only ever used
          when explicitly requested (the safe default).
        - ``text_direction`` (str) — one of ``horizontal-lr`` (default),
          ``horizontal-rl``, ``vertical-lr``, ``vertical-rl``.
        - ``model`` (str | None) — path to a segmentation ``.mlmodel``; default
          ``None`` loads Kraken's bundled ``blla.mlmodel``.
        - ``autocast`` (bool) — default ``False``; run with automatic mixed
          precision on a compatible device.

        Kraken's ``mask`` and ``reading_order_fn`` parameters are deliberately
        not exposed: the former is a numpy array (not a flat-dict option) and
        the latter a callable.
        """
        from kraken.blla import segment as blla_segment
        from PIL import Image

        device = str(options.get("device", _DEFAULT_DEVICE))
        text_direction = str(options.get("text_direction", _DEFAULT_TEXT_DIRECTION))
        if text_direction not in _TEXT_DIRECTIONS:
            raise ValueError(
                f"Invalid Kraken text_direction {text_direction!r}; expected one of "
                f"{sorted(_TEXT_DIRECTIONS)}"
            )
        autocast = bool(options.get("autocast", _DEFAULT_AUTOCAST))
        model = self._load_model(options)

        with Image.open(image_path) as img:
            width, height = img.size
            result = blla_segment(
                img,
                model=model,
                device=device,
                text_direction=text_direction,
                autocast=autocast,
            )

        lines = [
            KrakenLine(
                baseline=_as_points(line.baseline),
                boundary=_as_points(line.boundary) if line.boundary else None,
                region_ids=tuple(line.regions or ()),
            )
            for line in (result.lines or [])
        ]
        regions = {
            region_type: [
                KrakenRegion(id=region.id, boundary=_as_points(region.boundary))
                for region in region_list
            ]
            for region_type, region_list in (result.regions or {}).items()
        }

        # Kraken already returns original-space coordinates (see the module
        # docstring), so a unit scale just rounds/clamps to integer pixels.
        return kraken_segmentation_to_regions(
            lines,
            regions,
            model_size=(width, height),
            original_size=(width, height),
        )

    def _load_model(self, options: dict[str, Any]) -> Any:
        """Resolve the segmentation model to a Kraken model object.

        ``None`` means "use the bundled ``blla.mlmodel``" and records the
        identity ``bundled:blla.mlmodel``. Any other value is treated as a
        local ``.mlmodel`` path, loaded through ``TorchVGSLModel.load_model``
        and recorded as ``file:<path>``.
        """
        model = options.get("model")
        if not model:
            self.model_path = None
            self.resolved_weights = _BUNDLED_MODEL_IDENTITY
            return None

        path = Path(str(model)).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Kraken segmentation model not found: {path}")

        from kraken.lib.vgsl import TorchVGSLModel

        self.model_path = path.resolve()
        self.resolved_weights = f"file:{self.model_path}"
        return TorchVGSLModel.load_model(str(self.model_path))
