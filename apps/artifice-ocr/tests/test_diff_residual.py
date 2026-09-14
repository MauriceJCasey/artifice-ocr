# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Commit 6: diff-residual segmentation provider.

Covers, split along the same seam as Commits 4-5:

- the conservative component classification (underline / marginalia /
  ``unclassified``, and the ambiguous shapes that must *not* be force-fit);
- the component → ``Region`` conversion;
- reference / option validation (clear errors, never bare exceptions);
- availability safety with OpenCV absent (``False``, never raises, never
  partially imports) and registry listing;
- generated-image alignment and component extraction (phase-correlation
  translation, ORB non-square transform, coloured annotations, insufficient
  features, invalid references, debug artifacts) — these run the real OpenCV
  algorithm and skip per-test when ``cv2`` is absent;
- one opt-in ``live_interop`` end-to-end run.

The generated-image tests skip (``pytest.importorskip``) when OpenCV is not
installed: the ``segmentation-diff-residual`` extra is deliberately not part of
``--extra all`` or the dev group, so a clean CI box without the extra still
passes the default suite — exactly the availability posture Commits 4-5
established.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest
from artifice_ocr.segmentation.diff_residual import (
    Component,
    DiffResidualError,
    DiffResidualProvider,
    classify_component,
    components_to_regions,
)

# --------------------------------------------------------------------------- #
# 1. Classification (pure — no OpenCV required)
# --------------------------------------------------------------------------- #


def test_classify_underline():
    # Thin, wide, horizontal, sitting under existing text.
    assert (
        classify_component(
            width=300,
            height=5,
            deviation_deg=3.0,
            centroid=(150, 220),
            has_ink_above=True,
            text_bbox=(50, 40, 400, 500),
        )
        == "underline"
    )


def test_classify_marginalia_left_margin():
    # A block in the left margin, outside the text block.
    assert (
        classify_component(
            width=30,
            height=40,
            deviation_deg=80.0,
            centroid=(15, 300),
            has_ink_above=False,
            text_bbox=(60, 40, 400, 500),
        )
        == "marginalia"
    )


def test_classify_marginalia_top_margin():
    assert (
        classify_component(
            width=50,
            height=20,
            deviation_deg=45.0,
            centroid=(200, 10),
            has_ink_above=False,
            text_bbox=(60, 40, 400, 500),
        )
        == "marginalia"
    )


def test_classify_unclassified_interior_blob():
    # A square blob inside the text block, nothing annotation-shaped about it.
    assert (
        classify_component(
            width=15,
            height=15,
            deviation_deg=45.0,
            centroid=(200, 250),
            has_ink_above=False,
            text_bbox=(60, 40, 400, 500),
        )
        == "unclassified"
    )


def test_ambiguous_horizontal_without_ink_above_is_not_underline():
    # Wide, thin, horizontal — but no text above it, so it is NOT an underline
    # (a bare line floating alone must not be force-fit).
    assert (
        classify_component(
            width=200,
            height=4,
            deviation_deg=5.0,
            centroid=(150, 250),
            has_ink_above=False,
            text_bbox=(60, 40, 400, 500),
        )
        == "unclassified"
    )


def test_ambiguous_diagonal_stroke_is_not_underline():
    # Wide, thin, with ink above — but steeply diagonal, so not an underline.
    assert (
        classify_component(
            width=120,
            height=5,
            deviation_deg=50.0,
            centroid=(150, 250),
            has_ink_above=True,
            text_bbox=(60, 40, 400, 500),
        )
        == "unclassified"
    )


def test_marginalia_requires_outside_text_bbox():
    # The same left-margin geometry but inside the text block → unclassified.
    assert (
        classify_component(
            width=30,
            height=40,
            deviation_deg=80.0,
            centroid=(70, 300),
            has_ink_above=False,
            text_bbox=(60, 40, 400, 500),
        )
        == "unclassified"
    )


