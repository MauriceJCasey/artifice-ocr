# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""PDF export: collect processed text, structure it, and render a readable PDF.

Continuous-flow reading document using reportlab Platypus with the
public_history design tokens (Playfair Display for headings, Libre Baskerville
for body text).

The structuring pass is optional (--no-structure) and guarded: if the model
alters any word, the original text is kept instead.
"""

import importlib.resources
import itertools
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from artifice_output import layout_for_path, slugify
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

from .output import find_page_document, page_path, page_stage, stage_dir
from .validation import validate_path

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


# ---------------------------------------------------------------------------
# Natural sort
# ---------------------------------------------------------------------------


def _natural_sort_key(s: str) -> list:
    """Sort key that handles embedded numbers naturally.

    'page2' sorts before 'page10'.
    """
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------------------
# Collect pages
# ---------------------------------------------------------------------------


def _load_manifest(path: Path) -> dict | None:
    """Load a manifest, normalising to the page-entry mapping.

    Real manifests written by tropy_write.write_manifest() nest the entries
    under a top-level "pages" key (alongside "project"/"output_layout");
    older/synthetic manifests are a flat stem->entry mapping.  Both shapes
    are accepted and returned as the flat mapping.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("pages"), dict):
        return data["pages"]
    return data


def _find_manifest(folder: Path, explicit_path: str | None = None) -> dict | None:
    """Find a canonical or legacy manifest in the folder's parent chain."""
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return _load_manifest(p)
        log.warning("Explicit manifest path not found: %s", explicit_path)
        return None

    # Walk up parent directories
    current = folder
    for _ in range(10):  # safety limit
        for name in ("manifest.json", "tropy_manifest.json"):
            manifest_path = current / name
            if manifest_path.exists():
                return _load_manifest(manifest_path)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _stage_fallback_order(stage: str) -> list[str]:
    """Return preferred stage + fallbacks for reading text.

    Mirrors the order in tropy_write.entries_from_items():
    translated > cleaned > raw_ocr.
    """
    order = {
        "translated": ["translated", "cleaned", "raw_ocr"],
        "cleaned": ["cleaned", "raw_ocr", "translated"],
        "raw_ocr": ["raw_ocr", "cleaned", "translated"],
    }
    return order.get(stage, ["cleaned", "raw_ocr", "translated"])


def _page_stage_text(folder: str | Path, stem: str, stage: str) -> str | None:
    """Read *stem*'s text for *stage* from its persisted PAGE document.

    PAGE is the authoritative record, so export reads the requested TextEquiv
    index first. Returns ``None`` when no PAGE document exists (a pre-PAGE
    folder) or the selected index is empty, so callers fall back to the legacy
    ``.txt`` projection.
    """
    doc = find_page_document(folder, stem)
    if doc is None:
        return None
    text = doc.text(page_stage(stage))
    return text or None


