#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OCR accuracy and wall-time measurement harness.

This is the harness docs/superpowers/plans/2026-09-09-olmocr2-optimisation.md
(Task 5) calls the real blocker: there is no way to check its claims against
this app's actual behaviour, because there is no accuracy measurement at
all. Every number that plan cites is from a paper, not from this codebase.
(The original findings document it was drafted from was never checked into
version control — this plan is the closest surviving in-repo reference.)

Metric: character error rate (CER), a standard Levenshtein-distance-based
metric — NOT SpACER, the metric [CENT] (arXiv:2608.30616) uses. [CENT] picks
SpACER specifically because "CER assumes a fixed linear reading order that
does not hold for scattered annotations" (s5.1) — a real caveat for
marginalia and annotated archival material. SpACER's exact algorithm is not
reproducible from a citation alone; CER is implemented here because it is
well-defined and enough to detect changes of the size the referenced papers
describe (single-digit-percent swings). A future SpACER implementation is
welcome if the exact algorithm is sourced from the paper.

Usage:
    uv run python scripts/measure_ocr_accuracy.py eval_corpus/

Expects, for each page in the given directory:
    <stem>.png|.jpg|.jpeg|.tif|.tiff   the scanned page image
    <stem>.txt                         the ground-truth transcription
    <stem>.orientation                 optional; a single int 1-8 (default 1)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein character edit distance / len(reference), clamped to
    [0, 1]. ``(0, 0)`` (both empty) is defined as zero error, not a division
    by zero."""
    if not reference and not hypothesis:
        return 0.0
    if not reference:
        return 1.0

    # Standard O(len(ref) * len(hyp)) DP edit distance.
    ref, hyp = reference, hypothesis
    prev = list(range(len(hyp) + 1))
    for i, r_ch in enumerate(ref, start=1):
        curr = [i] + [0] * len(hyp)
        for j, h_ch in enumerate(hyp, start=1):
            cost = 0 if r_ch == h_ch else 1
            curr[j] = min(
                prev[j] + 1,  # deletion
                curr[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution/match
            )
        prev = curr
    distance = prev[-1]
    return min(1.0, distance / len(ref))


def _ocr_page(image_path: Path, orientation: int) -> str:
    """Run this app's own OCR stage on one page. Imports lazily so unit tests
    that stub this function never need a live model backend."""
    from artifice_ocr.stages.ocr import _ocr_single_image

    text, _engine = _ocr_single_image(image_path, orientation)
    return text


def measure_page(
    image_path: Path, ground_truth_path: Path, *, orientation: int = 1
) -> dict[str, Any]:
    """OCR one page and score it against its ground truth. Returns a dict
    with ``page``, ``cer``, ``wall_time_s``, ``reference_chars``."""
    reference = ground_truth_path.read_text(encoding="utf-8")
    start = time.monotonic()
    hypothesis = _ocr_page(image_path, orientation)
    elapsed = time.monotonic() - start
    return {
        "page": image_path.name,
        "cer": character_error_rate(reference, hypothesis),
        "wall_time_s": elapsed,
        "reference_chars": len(reference),
    }


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff")


def _discover_pages(corpus_dir: Path) -> list[tuple[Path, Path, int]]:
    pages = []
    for txt_path in sorted(corpus_dir.glob("*.txt")):
        stem = txt_path.stem
        image_path = next(
            (
                corpus_dir / f"{stem}{suffix}"
                for suffix in _IMAGE_SUFFIXES
                if (corpus_dir / f"{stem}{suffix}").exists()
            ),
            None,
        )
        if image_path is None:
            print(f"WARNING: no image found for {txt_path.name}, skipping", file=sys.stderr)
            continue
        orient_path = corpus_dir / f"{stem}.orientation"
        orientation = int(orient_path.read_text().strip()) if orient_path.exists() else 1
        pages.append((image_path, txt_path, orientation))
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "corpus_dir", type=Path, help="Directory of <stem>.{png,jpg,...} + <stem>.txt pairs"
    )
    args = parser.parse_args()

    pages = _discover_pages(args.corpus_dir)
    if not pages:
        print(f"No page/ground-truth pairs found in {args.corpus_dir}", file=sys.stderr)
        return 1

    results = [measure_page(img, gt, orientation=orient) for img, gt, orient in pages]

    total_chars = sum(r["reference_chars"] for r in results)
    weighted_cer = (
        sum(r["cer"] * r["reference_chars"] for r in results) / total_chars if total_chars else 0.0
    )
    total_time = sum(r["wall_time_s"] for r in results)

    for r in results:
        print(f"{r['page']:40s} CER={r['cer']:.4f}  {r['wall_time_s']:.2f}s")
    print("-" * 60)
    print(f"{'weighted CER':40s} {weighted_cer:.4f}")
    print(f"{'total wall time':40s} {total_time:.2f}s over {len(results)} page(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
