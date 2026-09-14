# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Write OCR results to their original Tropy photos through Tropy's API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...output import page_stage
from ...tropy_api import TropyAPIClient, TropyAPIError, TropyConnection, connect
from ...tropy_db import resolve_project_db_path
from ..runtime import state
from ..validation import validate_directory

router = APIRouter(tags=["tropy-notes"])

_STAGE_FIELDS = {
    "raw_ocr": ("raw", "extracted_text", "raw_text"),
    "cleaned": ("cleaned", "cleaned_text", "cleaned_text"),
    "translated": ("translated", "translated_text", "translated_text"),
}


class TropyNotesRequest(BaseModel):
    source: Literal["queue", "history"] = "queue"
    item_ids: list[str | int] = Field(default_factory=list)
    stage: str = "cleaned"
    project_path: str | None = None


class TropyNotesCommitRequest(TropyNotesRequest):
    expected_write_count: int


@dataclass(frozen=True)
class NoteEntry:
    photo_id: int
    item_id: int | None
    text: str
    label: str
    language: str
    project: Path


@dataclass
class NotePlan:
    entry: NoteEntry
    action: str
    reason: str = ""


def _language(value: str | None) -> str:
    clean = (value or "en").strip().lower()
    return clean if clean.isalpha() and len(clean) <= 3 else "en"


def _same_path(left: Path, right: Path) -> bool:
    return (
        str(left.resolve()).replace("\\", "/").casefold()
        == str(right.resolve()).replace("\\", "/").casefold()
    )


def _queue_entries(req: TropyNotesRequest) -> tuple[list[NoteEntry], int]:
    items = [state.get(str(value)) for value in req.item_ids]
    items = [item for item in items if item is not None]

    bucket, field, _history_field = _STAGE_FIELDS[req.stage]
    entries: list[NoteEntry] = []
    for item in items:
        # A reviewer has explicitly said this transcription contains invented
        # text. Keep it as a diagnostic example, but never write it to Tropy.
        if item.fabricated_result:
            continue
        src = item.source or {}
        photo_id = src.get("photo_id")
        project = src.get("tropy_project")
        if photo_id is None or not project:
            continue
        # PAGE is the authoritative record — read the requested TextEquiv index
        # first, falling back to the in-memory stage result for pre-PAGE runs.
        page_doc = (item.results.get(bucket) or {}).get("_page_document")
        text = (page_doc.text(page_stage(req.stage)) if page_doc is not None else "") or ""
        if not text:
            text = (item.results.get(bucket) or {}).get(field, "") or ""
        entries.append(
            NoteEntry(
                photo_id=int(photo_id),
                item_id=int(src["tropy_item_id"]) if src.get("tropy_item_id") is not None else None,
                text=text,
                label=item.name,
                language=_language(item.language),
                project=resolve_project_db_path(project).resolve(),
            )
        )
    return entries, len(items)


def _history_entries(req: TropyNotesRequest) -> tuple[list[NoteEntry], int]:
    if not req.item_ids:
        return [], 0
    _bucket, _field, history_field = _STAGE_FIELDS[req.stage]
    rows = []
    for value in req.item_ids:
        try:
            row = state.history.get_item(int(value))
        except (TypeError, ValueError):
            row = None
        if row is not None:
            rows.append(row)

    entries: list[NoteEntry] = []
    for row in rows:
        if row["fabricated_result"]:
            continue
        if row["photo_id"] is None or not row["tropy_project_path"]:
            continue
        entries.append(
            NoteEntry(
                photo_id=int(row["photo_id"]),
                item_id=int(row["tropy_item_id"]) if row["tropy_item_id"] is not None else None,
                text=row[history_field] or "",
                label=row["name"] or f"History item {row['item_id']}",
                language=_language(row["language"]),
                project=resolve_project_db_path(row["tropy_project_path"]).resolve(),
            )
        )
    return entries, len(rows)


def _entries(req: TropyNotesRequest) -> tuple[list[NoteEntry], int]:
    if req.stage not in _STAGE_FIELDS:
        raise HTTPException(status_code=400, detail=f"Unknown stage: {req.stage}")
    return _history_entries(req) if req.source == "history" else _queue_entries(req)


def _target_project(req: TropyNotesRequest, entries: list[NoteEntry]) -> Path | None:
    if req.project_path:
        validated = validate_directory(req.project_path, "project_path")
        return resolve_project_db_path(validated).resolve()
    projects: list[Path] = []
    for entry in entries:
        if not any(_same_path(entry.project, known) for known in projects):
            projects.append(entry.project)
    return projects[0] if len(projects) == 1 else None


