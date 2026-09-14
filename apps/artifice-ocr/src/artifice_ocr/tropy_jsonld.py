# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bi-directional Tropy JSON-LD file bridge.

Replaces the 7-route SQLite read/write integration (``tropy_read.py`` +
``tropy_write.py`` + ``tropy.py``) with a frictionless file-based workflow:

1. **Import**: User exports from Tropy (File → Export → JSON-LD), provides
   the file to artifice-ocr.  Relative photos are resolved relative to the
   JSON-LD file's directory.  Absolute photos are validated by
   ``_tropy_pathcheck`` against a blocklist of system directories — NAS
   mounts, external drives and research archives are explicitly permitted.

2. **Export**: artifice-ocr generates a JSON-LD file the user imports back
   into Tropy (File → Import Items…). The item envelope preserves Tropy's
   own ``photo`` / ``note`` / ``template`` structure so the round-trip is
   transparent.

This module is library-level — no FastAPI imports, same discipline as the
old ``tropy_read.py``.
"""

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from ._logging import get_logger

log = get_logger("tropy_jsonld")

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

MAX_FILE_BYTES = 64 * 1024 * 1024  # 64 MB

MANIFEST_SCHEMA_VERSION = "1.0"
MAX_DEPTH = 32
MAX_NODES = 50_000
ALLOWED_SUFFIXES = frozenset({".json", ".jsonld"})

TROPY_CONTEXT = {
    # JSON-LD requires this keyword to be a number. A string looks equivalent
    # in our own parser but Tropy's jsonld processor rejects it as an
    # unsupported version, making every generated export impossible to import.
    "@version": 1.1,
    "@vocab": "https://tropy.org/v1/tropy#",
    "template": {"@type": "@id"},
    "photo": {
        "@id": "https://tropy.org/v1/tropy#photo",
        "@container": "@list",
        "@context": {
            "note": {"@id": "https://tropy.org/v1/tropy#note", "@container": "@list"},
            "selection": {
                "@id": "https://tropy.org/v1/tropy#selection",
                "@container": "@list",
            },
        },
    },
    "note": {"@id": "https://tropy.org/v1/tropy#note", "@container": "@list"},
    "selection": {
        "@id": "https://tropy.org/v1/tropy#selection",
        "@container": "@list",
    },
    "title": "http://purl.org/dc/elements/1.1/title",
}

TITLE_PROPERTY = "http://purl.org/dc/elements/1.1/title"

# Characters Windows forbids in a path segment, plus the separators.
_UNSAFE = '<>:"/\\|?*'

# Rollback feature flag: when set, absolute photo paths hit the old
# raise-on-absolute branch instead of the pathcheck pathway.  Off by default.
_RELATIVE_ONLY = os.environ.get("ARTIFICE_OCR_TROPY_RELATIVE_ONLY", "0") == "1"


class TropyImportError(ValueError):
    """User-facing parse/validation failure.

    Message must NEVER contain a resolved absolute path — it may be
    returned to the browser as a 400 detail.
    """


# --------------------------------------------------------------------------- #
# data classes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ImportedPhoto:
    """One resolved photo from a Tropy JSON-LD export."""

    group: str
    item_node: dict
    photo_index: int
    path_rel: str
    resolved: Path
    mimetype: str
    checksum: str
    page: int | None
    missing: bool


@dataclass(frozen=True)
class ImportedItem:
    """One item from a Tropy JSON-LD export, plus its resolved photos."""

    group: str
    title: str
    photos: list  # list[ImportedPhoto]

    @property
    def label(self) -> str:
        n = len(self.photos)
        return f"{self.title}  —  {n} page(s)"


@dataclass(frozen=True)
class ImportPreview:
    """Result of calling :func:`load_export`."""

    export_name: str  # file NAME only, never full path
    items: list  # list[ImportedItem]
    warnings: list  # list[str]


# --------------------------------------------------------------------------- #
# path naming utilities (moved from tropy_read.py)
# --------------------------------------------------------------------------- #


def safe_name(name: str, fallback: str = "untitled") -> str:
    """Make a string usable as a single path segment."""
    cleaned = "".join("_" if c in _UNSAFE else c for c in (name or "")).strip(" .")
    cleaned = " ".join(cleaned.split())
    return cleaned[:120] or fallback


def page_stem(
    item_title: str,
    filename: str,
    page: int | None,
    mimetype: str,
    resolved: Path,
) -> str:
    """Output key for one page: ``<Item Title>/<file>_p0002``.

    The subdirectory groups an item's pages together; the page suffix is
    what stops every page of a PDF from colliding on the checksum stem.
    """
    is_pdf = mimetype == "application/pdf" or resolved.suffix.lower() == ".pdf"
    base = safe_name(Path(filename).stem, fallback="page")
    folder = safe_name(item_title, fallback="untitled")
    if is_pdf:
        return f"{folder}/{base}_p{(page or 0) + 1:04d}"
    return f"{folder}/{base}"


def stem_discriminator(checksum: str = "", photo_id: int | None = None, path_rel: str = "") -> str:
    """A short, stable per-photo suffix for disambiguating a colliding stem.

    Preferred order: a checksum prefix, then the photo id, then a hash of
    the photo's own relative path. Deliberately never derived from a batch
    index or list position — that would change every time the same photos
    happened to enumerate in a different order, which is not "stable" in
    the sense a resume check needs.
    """
    if checksum:
        return checksum[:10]
    if photo_id is not None:
        return f"id{photo_id}"
    if path_rel:
        return hashlib.sha1(path_rel.encode("utf-8")).hexdigest()[:10]
    return "dup"


def disambiguate_stems(stems: list[str], discriminators: list[str]) -> list[str]:
    """Give the SECOND and later occurrence of a duplicate stem a distinct
    suffix; the FIRST occurrence is returned unchanged.

    `stems` and `discriminators` must be the same length and in the same
    order the photos will become JobItems — index *i*'s discriminator
    belongs to index *i*'s stem. Only actual collisions *within this list*
    get a suffix, so a batch with none of its own returns every stem
    byte-identical to the input — which is what keeps every output already
    on disk (all written under the pre-disambiguation stem format) matching
    on the next resume.
    """
    counts: dict[str, int] = {}
    result: list[str] = []
    for stem, disc in zip(stems, discriminators, strict=True):
        n = counts.get(stem, 0)
        counts[stem] = n + 1
        result.append(stem if n == 0 else f"{stem}__{disc}")
    return result


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _as_list(value: Any) -> list:
    """Normalise a scalar or list into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _canonical_json(node: dict) -> bytes:
    """Deterministic byte-serialisation for stable group-id hashing."""
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