def test_classification_only_emits_artifact_vocabulary():
    from artifice_ocr.pagexml import ARTIFICE_REGION_TYPES, artifice_custom, page_region_type

    cases = [
        dict(width=300, height=5, deviation_deg=3.0, centroid=(150, 220), has_ink_above=True),
        dict(width=30, height=40, deviation_deg=80.0, centroid=(15, 300), has_ink_above=False),
        dict(width=15, height=15, deviation_deg=45.0, centroid=(200, 250), has_ink_above=False),
    ]
    text_bbox = (60, 40, 400, 500)
    for kw in cases:
        typ = classify_component(text_bbox=text_bbox, **kw)
        assert typ in ARTIFICE_REGION_TYPES
        # Both real types map to legal PAGE and round-trip through the custom
        # vocabulary; "unclassified" is "other".
        assert page_region_type(typ) in {"marginalia", "other"}
        assert artifice_custom(typ) == f"structure {{type:{typ};}}"


# --------------------------------------------------------------------------- #
# 2. Component → Region conversion (pure)
# --------------------------------------------------------------------------- #


def test_components_to_regions_builds_regions():
    components = [
        Component(
            bbox=(100, 205, 300, 4), centroid=(250, 207), deviation_deg=1.0, has_ink_above=True
        ),
        Component(
            bbox=(10, 300, 30, 40), centroid=(25, 320), deviation_deg=80.0, has_ink_above=False
        ),
    ]
    regions = components_to_regions(components, text_bbox=(60, 40, 400, 500))
    assert [r.id for r in regions] == ["diffres-0001", "diffres-0002"]
    assert [r.type for r in regions] == ["underline", "marginalia"]
    assert regions[0].polygon == [(100, 205), (400, 205), (400, 209), (100, 209)]
    assert all(r.order is None for r in regions)  # pipeline applies reading order
    assert all(r.confidence is None for r in regions)  # no confidence concept here


def test_components_to_regions_no_text_bbox_means_no_marginalia():
    # With a blank reference there is no text block, so nothing can be
    # "marginalia"; a wide flat line with ink above stays unclassified.
    components = [
        Component(
            bbox=(100, 205, 300, 4),
            centroid=(250, 207),
            deviation_deg=1.0,
            has_ink_above=False,
        )
    ]
    regions = components_to_regions(components, text_bbox=None)
    assert regions[0].type == "unclassified"


# --------------------------------------------------------------------------- #
# 3. Reference / option validation (no OpenCV required)
# --------------------------------------------------------------------------- #


def test_reference_missing_raises_actionable_error(tmp_path):
    provider = DiffResidualProvider()
    missing = tmp_path / "no_such.png"
    with pytest.raises(DiffResidualError, match="Reference image not found"):
        provider._resolve_reference({"reference_image": str(missing)})


def test_reference_absent_option_raises_actionable_error():
    provider = DiffResidualProvider()
    with pytest.raises(DiffResidualError, match="requires 'reference_image'"):
        provider._resolve_reference({})


def test_reference_unreadable_raises_actionable_error(tmp_path):
    provider = DiffResidualProvider()
    bad = tmp_path / "corrupt.png"
    bad.write_text("this is not an image")
    with pytest.raises(DiffResidualError, match="could not be read as an image"):
        provider._resolve_reference({"reference_image": str(bad)})


def test_reference_valid_path_resolves(tmp_path):
    from PIL import Image

    provider = DiffResidualProvider()
    good = tmp_path / "ref.png"
    Image.new("RGB", (20, 20), "white").save(good)
    assert provider._resolve_reference({"reference_image": str(good)}) == good.resolve()


def test_invalid_alignment_method_rejected():
    provider = DiffResidualProvider()
    with pytest.raises(DiffResidualError, match="Invalid alignment_method"):
        provider._normalise_method({"alignment_method": "banana"})


def test_invalid_colour_channel_rejected():
    import artifice_ocr.segmentation.diff_residual as mod

    with pytest.raises(DiffResidualError, match="Invalid colour_channel"):
        mod._normalise_channel("purple")
    # Normalisation covers names, aliases, and raw BGR indices.
    assert mod._normalise_channel("grey") == "gray"
    assert mod._normalise_channel("RED") == "red"
    assert mod._normalise_channel("2") == "red"


