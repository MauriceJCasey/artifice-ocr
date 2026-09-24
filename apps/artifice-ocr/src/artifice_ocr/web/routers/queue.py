# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Queue management routes."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from shared_ui.path_validation import PathValidationError, sanitise_path_component
from shared_ui.uploads import UploadTooLarge, read_capped_to_tempfile

from ... import config
from ...output import load_page, page_path
from ...pagexml import STAGE_TEXT_EQUIV_INDEX, PageDocument, TextRegion, write
from ..models import (
    AddPathsRequest,
    BatchReplaceRequest,
    FabricatedResultRequest,
    RawTextRequest,
    RegionReorderRequest,
    RegionTextRequest,
    RemoveRequest,
    ReorderRequest,
    ReprocessRequest,
)
from ..runtime import (
    _IMAGE_PASSTHROUGH_TYPES,
    SUPPORTED_EXTENSIONS,
    batch_replace,
    render_page_image,
    reprocess_item,
    save_cleaned_text,
    save_raw_text,
    save_translated_text,
    set_fabricated_result,
    state,
)
from ..serializers import serialize_item_preview
from ..validation import validate_directory

router = APIRouter(tags=["queue"])


@router.get("/api/queue")
def get_queue() -> dict:
    return {"items": state.queue_snapshot(), "status": state.status()}


@router.post("/api/queue/add-paths")
def add_paths(req: AddPathsRequest) -> dict:
    safe = [validate_directory(p, "path") for p in req.paths]
    added = state.add_paths(safe)
    return {"added": len(added), "items": state.queue_snapshot()}


@router.post("/api/queue/remove")
def remove_items(req: RemoveRequest) -> dict:
    try:
        removed = state.remove(req.ids)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"removed": removed, "items": state.queue_snapshot()}


@router.post("/api/queue/clear")
def clear_queue() -> dict:
    try:
        state.clear()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"items": []}


@router.get("/api/queue/{item_id}/preview")
def queue_item_preview(item_id: str) -> dict:
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    return serialize_item_preview(item)


@router.get("/api/queue/{item_id}/image")
def queue_item_image(item_id: str):
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")

    # A Tropy-imported photo may have passed pathcheck but not exist on disk
    # (the import sets a 'missing' flag). FileResponse on a non-existent path
    # produces a raw Starlette 404 with no useful detail; check first so the
    # client gets an actionable message and the preview pane can show it.
    if not Path(item.path).exists():
        raise HTTPException(
            status_code=404,
            detail=f"Source file not found on disk: {Path(item.path).name}",
        )

    suffix = Path(item.path).suffix.lower()
    media_type = _IMAGE_PASSTHROUGH_TYPES.get(suffix)
    if media_type:
        return FileResponse(item.path, media_type=media_type)

    try:
        png_bytes = render_page_image(item)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return Response(content=png_bytes, media_type="image/png")


@router.post("/api/queue/{item_id}/raw-text")
def save_raw_text_route(item_id: str, req: RawTextRequest) -> dict:
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    return save_raw_text(item, req.text)


@router.post("/api/queue/{item_id}/cleaned-text")
def save_cleaned_text_route(item_id: str, req: RawTextRequest) -> dict:
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    return save_cleaned_text(item, req.text)


@router.post("/api/queue/{item_id}/translated-text")
def save_translated_text_route(item_id: str, req: RawTextRequest) -> dict:
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    return save_translated_text(item, req.text)


@router.post("/api/queue/{item_id}/fabricated-result")
def set_fabricated_result_route(item_id: str, req: FabricatedResultRequest) -> dict:
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    return set_fabricated_result(item, req.fabricated)


@router.post("/api/queue/{item_id}/reprocess")
def reprocess_item_route(item_id: str, req: ReprocessRequest) -> dict:
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    if req.from_stage not in ("raw", "cleaned", "translate"):
        raise HTTPException(
            status_code=400, detail="from_stage must be 'raw', 'cleaned', or 'translate'"
        )
    if not req.stages:
        raise HTTPException(status_code=400, detail="No stages to re-run")
    try:
        return reprocess_item(item, req.from_stage, req.stages)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/queue/batch-replace")
def batch_replace_route(req: BatchReplaceRequest) -> dict:
    if not req.find:
        raise HTTPException(status_code=400, detail="find string is required")
    if not req.stages:
        raise HTTPException(status_code=400, detail="At least one stage is required")
    return batch_replace(req.find, req.replace, req.stages, req.item_ids)


@router.post("/api/queue/reorder")
def reorder_queue(req: ReorderRequest) -> dict:
    try:
        state.reorder(req.drag_id, req.drop_id, req.before)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"items": state.queue_snapshot()}


# ── Region-aware PAGE correction ────────────────────────────────────────────
# The region-aware sibling of the whole-page `raw-text`/`cleaned-text`/
# `translated-text` routes above. Both edit an item's transcription, but the
# whole-page routes operate on the legacy `.txt`/JSON layer and remain the
# correct surface for segmentation-disabled items (whose PAGE document holds
# one implicit region); these operate on the authoritative persisted PAGE
# document for a queue item, keyed by `stem` inside the current output
# directory. A correction updates only the selected stage index; deletion and
# reorder never touch any region's polygon/geometry.

