# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""PDF export: collect processed text, structure it, and render a readable PDF.

This package replaces the former flat `pdf_export.py` module (split per
"Test Bloat and God Script Refactor.md", Phase 5 Priority 2 — the module had
grown to 1,332 lines mixing style/font registration, manifest lookup, page
collection, bilingual pairing, model-assisted structuring, orchestration,
path validation, PDF rendering, Markdown rendering, and PAGE XML export).

This `__init__.py` is a compatibility facade: every name that used to be
`artifice_ocr.pdf_export.X` is still `artifice_ocr.pdf_export.X`, so no
caller (routers, cli.py, runtime.py, or any test) needed to change. Internal
organization only — the implementation now lives in:

  models.py           PageText, BilingualPageText
  collection.py        collect_folder, collect_stems, collect_bilingual_folder
                        + manifest/stage-lookup helpers
  structure.py          structure_pages, _structure_bilingual_pages
  render_pdf.py          PDFStyle, font registration, render_pdf, render_bilingual_pdf
  render_markdown.py     render_markdown, render_bilingual_markdown
  service.py            compile, compile_batch, export_page_xml,
                        path/filename helpers — orchestrates the above

`compile` is monkeypatched by name in tests (e.g. test_web.py's
`monkeypatch.setattr(pdf_export_module, "compile", ...)`); that continues to
work because runtime.py and cli.py both call it as `pdf_export.compile(...)`
(a dynamic module-attribute lookup), not a name bound at import time.
"""

from artifice_ocr._logging import get_logger

from .collection import (
    _find_manifest,
    _find_text_dir,
    _load_manifest,
    _natural_sort_key,
    _page_stage_text,
    _stage_fallback_order,
    collect_bilingual_folder,
    collect_folder,
    collect_stems,
)
from .models import BilingualPageText, PageText
from .render_markdown import render_bilingual_markdown, render_markdown
from .render_pdf import (
    PDF_STYLES,
    PDFStyle,
    _clean_para,
    _escape_html,
    _get_styles,
    _register_fonts,
    render_bilingual_pdf,
    render_pdf,
)
from .service import (
    _default_export_path,
    _safe_filename,
    compile,
    compile_batch,
    default_batch_output,
    export_page_xml,
    page_export_dir,
)
from .structure import _structure_bilingual_pages, structure_pages

log = get_logger("pdf_export")

__all__ = [
    "PDF_STYLES",
    "BilingualPageText",
    "PDFStyle",
    "PageText",
    # Public API.
    "collect_bilingual_folder",
    "collect_folder",
    "collect_stems",
    "compile",
    "compile_batch",
    "default_batch_output",
    "export_page_xml",
    "log",
    "page_export_dir",
    "render_bilingual_markdown",
    "render_bilingual_pdf",
    "render_markdown",
    "render_pdf",
    "structure_pages",
    # Not part of the intended public API, but re-exported at this exact path
    # so anything that already reached into it (a test, a future maintainer's
    # muscle memory) keeps working. See the compatibility-facade note above.
    "_clean_para",
    "_default_export_path",
    "_escape_html",
    "_find_manifest",
    "_find_text_dir",
    "_get_styles",
    "_load_manifest",
    "_natural_sort_key",
    "_page_stage_text",
    "_register_fonts",
    "_safe_filename",
    "_stage_fallback_order",
    "_structure_bilingual_pages",
]
