# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Commit 5: Kraken BLLA segmentation provider.

Covers, without installing Kraken or running the model:

- the Kraken region-class → Artifice type mapping (everything is
  ``"unclassified"`` — the annotation vocabulary has no document-layout
  category);
- the Kraken segmentation → ``Region``/``Line`` conversion (region polygons,
  line polygons, baselines, and region reading order derived from Kraken's
  line reading order);
- orphan-line handling (lines no region covers, and a line-only model);
- availability safety with the optional package absent (``False``, never
  raises, never partially imports);
- registry listing (``list_available`` omits the provider when unavailable);
- coordinate restoration through the shared helper (unit scale — Kraken
  returns original-space pixels — plus the scale/clamp behaviour for the
  record).

Plus one opt-in ``live_interop`` manuscript test that runs real Kraken BLLA
segmentation and is excluded from the default suite by ``pyproject.toml``'s
``addopts``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from artifice_ocr.segmentation.kraken import (
    KrakenLine,
    KrakenProvider,
    KrakenRegion,
    _kraken_region_type,
    kraken_segmentation_to_regions,
)

# --------------------------------------------------------------------------- #
# 1. Region-type mapping
# --------------------------------------------------------------------------- #


def test_region_type_mapping_is_unclassified_and_page_legal():
    from artifice_ocr.pagexml import artifice_custom, page_region_type

    # The default blla.mlmodel emits exactly one region class, "text", and it
    # has no Artifice annotation equivalent — so it normalises to
    # "unclassified". An unknown (custom-model) class does the same.
    for region_type in ("text", "image", "table", "not-a-real-kraken-type"):
        assert _kraken_region_type(region_type) == "unclassified"
        assert page_region_type(_kraken_region_type(region_type)) == "other"
        assert artifice_custom(_kraken_region_type(region_type)) == (
            "structure {type:unclassified;}"
        )


# --------------------------------------------------------------------------- #
# 2. Native-output conversion, no Kraken required
# --------------------------------------------------------------------------- #


def test_kraken_segmentation_to_regions_builds_regions_lines_and_baselines():
    regions = {
        "text": [
            KrakenRegion(id="r1", boundary=((10, 10), (200, 10), (200, 90), (10, 90))),
            KrakenRegion(id="r2", boundary=((10, 100), (200, 100), (200, 180), (10, 180))),
        ]
    }
    lines = [
        KrakenLine(
            baseline=((12, 40), (180, 40)),
            boundary=((12, 30), (180, 30), (180, 50), (12, 50)),
            region_ids=("r1",),
        ),
        KrakenLine(
            baseline=((12, 70), (180, 70)),
            boundary=((12, 60), (180, 60), (180, 80), (12, 80)),
            region_ids=("r1",),
        ),
        KrakenLine(
            baseline=((12, 130), (180, 130)),
            boundary=((12, 120), (180, 120), (180, 140), (12, 140)),
            region_ids=("r2",),
        ),
    ]
    result = kraken_segmentation_to_regions(
        lines, regions, model_size=(200, 200), original_size=(200, 200)
    )

    assert [r.id for r in result] == ["kraken-0001", "kraken-0002"]
    assert [r.type for r in result] == ["unclassified", "unclassified"]
    assert [r.order for r in result] == [0, 1]  # r1's first line precedes r2's

    # Region polygons are restored (integer, original space).
    assert result[0].polygon == [(10, 10), (200, 10), (200, 90), (10, 90)]

    # Lines are nested in their region, in reading order, with baselines.
    assert len(result[0].lines) == 2
    assert len(result[1].lines) == 1
    assert [line.id for line in result[0].lines] == ["kraken-line-0001", "kraken-line-0002"]
    assert result[0].lines[0].baseline == [(12, 40), (180, 40)]
    assert result[0].lines[0].polygon == [(12, 30), (180, 30), (180, 50), (12, 50)]
    assert result[1].lines[0].baseline == [(12, 130), (180, 130)]

    # No region confidence — Kraken BLLA emits none.
    assert all(r.confidence is None for r in result)


def test_kraken_segmentation_to_regions_line_only_model():
    # A line-only model: no regions at all. Every line lands in one synthetic
    # "unclassified" region covering the lines' combined extent.
    lines = [
        KrakenLine(
            baseline=((10, 20), (100, 20)),
            boundary=((10, 15), (100, 15), (100, 25), (10, 25)),
        ),
        KrakenLine(
            baseline=((10, 40), (90, 40)),
            boundary=((10, 35), (90, 35), (90, 45), (10, 45)),
        ),
    ]
    result = kraken_segmentation_to_regions(
        lines, {}, model_size=(120, 80), original_size=(120, 80)
    )
    assert len(result) == 1
    region = result[0]
    assert region.type == "unclassified"
    assert region.order == 0
    assert len(region.lines) == 2
    # Bounding box of both lines: x in [10, 100], y in [15, 45].
    assert region.polygon == [(10, 15), (100, 15), (100, 45), (10, 45)]