# --------------------------------------------------------------------------- #
# 4. Availability / registry safety with OpenCV absent
# --------------------------------------------------------------------------- #


def test_provider_module_imports_without_optional_package():
    # This test module already imported diff_residual at the top without cv2 —
    # re-assert the contract explicitly.
    import artifice_ocr.segmentation.diff_residual as mod

    assert mod.DiffResidualProvider.name == "diff-residual"
    assert "segmentation-diff-residual" in mod.DiffResidualProvider.requirements


def test_is_available_false_and_never_raises_when_package_absent(monkeypatch):
    provider = DiffResidualProvider()
    # None in sys.modules makes ``import cv2`` raise ImportError regardless of
    # whether OpenCV happens to be installed on this box.
    monkeypatch.setitem(sys.modules, "cv2", None)
    assert provider.is_available() is False


def test_list_available_omits_diff_residual_when_not_installed(monkeypatch):
    from artifice_ocr.segmentation.registry import get_provider, list_available

    monkeypatch.setitem(sys.modules, "cv2", None)
    available = list_available()
    assert "diff-residual" not in [p.name for p in available]

    provider = get_provider("diff-residual")
    assert provider.name == "diff-residual"
    assert provider.is_available() is False


# --------------------------------------------------------------------------- #
# 5. Generated-image alignment and extraction (real OpenCV, skip per test)
# --------------------------------------------------------------------------- #


def _save(array, path: Path) -> Path:
    import cv2

    cv2.imwrite(str(path), array)
    return path


def _line_page(width: int = 500, height: int = 600):
    """A white page with horizontal 'text' lines, well inside the margins.

    Content lives in x∈[100,400], leaving a wide white margin so a small
    translation never exposes content at the frame edge (which would read as a
    spurious residual).
    """
    import cv2

    page = np.full((height, width, 3), 255, np.uint8)
    for i in range(8):
        cv2.rectangle(page, (100, 60 + i * 60), (400, 78 + i * 60), (0, 0, 0), -1)
    return page


def _translated_underlined_pair(tmp_path: Path):
    """Reference + input (translated by (5,7), underlined under the 4th line)."""
    import cv2

    ref = _line_page()
    matrix = np.array([[1, 0, 5], [0, 1, 7]], dtype=np.float32)
    inp = cv2.warpAffine(ref, matrix, (500, 600), borderValue=(255, 255, 255))
    cv2.rectangle(inp, (100, 205), (400, 209), (0, 0, 0), -1)  # underline
    return _save(ref, tmp_path / "ref.png"), _save(inp, tmp_path / "inp.png")


def test_phase_correlation_detects_underline(tmp_path):
    pytest.importorskip("cv2")
    ref, inp = _translated_underlined_pair(tmp_path)
    provider = DiffResidualProvider()
    regions = provider.segment(
        inp, {"reference_image": str(ref), "alignment_method": "phase_correlation"}
    )

    assert provider.alignment_method_used == "phase_correlation"
    assert provider.resolved_weights is not None
    assert provider.resolved_weights.startswith("alignment:phase_correlation;opencv:")
    underlines = [r for r in regions if r.type == "underline"]
    assert len(underlines) == 1, f"expected exactly one underline, got {regions}"
    x0, y0 = underlines[0].polygon[0]
    assert 95 <= x0 <= 105 and 200 <= y0 <= 210  # the drawn underline, in input space


def test_auto_prefers_phase_correlation_for_translation(tmp_path):
    pytest.importorskip("cv2")
    ref, inp = _translated_underlined_pair(tmp_path)
    provider = DiffResidualProvider()
    regions = provider.segment(inp, {"reference_image": str(ref)})
    assert provider.alignment_method_used == "phase_correlation"
    assert any(r.type == "underline" for r in regions)


