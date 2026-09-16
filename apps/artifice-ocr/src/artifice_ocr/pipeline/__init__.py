# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OCR pipeline facade — resume/skip checks and the four stage runners.

The resume/skip-check functions (``_output_exists``, ``_load_existing_text``,
``_load_ocr_sidecar``, ``_ocr_should_resume``) and the four stage-runner
functions that call them (``run_ocr_step``, ``run_cleanup_step``,
``run_title_step``, ``run_translate_step``) deliberately live HERE, in this
package module, rather than in a submodule. Tests monkeypatch
``artifice_ocr.pipeline._output_exists`` and ``artifice_ocr.pipeline._load_existing_text``
on this module; for those patches to reach the call sites inside the stage
runners, both the patched functions and their callers must resolve those names
through this module's globals. Splitting them apart would silently break the
patches.

The stage modules (``ocr``, ``cleanup``, ``title``, ``translate``) are imported
from ``artifice_ocr.stages`` and re-exported as top-level attributes so that
``patch("artifice_ocr.pipeline.ocr.perform")`` keeps mutating the shared module
object itself.
"""

import json
import time
from pathlib import Path

from .._logging import get_logger
from ..config import get as cfg
from ..output import load_page, record_dir, stage_dir
from ..pagexml import PageDocument
from ..stages import cleanup, ocr, title, translate

log = get_logger("pipeline")

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}

# Why a stage reported `_skipped: True`. Distinct from `_skipped`
# itself so a caller (jobs.py) can tell "the user never enabled this stage"
# apart from "this page was already processed" — a re-run of an already-OCR'd
# folder looked identical to a deselected stage before this existed, which is
# exactly the bug this pair of constants fixes.
SKIP_NOT_SELECTED = "not_selected"
SKIP_ALREADY_EXISTS = "already_exists"

# The id of the single implicit full-page region used in segmentation-disabled
# mode. There is exactly one region per page until Commit 3 introduces real
# segmentation.
_IMPLICIT_REGION_ID = "region-0001"

from .document import (  # noqa: E402
    _build_implicit_document,
    _persist_page,
    _processing_step_value,
    _record_stage,
)
from .segmentation_ocr import _run_segmented_ocr  # noqa: E402


def _output_exists(stage: str, stem: str, output_dir: str) -> bool:
    """Check if output for a stage already exists."""
    p = stage_dir(output_dir, stage) / "text" / f"{stem}.txt"
    return p.exists()


def _load_existing_text(stage: str, stem: str, output_dir: str) -> str:
    p = stage_dir(output_dir, stage) / "text" / f"{stem}.txt"
    return p.read_text(encoding="utf-8")


def _load_ocr_sidecar(stem: str, output_dir: str) -> dict | None:
    """Read the raw_ocr JSON sidecar for `stem`, or None if it doesn't exist
    or can't be parsed."""
    p = record_dir(output_dir, "raw_ocr") / f"{stem}.json"
    if not p.exists():
        p = Path(output_dir) / "raw_ocr" / "json" / f"{stem}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _ocr_should_resume(key: str, output_dir: str, source: dict | None) -> bool:
    """Decide whether OCR should be skipped and the existing output reused.

    Two colliding Tropy photos can share an output key (see
    ``tropy_jsonld.page_stem``'s docstring) — so existence of ``{key}.txt``
    alone is not proof this is the SAME photo the caller is about to OCR.
    When both the current photo and the existing sidecar carry an identity
    (checksum and/or photo id), they must match or this re-OCRs instead of
    silently reusing another photo's text.

    Non-destructive by design: every sidecar written before this check
    existed carries no identity fields at all, and any source lacking an
    identity (a plain, non-Tropy file) has nothing to compare — both cases
    fall back to the plain existence check that has always governed resume,
    so every output already on disk stays valid.
    """
    if not _output_exists("raw_ocr", key, output_dir):
        return False

    current = ocr._source_identity_fields(source)
    if not current:
        return True  # nothing to compare against — existence is enough

    sidecar = _load_ocr_sidecar(key, output_dir)
    if not sidecar:
        return True  # sidecar missing or unreadable — legacy fallback

    sidecar_identity = {
        k: sidecar[k] for k in ("checksum", "photo_id") if sidecar.get(k) not in (None, "")
    }
    if not sidecar_identity:
        return True  # sidecar predates identity tracking — legacy fallback

    if "checksum" in current and "checksum" in sidecar_identity:
        return current["checksum"] == sidecar_identity["checksum"]
    if "photo_id" in current and "photo_id" in sidecar_identity:
        return current["photo_id"] == sidecar_identity["photo_id"]
    return True  # no field comparable on both sides — fall back to existence