def collect_folder(
    folder: str,
    *,
    stage: str = "cleaned",
    manifest_path: str | None = None,
) -> list[PageText]:
    """Collect processed text files from a folder.

    Ordering priority:
      1. If tropy_manifest.json exists (parent chain or explicit path):
         use page_number/item_title for ordering and labels.
      2. Otherwise: natural-sort filenames and use filename as label.

    `stage` selects which processed text to read — falls back through
    the stage fallback order if the preferred stage is missing.

    The folder can be:
      - An output root (e.g. "output"): looks for folder/stage/text/*.txt
      - An item folder (e.g. "output/cleaned/text/Fritz Eberhard KV"): looks for *.txt directly
      - A stage/text folder (e.g. "output/cleaned/text"): looks for *.txt directly
    """
    folder_path = Path(folder)
    manifest = _find_manifest(folder_path, manifest_path)
    pages: list[PageText] = []

    # Determine the text directory to search
    # Case 1: folder itself contains .txt files directly
    if any(folder_path.glob("*.txt")):
        primary_dir = folder_path
    else:
        # Case 2: folder/stage/text contains .txt files
        stage_dirs = _stage_fallback_order(stage)
        primary_dir = None

        for s in stage_dirs:
            candidate = stage_dir(folder_path, s) / "text"
            if candidate.exists() and any(candidate.glob("*.txt")):
                primary_dir = candidate
                break

        if primary_dir is None:
            log.warning("No text files found in %s", folder)
            return []

    # Find all .txt files recursively
    txt_files = sorted(primary_dir.rglob("*.txt"))

    if manifest:
        # Build a lookup from filename stem to manifest entry
        # Manifest keys are like "Fritz Eberhard KV/Eberhard KV 3_p0002"
        # We need to match them to files by stem name
        stem_to_entry: dict[str, dict] = {}
        for key, entry in manifest.items():
            # Use the last part of the key (filename without extension) as the lookup key
            file_stem = Path(key).stem
            stem_to_entry[file_stem] = entry
            # Also store the full key for prefix matching
            stem_to_entry[key] = entry

        for txt_path in txt_files:
            file_stem = txt_path.stem
            rel_stem = txt_path.relative_to(primary_dir).with_suffix("").as_posix()

            # Try exact stem match first, then try to find in manifest by filename
            entry = stem_to_entry.get(file_stem)
            matched_key = None

            # If not found, try matching by the last part of any manifest key
            if entry is None:
                for key, e in manifest.items():
                    if key.endswith(f"/{file_stem}") or Path(key).stem == file_stem:
                        entry = e
                        matched_key = key
                        break
            else:
                matched_key = next((k for k, e in manifest.items() if e is entry), None)

            if entry:
                page_stem = matched_key or rel_stem
                pages.append(
                    PageText(
                        label=entry.get("item_title", txt_path.stem),
                        text=_page_stage_text(folder_path, page_stem, stage)
                        or txt_path.read_text(encoding="utf-8"),
                        source_path=txt_path,
                        page_number=entry.get("page_number"),
                        item_title=entry.get("item_title"),
                        # The full manifest key ("Item/page") is the true pipeline
                        # stem — unique per page, so the structure cache cannot
                        # collide across items sharing a page filename.
                        stem=page_stem,
                    )
                )
            else:
                pages.append(
                    PageText(
                        label=txt_path.stem,
                        text=_page_stage_text(folder_path, rel_stem, stage)
                        or txt_path.read_text(encoding="utf-8"),
                        source_path=txt_path,
                        stem=rel_stem,
                    )
                )

        # Sort by page_number (None goes last)
        pages.sort(key=lambda p: (p.page_number is None, p.page_number or 0))

        # Get the item_title from the first page that has one
        title = None
        for p in pages:
            if p.item_title:
                title = p.item_title
                break
        if title:
            for p in pages:
                if not p.item_title:
                    p.item_title = title
    else:
        # No manifest: natural-sort by filename
        txt_files.sort(key=lambda p: _natural_sort_key(p.name))
        for txt_path in txt_files:
            rel_stem = txt_path.relative_to(primary_dir).with_suffix("").as_posix()
            pages.append(
                PageText(
                    label=txt_path.stem,
                    text=_page_stage_text(folder_path, rel_stem, stage)
                    or txt_path.read_text(encoding="utf-8"),
                    source_path=txt_path,
                    stem=rel_stem,
                )
            )

    log.info("Collected %d page(s) from %s", len(pages), folder)
    return pages


# ---------------------------------------------------------------------------
# Collect pages by stem (batch export)
# ---------------------------------------------------------------------------