# --------------------------------------------------------------------------- #
# import — public entry points
# --------------------------------------------------------------------------- #


def load_export(raw: str | Path) -> ImportPreview:
    """Parse and validate a Tropy JSON-LD export file from a filesystem path.

    Each step raises :class:`TropyImportError` on failure. Messages are
    sanitised — they never contain a resolved absolute path.
    """
    # 1. Path gate
    p = Path(raw).expanduser()
    suffix = p.suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise TropyImportError(f"File '{p.name}' is not a JSON-LD file (expected .json or .jsonld)")
    p = p.resolve(strict=False)
    if not p.is_file():
        raise TropyImportError(f"'{p.name}' is not a file")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise TropyImportError(
            f"File '{p.name}' is too large ({p.stat().st_size} bytes; max {MAX_FILE_BYTES})"
        )
    export_dir = p.parent

    # 2. Parse
    try:
        data = json.loads(p.read_bytes())
    except RecursionError:
        raise TropyImportError("JSON-LD file is too deeply nested to parse") from None
    except (json.JSONDecodeError, OSError) as exc:
        raise TropyImportError(f"Could not parse JSON-LD file: {exc}") from None

    return _parse_graph(data, export_dir=export_dir, export_name=p.name)


def load_export_content(
    text: str,
    *,
    filename: str | None = None,
) -> ImportPreview:
    """Parse and validate Tropy JSON-LD export content (drag-and-drop).

    Parameters
    ----------
    text : str
        The raw JSON-LD text content.
    filename : str | None
        Display name for error messages — **never** joined into a filesystem
        path.  Defaults to ``"dropped-export.jsonld"``.
    """
    # Encode to UTF-8 bytes for size check (chars != bytes)
    try:
        raw_bytes = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TropyImportError(f"Could not encode export content as UTF-8: {exc}") from exc
    if len(raw_bytes) > MAX_FILE_BYTES:
        raise TropyImportError(
            f"Export content is too large ({len(raw_bytes)} bytes; max {MAX_FILE_BYTES})"
        )

    # Parse from bytes (handles BOM)
    try:
        data = json.loads(raw_bytes)
    except RecursionError:
        raise TropyImportError("JSON-LD content is too deeply nested to parse") from None
    except (json.JSONDecodeError, ValueError) as exc:
        raise TropyImportError(f"Could not parse JSON-LD content: {exc}") from None

    export_name = Path(filename).name if filename else "dropped-export.jsonld"

    return _parse_graph(data, export_dir=None, export_name=export_name)


