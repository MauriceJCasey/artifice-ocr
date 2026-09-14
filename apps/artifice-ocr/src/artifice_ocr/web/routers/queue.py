# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Queue management routes."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from shared_ui.path_validation import PathValidationError, sanitise_path_component
from shared_ui.uploads import UploadTooLarge, read_capped_to_tempfile

from ..models import (
    AddPathsRequest,
    BatchReplaceRequest,
    FabricatedResultRequest,
    RawTextRequest,
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
async def upload_files(files: list[UploadFile] = File(...)) -> dict:
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
