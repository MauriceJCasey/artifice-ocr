# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""History routes: listing runs, items, detail, search, delete, image, raw-text."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from ..models import FabricatedResultRequest, RawTextRequest
from ..runtime import _IMAGE_PASSTHROUGH_TYPES, render_page_image_from, state
from ..serializers import (
    serialize_fabricated_result,
    serialize_history_item,
    serialize_history_item_detail,
    serialize_history_run,
)

router = APIRouter(tags=["history"])

# Full-text search support — queries across processed texts
_SEARCH_LIMIT = 200


@router.get("/api/history/runs")
def history_runs() -> dict:
    return {"runs": [serialize_history_run(r) for r in state.history.list_runs()]}


@router.get("/api/history/fabricated-results")
def history_fabricated_results() -> dict:
    """Export reviewer-labelled examples for deterministic guard development."""
    return {
        "schema_version": 1,
        "description": "Human-labelled OCR outputs that contain invented text",
        "items": [
            serialize_fabricated_result(row) for row in state.history.list_fabricated_results()
        ],
    }


@router.get("/api/history/runs/{run_id}/items")
def history_run_items(run_id: int) -> dict:
    return {"items": [serialize_history_item(r) for r in state.history.list_items(run_id)]}


@router.get("/api/history/items/{item_id}")
def history_item_detail(item_id: int) -> dict:
    row = state.history.get_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="History item not found")
    return serialize_history_item_detail(row)


@router.get("/api/history/search")
def history_search(q: str = "") -> dict:
    if not q.strip():
        return {"items": []}
    items = state.history.search_items(q)
    results = [serialize_history_item(r) for r in items]
    return {"items": results}


@router.get("/api/history/fulltext")
def history_fulltext_search(q: str = "") -> dict:
    """Search the full text of all processed documents (raw, cleaned, translated)."""
    if not q.strip():
        return {"results": []}
    rows = state.history.fulltext_search(q, limit=_SEARCH_LIMIT)
    return {"results": [dict(r) for r in rows]}


@router.delete("/api/history/runs/{run_id}")
def history_delete_run(run_id: int) -> dict:
    state.history.delete_run(run_id)
    return {"ok": True}


@router.get("/api/history/items/{item_id}/image")
def history_item_image(item_id: int):
    row = state.history.get_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="History item not found")

    source = row["source_file"]
    if not source or not Path(source).exists():
        raise HTTPException(status_code=404, detail="Source file not found on disk")

    page = row["page"]
    if page is None:
        m = re.search(r"p\.(\d+)\s*$", row["name"] or "")
        page = (int(m.group(1)) - 1) if m else 0

    suffix = Path(source).suffix.lower()
    media_type = _IMAGE_PASSTHROUGH_TYPES.get(suffix)
    if media_type:
        return FileResponse(source, media_type=media_type)

    try:
        png_bytes = render_page_image_from(source, page)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return Response(content=png_bytes, media_type="image/png")


@router.post("/api/history/items/{item_id}/raw-text")
def history_item_save_raw_text(item_id: int, req: RawTextRequest) -> dict:
    row = state.history.get_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="History item not found")
    state.history.update_raw_text(item_id, req.text)
    updated = state.history.get_item(item_id)
    return serialize_history_item_detail(updated)


@router.post("/api/history/items/{item_id}/cleaned-text")
def history_item_save_cleaned_text(item_id: int, req: RawTextRequest) -> dict:
    row = state.history.get_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="History item not found")
    state.history.update_cleaned_text(item_id, req.text)
    updated = state.history.get_item(item_id)
    return serialize_history_item_detail(updated)


@router.post("/api/history/items/{item_id}/translated-text")
def history_item_save_translated_text(item_id: int, req: RawTextRequest) -> dict:
    row = state.history.get_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="History item not found")
    state.history.update_translated_text(item_id, req.text)
    updated = state.history.get_item(item_id)
    return serialize_history_item_detail(updated)


@router.post("/api/history/items/{item_id}/fabricated-result")
def history_item_set_fabricated_result(item_id: int, req: FabricatedResultRequest) -> dict:
    if state.history.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="History item not found")
    state.history.set_fabricated_result(item_id, req.fabricated)
    return serialize_history_item_detail(state.history.get_item(item_id))