def collect_stems(
    stems: list[str],
    *,
    output_dir: str = "output",
    stage: str = "cleaned",
    manifest_path: str | None = None,
) -> tuple[list[PageText], list[str]]:
    """Collect specific pages by pipeline stem — the batch the queue knows.

    Each stem maps to ``<output_dir>/<stage>/text/<stem>.txt``; the usual
    stage fallback order applies per page.  Stems keep caller (queue) order
    and are deduplicated.  Pages with no processed text in any stage are
    skipped and returned in the second element of the tuple — never
    silently dropped.

    Returns ``(pages, skipped_stems)``.
    """
    output_path = Path(output_dir)
    manifest = _find_manifest(output_path, manifest_path)

    stem_to_entry: dict[str, dict] = {}
    if manifest:
        for key, entry in manifest.items():
            stem_to_entry[key] = entry
            stem_to_entry.setdefault(Path(key).stem, entry)

    stage_dirs = _stage_fallback_order(stage)
    pages: list[PageText] = []
    skipped: list[str] = []
    stage_counts: dict[str, int] = {}
    seen: set[str] = set()

    for stem in stems:
        if stem in seen:
            continue
        seen.add(stem)

        txt_path = None
        stage_used = None
        for s in stage_dirs:
            candidate = stage_dir(output_path, s) / "text" / f"{stem}.txt"
            if candidate.exists():
                txt_path = candidate
                stage_used = s
                break

        if txt_path is None:
            skipped.append(stem)
            continue

        stage_counts[stage_used] = stage_counts.get(stage_used, 0) + 1
        entry = stem_to_entry.get(stem) or stem_to_entry.get(Path(stem).stem)
        item_title = entry.get("item_title") if entry else None

        pages.append(
            PageText(
                label=Path(stem).name,
                text=_page_stage_text(output_path, stem, stage)
                or txt_path.read_text(encoding="utf-8"),
                source_path=txt_path,
                page_number=entry.get("page_number") if entry else None,
                item_title=item_title,
                stem=stem,
                section=item_title or (stem.split("/")[0] if "/" in stem else None),
            )
        )

    if skipped:
        log.info(
            "Skipped %d stem(s) with no processed text: %s", len(skipped), ", ".join(skipped[:5])
        )
    if len(stage_counts) > 1:
        # The stage fallback mixed sources — make it visible which stage
        # the pages actually came from.
        mix = ", ".join(f"{s}: {n}" for s, n in sorted(stage_counts.items()))
        log.info("Collected pages from mixed stages (%s)", mix)
    log.info("Collected %d page(s) from %d stem(s) in %s", len(pages), len(seen), output_dir)
    return pages, skipped


# ---------------------------------------------------------------------------
# Collect bilingual pages
# ---------------------------------------------------------------------------


def collect_bilingual_folder(
    folder: str,
    *,
    manifest_path: str | None = None,
) -> list[BilingualPageText]:
    """Pair cleaned + translated text files by matching filenames/stems.

    Reads from ``folder/cleaned/text/`` and ``folder/translated/text/``
    (or a parent chain containing those).  Uses manifest for page ordering
    if available.  Missing translated files → blank right column.
    """
    folder_path = Path(folder)
    manifest = _find_manifest(folder_path, manifest_path)

    # Resolve cleaned and translated text directories
    cleaned_dir = _find_text_dir(folder_path, "cleaned")
    translated_dir = _find_text_dir(folder_path, "translated")

    if cleaned_dir is None:
        log.warning("No cleaned text files found in %s", folder)
        return []

    cleaned_files = {f.stem: f for f in cleaned_dir.glob("*.txt")}
    translated_files: dict[str, Path] = {}
    if translated_dir is not None:
        translated_files = {f.stem: f for f in translated_dir.glob("*.txt")}

    pages: list[BilingualPageText] = []

    if manifest:
        stem_to_entry: dict[str, dict] = {}
        for key, entry in manifest.items():
            file_stem = Path(key).stem
            stem_to_entry[file_stem] = entry
            stem_to_entry[key] = entry

        for stem, txt_path in cleaned_files.items():
            entry = stem_to_entry.get(stem)
            if entry is None:
                for key, e in manifest.items():
                    if key.endswith(f"/{stem}") or Path(key).stem == stem:
                        entry = e
                        break

            trans_path = translated_files.get(stem)
            original_text = _page_stage_text(folder_path, stem, "cleaned") or txt_path.read_text(
                encoding="utf-8"
            )
            translated_text = _page_stage_text(folder_path, stem, "translated") or (
                trans_path.read_text(encoding="utf-8") if trans_path else ""
            )
            pages.append(
                BilingualPageText(
                    label=entry.get("item_title", stem) if entry else stem,
                    text=original_text,
                    source_path=txt_path,
                    page_number=entry.get("page_number") if entry else None,
                    item_title=entry.get("item_title") if entry else None,
                    original_text=original_text,
                    translated_text=translated_text,
                )
            )

        pages.sort(key=lambda p: (p.page_number is None, p.page_number or 0))

        title = None
        for p in pages:
            if p.item_title:
                title = p.item_title
                break
        if title:
            for p in pages:
                if not p.item_title:
                    p.item_title = title
    else:
        sorted_stems = sorted(cleaned_files.keys(), key=_natural_sort_key)
        for stem in sorted_stems:
            txt_path = cleaned_files[stem]
            trans_path = translated_files.get(stem)
            original_text = _page_stage_text(folder_path, stem, "cleaned") or txt_path.read_text(
                encoding="utf-8"
            )
            translated_text = _page_stage_text(folder_path, stem, "translated") or (
                trans_path.read_text(encoding="utf-8") if trans_path else ""
            )
            pages.append(
                BilingualPageText(
                    label=stem,
                    text=original_text,
                    source_path=txt_path,
                    original_text=original_text,
                    translated_text=translated_text,
                )
            )

    log.info("Collected %d bilingual page(s) from %s", len(pages), folder)
    return pages