_REGION_STAGES = tuple(STAGE_TEXT_EQUIV_INDEX)


def _item_output_dir() -> str:
    """The output root the current item's PAGE document lives under.

    Mirrors ``runtime._save_stage_text``: a run in progress owns the directory
    it was started with; otherwise the configured default.
    """
    return state.runner.output_dir if state.runner else config.get("output_dir")


def _load_item_page(item) -> PageDocument:
    """Return the persisted PAGE document for *item*, or 404.

    A 404 here means segmentation was never enabled for the item, or OCR has
    not run yet — a clear signal, not a bare ``AttributeError`` on ``None``.
    """
    page = load_page(_item_output_dir(), item.stem)
    if page is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No PAGE document found for this item — segmentation was not "
                "used for it, or OCR has not run yet"
            ),
        )
    return page


def _find_region(page: PageDocument, region_id: str) -> TextRegion:
    for region in page.regions:
        if region.id == region_id:
            return region
    raise HTTPException(status_code=404, detail=f"Region {region_id!r} not found")


def _persist_item_page(item, page: PageDocument) -> None:
    """Write *page* back to its persistence path, failing loudly rather than
    silently. Unlike the pipeline's best-effort ``_persist_page``, a
    user-initiated edit that cannot be saved must not look like it was."""
    try:
        write(page, page_path(_item_output_dir(), item.stem))
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not save the PAGE document: {exc}",
        ) from exc


@router.post("/api/queue/{item_id}/region/{region_id}/text")
def save_region_text_route(item_id: str, region_id: str, req: RegionTextRequest) -> dict:
    """Retype/edit one region's text at one PAGE stage index.

    ``set_stage_text`` replaces exactly one ``TextEquiv`` while preserving every
    other stage on that region and every other region — the plan's "a correction
    updates only the selected index" requirement, implemented in the model.
    """
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    if req.stage not in _REGION_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown stage {req.stage!r} (expected one of {', '.join(_REGION_STAGES)})",
        )
    page = _load_item_page(item)
    region = _find_region(page, region_id)
    region.set_stage_text(req.stage, req.text)
    _persist_item_page(item, page)
    return {"ok": True, "region_id": region_id, "stage": req.stage, "text": req.text}


@router.delete("/api/queue/{item_id}/region/{region_id}")
def delete_region_route(item_id: str, region_id: str) -> dict:
    """Remove a region and its reading-order reference; geometry is untouched."""
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    page = _load_item_page(item)
    _find_region(page, region_id)  # 404 before mutating anything
    page.regions = [r for r in page.regions if r.id != region_id]
    page.reading_order = [rid for rid in page.reading_order if rid != region_id]
    _persist_item_page(item, page)
    return {"ok": True, "region_id": region_id}


@router.post("/api/queue/{item_id}/regions/reorder")
def reorder_regions_route(item_id: str, req: RegionReorderRequest) -> dict:
    """Set the reading order; no region geometry is ever changed.

    ``region_ids`` must be an exact permutation of the document's region ids.
    An explicit full-order list (rather than a drag/drop pair) is idempotent,
    leaves no ambiguity at list edges, and lets the server validate the whole
    list against the document at once.
    """
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    page = _load_item_page(item)
    actual = {region.id for region in page.regions}
    # Length must match too: set(req.region_ids) == actual alone lets a longer
    # list that duplicates one id while still covering every id slip through
    # (e.g. 3 real regions, a 4-element request repeating one of them) — the
    # set comparison sees the same three unique values and passes.
    if len(req.region_ids) != len(actual) or set(req.region_ids) != actual:
        raise HTTPException(
            status_code=400,
            detail="region_ids must be exactly the page's region ids, each exactly once",
        )
    page.reading_order = list(req.region_ids)
    _persist_item_page(item, page)
    return {"ok": True, "reading_order": page.reading_order}


def _regions_in_reading_order(page: PageDocument) -> list[TextRegion]:
    """Return *page*'s regions in its declared reading order.

    Mirrors ``PageDocument.text``'s ordering contract: the reading_order list
    first, then any region missing from it (the fallback for documents whose
    reading_order was never populated). Regions present in ``regions`` but not
    in ``reading_order`` still surface — dropping them here would silently
    hide them from the review UI.
    """
    by_id = {region.id: region for region in page.regions}
    ordered = [by_id[rid] for rid in page.reading_order if rid in by_id]
    ordered.extend(region for region in page.regions if region.id not in page.reading_order)
    return ordered


def _region_errors(page: PageDocument) -> dict[str, str]:
    """Map region id -> error message from persisted ``region-error`` steps.

    A segmentation provider's per-region failure is recorded by the pipeline as
    PAGE processing-step metadata (``pipeline.py``'s "region failure must not
    fail the page" path), not on ``TextRegion`` itself — this extracts it back
    out so the review UI can flag a region that never got OCR text.
    """
    errors: dict[str, str] = {}
    for step in page.metadata.processing_steps:
        if step.name != "region-error":
            continue
        try:
            payload = json.loads(step.value)
        except (json.JSONDecodeError, TypeError):
            continue
        region_id, error = payload.get("region_id"), payload.get("error")
        if region_id and error:
            errors[str(region_id)] = str(error)
    return errors