def _text_page(width: int = 600, height: int = 700):
    """A page of rendered text — dense, non-repetitive texture for ORB."""
    import cv2
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSerif.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    body = (
        "In the year of our Lord one thousand seven hundred and ninety the "
        "people of this parish did gather to record the weather and the crops "
        "and the price of bread upon the market square. "
    )
    for i in range(20):
        start = (i * 11) % len(body)
        draw.text((60, 60 + i * 32), (body[start:] + " " + body)[:60], fill="black", font=font)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _non_square_transformed_pair(tmp_path: Path):
    """Reference + input under a non-square affine (0.97x/1.03y) + 1.5° rotation.

    The marginalia note is a solid block in the left margin, outside the text.
    """
    import cv2

    width, height = 600, 700
    ref = _text_page(width, height)
    rotation = cv2.getRotationMatrix2D((width / 2, height / 2), 1.5, 1.0)
    rotated = cv2.warpAffine(
        ref, rotation, (width, height), flags=cv2.INTER_LINEAR, borderValue=(255, 255, 255)
    )
    src = np.array([[0, 0], [width, 0], [0, height]], dtype=np.float32)
    dst = np.array([[0, 0], [int(width * 0.97), 0], [0, int(height * 1.03)]], dtype=np.float32)
    matrix = cv2.getAffineTransform(src, dst)
    inp = cv2.warpAffine(
        rotated, matrix, (width, height), flags=cv2.INTER_LINEAR, borderValue=(255, 255, 255)
    )
    cv2.rectangle(inp, (12, 400), (42, 430), (0, 0, 0), -1)  # marginalia
    return _save(ref, tmp_path / "ref.png"), _save(inp, tmp_path / "inp.png")


def test_orb_detects_marginalia_under_non_square_transform(tmp_path):
    pytest.importorskip("cv2")
    ref, inp = _non_square_transformed_pair(tmp_path)
    provider = DiffResidualProvider()
    regions = provider.segment(inp, {"reference_image": str(ref), "alignment_method": "orb"})

    assert provider.alignment_method_used == "orb"
    marginalia = [r for r in regions if r.type == "marginalia"]
    assert len(marginalia) == 1, f"expected one marginalia region, got {regions}"
    x0, y0 = marginalia[0].polygon[0]
    assert 5 <= x0 <= 20 and 395 <= y0 <= 405
    # No alignment halo may be force-classified as an underline.
    assert not any(r.type == "underline" for r in regions)


def test_coloured_annotation_detected_and_channel_selectable(tmp_path):
    pytest.importorskip("cv2")
    import cv2

    ref = _line_page()
    inp = ref.copy()
    # A pure-red underline: BGR (0,0,255). In the *red* channel it is invisible
    # (255 on white 255); in gray and blue it differs strongly.
    cv2.rectangle(inp, (100, 205), (400, 209), (0, 0, 255), -1)
    ref_p = _save(ref, tmp_path / "ref.png")
    inp_p = _save(inp, tmp_path / "inp.png")

    gray = DiffResidualProvider().segment(inp_p, {"reference_image": str(ref_p)})
    assert any(r.type == "underline" for r in gray), "red annotation must show in gray"

    red = DiffResidualProvider().segment(
        inp_p, {"reference_image": str(ref_p), "colour_channel": "red"}
    )
    assert not any(r.type == "underline" for r in red), "red annotation is invisible in red channel"


def test_insufficient_features_fails_gracefully(tmp_path):
    pytest.importorskip("cv2")
    import cv2

    blank = np.full((300, 300, 3), 255, np.uint8)
    inp = blank.copy()
    cv2.rectangle(inp, (50, 50), (80, 80), (0, 0, 0), -1)
    ref_p = _save(blank, tmp_path / "blank_ref.png")
    inp_p = _save(inp, tmp_path / "blank_inp.png")

    provider = DiffResidualProvider()
    with pytest.raises(DiffResidualError, match="too few features|Insufficient"):
        provider.segment(inp_p, {"reference_image": str(ref_p), "alignment_method": "orb"})


def test_invalid_reference_missing_file_end_to_end(tmp_path):
    pytest.importorskip("cv2")
    provider = DiffResidualProvider()
    with pytest.raises(DiffResidualError, match="Reference image not found"):
        provider.segment(tmp_path / "x.png", {"reference_image": str(tmp_path / "nope.png")})


