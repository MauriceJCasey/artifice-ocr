# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run the structure stage over collected pages before rendering.

Extracted from the former flat `pdf_export.py` module — see this package's
`__init__.py` for the compatibility facade that keeps every existing
`artifice_ocr.pdf_export.X` import working unchanged.
"""

from collections.abc import Callable

from artifice_ocr._logging import get_logger

from .models import BilingualPageText, PageText

log = get_logger("pdf_export")

# ---------------------------------------------------------------------------
# Structure pages
# ---------------------------------------------------------------------------


def structure_pages(
    pages: list[PageText],
    on_progress: Callable[[str], None] | None = None,
    on_rejected: Callable[[str], None] | None = None,
    output_dir: str = "output",
) -> list[PageText]:
    """Run the structure stage on each page.

    Pages whose structuring is rejected keep their original text.
    `on_progress(message)` is called once per page with a status message.
    `on_rejected(label)` is called once per page whose guard rejected.
    Returns a new list with structured text.

    `output_dir` and each page's `stem` key the structured-text resume
    cache — passing the full pipeline stem ("Item/page") keeps items whose
    pages share a filename from colliding in the cache.
    """
    on_progress = on_progress or (lambda msg: None)
    on_rejected = on_rejected or (lambda label: None)
    from artifice_ocr.stages import structure

    structured: list[PageText] = []
    n = len(pages)

    for i, page in enumerate(pages):
        message = f"Structuring {i + 1}/{n}: {page.label}"
        log.info(message)
        on_progress(message)
        result = structure.perform(
            page.text,
            source_file=str(page.source_path),
            output_dir=output_dir,
            stem=page.stem,
        )
        guard_ok = result.get("guard", {}).get("ok", True)
        if not guard_ok:
            on_rejected(page.label)
        structured.append(
            PageText(
                label=page.label,
                text=result.get("structured_text", page.text),
                source_path=page.source_path,
                page_number=page.page_number,
                item_title=page.item_title,
                stem=page.stem,
                section=page.section,
            )
        )

    return structured


def _structure_bilingual_pages(
    pages: list[BilingualPageText],
    on_progress: Callable[[str], None] | None = None,
    on_rejected: Callable[[str], None] | None = None,
    output_dir: str = "output",
) -> list[BilingualPageText]:
    """Structure bilingual pages: add paragraph breaks to original_text only.

    Translated text is left as-is — the translator already handled paragraph
    structure.  Rejected structuring keeps the original text unchanged.
    """
    on_progress = on_progress or (lambda msg: None)
    on_rejected = on_rejected or (lambda label: None)
    from artifice_ocr.stages import structure

    structured: list[BilingualPageText] = []
    n = len(pages)

    for i, page in enumerate(pages):
        message = f"Structuring {i + 1}/{n}: {page.label}"
        log.info(message)
        on_progress(message)
        result = structure.perform(
            page.original_text,
            source_file=str(page.source_path),
            output_dir=output_dir,
            stem=page.stem,
        )
        guard_ok = result.get("guard", {}).get("ok", True)
        if not guard_ok:
            on_rejected(page.label)
        structured.append(
            BilingualPageText(
                label=page.label,
                text=result.get("structured_text", page.original_text),
                source_path=page.source_path,
                page_number=page.page_number,
                item_title=page.item_title,
                original_text=result.get("structured_text", page.original_text),
                translated_text=page.translated_text,
            )
        )

    return structured
