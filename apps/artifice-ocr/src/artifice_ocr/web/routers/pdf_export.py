# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""PDF export routes: start, status, SSE events, download."""

import asyncio
import json
import queue
from pathlib import Path

from artifice_output import discover_projects
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from ... import config
from ...output import page_path
from ...pdf_export import collect_bilingual_folder, collect_folder, export_page_xml
from ..models import PageExportRequest, PdfExportRequest
from ..runtime import pdf_export_state, start_pdf_export
from ..validation import validate_directory

router = APIRouter(tags=["pdf-export"])


@router.get("/api/output/projects")
def output_projects(root: str | None = None) -> dict:
    """List canonical projects beneath the configured output root."""
    output_root = root or config.get("output_dir", "output")
    safe_root = validate_directory(output_root, "output_root")
    return {"root": safe_root, "projects": discover_projects(safe_root)}


@router.get("/api/pdf-export/preview")
def pdf_export_preview(folder: str, stage: str = "cleaned", bilingual: bool = False) -> dict:
    """Return counts and warnings before a potentially expensive export."""
    safe_folder = validate_directory(folder, "folder")
    if bilingual:
        pages = collect_bilingual_folder(safe_folder)
        return {
            "folder": safe_folder,
            "stage": "cleaned + translated",
            "pages": len(pages),
            "translated": sum(bool(page.translated_text) for page in pages),
            "warnings": (
                ["Some pages have no translation"]
                if any(not page.translated_text for page in pages)
                else []
            ),
        }
    pages = collect_folder(safe_folder, stage=stage)
    return {
        "folder": safe_folder,
        "stage": stage,
        "pages": len(pages),
        "warnings": [] if pages else [f"No {stage} text was found in this folder"],
    }


@router.post("/api/pdf-export/start")
def pdf_export_start(req: PdfExportRequest) -> dict:
    folder = validate_directory(req.folder, "folder")
    if req.output is not None:
        validate_directory(req.output, "output")
    if req.manifest is not None:
        validate_directory(req.manifest, "manifest")
    # validate_directory only proves the path is inside an allowed root — it does
    # not prove the path is a directory. Point-at-a-file was the exact failure a
    # user hit (they selected a .pdf inside output/cleaned/text/ and got the
    # generic "No pages found"). Answer it here with guidance instead. Runs
    # AFTER the output/manifest checks so an out-of-roots output still yields the
    # roots-rejection message even when the folder happens not to exist.
    folder_path = Path(folder)
    if folder_path.exists() and not folder_path.is_dir():
        raise HTTPException(
            status_code=400,
            detail=(
                "Input must be a folder, not a file. Point at your output "
                "folder (the one holding cleaned/, raw_ocr/…) — the Stage "
                "selector chooses which text is read."
            ),
        )
    if not folder_path.exists():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Folder not found: {req.folder}. Point at your output folder "
                "(the one holding cleaned/, raw_ocr/…)."
            ),
        )
    started = start_pdf_export(
        req.folder,
        stage=req.stage,
        structure=req.structure,
        output=req.output,
        manifest_path=req.manifest,
        format=req.format,
        style=req.style,
        bilingual=req.bilingual,
    )
    if not started:
        raise HTTPException(status_code=409, detail="A PDF export is already running")
    return {"ok": True}


@router.get("/api/pdf-export/status")
def pdf_export_status_route() -> dict:
    return {
        "status": pdf_export_state.status,
        "error": pdf_export_state.error,
        "output_path": pdf_export_state.output_path,
    }


@router.post("/api/pdf-export/cancel")
def pdf_export_cancel() -> dict:
    if pdf_export_state.status != "running":
        return {"ok": False, "reason": "No PDF export is running"}
    pdf_export_state.cancel_requested.set()
    return {"ok": True}


@router.get("/api/pdf-export/events")
async def pdf_export_events():
    async def gen():
        while True:
            try:
                event = await asyncio.to_thread(pdf_export_state.events.get, True, 1.0)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("done", "error"):
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/pdf-export/download")
def pdf_export_download():
    if not pdf_export_state.output_path:
        raise HTTPException(status_code=404, detail="No PDF has been compiled yet")
    path = Path(pdf_export_state.output_path)
    ext = path.suffix.lower()
    media_type = "text/markdown" if ext == ".md" else "application/pdf"
    return FileResponse(path, media_type=media_type, filename=path.name)


# ---------------------------------------------------------------------------
# PAGE export
# ---------------------------------------------------------------------------
#
# Unlike `/api/pdf-export/*`, which compiles pages through an LLM structuring
# pass and therefore needs the status/SSE/cancel state machine above, PAGE
# export is a pure copy of already-persisted XML files. It is synchronous and
# needs none of that machinery.


@router.get("/api/page-export/download")
def page_export_download(output_dir: str, stem: str) -> FileResponse:
    """Serve one persisted PAGE document as an XML download."""
    safe_dir = validate_directory(output_dir, "output_dir")
    path = page_path(safe_dir, stem)
    if not path.is_file():
        # Deliberately omits the resolved path — this file's error messages
        # never disclose server filesystem layout.
        raise HTTPException(
            status_code=404,
            detail="No PAGE document found for this page — run OCR on it first.",
        )
    return FileResponse(path, media_type="application/xml", filename=f"{Path(stem).name}.xml")


@router.post("/api/page-export/start")
def page_export_start(req: PageExportRequest) -> dict:
    """Copy selected pages' persisted PAGE XML into the PAGE export directory.

    Synchronous — no model calls, so no status/SSE/cancel state machine.
    """
    output_dir = validate_directory(req.output_dir, "output_dir")
    output = validate_directory(req.output, "output") if req.output is not None else None
    if not req.stems:
        raise HTTPException(
            status_code=400,
            detail="No pages selected — `stems` must be a non-empty list.",
        )
    export_dir, exported, skipped = export_page_xml(
        req.stems, output_dir=output_dir, output=output
    )
    # `skipped` non-empty is a partial result, not a failure — report 200.
    return {
        "export_dir": str(export_dir),
        "exported": exported,
        "skipped": skipped,
    }
