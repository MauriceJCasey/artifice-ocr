# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._logging import get_logger
from .config import get as cfg
from .output import load_page, page_path, record_dir, stage_dir
from .pagexml import PageDocument, ProcessingStep, TextRegion, artifice_custom, write
from .stages import cleanup, ocr, title, translate

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


def _processing_step_value(**fields: Any) -> str:
    """A compact JSON provenance string for a PAGE processing step.

    Mirrors the fields already recorded in the legacy stage sidecar (engine /
    model) rather than inventing new ones.
    """
    return json.dumps(
        {key: value for key, value in fields.items() if value not in (None, "")},
        sort_keys=True,
    )


def _build_implicit_document(
    file_path: str | Path,
    *,
    page: int | None,
    orientation: int,
) -> PageDocument | None:
    """Build the segmentation-disabled PAGE document: one implicit region
    covering the original page.

    Returns ``None`` for a whole-PDF (multiple pages OCR'd as one unit) — there
    is no single rendered page dimension to cover, so no authoritative per-page
    PAGE document is produced. This matches the plan's "use the selected
    rendered page dimensions for PDF JobItems"; a non-Tropy whole-PDF is OCR'd
    as one legacy unit and stays legacy-only.
    """
    f = Path(file_path)
    if f.suffix.lower() == ".pdf" and page is None:
        return None
    try:
        width, height = ocr.image_dimensions(str(f), page=page, orientation=orientation)
    except Exception as exc:
        # A dimension probe failure must not block the stage's own execution —
        # PAGE persistence is a best-effort side effect.
        log.warning("Could not measure %s for PAGE: %s", f.name, exc)
        return None
    region = TextRegion(
        id=_IMPLICIT_REGION_ID,
        type="paragraph",
        custom=artifice_custom("unclassified"),
        polygon=[(0, 0), (width, 0), (width, height), (0, height)],
    )
    return PageDocument(
        image_filename=f.name,
        image_width=width,
        image_height=height,
        regions=[region],
        reading_order=[_IMPLICIT_REGION_ID],
    )


def _record_stage(page_doc: PageDocument, *, name: str, value: str) -> None:
    """Append one processing step and bump ``last_change``."""
    stamp = datetime.now(UTC)
    page_doc.metadata.processing_steps.append(
        ProcessingStep(name=name, value=value, date=stamp)
    )
    page_doc.metadata.last_change = stamp


def _persist_page(page_doc: PageDocument, output_dir: str, key: str) -> None:
    """Write a PAGE document to its persistence path (best-effort)."""
    try:
        write(page_doc, page_path(output_dir, key))
    except OSError as exc:
        log.warning("Could not persist PAGE for %s: %s", key, exc)


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
                value=_processing_step_value(backend=data.get("engine"), model=data.get("model")),
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