def test_kraken_segmentation_to_regions_orphan_lines_get_a_region():
    # One region covers the top lines; a second line falls outside any region
    # and is collected into a synthetic region rather than dropped.
    regions = {
        "text": [
            KrakenRegion(id="r1", boundary=((10, 10), (200, 10), (200, 60), (10, 60))),
        ]
    }
    lines = [
        KrakenLine(
            baseline=((12, 30), (180, 30)),
            boundary=((12, 20), (180, 20), (180, 40), (12, 40)),
            region_ids=("r1",),
        ),
        KrakenLine(
            baseline=((12, 120), (180, 120)),
            boundary=((12, 110), (180, 110), (180, 130), (12, 130)),
        ),
    ]
    result = kraken_segmentation_to_regions(
        lines, regions, model_size=(200, 200), original_size=(200, 200)
    )
    assert len(result) == 2
    # r1 (first line) before the synthetic region (second line).
    assert [r.type for r in result] == ["unclassified", "unclassified"]
    assert result[0].id == "kraken-0001"
    assert result[1].id == "kraken-0002"
    assert len(result[0].lines) == 1
    assert len(result[1].lines) == 1
    assert result[1].lines[0].baseline == [(12, 120), (180, 120)]


def test_kraken_segmentation_to_regions_skips_geometryless_lines():
    regions = {
        "text": [
            KrakenRegion(id="r1", boundary=((10, 10), (200, 10), (200, 60), (10, 60))),
        ]
    }
    lines = [
        KrakenLine(baseline=(), boundary=None, region_ids=("r1",)),  # nothing
        KrakenLine(
            baseline=((12, 30), (180, 30)),
            boundary=((12, 20), (180, 20), (180, 40), (12, 40)),
            region_ids=("r1",),
        ),
    ]
    result = kraken_segmentation_to_regions(
        lines, regions, model_size=(200, 200), original_size=(200, 200)
    )
    assert len(result) == 1
    assert len(result[0].lines) == 1


def test_baseline_with_many_points_is_preserved():
    # Kraken baselines are polylines (curved text → more than two points); the
    # mapping must not collapse them to two endpoints.
    baseline = ((0, 10), (50, 12), (100, 10), (150, 8), (200, 10))
    lines = [
        KrakenLine(
            baseline=baseline,
            boundary=((0, 0), (200, 0), (200, 20), (0, 20)),
        )
    ]
    result = kraken_segmentation_to_regions(
        lines, {}, model_size=(200, 20), original_size=(200, 20)
    )
    assert result[0].lines[0].baseline == [(0, 10), (50, 12), (100, 10), (150, 8), (200, 10)]


# --------------------------------------------------------------------------- #
# 3. Availability safety with the optional package absent
# --------------------------------------------------------------------------- #


def test_provider_module_imports_without_optional_package():
    # This test module already imported artifice_ocr.segmentation.kraken at the
    # top without kraken installed — re-assert the contract explicitly.
    import artifice_ocr.segmentation.kraken as mod

    assert mod.KrakenProvider.name == "kraken"
    assert "segmentation-kraken" in mod.KrakenProvider.requirements


def test_is_available_false_and_never_raises_when_package_absent(monkeypatch):
    import sys

    provider = KrakenProvider()
    # None in sys.modules makes ``import kraken`` raise ImportError regardless
    # of whether the package happens to be installed on this box.
    monkeypatch.setitem(sys.modules, "kraken", None)
    assert provider.is_available() is False


def test_list_available_omits_kraken_when_not_installed(monkeypatch):
    import sys

    from artifice_ocr.segmentation.registry import get_provider, list_available

    monkeypatch.setitem(sys.modules, "kraken", None)
    available = list_available()
    assert "kraken" not in [p.name for p in available]

    # get_provider returns the registered provider; is_available() is False.
    provider = get_provider("kraken")
    assert provider.name == "kraken"
    assert provider.is_available() is False


# --------------------------------------------------------------------------- #
# 4. Coordinate handling through the shared helper
# --------------------------------------------------------------------------- #


def test_coordinate_restoration_invokes_shared_helper(monkeypatch):
    import artifice_ocr.segmentation.kraken as mod

    calls: list[tuple[tuple[int, int], tuple[int, int]]] = []
    real = mod.restore_original_coordinates

    def _spy(points, *, model_size, original_size):
        calls.append((model_size, original_size))
        return real(points, model_size=model_size, original_size=original_size)

    monkeypatch.setattr(mod, "restore_original_coordinates", _spy)

    kraken_segmentation_to_regions(
        [
            KrakenLine(
                baseline=((0, 0), (10, 0)),
                boundary=((0, 0), (10, 0), (10, 5), (0, 5)),
            )
        ],
        {},
        model_size=(100, 100),
        original_size=(100, 100),
    )
    assert calls, "restore_original_coordinates should be used for geometry"
    assert all(call == ((100, 100), (100, 100)) for call in calls)