# --------------------------------------------------------------------------- #
# import — shared core
# --------------------------------------------------------------------------- #


def _parse_graph(
    data: Any,
    *,
    export_dir: Path | None,
    export_name: str,
) -> ImportPreview:
    """Shared core: budget check, envelope unwrap, graph walk, per-photo
    validation."""
    # 1. Depth and node budget
    _check_budget(data)

    # 2. Shape validation — unwrap envelope
    if isinstance(data, dict):
        graph: list[dict] = _as_list(data["@graph"]) if "@graph" in data else [data]
    elif isinstance(data, list):
        graph = data
    else:
        raise TropyImportError("JSON-LD root must be an object or array, not a scalar")

    # 3. Walk graph members
    items: list[ImportedItem] = []
    warnings: list[str] = []
    photo_count_total = 0

    for idx, node in enumerate(graph):
        if not isinstance(node, dict):
            continue
        ntype = _as_list(node.get("@type", []))

        is_item = "Item" in ntype or any(isinstance(t, str) and t.endswith("#Item") for t in ntype)
        if not is_item:
            continue  # skip Template, Field, List without complaint

        # Extract title
        title = None
        for key in (
            TITLE_PROPERTY,
            "title",
            "dc:title",
            "http://purl.org/dc/terms/title",
        ):
            if key in node:
                val = node[key]
                if isinstance(val, list):
                    val = val[0] if val else None
                if isinstance(val, dict) and "@value" in val:
                    val = val["@value"]
                if isinstance(val, str) and val.strip():
                    title = val.strip()
                    break
        if title is None:
            title = f"Item {idx + 1}"

        # Extract photos
        raw_photos = node.get("photo", [])
        if isinstance(raw_photos, dict):
            raw_photos = [raw_photos]
        elif not isinstance(raw_photos, list):
            raw_photos = []

        # Deterministic group-id — stable across re-parses
        group = f"{hashlib.sha256(_canonical_json(node)).hexdigest()[:12]}:{idx}"
        imported_photos: list[ImportedPhoto] = []

        for pi, pnode in enumerate(raw_photos):
            if not isinstance(pnode, dict):
                warnings.append(f"Photo entry {pi + 1} in item '{title}' is not a dict — skipped")
                continue
            raw_path = pnode.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                warnings.append(f"Photo entry {pi + 1} in item '{title}' has no path — skipped")
                continue

            # ---- photo path handling ------------------------------------
            path_rel = raw_path.replace("\\", "/")

            # UNC paths — reject unconditionally (must check before
            # POSIX-absolute, since // starts with /)
            if path_rel.startswith("//"):
                raise TropyImportError(
                    f"Photo path '{path_rel}' in item '{title}' is a UNC path — "
                    f"all paths must be relative to the export file's folder"
                )

            # Windows drive letter
            windows_abs = bool(re.match(r"^[A-Za-z]:/", path_rel))
            is_absolute = path_rel.startswith("/") or windows_abs

            if is_absolute:
                _handle_absolute_photo(
                    raw_path,
                    path_rel,
                    title,
                    warnings,
                )
                # _handle_absolute_photo either raises or returns nothing;
                # the resolved path and missing flag come from the pathcheck
                # module.  However, to avoid restructuring the entire loop,
                # we inline the pathcheck call here after the function
                # validates.
                resolved, missing, is_symlink = _validate_and_resolve_absolute(
                    raw_path,
                    title,
                )
                if is_symlink:
                    warnings.append(
                        f"Photo '{Path(path_rel).name}' in item '{title}' "
                        f"is a symbolic link — followed to its target"
                    )
            else:
                # ---- relative path --------------------------------------
                if export_dir is None:
                    # Content import — cannot resolve relative paths
                    warnings.append(
                        f"Photo '{path_rel}' in item '{title}' uses a "
                        f"relative path — save the export to disk and "
                        f"import by path to resolve it"
                    )
                    continue

                # .. segments
                segments = path_rel.split("/")
                if ".." in segments:
                    raise TropyImportError(
                        f"Photo path '{path_rel}' in item '{title}' escapes the export folder"
                    )

                # Resolve and containment
                resolved = (export_dir / path_rel).resolve()

                try:
                    resolved.relative_to(export_dir)
                except ValueError as err:
                    raise TropyImportError(
                        f"Photo path '{path_rel}' in item '{title}' escapes the export folder"
                    ) from err

                missing = not resolved.exists()

            mimetype = pnode.get("mimetype", "")
            checksum = pnode.get("checksum", "")
            page = pnode.get("page")
            if page is not None and isinstance(page, (int, float)):
                page = int(page)
            else:
                page = 0 if mimetype == "application/pdf" else None

            imported_photos.append(
                ImportedPhoto(
                    group=group,
                    item_node=node,
                    photo_index=pi,
                    path_rel=path_rel,
                    resolved=resolved,
                    mimetype=mimetype,
                    checksum=checksum,
                    page=page,
                    missing=missing,
                )
            )
            photo_count_total += 1

        if imported_photos:
            items.append(ImportedItem(group=group, title=title, photos=imported_photos))

    if photo_count_total == 0:
        warnings.append("No photos with valid paths found in the export")

    return ImportPreview(export_name=export_name, items=items, warnings=warnings)