def _find_text_dir(folder_path: Path, stage: str) -> Path | None:
    """Locate the text directory for a given stage within folder."""
    if any(folder_path.glob("*.txt")) and stage == "cleaned":
        return folder_path
    candidate = stage_dir(folder_path, stage) / "text"
    if candidate.exists() and any(candidate.glob("*.txt")):
        return candidate
    return None


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


# ---------------------------------------------------------------------------
# Compile (collect → structure → render)
# ---------------------------------------------------------------------------


def compile(
    folder: str,
    *,
    stage: str = "cleaned",
    structure: bool = True,
    output: str | Path | None = None,
    manifest_path: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    format: str = "pdf",
    style: str = "readable",
    bilingual: bool = False,
) -> Path:
    """Collect, optionally structure, and render one folder into a PDF or Markdown.

    ``bilingual=True`` pairs cleaned + translated text into two-column output.
    Missing translations produce blank right columns.  Structure pass is
    skipped by default for bilingual mode (pass ``structure=True`` to opt in).
    """
    on_progress = on_progress or (lambda msg: None)
    validate_path(folder, "folder")
    if output is not None:
        validate_path(str(output), "output")
    if manifest_path is not None:
        validate_path(manifest_path, "manifest_path")
    folder_path = Path(folder)

    if structure:
        # The structuring pass is a model call; resolve its model/backend the
        # same way the pipeline does, so the empty/auto defaults resolve to a
        # concrete model instead of reaching the provider with an empty name.
        from artifice_ocr._resolution import resolve_models_for_run

        resolve_models_for_run(stages={"cleanup"})

    if bilingual:
        on_progress(f"Collecting bilingual pages from {folder_path}...")
        bilingual_pages = collect_bilingual_folder(str(folder_path), manifest_path=manifest_path)
        if not bilingual_pages:
            raise ValueError(
                f"No cleaned/ and translated/ text found under {folder}. "
                "Bilingual mode needs both a cleaned and a translated stage. "
                "Point at your output folder and run those stages first."
            )
        on_progress(f"Found {len(bilingual_pages)} page(s)")

        if structure:
            bilingual_pages = _structure_bilingual_pages(
                bilingual_pages,
                on_progress=on_progress,
                output_dir=str(folder_path) if layout_for_path(folder_path) else "output",
            )

        title = next((p.item_title for p in bilingual_pages if p.item_title), None)
        if output is None:
            output_path = _default_export_path(folder_path, format, bilingual=True)
        else:
            output_path = Path(output)

        if format == "md":
            on_progress("Rendering bilingual Markdown...")
            result_path = render_bilingual_markdown(bilingual_pages, output_path, title=title)
        else:
            on_progress("Rendering bilingual PDF...")
            result_path = render_bilingual_pdf(
                bilingual_pages, output_path, title=title, style=style
            )

        on_progress(f"Done: {len(bilingual_pages)} page(s) -> {result_path}")
        return result_path

    on_progress(f"Collecting pages from {folder_path}...")
    pages = collect_folder(str(folder_path), stage=stage, manifest_path=manifest_path)
    if not pages:
        raise ValueError(
            f"No '{stage}' text found under {folder}. Looked for "
            f"{folder}/{stage}/text/*.txt (and .txt files directly in the "
            "folder). Point at your output folder, or pick a Stage that has "
            "been run — the pipeline must finish that stage before it can be "
            "compiled."
        )
    on_progress(f"Found {len(pages)} page(s)")

    rejected: list[str] = []
    if structure:
        pages = structure_pages(
            pages,
            on_progress=on_progress,
            on_rejected=lambda label: rejected.append(label),
            output_dir=str(folder_path) if layout_for_path(folder_path) else "output",
        )

    title = next((p.item_title for p in pages if p.item_title), None)
    output_path = _default_export_path(folder_path, format) if output is None else Path(output)

    if format == "md":
        on_progress("Rendering Markdown...")
        result_path = render_markdown(pages, output_path, title=title)
    else:
        on_progress("Rendering PDF...")
        result_path = render_pdf(pages, output_path, title=title, style=style)

    if rejected:
        on_progress(
            f"Guard rejected structure for {len(rejected)} of {len(pages)} page(s) — "
            "original text kept"
        )
    on_progress(f"Done: {len(pages)} page(s) -> {result_path}")
    return result_path


