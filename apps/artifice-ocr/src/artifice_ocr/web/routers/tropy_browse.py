# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Live read-only .tpy browse routes — feature-flagged.

Enabled via the Settings toggle ``tropy_live_browse_enabled`` (persisted, no
restart required) or the environment variable ``ARTIFICE_OCR_TROPY_LIVE_READ=1``
(fallback override for advanced/CI use). Provides browsing of Tropy projects,
lists, tags, items, and photos directly from a .tpy file, plus enqueueing
items for OCR without a manual JSON-LD export.
"""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from ... import config
from ..._logging import get_logger
from ...tropy_db import (
    TropyDBError,
    get_item,
    items_to_job_items,
    list_items,
    list_lists,
    list_projects,
    list_tags,
    missing_asset_count,
    recent_projects,
    resolve_project_db_path,
)
from ..models import TropyBrowseRequest, TropyEnqueueRequest
from ..runtime import state
from ..validation import validate_directory

log = get_logger("tropy_browse")

router = APIRouter(tags=["tropy-browse"])


def _live_browse_enabled() -> bool:
    """Return True if live Tropy .tpy browsing is enabled.

    Checks the environment variable first (fallback override for advanced/CI
    use), then the persisted config setting (GUI toggle, takes effect without
    a server restart).
    """
    if os.environ.get("ARTIFICE_OCR_TROPY_LIVE_READ", "0") == "1":
        return True
    return config.get("tropy_live_browse_enabled", False)


def _check_enabled() -> None:
    if not _live_browse_enabled():
        raise HTTPException(status_code=404, detail="Live Tropy browse is not enabled")


def _resolve_db_path(raw: str) -> Path:
    """Validate the user-supplied path, then resolve it to the ``.tpy`` file.

    Accepts a ``.tropy`` bundle directory, a ``project.tpy`` (or any ``.tpy``)
    file, or a containing folder.  Validation runs on the *user-supplied* path
    first; the derived ``.tpy`` is always a child of (or identical to) that
    validated path, so nothing bypasses ``validate_path``.
    """
    validated = validate_directory(raw, "path")
    return resolve_project_db_path(validated)


def _resolve_output_dir(raw: str) -> str:
    """Validate and return the output directory path."""
    return validate_directory(raw, "output_dir")


# --------------------------------------------------------------------------- #
# browse routes
# --------------------------------------------------------------------------- #


@router.get("/api/tropy/browse/recent")
def browse_recent() -> dict:
    """List Tropy's recently-opened projects (from its own ``state.json``).

    Soft failure: no Tropy install, no ``state.json``, or unreadable JSON all
    return an empty list.  Paths are returned verbatim — each is re-validated
    when the user actually loads it through the other browse routes.
    """
    _check_enabled()
    return {"projects": [str(p) for p in recent_projects()]}


@router.post("/api/tropy/browse/projects")
def browse_projects(req: TropyBrowseRequest) -> dict:
    """List all projects in a Tropy .tpy database."""
    _check_enabled()
    try:
        db_path = _resolve_db_path(req.path)
        projects = list_projects(db_path)
    except TropyDBError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"projects": projects}


@router.post("/api/tropy/browse/lists")
def browse_lists(req: TropyBrowseRequest) -> dict:
    """List all lists in a Tropy .tpy database."""
    _check_enabled()
    try:
        db_path = _resolve_db_path(req.path)
        lists = list_lists(db_path)
    except TropyDBError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"lists": lists}


@router.post("/api/tropy/browse/tags")
def browse_tags(req: TropyBrowseRequest) -> dict:
    """List all tags in a Tropy .tpy database."""
    _check_enabled()
    try:
        db_path = _resolve_db_path(req.path)
        tags = list_tags(db_path)
    except TropyDBError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"tags": tags}


@router.post("/api/tropy/browse/items")
def browse_items(
    req: TropyBrowseRequest,
    list_id: int | None = Query(None),
    tag: str | None = Query(None),
) -> dict:
    """List items in a Tropy .tpy database, optionally filtered.

    Query parameters:
    - ``list_id``: filter items to a specific list
    - ``tag``: filter items to a specific tag name
    """
    _check_enabled()
    try:
        db_path = _resolve_db_path(req.path)
        items = list_items(db_path, list_id=list_id, tag=tag)
    except TropyDBError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": [
            {
                "item_id": it.item_id,
                "title": it.title,
                "photo_count": len(it.photos),
                "missing_count": sum(1 for p in it.photos if p.missing),
                "photos": [
                    {
                        "photo_id": p.photo_id,
                        "path": p.path,
                        "page": p.page,
                        "mimetype": p.mimetype,
                        "checksum": p.checksum,
                        "orientation": p.orientation,
                        "missing": p.missing,
                    }
                    for p in it.photos
                ],
            }
            for it in items
        ]
    }


@router.post("/api/tropy/browse/items/{item_id}")
def browse_single_item(req: TropyBrowseRequest, item_id: int) -> dict:
    """Get a single item with its photos from a Tropy .tpy database."""
    _check_enabled()
    try:
        db_path = _resolve_db_path(req.path)
        item = get_item(db_path, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        return {
            "item": {
                "item_id": item.item_id,
                "title": item.title,
                "photo_count": len(item.photos),
                "missing_count": sum(1 for p in item.photos if p.missing),
                "photos": [
                    {
                        "photo_id": p.photo_id,
                        "path": p.path,
                        "page": p.page,
                        "mimetype": p.mimetype,
                        "checksum": p.checksum,
                        "orientation": p.orientation,
                        "missing": p.missing,
                    }
                    for p in item.photos
                ],
            }
        }
    except TropyDBError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# enqueue route
# --------------------------------------------------------------------------- #


@router.post("/api/tropy/browse/enqueue")
def enqueue_from_tropy(req: TropyEnqueueRequest) -> dict:
    """Enqueue items from a live .tpy browse for OCR processing."""
    _check_enabled()
    try:
        db_path = _resolve_db_path(req.path)
        output_dir = _resolve_output_dir(req.output_dir)
        items = []
        requested_photos = set(req.photo_ids or [])
        for item_id in req.item_ids:
            item = get_item(db_path, item_id)
            if item is not None:
                if req.photo_ids is not None:
                    item = type(item)(
                        item_id=item.item_id,
                        title=item.title,
                        photos=[
                            photo for photo in item.photos if photo.photo_id in requested_photos
                        ],
                    )
                    if not item.photos:
                        continue
                items.append(item)
        missing, total = missing_asset_count(items)
        job_items = items_to_job_items(items, project_db=db_path, output_dir=output_dir)
        added = state.add_items(job_items)
        return {
            "added": len(added),
            "missing": missing,
            "total": total,
            "items": state.queue_snapshot(),
        }
    except TropyDBError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
