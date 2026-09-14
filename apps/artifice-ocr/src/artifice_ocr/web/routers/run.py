# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run control routes: start, pause, resume, cancel, skip, retry, status."""

from artifice_output import ProjectLayout
from fastapi import APIRouter, HTTPException

from ... import config
from ...jobs import STAGES
from ..models import SkipRequest, StartRunRequest
from ..runtime import state
from ..validation import validate_directory

router = APIRouter(tags=["run"])


@router.post("/api/run/start")
def start_run(req: StartRunRequest) -> dict:
    stages = {s for s in req.stages if s in STAGES}
    if "ocr" not in stages:
        raise HTTPException(status_code=409, detail="OCR is required for every new run")
    output_dir = validate_directory(req.output_dir, "output_dir")
    if req.project or req.output_dir == "output":
        layout = ProjectLayout(output_dir, req.project or "OCR project", create=True)
        output_dir = str(layout.project_dir)
    # Segmentation is a config.py setting (Commit 3+), read by pipeline.py via
    # cfg("segmentation_enabled") — not a start_run() argument, which only
    # accepts stages/output_dir/force. Always set segmentation_enabled
    # explicitly from the request rather than only when a provider is chosen:
    # otherwise a prior run's enabled setting would silently persist and
    # re-enable segmentation for a run where the toggle is now off.
    overrides = {"segmentation_enabled": bool(req.segmentation_provider)}
    if req.segmentation_provider:
        overrides["segmentation_provider"] = req.segmentation_provider
    config.apply_overrides(overrides)
    try:
        state.start_run(stages=stages, output_dir=output_dir, force=req.force)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "output_dir": output_dir}


@router.post("/api/run/pause")
def pause_run() -> dict:
    state.pause()
    return {"ok": True}


@router.post("/api/run/resume")
def resume_run() -> dict:
    state.resume()
    return {"ok": True}


@router.post("/api/run/cancel")
def cancel_run() -> dict:
    state.cancel()
    return {"ok": True}


@router.post("/api/run/skip")
def skip_item(req: SkipRequest) -> dict:
    ok = state.skip(req.id)
    return {"ok": ok}


@router.post("/api/run/retry")
def retry_selected(ids: list[str]) -> dict:
    """Re-run one or more finished/failed items."""
    ok = state.retry(ids)
    return {"ok": ok}


@router.get("/api/run/status")
def run_status() -> dict:
    return state.status()
