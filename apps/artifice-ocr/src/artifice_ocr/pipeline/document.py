# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""PAGE-document construction and persistence helpers.

These build the segmentation-disabled single-region PAGE document, record
processing-step provenance, and write the result to disk. None of these
functions are patch targets, so they live in their own submodule away from the
resume/skip and stage-runner functions that stay in ``__init__.py``.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .._logging import get_logger
from ..output import page_path
from ..pagexml import PageDocument, ProcessingStep, TextRegion, artifice_custom, write
from ..stages import ocr
from . import _IMPLICIT_REGION_ID

log = get_logger("pipeline")


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
    page_doc.metadata.processing_steps.append(ProcessingStep(name=name, value=value, date=stamp))
    page_doc.metadata.last_change = stamp


def _persist_page(page_doc: PageDocument, output_dir: str, key: str) -> None:
    """Write a PAGE document to its persistence path (best-effort)."""
    try:
        write(page_doc, page_path(output_dir, key))
    except OSError as exc:
        log.warning("Could not persist PAGE for %s: %s", key, exc)
