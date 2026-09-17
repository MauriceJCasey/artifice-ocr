# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Collect processed text pages into `PageText`/`BilingualPageText` lists.

Extracted from the former flat `pdf_export.py` module — see this package's
`__init__.py` for the compatibility facade that keeps every existing
`artifice_ocr.pdf_export.X` import working unchanged.
"""

import json
import re
from pathlib import Path

from artifice_ocr._logging import get_logger

from ..output import find_page_document, page_stage, stage_dir
from .models import BilingualPageText, PageText

log = get_logger("pdf_export")

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
            # Tropy-sourced output nests one level under an item-title
            # subdirectory (candidate/<item title>/<stem>.txt) — a
            # non-recursive glob here found nothing and rejected every
            # stage, even though the recursive rglob() two lines below
            # (the actual collection pass) would have found them fine.
            if candidate.exists() and any(candidate.rglob("*.txt")):
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

    # rglob, not glob: Tropy-sourced output nests one level under an
    # item-title subdirectory (see the matching comment in collect_folder).
    cleaned_files = {f.stem: f for f in cleaned_dir.rglob("*.txt")}
    translated_files: dict[str, Path] = {}
    if translated_dir is not None:
        translated_files = {f.stem: f for f in translated_dir.rglob("*.txt")}

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
    # rglob, not glob: see the matching comment in collect_folder.
    if candidate.exists() and any(candidate.rglob("*.txt")):
        return candidate
    return None