def run_ocr_step(
    file_path: str | Path,
    output_dir: str,
    *,
    skip_ocr: bool = False,
    resume: bool = True,
    force: bool = False,
    page: int | None = None,
    stem: str | None = None,
    orientation: int = 1,
    source: dict | None = None,
) -> dict:
    """Run the OCR stage for one file, or resolve it from existing output.

    `page` selects a single 0-based PDF page; `stem` overrides the output key,
    which is what keeps per-page outputs from colliding on the filename stem.
    `orientation` is Tropy's `photos.orientation` value (EXIF 1-8 convention,
    1 = normal) — a Tropy-sourced item can be scanned rotated or upside-down
    with nothing in the filename or the image's own EXIF data to say so; this
    is the one place that information travels from the Tropy database to the
    image the model actually sees. `source` is the JobItem's own source dict
    (checksum / photo id, when the item came from Tropy) — it's what lets the
    resume check (:func:`_ocr_should_resume`) tell two colliding stems apart,
    and it's forwarded into the sidecar JSON so a *future* resume can do the
    same.

    Returns the raw_ocr data dict, annotated with `_elapsed` and (when the
    stage did not actually run) `_skipped` plus `_skip_reason` — either
    ``SKIP_NOT_SELECTED`` (the user didn't enable OCR) or
    ``SKIP_ALREADY_EXISTS`` (paired with `_skip_key`, the output key whose
    existing text was reused).
    """
    f = Path(file_path)
    key = stem or f.stem
    t0 = time.monotonic()
    page_doc: PageDocument | None = None

    if skip_ocr:
        log.info("OCR %s [skipped by user]", f.name)
        data = {
            "source_file": str(f),
            "stage": "raw_ocr",
            "extracted_text": "(OCR skipped)",
            "_skipped": True,
            "_skip_reason": SKIP_NOT_SELECTED,
        }
    elif resume and not force and _ocr_should_resume(key, output_dir, source):
        log.info("OCR %s [skip — already done]", key)
        # PAGE is the authoritative record: re-serve already-done text from it
        # when present, falling back to the .txt projection for pre-PAGE
        # folders. The resume *decision* still goes through the identity-aware
        # `.txt` check above — PAGE carries no checksum/photo-id, so it cannot
        # replace that safety net.
        page_doc = load_page(output_dir, key)
        if page_doc is not None:
            text = page_doc.text("raw") or ""
        else:
            text = _load_existing_text("raw_ocr", key, output_dir)
            # A pre-PAGE folder: build an in-memory compatibility document so a
            # caller that now expects one has it — but do NOT write it; this
            # page was never re-processed.
            page_doc = _build_implicit_document(f, page=page, orientation=orientation)
        data = {
            "source_file": str(f),
            "stage": "raw_ocr",
            "extracted_text": text,
            "_skipped": True,
            "_skip_reason": SKIP_ALREADY_EXISTS,
            "_skip_key": key,
        }
    else:
        # Pre-flight: a Tropy-imported photo may have passed pathcheck but
        # not exist on disk (the import sets a 'missing' flag but still
        # queues the item). Failing here with an actionable message beats
        # a raw FileNotFoundError from inside the OCR backend.
        if not f.exists():
            raise FileNotFoundError(f"Source file not found on disk: {f.name}")
        segmented = bool(cfg("segmentation_enabled")) and not (
            f.suffix.lower() == ".pdf" and page is None
        )
        if segmented:
            data = _run_segmented_ocr(
                f,
                output_dir=output_dir,
                page=page,
                stem=stem,
                orientation=orientation,
                source=source,
            )
            page_doc = data.get("_page_document")
        else:
            data = ocr.perform(
                str(f),
                output_dir=output_dir,
                page=page,
                stem=stem,
                orientation=orientation,
                source=source,
            )
            # PAGE persistence is a pure side effect of an actual stage execution —
            # never of a resume/skip, and never of the legacy .txt/JSON write the
            # perform() call above already did.
            page_doc = load_page(output_dir, key) or _build_implicit_document(
                f, page=page, orientation=orientation
            )
            if page_doc is not None:
                page_doc.regions[0].set_stage_text("raw", data.get("extracted_text", ""))
                _record_stage(
                    page_doc,
                    name="ocr",
                    value=_processing_step_value(
                        backend=data.get("engine"), model=data.get("model")
                    ),
                )
                _persist_page(page_doc, output_dir, key)

    if page_doc is not None:
        data["_page_document"] = page_doc
    data["_elapsed"] = time.monotonic() - t0
    return data