# ---------------------------------------------------------------------------
# Batch compile (queue selection / whole run -> one combined PDF)
# ---------------------------------------------------------------------------


def _safe_filename(name: str) -> str:
    """Strip characters Windows forbids in filenames."""
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "batch"


def _default_export_path(folder: Path, format: str, *, bilingual: bool = False) -> Path:
    """Choose a readable default without breaking legacy input folders."""
    ext = ".md" if format == "md" else ".pdf"
    suffix = "_bilingual" if bilingual else ""
    layout = layout_for_path(folder)
    if layout is not None:
        destination = layout.export_dir("pdf" if format == "pdf" else "markdown")
        destination.mkdir(parents=True, exist_ok=True)
        return destination / f"{slugify(folder.name)}{suffix}{ext}"
    destination = Path("output")
    destination.mkdir(exist_ok=True)
    return destination / f"{folder.name}{suffix}{ext}"


def default_batch_output(
    stems: list[str],
    *,
    output_dir: str = "output",
    format: str = "pdf",
) -> Path:
    """Timestamped default output path for a batch export.

    Named after the single common item folder when every stem shares one,
    else "batch".  The timestamp keeps repeated exports from silently
    overwriting each other.
    """
    tops = {s.split("/")[0] for s in stems if "/" in s}
    name = tops.pop() if len(tops) == 1 and all("/" in s for s in stems) else "batch"
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    ext = ".md" if format == "md" else ".pdf"
    layout = layout_for_path(output_dir)
    destination = (
        layout.export_dir("pdf" if format == "pdf" else "markdown") if layout else Path(output_dir)
    )
    destination.mkdir(parents=True, exist_ok=True)
    return destination / f"{_safe_filename(name)}-{stamp}{ext}"


