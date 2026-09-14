# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Commit 4: DocLayout-YOLO segmentation provider.

Covers, without downloading weights or running the model:

- the documented DocStructBench → Artifice label map (every class maps to
  ``"unclassified"`` — the annotation vocabulary has no layout categories);
- the detection → ``Region`` conversion (type, confidence, polygon);
- availability safety with the optional package absent (``False``, never
  raises, never partially imports);
- registry listing (``list_available`` omits the provider when unavailable);
- coordinate restoration through the shared helper.

Plus one opt-in ``live_interop`` two-column test that runs real
DocLayout-YOLO inference and is excluded from the default suite by
``pyproject.toml``'s ``addopts``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from artifice_ocr.segmentation.doclayout_yolo import (
    Detection,
    DocLayoutYoloProvider,
    detections_to_regions,
)

DOCSTRUCTBENCH_CLASSES = [
    "title",
    "plain text",
    "abandon",
    "figure",
    "figure_caption",
    "table",
    "table_caption",
    "table_footnote",
    "isolate_formula",
    "formula_caption",
]


# --------------------------------------------------------------------------- #
# 1. Label map and conversion, no weights required
# --------------------------------------------------------------------------- #


def test_label_map_covers_every_docstructbench_class():
    from artifice_ocr.segmentation.doclayout_yolo import _DOCLAYOUT_YOLO_LABEL_MAP

    assert set(_DOCLAYOUT_YOLO_LABEL_MAP) == set(DOCSTRUCTBENCH_CLASSES)


def test_every_class_maps_to_unclassified_and_page_legal():
    from artifice_ocr.pagexml import artifice_custom, page_region_type
    from artifice_ocr.segmentation.doclayout_yolo import _DOCLAYOUT_YOLO_LABEL_MAP

    for label in DOCSTRUCTBENCH_CLASSES:
        artifact_type = _DOCLAYOUT_YOLO_LABEL_MAP[label]
        assert artifact_type == "unclassified"
        # The PAGE-legal type for "unclassified" is "other", and the custom
        # string round-trips through the Artifice vocabulary.
        assert page_region_type(artifact_type) == "other"
        assert artifice_custom(artifact_type) == "structure {type:unclassified;}"


def test_detections_to_regions_builds_regions_with_type_and_confidence():
    detections = [
        Detection(box=(10.0, 20.0, 200.0, 80.0), class_id=0, confidence=0.9),
        Detection(box=(10.0, 100.0, 200.0, 160.0), class_id=1, confidence=0.5),
        # Unknown class id — normalises to "unclassified", never raises.
        Detection(box=(300.0, 20.0, 500.0, 80.0), class_id=999, confidence=0.7),
    ]
    names = {0: "title", 1: "plain text"}
    regions = detections_to_regions(
        detections, names=names, model_size=(600, 400), original_size=(600, 400)
    )
    assert len(regions) == 3
    assert [r.type for r in regions] == ["unclassified", "unclassified", "unclassified"]
    assert [r.confidence for r in regions] == [0.9, 0.5, 0.7]
    assert regions[0].polygon == [(10, 20), (200, 20), (200, 80), (10, 80)]
    # order is left unset so the pipeline's geometric fallback applies.
    assert all(r.order is None for r in regions)


# --------------------------------------------------------------------------- #
# 2. Availability safety with the optional package absent
# --------------------------------------------------------------------------- #


def test_provider_module_imports_without_optional_package():
    # This test module already imported artifice_ocr.segmentation.doclayout_yolo
    # at the top without doclayout-yolo installed — re-assert the contract
    # explicitly so a future top-level import regression is caught here.
    import artifice_ocr.segmentation.doclayout_yolo as mod

    assert mod.DocLayoutYoloProvider.name == "doclayout-yolo"
    assert "segmentation-doclayout-yolo" in mod.DocLayoutYoloProvider.requirements


def test_is_available_false_and_never_raises_when_package_absent(monkeypatch):
    import sys

    provider = DocLayoutYoloProvider()
    # None in sys.modules makes ``import doclayout_yolo`` raise ImportError
    # regardless of whether the package happens to be installed on this box.
    monkeypatch.setitem(sys.modules, "doclayout_yolo", None)
    assert provider.is_available() is False


# --------------------------------------------------------------------------- #
# 3. Registry listing omits the provider when unavailable
# --------------------------------------------------------------------------- #


def test_list_available_omits_doclayout_when_not_installed(monkeypatch):
    import sys

    from artifice_ocr.segmentation.registry import get_provider, list_available

    monkeypatch.setitem(sys.modules, "doclayout_yolo", None)
    available = list_available()
    # Membership, not exact list: other optional providers (kraken, diff-residual
    # in Commit 5/6) may or may not be installed on this box.
    assert "doclayout-yolo" not in [p.name for p in available]

    # get_provider returns the registered provider; is_available() is False.
    provider = get_provider("doclayout-yolo")
    assert provider.name == "doclayout-yolo"
    assert provider.is_available() is False


# --------------------------------------------------------------------------- #
# 4. Coordinate restoration through the shared helper
# --------------------------------------------------------------------------- #


def test_coordinate_restoration_invokes_shared_helper(monkeypatch):
    import artifice_ocr.segmentation.doclayout_yolo as mod

    calls: list[tuple[tuple[int, int], tuple[int, int]]] = []
    real = mod.restore_original_coordinates

    def _spy(points, *, model_size, original_size):
        calls.append((model_size, original_size))
        return real(points, model_size=model_size, original_size=original_size)

    monkeypatch.setattr(mod, "restore_original_coordinates", _spy)

    detections_to_regions(
        [Detection(box=(0.0, 0.0, 512.0, 512.0), class_id=1, confidence=0.9)],
        names={1: "plain text"},
        model_size=(512, 512),
        original_size=(1000, 1000),
    )
    assert calls == [((512, 512), (1000, 1000))]


