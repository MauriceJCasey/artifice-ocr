# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Segmentation-aware OCR internals.

These materialise the original-space page image, crop per-region temp PNGs, and
run the segmentation → per-region-OCR → PAGE-assembly path. None of these
functions are patch targets, so they live in their own submodule. ``_run_segmented_ocr``
is imported by ``run_ocr_step`` (which stays in ``__init__.py``) via a plain
``from .segmentation_ocr import _run_segmented_ocr`` — safe because the function
itself is never patched directly, and its body references none of the
resume/skip-check functions that tests patch on ``artifice_ocr.pipeline``.
"""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .._logging import get_logger
from ..config import get as cfg
from ..output import write_stage_output
from ..pagexml import PageDocument
from ..pagexml.convert import assign_region_ids, order_regions, to_text_region
from ..pagexml.geometry import padded_crop_bounds, restore_original_coordinates
from ..stages import ocr
from .document import _persist_page, _processing_step_value, _record_stage

log = get_logger("pipeline")


def _segment_source_image(
    file_path: str | Path,
    *,
    page: int | None,
    orientation: int,
) -> tuple[Path, bool, int]:
    """Materialise the original-space page image segmentation works on.

    Returns ``(path, is_temp, total_pages)``. For a PDF page or an
    orientation-corrected plain image the pipeline renders a temp PNG in the
    same original space :func:`ocr.image_dimensions` describes; for a plain
    image with no orientation correction the source file *is* the original
    space and is returned untouched. A whole-PDF (``page is None``) is not
    supported here — the caller keeps the legacy path for that case.
    """
    f = Path(file_path)
    is_pdf = f.suffix.lower() == ".pdf"
    if is_pdf and page is not None:
        img, total = ocr._pdf_single_page_image(f, page, orientation)
        return img, True, total
    if orientation == 1:
        return f, False, 1

    import fitz  # PyMuPDF

    doc = fitz.open(str(f))
    try:
        page_obj = doc[0]
        orient_mat = ocr._exif_orientation_matrix(
            orientation, page_obj.rect.width, page_obj.rect.height
        )
        if orient_mat is None:
            return f, False, 1  # unrecognised orientation -> raw bytes, native space
        native = fitz.Pixmap(str(f))
        zoom = native.width / page_obj.rect.width if page_obj.rect.width else 1.0
        mat = fitz.Matrix(zoom, zoom) * orient_mat
        pix = page_obj.get_pixmap(matrix=mat)
        tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_seg_"))
        img = tmp_dir / "page_0001.png"
        pix.save(str(img))
        return img, True, 1
    finally:
        doc.close()


def _crop_to_temp(source: Path, bounds: tuple[int, int, int, int]) -> Path:
    """Crop *source* to *bounds* and write a fresh temp PNG. Returns its path."""
    from PIL import Image

    x0, y0, x1, y1 = bounds
    tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_seg_crop_"))
    out = tmp_dir / "crop.png"
    with Image.open(source) as img:
        img.crop((x0, y0, x1, y1)).save(out)
    return out


def _run_segmented_ocr(
    file_path: str | Path,
    *,
    output_dir: str,
    page: int | None,
    stem: str | None,
    orientation: int,
    source: dict | None,
) -> dict:
    """Segmentation-enabled OCR: segment, crop, per-region OCR, assemble.

    Returns the same shape as :func:`ocr.perform` (``extracted_text``,
    ``engine``, ``model``, sidecar fields) plus ``_page_document`` and a
    ``segmentation`` metadata block. Writes the legacy raw_ocr ``.txt``/
    ``.json`` projections and persists the authoritative PAGE document, so a
    caller that switches segmentation on sees the same downstream surfaces as
    the whole-page path.
    """
    from artifice_ocr.segmentation.registry import get_provider

    f = Path(file_path)
    key = stem or f.stem

    provider_name = str(cfg("segmentation_provider", "passthrough") or "passthrough")
    provider = get_provider(provider_name)
    if not provider.is_available():
        raise RuntimeError(
            f"Segmentation provider {provider.name!r} is not available: {provider.requirements}"
        )
    options = dict(cfg("segmentation_options", {}) or {})
    padding_percent = float(cfg("segmentation_crop_padding_percent", 2.0) or 0.0)
    padding_fraction = padding_percent / 100.0
    model = ocr.model_for("vision")

    width, height = ocr.image_dimensions(str(f), page=page, orientation=orientation)
    image, is_temp, total_pages = _segment_source_image(f, page=page, orientation=orientation)
    try:
        segments = provider.segment(image, options=options)

        # A provider that resolves weights at run time (DocLayout-YOLO in
        # Commit 4+) records the resolved artifact identity on itself; surface
        # it into the PAGE "segmentation" processing step. Passthrough sets no
        # such attribute, and the missing-value filter in
        # _processing_step_value drops the key rather than recording a null.
        model_identity = getattr(provider, "resolved_weights", None)

        # Normalise geometry into integer original-space pixels, reusing the
        # same rounding/clamping convention as restore_original_coordinates
        # (a unit scale) so there is exactly one convention, not two.
        def _clamp(points):
            return restore_original_coordinates(
                points, model_size=(width, height), original_size=(width, height)
            )

        for region in segments:
            region.polygon = _clamp(region.polygon)
            for line in region.lines:
                line.polygon = _clamp(line.polygon)
                if line.baseline:
                    line.baseline = _clamp(line.baseline)

        ordered = order_regions(segments)
        region_ids = assign_region_ids(ordered)
        text_regions = [
            to_text_region(region, rid) for region, rid in zip(ordered, region_ids, strict=True)
        ]

        page_doc = PageDocument(
            image_filename=f.name,
            image_width=width,
            image_height=height,
            regions=text_regions,
            reading_order=region_ids,
        )
        _record_stage(
            page_doc,
            name="segmentation",
            value=_processing_step_value(
                provider=provider.name,
                options=options or None,
                padding_percent=padding_percent,
                region_count=len(text_regions),
                model=model_identity,
            ),
        )

        engine_tags: list[str] = []
        for region, text_region, rid in zip(ordered, text_regions, region_ids, strict=True):
            if region.error:
                _record_stage(
                    page_doc,
                    name="region-error",
                    value=json.dumps({"region_id": rid, "error": region.error}, sort_keys=True),
                )
                continue
            bounds = padded_crop_bounds(
                region.polygon, padding=padding_fraction, image_size=(width, height)
            )
            crop = _crop_to_temp(image, bounds)
            try:
                text, engine = ocr._ocr_single_image(crop, orientation=1)
                text_region.set_stage_text("raw", text)
                engine_tags.append(engine)
            except Exception as exc:
                log.warning("Region %s OCR failed: %s", rid, exc)
                _record_stage(
                    page_doc,
                    name="region-error",
                    value=json.dumps({"region_id": rid, "error": str(exc)}, sort_keys=True),
                )
            finally:
                crop.unlink(missing_ok=True)
    finally:
        if is_temp:
            image.unlink(missing_ok=True)

    engine_used = ocr._summarise_engines(engine_tags)
    _record_stage(
        page_doc,
        name="ocr",
        value=_processing_step_value(backend=engine_used, model=model),
    )

    extracted_text = page_doc.text("raw")

    sidecar = {
        "source_file": str(f),
        "stage": "raw_ocr",
        "extracted_text": extracted_text,
        "engine": engine_used,
        "model": model,
        "ocr_prompt": ocr._effective_prompt(
            cfg("ocr_prompt_instruction", ""), style=cfg("ocr_prompt_style", "raw")
        ),
        "timestamp": datetime.now(UTC).isoformat(),
        "page": page + 1 if page is not None else 1,
        "total_pages": total_pages,
        "segmentation": {
            "provider": provider.name,
            "options": options,
            "padding_percent": padding_percent,
            "region_count": len(text_regions),
            "region_error_count": sum(
                1 for step in page_doc.metadata.processing_steps if step.name == "region-error"
            ),
        },
    }
    sidecar.update(ocr._source_identity_fields(source))

    write_stage_output(output_dir, "raw_ocr", key, text_content=extracted_text, metadata=sidecar)
    _persist_page(page_doc, output_dir, key)

    data = dict(sidecar)
    data["_page_document"] = page_doc
    return data
