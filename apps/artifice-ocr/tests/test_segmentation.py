# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Commit 3: segmentation contract and passthrough plumbing.

Covers the neutral provider seam (Region/Line dataclasses, the
SegmentationProvider protocol, the registry), the neutral-to-PAGE converter
and reading-order fallback, the clamped-padding crop math, the always-available
passthrough provider, the disabled path's "never touch segmentation"
guarantee, and per-region error isolation.
"""

from pathlib import Path

import pytest
from artifice_ocr import config


def _real_png(tmp_path: Path, name: str = "page.png", size=(120, 200)) -> Path:
    """A real, decodable PNG — the crop and dimension probes open it."""
    from PIL import Image

    p = tmp_path / name
    Image.new("RGB", size, "white").save(p)
    return p


# --------------------------------------------------------------------------- #
# 1. Disabled path never imports or touches the segmentation package
# --------------------------------------------------------------------------- #


def test_disabled_path_never_touches_segmentation(tmp_path, monkeypatch):
    import sys

    from artifice_ocr import pipeline
    from artifice_ocr.stages import ocr

    config.apply_overrides({"segmentation_enabled": False})
    monkeypatch.setattr(
        ocr, "_ocr_single_image", lambda path, orientation=1: ("RAW TEXT.", "ollama")
    )

    called: list[str] = []
    monkeypatch.setattr(pipeline, "_run_segmented_ocr", lambda *a, **k: called.append("segmented"))

    # Drop any segmentation modules a prior test imported, so the assertion
    # below proves this disabled run did not (re)introduce them.
    for mod in list(sys.modules):
        if mod == "artifice_ocr.segmentation" or mod.startswith("artifice_ocr.segmentation."):
            del sys.modules[mod]

    img = _real_png(tmp_path)
    result = pipeline.run_ocr_step(str(img), str(tmp_path / "out"))

    assert result["extracted_text"] == "RAW TEXT."
    assert called == []
    assert not any(
        mod == "artifice_ocr.segmentation" or mod.startswith("artifice_ocr.segmentation.")
        for mod in sys.modules
    )


# --------------------------------------------------------------------------- #
# 2. Passthrough: one valid region, byte-identical transcription text
# --------------------------------------------------------------------------- #


def test_passthrough_produces_one_region_and_matching_text(tmp_path, monkeypatch):
    from artifice_ocr.output import page_path, stage_dir
    from artifice_ocr.pipeline import run_ocr_step
    from artifice_ocr.stages import ocr

    monkeypatch.setattr(
        ocr,
        "_ocr_single_image",
        lambda path, orientation=1: ("LINE ONE\nLINE TWO\n", "ollama"),
    )
    config.apply_overrides({"confidence_enabled": False})

    img = _real_png(tmp_path)

    config.apply_overrides({"segmentation_enabled": False})
    whole = run_ocr_step(str(img), str(tmp_path / "whole"), force=True)

    config.apply_overrides({"segmentation_enabled": True, "segmentation_provider": "passthrough"})
    seg = run_ocr_step(str(img), str(tmp_path / "seg"), force=True)

    # Byte-for-byte on the extracted text, not just "looks similar".
    assert seg["extracted_text"] == whole["extracted_text"] == "LINE ONE\nLINE TWO\n"

    doc = seg["_page_document"]
    assert doc is not None
    assert len(doc.regions) == 1
    region = doc.regions[0]
    assert region.type == "other"
    assert region.custom == "structure {type:unclassified;}"
    assert region.polygon == [(0, 0), (120, 0), (120, 200), (0, 200)]
    assert region.stage_text("raw") == "LINE ONE\nLINE TWO\n"
    assert doc.reading_order == [region.id]
    assert doc.image_width == 120 and doc.image_height == 200

    step_names = [step.name for step in doc.metadata.processing_steps]
    assert "segmentation" in step_names
    assert "ocr" in step_names
    assert seg["segmentation"]["provider"] == "passthrough"

    # Schema-valid PAGE.
    from artifice_ocr.pagexml import schema_path, serialize
    from lxml import etree

    schema = etree.XMLSchema(etree.parse(str(schema_path())))
    schema.assertValid(etree.fromstring(serialize(doc)))

    # The legacy raw_ocr .txt projection is the same ordered join, and the
    # authoritative PAGE file exists where the resolver says it should.
    txt = stage_dir(tmp_path / "seg", "raw_ocr") / "text" / f"{img.stem}.txt"
    assert txt.read_text(encoding="utf-8") == "LINE ONE\nLINE TWO\n"
    assert page_path(tmp_path / "seg", img.stem).is_file()


# --------------------------------------------------------------------------- #
# 3. Padding clamps at every page edge
# --------------------------------------------------------------------------- #


def test_padded_crop_bounds_clamps_at_all_edges():
    from artifice_ocr.pagexml.geometry import padded_crop_bounds

    # Top-left corner: padding can only clamp, never go negative.
    assert padded_crop_bounds(
        [(0, 0), (10, 0), (10, 10), (0, 10)], padding=0.5, image_size=(100, 100)
    ) == (0, 0, 15, 15)

    # Bottom-right corner touching both far edges.
    assert padded_crop_bounds(
        [(90, 90), (100, 90), (100, 100), (90, 100)],
        padding=0.5,
        image_size=(100, 100),
    ) == (85, 85, 100, 100)

    # Full-page region: padding is a no-op (clamps back to the bounds).
    assert padded_crop_bounds(
        [(0, 0), (100, 0), (100, 100), (0, 100)], padding=0.2, image_size=(100, 100)
    ) == (0, 0, 100, 100)

    # Region already past the far edge (defensive): clamps, never exceeds.
    assert padded_crop_bounds(
        [(95, 95), (120, 95), (120, 120), (95, 120)],
        padding=0.1,
        image_size=(100, 100),
    ) == (93, 93, 100, 100)

    # Region starting off-page (negative): clamps to zero.
    assert padded_crop_bounds(
        [(-5, -5), (5, -5), (5, 5), (-5, 5)], padding=0.5, image_size=(100, 100)
    ) == (0, 0, 10, 10)


# --------------------------------------------------------------------------- #
# 4. Neutral-to-PAGE conversion, ordering, ids, and partial failures
# --------------------------------------------------------------------------- #


def test_to_text_region_maps_type_custom_confidence_baseline():
    from artifice_ocr.pagexml.convert import to_text_region
    from artifice_ocr.segmentation.model import Line, Region

    region = Region(
        id="r1",
        type="marginalia",
        polygon=[(1, 2), (3, 2), (3, 4), (1, 4)],
        confidence=0.9,
        lines=[Line(id="l1", polygon=[(1, 2), (3, 2)], baseline=[(1, 2), (3, 2)])],
    )
    tr = to_text_region(region, "r1")
    assert tr.id == "r1"
    assert tr.type == "marginalia"
    assert tr.custom == "structure {type:marginalia;}"
    assert tr.polygon == [(1, 2), (3, 2), (3, 4), (1, 4)]
    assert tr.confidence == 0.9
    assert len(tr.lines) == 1
    assert tr.lines[0].id == "l1"
    assert tr.lines[0].baseline == [(1, 2), (3, 2)]

    # An unknown label normalises to "other" / "unclassified".
    unknown = Region(id="r2", type="not-a-real-type", polygon=[(0, 0), (1, 0), (1, 1), (0, 1)])
    tr2 = to_text_region(unknown, "r2")
    assert tr2.type == "other"
    assert tr2.custom == "structure {type:unclassified;}"


def test_order_regions_geometric_fallback_and_explicit_order():
    from artifice_ocr.pagexml.convert import order_regions
    from artifice_ocr.segmentation.model import Region

    top = Region(id="top", type="unclassified", polygon=[(0, 0), (10, 0), (10, 10), (0, 10)])
    bottom = Region(
        id="bottom", type="unclassified", polygon=[(0, 50), (10, 50), (10, 60), (0, 60)]
    )
    # Geometric fallback: top-to-bottom, then left-to-right.
    assert [r.id for r in order_regions([bottom, top])] == ["top", "bottom"]

    # Explicit order wins when every region carries one.
    a = Region(
        id="a",
        type="unclassified",
        polygon=[(0, 50), (10, 50), (10, 60), (0, 60)],
        order=1,
    )
    b = Region(
        id="b",
        type="unclassified",
        polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        order=0,
    )
    assert [r.id for r in order_regions([a, b])] == ["b", "a"]


def test_assign_region_ids_dedupes_and_fills_invalid():
    from artifice_ocr.pagexml.convert import assign_region_ids
    from artifice_ocr.segmentation.model import Region

    poly = [(0, 0), (1, 0), (1, 1), (0, 1)]
    regions = [
        Region(id="", type="unclassified", polygon=poly),
        Region(id="region-0001", type="unclassified", polygon=poly),
        Region(id="region-0001", type="unclassified", polygon=poly),
    ]
    ids = assign_region_ids(regions)
    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert ids == ["region-0001", "region-0001-1", "region-0001-2"]


def test_partial_region_failure_does_not_fail_page(tmp_path, monkeypatch):
    from artifice_ocr import pipeline
    from artifice_ocr.segmentation import registry
    from artifice_ocr.segmentation.model import Region
    from artifice_ocr.stages import ocr

    config.apply_overrides({"segmentation_enabled": True, "segmentation_provider": "passthrough"})

    class _TwoRegionProvider:
        name = "passthrough"
        requirements = ""

        def is_available(self):
            return True

        def segment(self, image_path, options):
            return [
                Region(
                    id="left",
                    type="unclassified",
                    polygon=[(0, 0), (50, 0), (50, 100), (0, 100)],
                    order=0,
                ),
                Region(
                    id="right",
                    type="unclassified",
                    polygon=[(50, 0), (100, 0), (100, 100), (50, 100)],
                    order=1,
                ),
            ]

    monkeypatch.setattr(registry, "get_provider", lambda name: _TwoRegionProvider())

    calls = []

    def _ocr(path, orientation=1):
        calls.append(path)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return "REGION TEXT", "ollama"

    monkeypatch.setattr(ocr, "_ocr_single_image", _ocr)

    img = _real_png(tmp_path, name="page.png", size=(100, 100))
    result = pipeline.run_ocr_step(str(img), str(tmp_path / "out"), force=True)

    assert len(calls) == 2
    # Only the successful region contributes text; the page still completes.
    assert result["extracted_text"] == "REGION TEXT"
    doc = result["_page_document"]
    assert len(doc.regions) == 2
    texts = [r.stage_text("raw") for r in doc.regions]
    assert texts.count("REGION TEXT") == 1

    errors = [s for s in doc.metadata.processing_steps if s.name == "region-error"]
    assert len(errors) == 1
    assert "boom" in errors[0].value
    assert "right" in errors[0].value
    assert result["segmentation"]["region_error_count"] == 1


# --------------------------------------------------------------------------- #
# 5. Availability list is safe with no optional packages installed
# --------------------------------------------------------------------------- #


def test_list_available_safe_with_no_optional_packages(monkeypatch):
    import sys

    from artifice_ocr.segmentation.registry import get_provider, list_available

    # Simulate a clean environment: none of the optional providers' packages
    # are installed. Monkeypatching each to None in sys.modules makes
    # ``import <pkg>`` raise regardless of whether it happens to be on this box.
    for pkg in ("cv2", "kraken", "doclayout_yolo"):
        monkeypatch.setitem(sys.modules, pkg, None)

    available = list_available()
    assert [p.name for p in available] == ["passthrough"]
    assert all(p.is_available() for p in available)

    assert get_provider("passthrough").is_available() is True

    # Commit 4 registers doclayout-yolo, so get_provider returns it rather
    # than raising KeyError — but without the optional package installed its
    # is_available() is False and list_available() omits it.
    assert get_provider("doclayout-yolo").is_available() is False

    # Commit 5 registers kraken the same way.
    assert get_provider("kraken").is_available() is False

    # Commit 6 registers diff-residual the same way.
    assert get_provider("diff-residual").is_available() is False

    with pytest.raises(KeyError):
        get_provider("not-a-real-provider")
