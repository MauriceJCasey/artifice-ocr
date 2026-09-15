# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Orchestrate collect -> structure -> render into the public compile API.

The structuring pass is optional (--no-structure) and guarded: if the model
alters any word, the original text is kept instead.

Extracted from the former flat `pdf_export.py` module — see this package's
`__init__.py` for the compatibility facade that keeps every existing
`artifice_ocr.pdf_export.X` import working unchanged.
"""

import re
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from artifice_output import layout_for_path, slugify

from artifice_ocr._logging import get_logger

from ..output import page_path
from ..validation import validate_path
from .collection import collect_bilingual_folder, collect_folder, collect_stems
from .render_markdown import render_bilingual_markdown, render_markdown
from .render_pdf import render_bilingual_pdf, render_pdf
from .structure import _structure_bilingual_pages, structure_pages

log = get_logger("pdf_export")

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
