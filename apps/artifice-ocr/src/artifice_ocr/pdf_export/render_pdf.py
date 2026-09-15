# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Render collected pages into a PDF (single-language and bilingual).

Continuous-flow reading document using reportlab Platypus with the
public_history design tokens (Playfair Display for headings, Libre Baskerville
for body text).

Extracted from the former flat `pdf_export.py` module — see this package's
`__init__.py` for the compatibility facade that keeps every existing
`artifice_ocr.pdf_export.X` import working unchanged.
"""

import importlib.resources
import itertools
import re
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from artifice_ocr._logging import get_logger

from .models import BilingualPageText, PageText

log = get_logger("pdf_export")

# ---------------------------------------------------------------------------
# Style presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PDFStyle:
    font_body: str = "librebaskerville"
    font_heading: str = "playfairdisplay"
    font_size_body: float = 10.5
    font_size_heading: float = 14.0
    line_height: float = 1.5
    show_provenance: bool = True
    margin_top: float = 50
    margin_bottom: float = 50
    margin_left: float = 60
    margin_right: float = 60


PDF_STYLES = {
    "readable": PDFStyle(font_size_body=12.0, font_size_heading=16.0, line_height=1.7),
    "academic": PDFStyle(
        font_body="times", font_heading="times", font_size_body=10.0, show_provenance=False
    ),
    "compact": PDFStyle(
        font_size_body=9.0, font_size_heading=12.0, line_height=1.3, margin_top=30, margin_bottom=30
    ),
}


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

# Resolved through importlib.resources, NOT a __file__-relative path. These fonts
# must survive being packaged: this app is distributed as a frozen .exe/.dmg, where
# __file__ points inside a temporary extraction directory and any `.parent.parent`
# walk lands somewhere meaningless. The previous form was
# `Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"`, which
# resolved outside src/ — so the fonts were excluded from the wheel entirely and
# PDF export raised at runtime in every installed copy while working perfectly in
# a source checkout.
#
# They live under `web/` only because that package already carries a package-data
# rule (pyproject.toml). They are NOT web assets and are deliberately NOT mounted —
# `/static` and `/shared` are the only mounts, and neither exposes this directory.
# If the web layer is ever restructured, move these with it and update the rule;
# ReportLab needs real TTFs and cannot read the woff2 files in packages/shared-ui,
# which is why this is a separate copy rather than a duplicate to be consolidated.
_FONTS_DIR = importlib.resources.files("artifice_ocr.web") / "fonts"
_FONTS_REGISTERED = False


def _register_fonts() -> None:
    """Register Playfair Display and Libre Baskerville with reportlab."""
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return

    try:
        pdfmetrics.registerFont(TTFont("PlayfairDisplay", str(_FONTS_DIR / "PlayfairDisplay.ttf")))
        pdfmetrics.registerFont(
            TTFont("PlayfairDisplay-Italic", str(_FONTS_DIR / "PlayfairDisplay-Italic.ttf"))
        )
        pdfmetrics.registerFont(
            TTFont("LibreBaskerville", str(_FONTS_DIR / "LibreBaskerville.ttf"))
        )
        pdfmetrics.registerFont(
            TTFont("LibreBaskerville-Italic", str(_FONTS_DIR / "LibreBaskerville-Italic.ttf"))
        )

        pdfmetrics.registerFontFamily(
            "LibreBaskerville",
            normal="LibreBaskerville",
            italic="LibreBaskerville-Italic",
        )
        pdfmetrics.registerFontFamily(
            "PlayfairDisplay",
            normal="PlayfairDisplay",
            italic="PlayfairDisplay-Italic",
        )
        _FONTS_REGISTERED = True
        log.info("Registered Playfair Display and Libre Baskerville fonts")
    except Exception as exc:
        log.warning("Could not register custom fonts, falling back to built-in: %s", exc)


def _get_styles(style: PDFStyle | None = None):
    """Build paragraph styles using bundled fonts (or built-in fallback)."""
    if style is None:
        style = PDFStyle()
    _register_fonts()
    use_custom = _FONTS_REGISTERED

    _body_map = {
        "librebaskerville": ("LibreBaskerville", "Times-Roman"),
        "times": ("Times-Roman", "Times-Roman"),
    }
    _heading_map = {
        "playfairdisplay": ("PlayfairDisplay", "Times-Bold"),
        "times": ("Times-Bold", "Times-Bold"),
    }
    _italic_map = {
        "librebaskerville": ("LibreBaskerville-Italic", "Times-Italic"),
        "times": ("Times-Italic", "Times-Italic"),
    }

    body_font = _body_map.get(style.font_body, ("Times-Roman", "Times-Roman"))[
        0 if use_custom else 1
    ]
    body_italic = _italic_map.get(style.font_body, ("Times-Italic", "Times-Italic"))[
        0 if use_custom else 1
    ]
    heading_font = _heading_map.get(style.font_heading, ("Times-Bold", "Times-Bold"))[
        0 if use_custom else 1
    ]

    styles = getSampleStyleSheet()

    body_style = ParagraphStyle(
        "PDFBody",
        parent=styles["Normal"],
        fontName=body_font,
        fontSize=style.font_size_body,
        leading=round(style.font_size_body * style.line_height, 1),
        spaceAfter=6,
        firstLineIndent=0,
        textColor="#1b1813",
    )

    heading_style = ParagraphStyle(
        "PDFHeading",
        parent=styles["Heading1"],
        fontName=heading_font,
        fontSize=style.font_size_heading,
        leading=round(style.font_size_heading * style.line_height, 1),
        spaceBefore=18,
        spaceAfter=10,
        textColor="#1b1813",
    )

    title_style = ParagraphStyle(
        "PDFTitle",
        parent=styles["Title"],
        fontName=heading_font,
        fontSize=26,
        leading=32,
        spaceAfter=24,
        textColor="#1b1813",
        alignment=1,  # center
    )

    provenance_style = ParagraphStyle(
        "PDFProvenance",
        parent=styles["Normal"],
        fontName=body_italic,
        fontSize=8,
        leading=10,
        spaceBefore=4,
        spaceAfter=12,
        textColor="#999999",
    )

    return body_style, heading_style, title_style, provenance_style


# ---------------------------------------------------------------------------
# Render PDF
# ---------------------------------------------------------------------------


def render_pdf(
    pages: list[PageText],
    output_path: Path,
    *,
    title: str | None = None,
    style: str = "readable",
) -> Path:
    """Render a continuous-flow PDF from the collected pages.

    Each page carries a small provenance marker (label + page number) so
    a reader can trace a passage back to its source scan.
    """
    style_obj = PDF_STYLES.get(style, PDF_STYLES["readable"])
    body_style, heading_style, title_style, provenance_style = _get_styles(style_obj)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=style_obj.margin_left,
        rightMargin=style_obj.margin_right,
        topMargin=style_obj.margin_top,
        bottomMargin=style_obj.margin_bottom,
    )

    story: list = []

    # Title page
    if title:
        story.append(Spacer(1, 80 * mm))
        story.append(Paragraph(_escape_html(title), title_style))
        story.append(Spacer(1, 20 * mm))
        story.append(PageBreak())

    # Pages
    current_section = None
    for i, page in enumerate(pages):
        # Section heading when a combined batch moves to a new item
        page_section = getattr(page, "section", None)
        if page_section and page_section != current_section:
            current_section = page_section
            story.append(Paragraph(_escape_html(current_section), heading_style))

        # Provenance marker at the top of each page section
        if style_obj.show_provenance:
            provenance = f"[{page.label}]"
            story.append(Paragraph(_escape_html(provenance), provenance_style))

        # Split text into paragraphs on double-newlines
        paragraphs = page.text.split("\n\n")
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # Check if this looks like a heading (short, possibly all caps or
            # starts with a salutation/date pattern)
            is_heading = len(para) < 80 and (
                para.isupper()
                or re.match(r"^(Dear|Sir|Madam|Subject|RE:|Date:)", para, re.IGNORECASE) is not None
            )

            if is_heading:
                story.append(Paragraph(_escape_html(para), heading_style))
            else:
                # Convert single newlines within a paragraph to spaces
                # (they're line breaks from OCR, not intentional)
                cleaned = re.sub(r"\n", " ", para)
                # Collapse multiple spaces
                cleaned = re.sub(r"  +", " ", cleaned)
                story.append(Paragraph(_escape_html(cleaned), body_style))

        # Add spacing between pages (except after the last one)
        if i < len(pages) - 1:
            story.append(Spacer(1, 8 * mm))

    # Build PDF
    doc.build(story)
    log.info("PDF written to %s (%d page(s))", output_path, len(pages))
    return output_path


# ---------------------------------------------------------------------------
# Render bilingual PDF (two-column: original | translation)
# ---------------------------------------------------------------------------


def render_bilingual_pdf(
    pages: list[BilingualPageText],
    output_path: Path,
    *,
    title: str | None = None,
    style: str = "readable",
) -> Path:
    """Render a two-column bilingual PDF from cleaned + translated pages.

    Each page section has:
      1. A provenance header row spanning both columns.
      2. A Table with two columns: Original | Translation.
      3. Paragraphs split on ``\\n\\n``, paired via ``zip_longest``
         (missing translations → blank right cell).
    """
    style_obj = PDF_STYLES.get(style, PDF_STYLES["readable"])
    body_style, heading_style, title_style, provenance_style = _get_styles(style_obj)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=style_obj.margin_left,
        rightMargin=style_obj.margin_right,
        topMargin=style_obj.margin_top,
        bottomMargin=style_obj.margin_bottom,
    )

    story: list = []

    # Title page
    if title:
        story.append(Spacer(1, 80 * mm))
        story.append(Paragraph(_escape_html(title), title_style))
        story.append(Spacer(1, 20 * mm))
        story.append(PageBreak())

    usable_width = A4[0] - style_obj.margin_left - style_obj.margin_right
    col_width = usable_width / 2

    for i, page in enumerate(pages):
        # Provenance header spanning both columns
        if style_obj.show_provenance:
            provenance = f"[{page.label}]"
            prov_table = Table(
                [[Paragraph(_escape_html(provenance), provenance_style)]],
                colWidths=[usable_width],
            )
            prov_table.setStyle(
                TableStyle(
                    [
                        ("SPAN", (0, 0), (-1, -1)),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(prov_table)

        orig_paras = [p.strip() for p in page.original_text.split("\n\n") if p.strip()]
        trans_paras = [p.strip() for p in page.translated_text.split("\n\n") if p.strip()]

        paired = list(itertools.zip_longest(orig_paras, trans_paras, fillvalue=""))

        if paired:
            table_data = [
                [Paragraph(_escape_html(_clean_para(p)), body_style) for p in row] for row in paired
            ]

            table = Table(table_data, colWidths=[col_width, col_width])
            table.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("GRID", (0, 0), (-1, -1), 0.5, "#cccccc"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(table)

        if i < len(pages) - 1:
            story.append(Spacer(1, 8 * mm))

    doc.build(story)
    log.info("Bilingual PDF written to %s (%d page(s))", output_path, len(pages))
    return output_path


def _clean_para(text: str) -> str:
    """Collapse newlines and multiple spaces within a paragraph for table cells."""
    text = re.sub(r"\n", " ", text)
    return re.sub(r"  +", " ", text)


def _escape_html(text: str) -> str:
    """Escape text for reportlab Paragraph markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