def _handle_absolute_photo(
    raw_path: str,
    path_rel: str,
    title: str,
    warnings: list[str],
) -> None:
    """Handle the rollback flag check for an absolute photo path.

    Raises :class:`TropyImportError` when the rollback flag is active.
    Otherwise returns silently — the caller proceeds with pathcheck.
    """
    if _RELATIVE_ONLY:
        raise TropyImportError(
            f"Photo path '{path_rel}' in item '{title}' is absolute — "
            f"all paths must be relative to the export file's folder"
        )


def _validate_and_resolve_absolute(
    raw_path: str,
    title: str,
) -> tuple[Path, bool, bool]:
    """Call the pathcheck module and return (resolved, missing, is_symlink).

    Translates ``ValueError`` from the pathcheck module into
    ``TropyImportError``, **preserving the message exactly** — the
    pathcheck module guarantees sanitised messages.
    """
    from ._tropy_pathcheck import (
        PhotoPathResult,
        validate_absolute_photo,
    )

    try:
        result: PhotoPathResult = validate_absolute_photo(raw_path)
    except ValueError as exc:
        raise TropyImportError(str(exc)) from exc

    return result.resolved, result.missing, result.is_symlink


def _check_budget(data: Any) -> None:
    """Iterative depth/node budget check — no recursion, so deeply nested
    JSON doesn't overflow the call stack."""
    stack: list[tuple[Any, int]] = [(data, 0)]
    visited = 0
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise TropyImportError(f"JSON-LD nesting exceeds maximum depth of {MAX_DEPTH}")
        visited += 1
        if visited > MAX_NODES:
            raise TropyImportError(f"JSON-LD exceeds maximum node count of {MAX_NODES}")
        if isinstance(node, dict):
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))


# --------------------------------------------------------------------------- #
# photos → JobItem mapping
# --------------------------------------------------------------------------- #


def photos_to_job_items(preview: ImportPreview, groups: list[str] | None = None) -> list:
    """Map :class:`ImportedPhoto` objects to :class:`JobItem` objects.

    The resulting ``source`` dict uses ``"tropy-jsonld"`` as the origin
    discriminator, replacing the old ``photo_id`` truthiness check.
    """
    from .jobs import JobItem

    group_set = None if groups is None else set(groups)
    pairs: list[tuple] = []  # (item, photo)

    for item in preview.items:
        if group_set is not None and item.group not in group_set:
            continue
        for photo in item.photos:
            pairs.append((item, photo))

    stems = [
        page_stem(item.title, Path(photo.path_rel).name, photo.page, photo.mimetype, photo.resolved)
        for item, photo in pairs
    ]
    discriminators = [
        stem_discriminator(checksum=photo.checksum, path_rel=photo.path_rel)
        for _item, photo in pairs
    ]
    final_stems = disambiguate_stems(stems, discriminators)

    result: list = []
    for (item, photo), stem in zip(pairs, final_stems, strict=True):
        is_pdf = photo.mimetype == "application/pdf" or photo.resolved.suffix.lower() == ".pdf"
        parts = [Path(photo.path_rel).name]
        if is_pdf and photo.page is not None:
            parts.append(f"p.{photo.page + 1}")
        label = "  ".join(parts)

        result.append(
            JobItem(
                path=str(photo.resolved),
                page=photo.page if is_pdf else None,
                output_stem=stem,
                label=label,
                source={
                    "origin": "tropy-jsonld",
                    "tropy_group": photo.group,
                    "item_node": photo.item_node,
                    "photo_index": photo.photo_index,
                    "photo_path_rel": photo.path_rel,
                    "checksum": photo.checksum,
                    "mimetype": photo.mimetype,
                    "item_title": item.title,
                    "orientation": 1,
                },
            )
        )
    return result


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportPhoto:
    """One photo to include in a Tropy export."""

    abs_path: Path
    text: str
    label: str
    language: str
    item_node: dict | None
    group: str | None
    photo_index: int | None
    path_rel: str | None
    checksum: str
    mimetype: str
    page: int | None = None


