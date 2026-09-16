# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native file-dialog routes: pick file, pick folder, save file, reveal."""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from shared_ui.filedialog import (
    FileType,
    pick_files_async,
    pick_folder_async,
    save_file_async,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["native-dialogs"])


@router.post("/api/native/pick-file")
async def pick_file(request: Request) -> dict[str, str | list[str]]:
    """Open a native file picker and return the selected path(s).

    Returns ``{"state": "selected"|"cancelled"|"unavailable", "paths": [...],
    "reason": "..."}`` — the shared file-dialog contract.  ``paths`` is
    non-empty only for ``"selected"`` and ``reason`` is non-empty only for
    ``"unavailable"``.  Multiple files may be selected.

    An optional JSON body ``{"preset": "images"|"json"|"tropy"}`` switches the
    file-type filter — ``"json"`` selects ``*.jsonld *.json`` files, ``"tropy"``
    selects ``*.tpy`` project databases.  Defaults to ``"images"`` for backward
    compatibility.
    """
    preset = "images"
    try:
        raw_body = await request.body()
        if raw_body:
            body = json.loads(raw_body)
            if isinstance(body, dict):
                preset = body.get("preset", "images")
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    # Constructed inside the handler, not at module scope: a FileType
    # description that fails the [word chars + spaces] rule raises ValueError
    # at construction, and a module-scope instance would crash the server at
    # import time rather than on the one request that uses it.
    if preset == "json":
        file_types = (
            FileType("JSON export", ("*.jsonld", "*.json")),
            FileType("All Files", ("*.*",)),
        )
    elif preset == "tropy":
        file_types = (
            FileType("Tropy project", ("*.tpy",)),
            FileType("All Files", ("*.*",)),
        )
    else:
        file_types = (
            FileType("Images", ("*.jpg", "*.jpeg", "*.png", "*.tiff", "*.gif")),
            FileType("All Files", ("*.*",)),
        )

    result = await pick_files_async(title="Select a file", file_types=file_types)
    return result.as_dict()


@router.post("/api/native/pick-folder")
async def pick_folder() -> dict[str, str | list[str]]:
    """Open a native folder picker and return the selected folder path.

    Returns ``{"state": "selected"|"cancelled"|"unavailable", "paths": [...],
    "reason": "..."}`` — the shared file-dialog contract.  Single selection.
    """
    result = await pick_folder_async(title="Select a folder")
    return result.as_dict()


@router.post("/api/native/save-file")
async def save_file(request: Request) -> dict[str, str | list[str]]:
    """Open a native save-file dialog and return the chosen path.

    Returns ``{"state": "selected"|"cancelled"|"unavailable", "paths": [...],
    "reason": "..."}`` — the shared file-dialog contract.  Single selection.

    An optional JSON body ``{"preset": "json", "default_name": "<name>"}``
    switches the file-type filter and the pre-filled filename.  The extension
    is carried by ``default_name`` (e.g. ``artifice-ocr-tropy.jsonld``) — the
    service applies no ``defaultextension`` policy.
    """
    preset = "json"
    default_name = "artifice-ocr-tropy.jsonld"
    try:
        raw_body = await request.body()
        if raw_body:
            body = json.loads(raw_body)
            if isinstance(body, dict):
                preset = body.get("preset", "json")
                default_name = body.get("default_name", default_name)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    if preset == "json":
        file_types = (
            FileType("JSON export", ("*.jsonld", "*.json")),
            FileType("All Files", ("*.*",)),
        )
    else:
        file_types = (FileType("All Files", ("*.*",)),)

    result = await save_file_async(
        title="Save Tropy export",
        default_name=default_name,
        file_types=file_types,
    )
    return result.as_dict()


@router.post("/api/native/reveal")
async def reveal_file(request: Request) -> dict:
    """Reveal a file in the OS file manager.

    Uses platform-specific commands:
    - macOS: ``open -R <path>``
    - Windows: opens the containing folder with the standard file association
    - Linux: ``xdg-open <parent_dir>``
    """
    import platform
    import subprocess

    data = await request.json()
    path = data.get("path", "")
    if not path:
        return {"ok": False, "error": "No path provided"}
    try:
        from ..validation import validate_directory

        resolved = validate_directory(path, "path")
    except HTTPException:
        return {"ok": False, "error": "Path not permitted"}
    try:
        p = Path(resolved)
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", "-R", "--", str(p)])
        elif system == "Windows":
            # ``os.startfile`` delegates to the user's configured file
            # manager without constructing a child-process command line.
            os.startfile(str(p.parent))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
        return {"ok": True}
    except Exception:
        logger.exception("Failed to reveal file: %s", resolved)
        return {"ok": False, "error": "Could not reveal file in system file manager"}