@router.get("/api/queue/{item_id}/regions")
def get_regions_route(item_id: str) -> dict:
    """Return an item's persisted PAGE regions in reading order.

    The read counterpart of the region correction routes above: it serialises
    the authoritative PAGE document so a review UI can draw each region's
    polygon over the source image (``/api/queue/{item_id}/image``) and edit
    its text. Read-only — it never mutates the persisted document.
    """
    item = state.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found in the queue")
    page = load_page(_item_output_dir(), item.stem)
    if page is None:
        # No PAGE document is the normal case for any item processed without
        # segmentation (the default), so it's an empty result, not an error.
        # A 404 here is reserved for an item that isn't in the queue; the
        # routes that edit regions still 404, since there's nothing to edit.
        return {"reading_order": [], "regions": []}
    errors = _region_errors(page)
    regions = [
        {
            "id": region.id,
            "type": region.type,
            "polygon": [list(point) for point in region.polygon],
            "confidence": region.confidence,
            "raw": region.stage_text("raw"),
            "cleaned": region.stage_text("cleaned"),
            "translated": region.stage_text("translated"),
            "error": errors.get(region.id),
        }
        for region in _regions_in_reading_order(page)
    ]
    return {"reading_order": list(page.reading_order), "regions": regions}


# ── File upload ────────────────────────────────────────────────────────────
# Upload guards (size cap, filename sanitisation) come from shared_ui.uploads
# and shared_ui.path_validation; this web layer translates their domain errors
# into HTTP responses.

_MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024  # 50 MB


def _staging_dir() -> Path:
    """Directory uploaded files are staged into, created on demand.

    Lives beside settings.json (``~/.artifice_ocr/``) rather than under a
    platformdirs path — this app deliberately does not use platformdirs.
    """
    return Path.home() / ".artifice_ocr" / "uploads"


def _unique_dest(staging: Path, safe_name: str) -> Path:
    """Return a non-colliding destination for *safe_name* inside *staging*.

    Two uploads named ``page1.jpg`` must both survive: the second becomes
    ``page1_1.jpg`` (then ``page1_2.jpg``, …) rather than overwriting the
    first or anything already staged.
    """
    dest = staging / safe_name
    if not dest.exists():
        return dest
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    counter = 1
    while True:
        candidate = staging / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


@router.post("/api/queue/upload")
async def upload_files(files: Annotated[list[UploadFile], File()]) -> dict:
    """Upload one or more files into the pipeline's staging directory.

    Filenames are sanitised with the shared ``sanitise_path_component`` guard
    used by the other apps to prevent path traversal. Anything whose
    extension is not in ``SUPPORTED_EXTENSIONS`` is rejected per-file, and
    files larger than 50 MB are refused during the read.

    **This is a batch endpoint and always returns HTTP 200** when the request
    itself is well-formed. Per-file outcomes are reported in the response
    body alongside the usual ``add-paths`` keys:

        {"uploaded": [{"filename": ..., "status": "ok"},
                      {"filename": ..., "status": "rejected", "reason": ...}],
         "added": ..., "items": [...]}

    One unacceptable file must not fail an otherwise good batch — a user
    dropping twelve files should get the eleven valid ones staged and a
    specific reason for the twelfth, not a single opaque error.

    A malformed filename (empty, ``"."`` or ``".."`` after cleaning) is the
    one case that does raise — HTTP 400 — because it indicates a crafted
    request rather than a user picking the wrong file.
    """
    staging = _staging_dir()
    staging.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    staged_paths: list[str] = []
    for upload in files:
        raw_name = upload.filename or ""
        try:
            safe_name = sanitise_path_component(raw_name)
        except PathValidationError as e:
            raise HTTPException(status_code=400, detail=e.public_message) from e
        ext = Path(safe_name).suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            results.append(
                {
                    "filename": raw_name,
                    "status": "rejected",
                    "reason": f"Extension {ext!r} not accepted. "
                    f"Allowed: {sorted(SUPPORTED_EXTENSIONS)}",
                }
            )
            continue

        try:
            spooled = await read_capped_to_tempfile(upload, _MAX_UPLOAD_BYTES)
        except UploadTooLarge:
            results.append(
                {
                    "filename": raw_name,
                    "status": "rejected",
                    "reason": "File exceeds 50 MB limit",
                }
            )
            continue

        dest = _unique_dest(staging, safe_name)

        def _persist(spooled_file, dest_path):
            import shutil

            with spooled_file, open(dest_path, "wb") as out:
                shutil.copyfileobj(spooled_file, out)

        await asyncio.to_thread(_persist, spooled, dest)
        staged_paths.append(str(dest))
        results.append({"filename": safe_name, "status": "ok"})

    added = state.add_paths(staged_paths)
    return {
        "uploaded": results,
        "added": len(added),
        "items": state.queue_snapshot(),
    }