def _note_html(text: str) -> str:
    """Build HTML note content from plain text."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        paras = "".join(f"<p>{escape(line)}</p>" for line in lines)
    else:
        paras = f"<p>{escape(text.strip())}</p>"
    return paras


def _note_node(text: str, language: str) -> dict:
    lang = (language or "en").strip().lower() or "en"
    return {
        "@type": "Note",
        "text": {"@value": text, "@language": lang},
        "html": {"@value": _note_html(text), "@language": lang},
    }


def _md5_checksum(path: Path) -> str | None:
    """Stream the file and compute its MD5 checksum."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _mimetype_from_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")


def _generator_string() -> str:
    """Return 'artifice-ocr <version>' without importing the package at module level."""
    try:
        from importlib.metadata import version

        return f"artifice-ocr {version('artifice-ocr')}"
    except Exception:
        return "artifice-ocr"


def resolve_export_text(
    output_dir: str | Path,
    stem: str,
    *,
    stage: str = "cleaned",
    fallback: str = "",
) -> str:
    """Text for one Tropy export note: PAGE TextEquiv index first, fallback last.

    PAGE is the authoritative record, so the JSON-LD export reads the requested
    stage's index (raw/cleaned/translated) before any legacy ``.txt`` value.
    Returns *fallback* when no PAGE document exists (a pre-PAGE folder) or the
    selected index is empty — callers pass the legacy ``.txt`` projection as the
    fallback.
    """
    from .output import find_page_document, page_stage

    doc = find_page_document(output_dir, stem)
    if doc is None:
        return fallback
    return doc.text(page_stage(stage)) or fallback


def build_export(photos: list[ExportPhoto]) -> dict:
    """Build a Tropy JSON-LD export document from :class:`ExportPhoto` objects.

    Photos are grouped by *group* (Tropy-sourced) or by *abs_path* (ad-hoc,
    one item per file). Tropy-sourced groups deep-copy the original
    ``item_node`` and rebuild the ``photo`` list to contain only photos
    that carry text.
    """
    # Partition into Tropy-sourced and ad-hoc
    tropy_groups: dict[str, list[ExportPhoto]] = {}  # group -> photos
    ad_hoc: list[ExportPhoto] = []

    for ep in photos:
        if ep.group is not None:
            tropy_groups.setdefault(ep.group, []).append(ep)
        else:
            ad_hoc.append(ep)

    graph: list[dict] = []

    # Tropy-sourced groups
    for _group, eps in tropy_groups.items():
        eps_with_text = [ep for ep in eps if ep.text.strip()]
        if not eps_with_text:
            continue
        # All eps in a group share the same item_node — deep-copy the first
        base_node = eps_with_text[0].item_node or {}
        item = copy.deepcopy(base_node)

        # Preserve each original photo node and append the OCR note. A minimal
        # rebuild loses PDF page identity, templates, selections and metadata.
        original_photos = _as_list(item.get("photo"))
        photo_list: list[dict] = []
        for ep in eps_with_text:
            if ep.photo_index is not None and 0 <= ep.photo_index < len(original_photos):
                photo_entry = copy.deepcopy(original_photos[ep.photo_index])
            else:
                photo_entry = {"@type": "Photo"}
            photo_entry["path"] = str(ep.abs_path)
            if ep.checksum:
                photo_entry["checksum"] = ep.checksum
            if ep.mimetype:
                photo_entry["mimetype"] = ep.mimetype
            if ep.page is not None:
                photo_entry["page"] = ep.page
            notes = _as_list(photo_entry.get("note"))
            notes.append(_note_node(ep.text, ep.language))
            photo_entry["note"] = notes
            photo_list.append(photo_entry)

        item["photo"] = photo_list
        graph.append(item)

    # Ad-hoc groups: one item per file, retaining every PDF page.
    ad_hoc_files: dict[str, list[ExportPhoto]] = {}
    for ep in ad_hoc:
        if ep.text.strip():
            ad_hoc_files.setdefault(str(ep.abs_path), []).append(ep)

    for abs_str, eps in ad_hoc_files.items():
        source = eps[0].abs_path
        photo_list = []
        for ep in sorted(eps, key=lambda value: value.page if value.page is not None else -1):
            checksum = ep.checksum or _md5_checksum(ep.abs_path)
            mimetype = ep.mimetype or _mimetype_from_suffix(ep.abs_path)
            photo_entry = {
                "@type": "Photo",
                "path": abs_str,
                "protocol": "file",
                "template": "https://tropy.org/v1/templates/photo",
                "mimetype": mimetype,
                "note": [_note_node(ep.text, ep.language)],
            }
            if ep.page is not None:
                photo_entry["page"] = ep.page
            if checksum:
                photo_entry["checksum"] = checksum
            photo_list.append(photo_entry)

        graph.append(
            {
                "@type": "Item",
                "title": source.stem,
                "template": "https://tropy.org/v1/templates/generic",
                "photo": photo_list,
            }
        )

    return {
        "@context": TROPY_CONTEXT,
        "@graph": graph,
        "generator": _generator_string(),
    }