def test_sift_used_when_explicitly_selected(tmp_path):
    pytest.importorskip("cv2")
    ref, inp = _non_square_transformed_pair(tmp_path)
    provider = DiffResidualProvider()
    regions = provider.segment(inp, {"reference_image": str(ref), "alignment_method": "sift"})
    assert provider.alignment_method_used == "sift"
    assert any(r.type == "marginalia" for r in regions)


# --------------------------------------------------------------------------- #
# 6. Debug artifacts (real OpenCV, skip per test)
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_debug_mode_writes_three_artifacts_and_never_touches_sources(tmp_path):
    pytest.importorskip("cv2")
    ref, inp = _translated_underlined_pair(tmp_path)
    ref_before = _sha256(ref)
    inp_before = _sha256(inp)

    provider = DiffResidualProvider()
    provider.segment(
        inp,
        {"reference_image": str(ref), "debug": True, "alignment_method": "phase_correlation"},
    )

    assert provider.debug_dir is not None
    assert provider.debug_dir.is_dir()
    assert provider.debug_dir.name.startswith("ocr_diffres_")
    assert set(p.name for p in provider.debug_dir.iterdir()) == {
        "aligned_reference.png",
        "residual_mask.png",
        "overlay.png",
    }
    # Neither source image was modified.
    assert _sha256(ref) == ref_before
    assert _sha256(inp) == inp_before


def test_debug_dir_is_not_inside_either_source_directory(tmp_path):
    pytest.importorskip("cv2")
    ref, inp = _translated_underlined_pair(tmp_path)
    provider = DiffResidualProvider()
    provider.segment(inp, {"reference_image": str(ref), "debug": True})
    assert provider.debug_dir is not None
    assert provider.debug_dir != ref.parent  # a fresh temp dir, not ref's folder
    assert provider.debug_dir != inp.parent


# --------------------------------------------------------------------------- #
# 7. Opt-in live interop (excluded from the default suite)
# --------------------------------------------------------------------------- #

_LIVE_MARKER = pytest.mark.live_interop


@_LIVE_MARKER
@pytest.mark.skipif(
    not __import__("os").environ.get("ARTIFICE_LIVE_SEGMENTATION"),
    reason="set ARTIFICE_LIVE_SEGMENTATION=1 to run the diff-residual end-to-end run",
)
def test_diff_residual_end_to_end_live(tmp_path, monkeypatch):
    import importlib.util

    if importlib.util.find_spec("cv2") is None:
        pytest.skip("cv2 not installed")

    from artifice_ocr import config
    from artifice_ocr.pipeline import run_ocr_step
    from artifice_ocr.stages import ocr

    ref, inp = _translated_underlined_pair(tmp_path)

    # This test's live surface is *segmentation*. Per-region OCR is stubbed so
    # it needs no live OCR backend; the crop → OCR → assemble pipeline path is
    # still what runs.
    monkeypatch.setattr(ocr, "_ocr_single_image", lambda path, orientation=1: ("TEXT", "stub"))

    config.apply_overrides(
        {
            "segmentation_enabled": True,
            "segmentation_provider": "diff-residual",
            "segmentation_options": {
                "reference_image": str(ref),
                "alignment_method": "phase_correlation",
            },
        }
    )

    result = run_ocr_step(str(inp), str(tmp_path / "out"), force=True)

    doc = result["_page_document"]
    assert doc is not None
    # At least the underline region is detected. The neutral Artifice type
    # travels through the PAGE mapping as the ``@custom`` string ("underline"
    # is a legal-PAGE "other"), so assert on ``custom``, not ``type``.
    assert any("type:underline" in r.custom for r in doc.regions)

    import json

    seg_steps = [s for s in doc.metadata.processing_steps if s.name == "segmentation"]
    assert seg_steps, "expected a segmentation processing step"
    model = json.loads(seg_steps[0].value)["model"]
    assert model.startswith("alignment:phase_correlation;opencv:")

    # XSD-valid PAGE (offline, against the vendored schema).
    from artifice_ocr.pagexml import schema_path, serialize
    from lxml import etree

    schema = etree.XMLSchema(etree.parse(str(schema_path())))
    schema.assertValid(etree.fromstring(serialize(doc)))
