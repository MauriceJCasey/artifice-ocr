# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Data structures shared across the pdf_export package.

Extracted from the former flat `pdf_export.py` module — see this package's
`__init__.py` for the compatibility facade that keeps every existing
`artifice_ocr.pdf_export.X` import working unchanged.
"""

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PageText:
    """One page of processed text destined for the PDF.

    `stem` is the pipeline stem relative to the stage text dir (forward
    slashes) — e.g. "Item Title/page_p0002" for Tropy pages, "page" for flat
    files.  It keys the structured-text resume cache, so it must stay unique
    per page within an output dir.  `section` groups pages under a heading
    when several items are combined into one PDF (batch export).
    """

    label: str
    text: str
    source_path: Path
    page_number: int | None = None
    item_title: str | None = None
    stem: str | None = None
    section: str | None = None


@dataclass
class BilingualPageText(PageText):
    """One page of bilingual text: original (cleaned) + translated."""

    original_text: str = ""
    translated_text: str = ""