def export_json(photos: list[ExportPhoto]) -> str:
    """Return the Tropy JSON-LD export as a formatted string."""
    return json.dumps(build_export(photos), indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# manifest writer (moved from tropy_write.py)
# --------------------------------------------------------------------------- #


def write_manifest(
    output_dir: str | Path,
    preview: ImportPreview,
    *,
    filename: str = "tropy_manifest.json",
) -> Path | None:
    """Write a versioned manifest mapping output stems back to their source photos.

    This is the documented contract between artifice-ocr and downstream
    consumers (artifice-graph, artificial analysis tools, hand-curated
    archival pipelines).  The manifest is a JSON file at
    ``<output_dir>/manifest.json`` in a canonical project (or the historical
    ``tropy_manifest.json`` name for legacy roots) with the following shape::

        {
            "schema_version": "1.0",
            "export": { "name": "<filename>", "imported": "<ISO-8601>" },
            "output_layout": "<stage>/text/<item title>/<file>_p<page>.txt",
            "pages": {
                "<output_stem>": {
                    "photo_id": null,
                    "page": <int|null>,
                    "page_number": <int>,
                    "source_path": "<absolute path>",
                    "mimetype": "<string>",
                    "orientation": 1,
                    "filename": "<basename>",
                    "item_title": "<Tropy item title>",
                    "checksum": "<hex>",
                    "photo_path_rel": "<path relative to export>",
                    "tropy_group": "<hash:idx identifier>"
                },
                ...
            }
        }

    - ``schema_version`` — "1.0" (current).  Consumers MUST refuse to
      process a manifest whose version they do not recognise.
    - ``export`` — the source export file name and UTC import timestamp.
    - ``output_layout`` — a human-readable description of the directory
      structure the output stem maps into.
    - ``pages`` — dict keyed by output stem (as produced by
      :func:`page_stem`).  Each value carries the provenance fields
      needed to trace an OCR result back to its Tropy photo.

    Written into *output_dir*. Swallows failure silently — the manifest is
    a convenience, never a blocker for a running pipeline.
    """
    out_dir = Path(output_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    # Canonical projects use a neutral manifest name at project root; retain
    # the historical filename for legacy output roots and explicit callers.
    if filename == "tropy_manifest.json" and (out_dir / "project.json").exists():
        filename = "manifest.json"
    target = out_dir / filename

    existing: dict = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    entries = existing.get("pages", {}) if isinstance(existing, dict) else {}

    for item in preview.items:
        for photo in item.photos:
            stem = page_stem(
                item.title,
                Path(photo.path_rel).name,
                photo.page,
                photo.mimetype,
                photo.resolved,
            )
            entries[stem] = {
                "photo_id": None,
                "page": photo.page,
                "page_number": (photo.page + 1) if photo.page is not None else 1,
                "source_path": str(photo.resolved),
                "mimetype": photo.mimetype,
                "orientation": 1,
                "filename": Path(photo.path_rel).name,
                "item_title": item.title,
                "checksum": photo.checksum,
                "photo_path_rel": photo.path_rel,
                "tropy_group": photo.group,
            }

    export_stamp = datetime.now(UTC).isoformat(timespec="seconds")
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "export": {
            "name": preview.export_name,
            "imported": export_stamp,
        },
        "output_layout": "<stage>/text/<item title>/<file>_p<page>.txt",
        "pages": entries,
    }
    try:
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return None

    log.info("Wrote manifest for %d page(s) to %s", len(entries), target)
    return target