def _preview(req: TropyNotesRequest) -> tuple[dict, list[NotePlan], TropyConnection | None]:
    entries, selected = _entries(req)
    target = _target_project(req, entries)
    counts = {
        "selected": selected,
        "ready": 0,
        "duplicate": 0,
        "empty": 0,
        "foreign": 0,
        "missing_photo": 0,
        "item_mismatch": 0,
        "error": 0,
        "ineligible": selected - len(entries),
    }
    blockers: list[str] = ["No results were selected"] if selected == 0 else []
    plans: list[NotePlan] = []
    connection: TropyConnection | None = None

    if selected and target is None:
        blockers.append("Select results from one Tropy project")
    else:
        for entry in entries:
            if not _same_path(entry.project, target):
                counts["foreign"] += 1
                plans.append(NotePlan(entry, "foreign", "belongs to another Tropy project"))
            elif not entry.text.strip():
                counts["empty"] += 1
                plans.append(NotePlan(entry, "empty", f"no {req.stage} text"))

    if counts["foreign"]:
        blockers.append("Some selected results belong to another Tropy project")

    if target is not None and not blockers:
        try:
            connection = connect(target)
        except TropyAPIError as exc:
            blockers.append(str(exc))
        else:
            already_planned = {id(plan.entry) for plan in plans}
            with TropyAPIClient(connection) as client:
                for entry in entries:
                    if id(entry) in already_planned:
                        continue
                    # A single slow or flaky photo must not sink the whole
                    # batch: a project of hundreds of pages has hundreds of
                    # chances to hit one bad Tropy response, and losing every
                    # other result's progress to it just forces a full,
                    # equally fragile retry. Record it and keep checking.
                    try:
                        photo = client.photo(entry.photo_id)
                        if photo is None:
                            counts["missing_photo"] += 1
                            plans.append(NotePlan(entry, "missing_photo", "photo no longer exists"))
                        elif (
                            entry.item_id is not None
                            and int(photo.get("item", -1)) != entry.item_id
                        ):
                            counts["item_mismatch"] += 1
                            plans.append(
                                NotePlan(entry, "item_mismatch", "photo belongs to another item")
                            )
                        elif client.has_identical_note(photo, entry.text):
                            counts["duplicate"] += 1
                            plans.append(
                                NotePlan(entry, "duplicate", "identical note already exists")
                            )
                        else:
                            counts["ready"] += 1
                            plans.append(NotePlan(entry, "ready"))
                    except TropyAPIError as exc:
                        counts["error"] += 1
                        plans.append(NotePlan(entry, "error", str(exc)))

    if selected and not entries:
        blockers.append("The selected results were not imported through Browse Project")

    result = {
        "blockers": blockers,
        "counts": counts,
        "write_count": counts["ready"],
        "item_errors": [
            {"label": plan.entry.label, "message": plan.reason}
            for plan in plans
            if plan.action == "error"
        ],
        "project": (
            {
                "name": connection.project_name,
                "id": connection.project_id,
                "version": connection.version,
                "port": connection.port,
            }
            if connection is not None
            else None
        ),
    }
    return result, plans, connection


@router.post("/api/tropy/notes/preview")
def tropy_notes_preview(req: TropyNotesRequest) -> dict:
    result, _plans, _connection = _preview(req)
    return result


@router.post("/api/tropy/notes/commit")
def tropy_notes_commit(req: TropyNotesCommitRequest) -> dict:
    result, plans, connection = _preview(req)
    if result["blockers"]:
        raise HTTPException(status_code=409, detail={"blockers": result["blockers"]})
    if result["write_count"] != req.expected_write_count:
        raise HTTPException(
            status_code=409,
            detail="The Tropy project or OCR results changed; preview again",
        )
    if connection is None:
        raise HTTPException(status_code=409, detail="Tropy is not connected")

    written = 0
    skipped = result["counts"]["duplicate"]
    errors: list[dict[str, str]] = []
    note_ids: list[int] = []
    with TropyAPIClient(connection) as client:
        try:
            client.verify_current()
        except TropyAPIError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        for plan in plans:
            if plan.action != "ready":
                continue
            try:
                # The preview can be older than the commit.  Re-read the photo
                # immediately before POSTing so an identical note added in the
                # meantime is skipped; POST is reserved for creating a new note.
                photo = client.photo(plan.entry.photo_id)
                if photo is None:
                    errors.append({"label": plan.entry.label, "message": "photo no longer exists"})
                    continue
                if client.has_identical_note(photo, plan.entry.text):
                    skipped += 1
                    continue
                note_ids.extend(
                    client.create_note(
                        plan.entry.photo_id,
                        plan.entry.text.strip(),
                        plan.entry.language,
                    )
                )
                written += 1
            except TropyAPIError as exc:
                # One photo's worth of trouble (a slow response, a note Tropy
                # rejected) must not cost every other "ready" item in a large
                # batch its already-checked, already-earned write.
                errors.append({"label": plan.entry.label, "message": str(exc)})

    return {
        "status": "complete" if not errors else "partial",
        "written": written,
        "skipped": skipped,
        "remaining": max(
            0,
            req.expected_write_count - written - (skipped - result["counts"]["duplicate"]),
        ),
        "errors": errors,
        "note_ids": note_ids,
        "project": result["project"],
    }
