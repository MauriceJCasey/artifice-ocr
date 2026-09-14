# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Resolve OCR stage directories in canonical and legacy output layouts."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pagexml import PageDocument

_CANONICAL = {
    "raw_ocr": "raw-ocr",
    "title": "titles",
    "structured": "structured",
    "cleaned": "cleaned",
    "translated": "translated",
}


def stage_dir(output_dir: str | Path, stage: str) -> Path:
    root = Path(output_dir)
    if (root / "project.json").is_file() and (root / "pipeline").is_dir():
        return root / "pipeline" / _CANONICAL.get(stage, stage)
    return root / stage


def record_dir(output_dir: str | Path, stage: str) -> Path:
    """Return the metadata directory, retaining ``json`` for legacy roots."""
    root = Path(output_dir)
    return stage_dir(root, stage) / ("records" if (root / "project.json").is_file() else "json")


def page_dir(output_dir: str | Path) -> Path:
    """Return the PAGE XML directory for a project, mirroring :func:`stage_dir`.

    PAGE is the authoritative per-page record, so it lives beside the stage
    directories: canonical projects (``project.json`` + ``pipeline/``) store it
    under ``pipeline/page/``, legacy roots under ``page/``. One ``.xml`` file
    per stem — the XML *is* the record, so there is no ``text``/``records``
    split.
    """
    root = Path(output_dir)
    if (root / "project.json").is_file() and (root / "pipeline").is_dir():
        return root / "pipeline" / "page"
    return root / "page"


def page_path(output_dir: str | Path, stem: str) -> Path:
    """Return the PAGE XML persistence path for one output stem."""
    return page_dir(output_dir) / f"{stem}.xml"


# The output-stage name → the PAGE TextEquiv stage name. ``raw_ocr`` is the
# on-disk stage; PAGE's :data:`~artifice_ocr.pagexml.STAGE_TEXT_EQUIV_INDEX`
# calls the same index "raw".
_PAGE_STAGE_BY_OUTPUT_STAGE = {
    "raw_ocr": "raw",
    "raw": "raw",
    "cleaned": "cleaned",
    "translated": "translated",
}


def page_stage(stage: str) -> str:
    """Map an output-stage name to the PAGE TextEquiv stage name."""
    return _PAGE_STAGE_BY_OUTPUT_STAGE.get(stage, stage)


def load_page(output_dir: str | Path, stem: str) -> "PageDocument | None":
    """Parse the persisted PAGE document for *stem*, or ``None``.

    Reads the exact persistence path (:func:`page_path`) — the pipeline always
    knows its own output root. A missing or corrupt PAGE file returns ``None``
    rather than raising so a legacy ``.txt`` read can proceed.
    """
    path = page_path(output_dir, stem)
    if not path.is_file():
        return None
    try:
        from .pagexml import parse

        return parse(path)
    except Exception:
        return None


def find_page_document(folder: str | Path, stem: str) -> "PageDocument | None":
    """Locate a persisted PAGE document for *stem* from *folder* or an ancestor.

    The PDF/Tropy collectors may be handed an output root, an item folder, or a
    stage text folder; PAGE lives at the root, so walk up until :func:`page_path`
    resolves to an existing file. Used by export surfaces — the pipeline itself
    calls :func:`load_page` with an exact root.
    """
    candidate = Path(folder).expanduser()
    if candidate.is_file():
        candidate = candidate.parent
    for current in (candidate, *candidate.parents):
        path = page_path(current, stem)
        if path.is_file():
            try:
                from .pagexml import parse

                return parse(path)
            except Exception:
                return None
    return None


def write_stage_output(
    output_dir: str | Path,
    stage: str,
    base_name: str,
    *,
    text_content: str,
    metadata: dict,
) -> None:
    """Write a stage's text output and its JSON metadata sidecar.

    Creates the stage's text/ and json-or-records/ directories as needed.
    Every stage in apps/artifice-ocr/src/artifice_ocr/stages/ writes its
    output this same way; this is the single implementation.
    """
    output_path = Path(output_dir)
    text_dir = stage_dir(output_path, stage) / "text"
    json_dir = record_dir(output_path, stage)
    text_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    text_path = text_dir / f"{base_name}.txt"
    json_path = json_dir / f"{base_name}.json"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    with open(text_path, "w", encoding="utf-8") as f:
        f.write(text_content)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
