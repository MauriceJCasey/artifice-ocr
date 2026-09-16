# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pipeline orchestration — single-file and batch entry points.

These functions drive the four stage runners (``run_ocr_step``,
``run_cleanup_step``, ``run_title_step``, ``run_translate_step``), which stay
in ``__init__.py``. The stage runners themselves are not patch targets (only
functions *they* call are patched), so importing them here by name from the
package is safe.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .._logging import get_logger
from ..config import get as cfg
from . import (
    SUPPORTED_EXTENSIONS,
    run_cleanup_step,
    run_ocr_step,
    run_title_step,
    run_translate_step,
)

log = get_logger("pipeline")


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