def _collect_files(input_path: str) -> list[Path]:
    """If input_path is a directory, return all supported files inside it.
    If it's a file, return a single-element list."""
    p = Path(input_path).resolve()
    if p.is_dir():
        files = sorted(
            f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        if not files:
            raise FileNotFoundError(
                f"No supported files ({', '.join(SUPPORTED_EXTENSIONS)}) in {p}"
            )
        return files
    return [p]


def run_pipeline(
    input_path: str,
    output_dir: str = "output",
    *,
    skip_translate: bool = False,
    skip_cleanup: bool = False,
    skip_ocr: bool = False,
    force: bool = False,
) -> dict:
    """
    Orchestrate: OCR -> Cleanup -> Translate for a single file.
    Returns dict with raw, cleaned, and optionally translated data.
    """
    files = _collect_files(input_path)
    if len(files) == 1:
        return _run_single(
            files[0],
            output_dir,
            skip_translate=skip_translate,
            skip_cleanup=skip_cleanup,
            skip_ocr=skip_ocr,
            force=force,
        )

    return run_pipeline_batch(
        [str(f) for f in files],
        output_dir,
        skip_translate=skip_translate,
        skip_cleanup=skip_cleanup,
        skip_ocr=skip_ocr,
        force=force,
    )


def _run_single(
    file_path: Path,
    output_dir: str,
    *,
    skip_translate: bool = False,
    skip_cleanup: bool = False,
    skip_ocr: bool = False,
    force: bool = False,
    resume: bool | None = None,
) -> dict:
    """Run the full pipeline on a single file."""
    if resume is None:
        resume = cfg("resume")

    stem = file_path.stem
    t_start = time.monotonic()
    log.info("Starting pipeline for %s", file_path.name)

    raw_data = run_ocr_step(
        file_path,
        output_dir,
        skip_ocr=skip_ocr,
        resume=resume,
        force=force,
    )
    cleaned_data = run_cleanup_step(
        raw_data,
        stem,
        output_dir,
        skip_cleanup=skip_cleanup,
        resume=resume,
        force=force,
    )

    result = {
        "raw": raw_data,
        "cleaned": cleaned_data,
    }

    if cfg("title_enabled"):
        result["title"] = run_title_step(
            cleaned_data,
            stem,
            output_dir,
            resume=resume,
            force=force,
        )

    if not skip_translate:
        result["translated"] = run_translate_step(
            cleaned_data,
            stem,
            output_dir,
            resume=resume,
            force=force,
        )

    elapsed = time.monotonic() - t_start
    log.info("Pipeline complete for %s in %.1fs", file_path.name, elapsed)
    return result


def _run_phase(
    files: list[Path],
    step_fn: Callable[[Path], dict],
    *,
    on_result: Callable[[Path, dict, float], None] | None = None,
) -> tuple[dict[str, dict], dict[str, float]]:
    """Run *step_fn* once per file in *files*, collecting each result and its
    elapsed time, both keyed by the file's string path.

    Timing is recorded as 0 for any file whose stage reported ``_skipped``
    — a skipped stage's "elapsed" time is an artifact of the resume check,
    not work actually done, and the caller only wants to know real per-file
    cost. (Extracted from four nearly-identical loops in
    ``run_pipeline_batch`` that were inconsistent about this: three zeroed a
    skipped file's timing, one didn't. This applies the rule uniformly.)

    *on_result*, if given, is called after each file with
    ``(file, result, elapsed)`` for phase-specific side effects (e.g. the
    OCR phase's per-file log line) that don't belong in the shared loop.
    """
    results: dict[str, dict] = {}
    timings: dict[str, float] = {}
    for f in files:
        fpath = str(f)
        t0 = time.monotonic()
        result = step_fn(f)
        elapsed = time.monotonic() - t0
        results[fpath] = result
        timings[fpath] = 0 if result.get("_skipped") else elapsed
        if on_result:
            on_result(f, result, elapsed)
    return results, timings


def run_pipeline_batch(
    file_paths: list[str],
    output_dir: str = "output",
    *,
    skip_translate: bool = False,
    skip_cleanup: bool = False,
    skip_ocr: bool = False,
    force: bool = False,
) -> dict:
    """
    Run pipeline on multiple files in three strictly sequential passes:
      1. OCR all files
      2. Cleanup all files
      3. Translate all files

    Only one inference engine is active at a time.
    Returns dict with per-file results and batch summary.
    """
    resume = cfg("resume")
    files = [Path(f).resolve() for f in file_paths]
    t_batch_start = time.monotonic()
    log.info("Batch: %d file(s), sequential passes", len(files))

    # Phase 1: Sequential OCR
    def _log_ocr_result(f: Path, result: dict, elapsed: float) -> None:
        skipped = " [skipped]" if result.get("_skipped") else ""
        log.info(
            "  OCR %s%s -> %d chars (%.1fs)",
            f.name,
            skipped,
            len(result["extracted_text"]),
            elapsed,
        )

    ocr_results, ocr_timings = _run_phase(
        files,
        lambda f: run_ocr_step(
            f,
            output_dir,
            skip_ocr=skip_ocr,
            resume=resume,
            force=force,
        ),
        on_result=_log_ocr_result,
    )

    # Phase 2: Sequential cleanup
    cleanup_results, cleanup_timings = _run_phase(
        files,
        lambda f: run_cleanup_step(
            ocr_results[str(f)],
            f.stem,
            output_dir,
            skip_cleanup=skip_cleanup,
            resume=resume,
            force=force,
        ),
    )

    # Phase 3: Sequential title (opt-in)
    title_enabled = cfg("title_enabled")
    title_results: dict[str, dict] = {}
    title_timings: dict[str, float] = {}

    if title_enabled:
        title_results, title_timings = _run_phase(
            files,
            lambda f: run_title_step(
                cleanup_results[str(f)],
                f.stem,
                output_dir,
                resume=resume,
                force=force,
            ),
        )

    # Phase 4: Sequential translate
    translate_results: dict[str, dict] = {}
    translate_timings: dict[str, float] = {}

    if not skip_translate:
        translate_results, translate_timings = _run_phase(
            files,
            lambda f: run_translate_step(
                cleanup_results[str(f)],
                f.stem,
                output_dir,
                resume=resume,
                force=force,
            ),
        )

    # Assemble results
    all_results: dict[str, dict] = {}
    timings: dict[str, dict[str, float]] = {}

    for f in files:
        fpath = str(f)
        file_timings: dict[str, float] = {"ocr": ocr_timings.get(fpath, 0)}
        result: dict[str, Any] = {
            "raw": ocr_results[fpath],
            "cleaned": cleanup_results[fpath],
        }
        if cleanup_timings.get(fpath, 0):
            file_timings["cleanup"] = cleanup_timings[fpath]
        if title_enabled:
            result["title"] = title_results[fpath]
            if title_timings.get(fpath, 0):
                file_timings["title"] = title_timings[fpath]
        if not skip_translate:
            result["translated"] = translate_results[fpath]
            if translate_timings.get(fpath, 0):
                file_timings["translate"] = translate_timings[fpath]
        all_results[fpath] = result
        timings[fpath] = file_timings

    batch_elapsed = time.monotonic() - t_batch_start
    log.info("Batch complete in %.1fs", batch_elapsed)

    return {
        "files": all_results,
        "batch_size": len(files),
        "batch_elapsed": batch_elapsed,
        "timings": timings,
    }