def test_coordinate_restoration_unit_scale_rounds_and_clamps():
    # Kraken returns original-space coordinates, so segment() passes a unit
    # scale — the helper then rounds/clamps fractional/out-of-range values.
    lines = [
        KrakenLine(
            baseline=((-1.4, 5.5), (100.6, 5.5)),
            boundary=((-1.4, 2.5), (100.6, 2.5), (100.6, 8.5), (-1.4, 8.5)),
        )
    ]
    result = kraken_segmentation_to_regions(
        lines, {}, model_size=(100, 100), original_size=(100, 100)
    )
    # -1.4 clamps to 0; 100.6 clamps to 100; 2.5 rounds to 2; 8.5 rounds to 8
    # (round-half-even → 8) — all integers in original space.
    assert result[0].lines[0].baseline == [(0, 6), (100, 6)]
    assert all(isinstance(v, int) for point in result[0].lines[0].baseline for v in point)


def test_coordinate_restoration_scales_when_model_size_differs():
    # If a future provider/model resized internally, the mapping scales back.
    # Kraken does not need this (it restores to original space), but the seam
    # must not accidentally assume unit scale.
    regions = {
        "text": [
            KrakenRegion(id="r1", boundary=((0, 0), (25, 0), (25, 25), (0, 25))),
        ]
    }
    result = kraken_segmentation_to_regions(
        [], regions, model_size=(50, 50), original_size=(100, 200)
    )
    assert result[0].polygon == [(0, 0), (50, 0), (50, 100), (0, 100)]


# --------------------------------------------------------------------------- #
# 5. Opt-in live manuscript interop
# --------------------------------------------------------------------------- #

_LIVE_MARKER = pytest.mark.live_interop


def _manuscript_page(dest: Path) -> Path:
    """Build a synthetic manuscript-style page for the live interop test.

    No committed fixture is a handwritten/manuscript page
    (``proceedings_usnm_173.jpg`` is a single printed journal column), so the
    live test constructs one: a single column of text lines standing in for a
    handwritten page. A real manuscript scan should replace this if one
    becomes available.
    """
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1200, 1600
    img = Image.new("L", (width, height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSerif.ttf", 36)
    except OSError:
        font = ImageFont.load_default()

    body = (
        "In the year of our Lord one thousand seven hundred and ninety the "
        "people of this parish did gather to record the weather and the crops "
    )
    line_height = 56
    for i in range(24):
        start = (i * 17) % len(body)
        line = (body[start:] + " " + body)[:55]
        draw.text((90, 70 + i * line_height), line, fill="black", font=font)
    img.save(dest)
    return dest


@_LIVE_MARKER
@pytest.mark.skipif(
    os.environ.get("ARTIFICE_LIVE_SEGMENTATION") != "1",
    reason="set ARTIFICE_LIVE_SEGMENTATION=1 to run real Kraken BLLA inference",
)
def test_kraken_blla_manuscript_live(tmp_path, monkeypatch):
    import importlib.util

    if importlib.util.find_spec("kraken") is None:
        pytest.skip("kraken not installed")

    from artifice_ocr import config
    from artifice_ocr.pipeline import run_ocr_step
    from artifice_ocr.stages import ocr

    page = _manuscript_page(tmp_path / "manuscript.png")

    # This test's live surface is *segmentation*. Per-region OCR is stubbed so
    # it needs no live OCR backend; the crop → OCR → assemble pipeline path is
    # still what runs.
    monkeypatch.setattr(
        ocr, "_ocr_single_image", lambda path, orientation=1: ("MANUSCRIPT LINE", "stub")
    )

    config.apply_overrides(
        {
            "segmentation_enabled": True,
            "segmentation_provider": "kraken",
            "segmentation_options": {"device": "cpu"},
        }
    )

    result = run_ocr_step(str(page), str(tmp_path / "out"), force=True)

    doc = result["_page_document"]
    assert doc is not None
    assert len(doc.regions) >= 1, "Kraken BLLA should detect at least one region"

    # Kraken is the first provider that populates line baselines.
    assert any(line.baseline for region in doc.regions for line in region.lines)

    assert all(r.stage_text("raw") for r in doc.regions)
    assert len(doc.reading_order) == len(doc.regions)

    # The resolved model identity was surfaced into PAGE metadata.
    import json

    seg_steps = [s for s in doc.metadata.processing_steps if s.name == "segmentation"]
    assert seg_steps, "expected a segmentation processing step"
    model = json.loads(seg_steps[0].value)["model"]
    assert model == "bundled:blla.mlmodel"

    # XSD-valid PAGE (offline, against the vendored schema).
    from artifice_ocr.pagexml import schema_path, serialize
    from lxml import etree

    schema = etree.XMLSchema(etree.parse(str(schema_path())))
    schema.assertValid(etree.fromstring(serialize(doc)))