def test_coordinate_restoration_scales_to_original_space():
    # The model letterboxed to 1024x1024; the original page is 1200x1800.
    # Model-space boxes must come back as original-space integer pixels.
    detections = [
        Detection(box=(0.0, 0.0, 1024.0, 1024.0), class_id=1, confidence=0.8),
    ]
    regions = detections_to_regions(
        detections,
        names={1: "plain text"},
        model_size=(1024, 1024),
        original_size=(1200, 1800),
    )
    assert regions[0].polygon == [(0, 0), (1200, 0), (1200, 1800), (0, 1800)]
    assert all(isinstance(v, int) for point in regions[0].polygon for v in point)


def test_coordinate_restoration_clamps_out_of_bounds_detection():
    detections = [
        Detection(box=(-50.0, -50.0, 2000.0, 3000.0), class_id=1, confidence=0.8),
    ]
    regions = detections_to_regions(
        detections,
        names={1: "plain text"},
        model_size=(1000, 1000),
        original_size=(500, 600),
    )
    # -50 clamps to 0; 2000*(500/1000)=1000 clamps to 500; 3000*(600/1000)=1800
    # clamps to 600.
    assert regions[0].polygon == [(0, 0), (500, 0), (500, 600), (0, 600)]


# --------------------------------------------------------------------------- #
# 5. Opt-in live two-column interop
# --------------------------------------------------------------------------- #

_LIVE_MARKER = pytest.mark.live_interop


def _two_column_page(dest: Path) -> Path:
    """Build a synthetic two-column page for the live interop test.

    No committed fixture is a two-column scan (``proceedings_usnm_173.jpg`` is
    a single journal column), so the live test constructs one. Real text is
    rendered into two columns so DocLayout-YOLO sees plausible text blocks; a
    genuine two-column scan should replace this if one becomes available.

    The first version of this fixture picked a fixed 60-character line length
    without checking it against the actual rendered width: at 24pt
    DejaVuSerif, 60 characters measures ~780-900px, but the two columns were
    only 580px apart — the "columns" physically overlapped into one
    continuous block of text, which is exactly why DocLayout-YOLO correctly
    detected a single "plain text" region instead of two (verified live: a
    real inference run against the old fixture returned 1 region). Column
    width is now measured with the actual font metrics and text is trimmed to
    fit, with a real visible gutter between columns, so what's rendered is
    genuinely two separate text blocks rather than a two-column *label* on
    what visually reads as one.
    """
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1200, 1600
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSerif.ttf", 24)
    except OSError:
        font = ImageFont.load_default()

    body = (
        "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
        "tempor incididunt ut labore et dolore magna aliqua ut enim ad minim "
    )
    col_width = 480
    gutter = 80
    columns = [60, 60 + col_width + gutter]  # 60 and 620; both columns fit within 1200

    def _fit(line: str) -> str:
        while line and draw.textlength(line, font=font) > col_width:
            line = line[:-1]
        return line

    line_height = 36
    top = 80
    for i in range(38):
        start = (i * 13) % len(body)
        line = _fit((body[start:] + " " + body)[:60])
        y = top + i * line_height
        for x in columns:
            draw.text((x, y), line, fill="black", font=font)
    img.save(dest)
    return dest


@_LIVE_MARKER
@pytest.mark.skipif(
    os.environ.get("ARTIFICE_LIVE_SEGMENTATION") != "1",
    reason="set ARTIFICE_LIVE_SEGMENTATION=1 to run real DocLayout-YOLO inference",
)
def test_doclayout_yolo_two_column_live(tmp_path, monkeypatch):
    import importlib.util

    if importlib.util.find_spec("doclayout_yolo") is None:
        pytest.skip("doclayout-yolo not installed")

    from artifice_ocr import config
    from artifice_ocr.pipeline import run_ocr_step
    from artifice_ocr.stages import ocr

    page = _two_column_page(tmp_path / "two_column.png")

    # This test's live surface is *segmentation*. Per-region OCR is stubbed so
    # it needs no live OCR backend; the crop → OCR → assemble pipeline path is
    # still what runs.
    monkeypatch.setattr(
        ocr, "_ocr_single_image", lambda path, orientation=1: ("COLUMN TEXT", "stub")
    )

    config.apply_overrides(
        {
            "segmentation_enabled": True,
            "segmentation_provider": "doclayout-yolo",
            "segmentation_options": {"device": "cpu", "confidence_threshold": 0.2},
        }
    )

    result = run_ocr_step(str(page), str(tmp_path / "out"), force=True)

    doc = result["_page_document"]
    assert doc is not None
    assert len(doc.regions) >= 2, "DocLayout-YOLO should detect multiple layout regions"

    # Every detected region got OCR text through the pipeline path, and the
    # reading order is fully assembled.
    assert all(r.stage_text("raw") for r in doc.regions)
    assert len(doc.reading_order) == len(doc.regions)

    # The resolved weights identity was surfaced into PAGE metadata.
    import json

    seg_steps = [s for s in doc.metadata.processing_steps if s.name == "segmentation"]
    assert seg_steps, "expected a segmentation processing step"
    model = json.loads(seg_steps[0].value)["model"]
    assert model.startswith("juliozhao/DocLayout-YOLO-DocStructBench@")

    # XSD-valid PAGE (offline, against the vendored schema).
    from artifice_ocr.pagexml import schema_path, serialize
    from lxml import etree

    schema = etree.XMLSchema(etree.parse(str(schema_path())))
    schema.assertValid(etree.fromstring(serialize(doc)))
