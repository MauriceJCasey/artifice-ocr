# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Render collected pages into Markdown (single-language and bilingual).

Extracted from the former flat `pdf_export.py` module — see this package's
`__init__.py` for the compatibility facade that keeps every existing
`artifice_ocr.pdf_export.X` import working unchanged.
"""

import itertools
from pathlib import Path

from artifice_ocr._logging import get_logger

from .models import BilingualPageText, PageText

log = get_logger("pdf_export")

# ---------------------------------------------------------------------------
# Render Markdown
# ---------------------------------------------------------------------------


def render_markdown(
    pages: list[PageText],
    output_path: Path,
    *,
    title: str | None = None,
) -> Path:
    """Render the collected pages into a Markdown file."""
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")

    current_section = None
    for i, page in enumerate(pages):
        page_section = getattr(page, "section", None)
        if page_section and page_section != current_section:
            current_section = page_section
            lines.append(f"## {current_section}")
            lines.append("")

        lines.append(f"### Page {i + 1}")
        lines.append("")
        lines.append(f"[{page.label}]")
        lines.append("")
        lines.append(page.text)
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Markdown written to %s (%d page(s))", output_path, len(pages))
    return output_path


# ---------------------------------------------------------------------------
# Render bilingual Markdown (pipe-table: original | translation)
# ---------------------------------------------------------------------------


def render_bilingual_markdown(
    pages: list[BilingualPageText],
    output_path: Path,
    *,
    title: str | None = None,
) -> Path:
    """Render bilingual pages into a Markdown file with pipe tables."""
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")

    for i, page in enumerate(pages):
        lines.append(f"## Page {i + 1}")
        lines.append("")
        lines.append(f"[{page.label}]")
        lines.append("")

        orig_paras = [p.strip() for p in page.original_text.split("\n\n") if p.strip()]
        trans_paras = [p.strip() for p in page.translated_text.split("\n\n") if p.strip()]

        paired = list(itertools.zip_longest(orig_paras, trans_paras, fillvalue=""))

        if paired:
            lines.append("| Original | Translation |")
            lines.append("|---|---|")
            for orig, trans in paired:
                orig_cell = orig.replace("|", "\\|").replace("\n", " ")
                trans_cell = trans.replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {orig_cell} | {trans_cell} |")
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Bilingual Markdown written to %s (%d page(s))", output_path, len(pages))
    return output_path