def run_cleanup_step(
    raw_data: dict,
    stem: str,
    output_dir: str,
    *,
    skip_cleanup: bool = False,
    resume: bool = True,
    force: bool = False,
) -> dict:
    """Run the cleanup stage for one file, or resolve it from existing output."""
    t0 = time.monotonic()
    page_doc = raw_data.get("_page_document") if isinstance(raw_data, dict) else None

    if skip_cleanup:
        log.info("Cleanup %s [skipped by user]", stem)
        data = {
            "source_file": raw_data["source_file"],
            "stage": "cleaned",
            "cleaned_text": raw_data["extracted_text"],
            "raw_text": raw_data["extracted_text"],
            "_skipped": True,
            "_skip_reason": SKIP_NOT_SELECTED,
        }
    elif resume and not force and _output_exists("cleaned", stem, output_dir):
        log.info("Cleanup %s [skip — already done]", stem)
        page_doc = page_doc or load_page(output_dir, stem)
        if page_doc is not None and page_doc.text("cleaned"):
            cleaned_text = page_doc.text("cleaned") or ""
        else:
            cleaned_text = _load_existing_text("cleaned", stem, output_dir)
        data = {
            "source_file": raw_data["source_file"],
            "stage": "cleaned",
            "cleaned_text": cleaned_text,
            "raw_text": raw_data["extracted_text"],
            "_skipped": True,
            "_skip_reason": SKIP_ALREADY_EXISTS,
            "_skip_key": stem,
        }
    else:
        data = cleanup.perform(
            raw_data["extracted_text"],
            source_file=raw_data["source_file"],
            output_dir=output_dir,
            stem=stem,
        )
        if page_doc is None:
            page_doc = load_page(output_dir, stem) or _build_implicit_document(
                raw_data.get("source_file", ""), page=None, orientation=1
            )
        if page_doc is not None:
            page_doc.regions[0].set_stage_text("cleaned", data.get("cleaned_text", ""))
            _record_stage(
                page_doc,
                name="cleanup",
                value=_processing_step_value(backend=data.get("engine"), model=data.get("model")),
            )
            _persist_page(page_doc, output_dir, stem)

    if page_doc is not None:
        data["_page_document"] = page_doc
    data["_elapsed"] = time.monotonic() - t0
    return data