def page_export_dir(output_dir: str | Path) -> Path:
    """The user-facing PAGE export directory, distinct from persistence.

    Persistence lives under ``pipeline/page/`` (or ``page/`` for legacy roots);
    this is where copies a user explicitly asked to download go — under
    ``exports/page/`` for a canonical project, else a ``page-export/`` sibling
    of the output root.
    """
    layout = layout_for_path(output_dir)
    if layout is not None:
        destination = layout.export_dir("page")
    else:
        destination = Path(output_dir) / "page-export"
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def export_page_xml(
    stems: list[str],
    *,
    output_dir: str = "output",
    output: str | Path | None = None,
) -> tuple[Path, list[str], list[str]]:
    """Copy each stem's persisted PAGE XML into the PAGE export directory.

    One ``.xml`` per source page (the authoritative record stays untouched at
    its persistence path — this writes user-requested copies). Stems with no
    persisted PAGE file are skipped, never silently dropped.

    Returns ``(export_dir, exported_paths, skipped_stems)``.
    """
    validate_path(output_dir, "output_dir")
    destination = Path(output) if output is not None else page_export_dir(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    exported: list[str] = []
    skipped: list[str] = []
    for stem in stems:
        source = page_path(output_dir, stem)
        if not source.is_file():
            skipped.append(stem)
            continue
        target = destination / f"{_safe_filename(stem)}.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        exported.append(str(target))

    if skipped:
        log.info(
            "Skipped %d stem(s) with no persisted PAGE file: %s",
            len(skipped),
            ", ".join(skipped[:5]),
        )
    log.info("Exported %d PAGE XML file(s) to %s", len(exported), destination)
    return destination, exported, skipped


def compile_batch(
    stems: list[str],
    *,
    output_dir: str = "output",
    stage: str = "cleaned",
    structure: bool = False,
    output: str | Path | None = None,
    format: str = "pdf",
    style: str = "readable",
    manifest_path: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Path:
    """Compile a batch of pages (by pipeline stem) into one combined PDF.

    This is the queue-driven counterpart of ``compile()``: the batch is what
    the user selected (or the whole run), not a folder on disk.  Pages are
    grouped under per-item section headings in a single continuous document.

    ``structure`` defaults to False — the structuring pass makes one model
    call per page, so batch export is verbatim concatenation unless the
    caller opts in.
    """
    on_progress = on_progress or (lambda msg: None)
    validate_path(output_dir, "output_dir")
    if output is not None:
        validate_path(str(output), "output")
    if manifest_path is not None:
        validate_path(manifest_path, "manifest_path")

    on_progress(f"Collecting {len(stems)} item(s) from {output_dir}...")
    pages, skipped = collect_stems(
        stems, output_dir=output_dir, stage=stage, manifest_path=manifest_path
    )
    if skipped:
        shown = ", ".join(skipped[:5]) + ("..." if len(skipped) > 5 else "")
        on_progress(f"Skipped {len(skipped)} item(s) with no processed text: {shown}")
    if not pages:
        raise ValueError("No pages found — none of the selected items have processed text")
    on_progress(f"Found {len(pages)} page(s)")

    rejected: list[str] = []
    if structure:
        pages = structure_pages(
            pages,
            on_progress=on_progress,
            on_rejected=lambda label: rejected.append(label),
            output_dir=output_dir,
        )

    # A single shared item title makes a good document title; a mixed batch
    # relies on its section headings instead.
    titles = {p.item_title for p in pages if p.item_title}
    title = titles.pop() if len(titles) == 1 else None

    if output is None:
        output_path = default_batch_output(stems, output_dir=output_dir, format=format)
    else:
        output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if format == "md":
        on_progress("Rendering Markdown...")
        result_path = render_markdown(pages, output_path, title=title)
    else:
        on_progress("Rendering PDF...")
        result_path = render_pdf(pages, output_path, title=title, style=style)

    if rejected:
        on_progress(
            f"Guard rejected structure for {len(rejected)} of {len(pages)} page(s) — "
            "original text kept"
        )
    on_progress(f"Done: {len(pages)} page(s) -> {result_path}")
    return result_path


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


def _clean_para(text: str) -> str:
    """Collapse newlines and multiple spaces within a paragraph for table cells."""
    text = re.sub(r"\n", " ", text)
    return re.sub(r"  +", " ", text)


def _escape_html(text: str) -> str:
    """Escape text for reportlab Paragraph markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