def run_title_step(
    cleaned_data: dict,
    stem: str,
    output_dir: str,
    *,
    skip_title: bool = False,
    resume: bool = True,
    force: bool = False,
) -> dict:
    """Run the title stage for one file, or resolve it from existing output."""
    t0 = time.monotonic()
    page_doc = cleaned_data.get("_page_document") if isinstance(cleaned_data, dict) else None

    if skip_title:
        log.info("Title %s [skipped by user]", stem)
        data = {
            "source_file": cleaned_data["source_file"],
            "stage": "title",
            "title": Path(cleaned_data["source_file"]).stem,
            "_skipped": True,
            "_skip_reason": SKIP_NOT_SELECTED,
        }
    elif resume and not force and _output_exists("title", stem, output_dir):
        log.info("Title %s [skip — already done]", stem)
        page_doc = page_doc or load_page(output_dir, stem)
        data = {
            "source_file": cleaned_data["source_file"],
            "stage": "title",
            "title": _load_existing_text("title", stem, output_dir),
            "_skipped": True,
            "_skip_reason": SKIP_ALREADY_EXISTS,
            "_skip_key": stem,
        }
    else:
        data = title.perform(
            cleaned_data["cleaned_text"],
            source_file=cleaned_data["source_file"],
            output_dir=output_dir,
            stem=stem,
        )
        # Titles are not part of PAGE's TextEquiv indices — they only record a
        # processing-step metadata entry (never a text index).
        page_doc = page_doc or load_page(output_dir, stem)
        if page_doc is not None:
            _record_stage(
                page_doc,
                name="title",
                value=_processing_step_value(model=data.get("model")),
            )
            _persist_page(page_doc, output_dir, stem)

    if page_doc is not None:
        data["_page_document"] = page_doc
    data["_elapsed"] = time.monotonic() - t0
    return data


def run_translate_step(
    cleaned_data: dict,
    stem: str,
    output_dir: str,
    *,
    resume: bool = True,
    force: bool = False,
) -> dict:
    """Run the translate stage for one file, or resolve it from existing output."""
    t0 = time.monotonic()
    page_doc = cleaned_data.get("_page_document") if isinstance(cleaned_data, dict) else None

    if resume and not force and _output_exists("translated", stem, output_dir):
        log.info("Translate %s [skip — already done]", stem)
        page_doc = page_doc or load_page(output_dir, stem)
        if page_doc is not None and page_doc.text("translated"):
            translated_text = page_doc.text("translated") or ""
        else:
            translated_text = _load_existing_text("translated", stem, output_dir)
        data = {
            "source_file": cleaned_data["source_file"],
            "stage": "translated",
            "translated_text": translated_text,
            "cleaned_text": cleaned_data["cleaned_text"],
            "_skipped": True,
            "_skip_reason": SKIP_ALREADY_EXISTS,
            "_skip_key": stem,
        }
    else:
        data = translate.perform(
            cleaned_data["cleaned_text"],
            source_file=cleaned_data["source_file"],
            output_dir=output_dir,
            stem=stem,
        )
        if page_doc is None:
            page_doc = load_page(output_dir, stem)
        if page_doc is not None:
            page_doc.regions[0].set_stage_text("translated", data.get("translated_text", ""))
            _record_stage(
                page_doc,
                name="translate",
                value=_processing_step_value(backend=data.get("engine"), model=data.get("model")),
            )
            _persist_page(page_doc, output_dir, stem)

    if page_doc is not None:
        data["_page_document"] = page_doc
    data["_elapsed"] = time.monotonic() - t0
    return data


from .batch import (  # noqa: E402
    _collect_files,
    _run_phase,
    run_pipeline,
    run_pipeline_batch,
)

__all__ = [
    # Stage-module constants (jobs.py reads these to explain skip reasons).
    "SKIP_ALREADY_EXISTS",
    "SKIP_NOT_SELECTED",
    # Shared module objects (tests patch `pipeline.ocr.perform` / `.cleanup.perform`).
    "cleanup",
    "ocr",
    "title",
    "translate",
    # Public pipeline entry points and helpers.
    "SUPPORTED_EXTENSIONS",
    "_collect_files",
    "_run_phase",
    "run_cleanup_step",
    "run_ocr_step",
    "run_pipeline",
    "run_pipeline_batch",
    "run_title_step",
    "run_translate_step",
    # Resume/skip checks kept here as historical patch targets.
    "_load_existing_text",
    "_output_exists",
]
