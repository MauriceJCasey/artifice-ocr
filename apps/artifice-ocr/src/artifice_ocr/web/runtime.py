# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Server-side state for the web frontend.

This plays the same role `gui/app.py` plays for the tkinter build: it owns the
queue of :class:`~artifice_ocr.jobs.JobItem` objects and the current
:class:`~artifice_ocr.jobs.JobRunner`, and turns runner events into something a
client can consume. Nothing in `jobs.py` changed to make this possible — the
runner already published progress on a plain `queue.Queue` with no tkinter
import, specifically so a second frontend could drain it differently.

Items are addressed by `id(item)` the same way `gui/widgets/queue_table.py`
addresses treeview rows — a stable string key that survives across SSE
messages without the client needing to understand JobItem internals.
"""

import contextlib
import json
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import config
from .._logging import get_logger
from ..history import HistoryStore
from ..jobs import STAGES, JobItem, JobRunner, State
from ..output import stage_dir
from ..pipeline import run_cleanup_step, run_translate_step
from .serializers import serialize_item, serialize_item_preview

log = get_logger("web.runtime")

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}

# Higher than OCR's own 200 dpi — this pane exists specifically so a user can
# zoom in past what machine OCR needs, to check an individual word by eye.
IMAGE_DPI = 300
IMAGE_MAX_LONG_EDGE = 3000

_IMAGE_PASSTHROUGH_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _item_key(item: JobItem) -> str:
    return str(id(item))


def render_page_image_from(path: str, page: int | None) -> bytes:
    """PNG bytes for a TIFF or single PDF page, given a path and page index.

    JPEG/PNG are not handled here — browsers render those natively, and
    callers should serve them as FileResponse using ``_IMAGE_PASSTHROUGH_TYPES``.

    PDF render DPI is capped by long edge rather than left uncapped, so an
    oversized scan doesn't produce an unreasonably large PNG; TIFF is
    converted at its native resolution.
    """
    import fitz  # PyMuPDF

    p = Path(path)
    suffix = p.suffix.lower()

    if suffix in (".tif", ".tiff"):
        return fitz.Pixmap(str(p)).tobytes("png")

    if suffix == ".pdf":
        doc = fitz.open(str(p))
        try:
            pdf_page = doc[page or 0]
            dpi = IMAGE_DPI
            long_edge_pt = max(pdf_page.rect.width, pdf_page.rect.height)
            long_edge_px = long_edge_pt / 72 * dpi
            if long_edge_px > IMAGE_MAX_LONG_EDGE:
                dpi = dpi * IMAGE_MAX_LONG_EDGE / long_edge_px
            return pdf_page.get_pixmap(dpi=max(int(dpi), 1)).tobytes("png")
        finally:
            doc.close()

    raise ValueError(f"No image renderer for {suffix} files")


def render_page_image(item: JobItem) -> bytes:
    """PNG bytes for a TIFF source, or the single PDF page ``item.page`` points
    at — delegates to ``render_page_image_from``.

    JPEG/PNG need no conversion — browsers render them natively —
    and are served directly by the route instead of coming through here.
    """
    return render_page_image_from(item.path, item.page)


_SAVE_CONFIG = {
    "raw": {
        "dir": "raw_ocr",
        "text_key": "extracted_text",
        "result_key": "raw",
    },
    "cleaned": {
        "dir": "cleaned",
        "text_key": "cleaned_text",
        "result_key": "cleaned",
    },
    "translated": {
        "dir": "translated",
        "text_key": "translated_text",
        "result_key": "translated",
    },
}


def _save_stage_text(item: JobItem, stage: str, text: str) -> dict[str, Any]:
    """Persist a manual correction to an item's stage text.

    Updates the in-memory copy first (what subsequent stages or Tropy write-back read).
    Also overwrites on-disk text/JSON files *if a prior run already produced them*.
    Only the text field (+ new `edited`/`edited_at`) changes in the JSON — provenance
    fields (`engine`/`model`/`prompt`/`timestamp`) are left untouched to record what
    the original stage actually produced.

    On the first edit, the original text is preserved alongside the corrected version
    (``original_{text_key}``) so the user can review what the model originally produced.
    """
    if stage not in _SAVE_CONFIG:
        raise ValueError(f"Unknown stage: {stage}")
    cfg = _SAVE_CONFIG[stage]

    current = (item.results.get(cfg["result_key"]) or {}).get(cfg["text_key"], "")
    # Preserve original text on first edit
    if current and current != text:
        orig_key = f"original_{cfg['text_key']}"
        if orig_key not in (item.results.get(cfg["result_key"]) or {}):
            item.results.setdefault(cfg["result_key"], {})[orig_key] = current

    item.results.setdefault(cfg["result_key"], {})[cfg["text_key"]] = text

    output_dir = state.runner.output_dir if state.runner else config.get("output_dir")
    text_path = stage_dir(output_dir, cfg["dir"]) / "text" / f"{item.stem}.txt"
    if text_path.exists():
        text_path.write_text(text, encoding="utf-8")

        json_path = stage_dir(output_dir, cfg["dir"]) / "records" / f"{item.stem}.json"
        if not json_path.exists():
            json_path = Path(output_dir) / cfg["dir"] / "json" / f"{item.stem}.json"
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            # Preserve original text on first disk edit
            orig_key = f"original_{cfg['text_key']}"
            if orig_key not in data and current and current != text:
                data[orig_key] = current
            data[cfg["text_key"]] = text
            data["edited"] = True
            data["edited_at"] = datetime.now(UTC).isoformat()
            json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    return serialize_item_preview(item)


def save_raw_text(item: JobItem, text: str) -> dict[str, Any]:
    """Persist a manual correction to an item's raw OCR text."""
    return _save_stage_text(item, "raw", text)


def save_cleaned_text(item: JobItem, text: str) -> dict[str, Any]:
    """Persist a manual correction to an item's cleaned text."""
    return _save_stage_text(item, "cleaned", text)


def save_translated_text(item: JobItem, text: str) -> dict[str, Any]:
    """Persist a manual correction to an item's translated text."""
    return _save_stage_text(item, "translated", text)


def set_fabricated_result(item: JobItem, value: bool) -> dict[str, Any]:
    """Capture a reviewer label in memory, history, and raw OCR metadata."""
    item.fabricated_result = bool(value)
    reviewed_at = datetime.now(UTC).isoformat()

    output_dir = state.runner.output_dir if state.runner else config.get("output_dir")
    json_path = stage_dir(output_dir, "raw_ocr") / "records" / f"{item.stem}.json"
    if not json_path.exists():
        json_path = Path(output_dir) / "raw_ocr" / "json" / f"{item.stem}.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("OCR metadata root is not an object")
            data["fabricated_result"] = item.fabricated_result
            data["fabricated_reviewed_at"] = reviewed_at if item.fabricated_result else None
            json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except (json.JSONDecodeError, OSError, TypeError, UnicodeDecodeError) as exc:
            # The in-memory flag is still authoritative and will be recorded
            # in history when the run finishes. Never destroy a corrupt record
            # merely to add review metadata; leave it intact for diagnosis.
            log.warning("Could not update OCR metadata %s: %s", json_path, exc)

    if item.history_item_id is not None:
        state.history.set_fabricated_result(item.history_item_id, item.fabricated_result)
    return serialize_item_preview(item)


def reprocess_item(item: JobItem, from_stage: str, stages: list[str]) -> dict[str, Any]:
    """Re-run downstream stages after a manual correction.

    ``from_stage`` is the stage whose text was corrected (``"raw"`` or
    ``"cleaned"``). ``stages`` lists which downstream stages to re-run
    (e.g. ``["cleanup", "translate"]``).

    Each re-run stage calls the same pipeline step the runner uses, so the
    on-disk text/JSON files are overwritten with fresh model output. The item's
    in-memory results, language, and confidence are updated in place.

    Returns the same shape as ``serialize_item_preview``.
    """
    output_dir = state.runner.output_dir if state.runner else config.get("output_dir")

    for stage in stages:
        if stage == "cleanup" and from_stage in ("raw", "cleanup"):
            raw_text = (item.results.get("raw") or {}).get("extracted_text", "")
            raw_data = {
                "source_file": item.path,
                "extracted_text": raw_text,
                "stage": "raw_ocr",
            }
            cleaned = run_cleanup_step(
                raw_data,
                item.stem,
                output_dir,
                skip_cleanup=False,
                resume=False,
                force=True,
            )
            item.results["cleaned"] = cleaned
            item.results.setdefault("cleaned", {})["cleaned_text"] = cleaned.get("cleaned_text", "")

        elif stage == "translate" and from_stage in ("raw", "cleaned", "translate"):
            cleaned_text = (item.results.get("cleaned") or {}).get("cleaned_text", "")
            cleaned_data = {
                "source_file": item.path,
                "cleaned_text": cleaned_text,
                "stage": "cleaned",
            }
            translated = run_translate_step(
                cleaned_data,
                item.stem,
                output_dir,
                resume=False,
                force=True,
            )
            item.results["translated"] = translated
            item.results.setdefault("translated", {})["translated_text"] = translated.get(
                "translated_text", ""
            )
            item.language = translated.get("source_language_name", "")
            conf = translated.get("confidence") or {}
            item.confidence = conf.get("overall_score")

    return serialize_item_preview(item)


def batch_replace(
    find: str, replace: str, stages: list[str], item_ids: list[str] | None = None
) -> dict:
    """Apply a find/replace correction to one or more queue items.

    ``stages`` lists which text stages to modify (``"raw"``, ``"cleaned"``,
    ``"translated"``). If ``item_ids`` is None or empty, applies to every
    item currently in the queue.

    Returns the updated queue snapshot.
    """
    items = state.items
    if item_ids:
        items = [state.get(i) for i in item_ids if state.get(i) is not None]

    updated = 0
    for item in items:
        if item.state in ("pending", "running", "cancelled"):
            continue
        for stage in stages:
            if stage not in _SAVE_CONFIG:
                continue
            cfg = _SAVE_CONFIG[stage]
            current = (item.results.get(cfg["result_key"]) or {}).get(cfg["text_key"], "")
            if not current:
                continue
            if find not in current:
                continue
            new_text = current.replace(find, replace)
            _save_stage_text(item, stage, new_text)
            updated += 1

    return {"updated": updated, "items": state.queue_snapshot()}


class RunState:
    """Everything about the batch currently queued or in flight.

    One instance per server process. A real multi-user deployment would key
    this per session; a local tool run by one person on their own machine
    does not need that complexity.
    """

    def __init__(self):
        config.load_config()
        config.apply_overrides(config.load_user_settings())

        self.items: list[JobItem] = []
        self._by_id: dict[str, JobItem] = {}
        self.runner: JobRunner | None = None
        self.run_id: int | None = None
        self._history: HistoryStore | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- history
    @property
    def history(self) -> HistoryStore:
        if self._history is None:
            self._history = HistoryStore(config.get("history_db"))
        return self._history

    # ----------------------------------------------------------------- queue
    def add_paths(self, paths: list[str]) -> list[JobItem]:
        """Resolve files and folders into queue items, skipping duplicates."""
        resolved: list[str] = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                resolved.extend(
                    str(f)
                    for f in sorted(p.rglob("*"))
                    if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
                )
            elif p.suffix.lower() in SUPPORTED_EXTENSIONS:
                resolved.append(str(p))

        known = {(i.path, i.stem) for i in self.items}
        added: list[JobItem] = []
        with self._lock:
            for path in resolved:
                item = JobItem(path=path)
                if (item.path, item.stem) in known:
                    continue
                known.add((item.path, item.stem))
                self.items.append(item)
                self._by_id[_item_key(item)] = item
                added.append(item)
        return added

    def add_items(self, items: list[JobItem]) -> list[JobItem]:
        """Add pre-built items (e.g. from a Tropy selection)."""
        known = {(i.path, i.stem) for i in self.items}
        added: list[JobItem] = []
        with self._lock:
            for item in items:
                if (item.path, item.stem) in known:
                    continue
                known.add((item.path, item.stem))
                self.items.append(item)
                self._by_id[_item_key(item)] = item
                added.append(item)
        return added

    # remove/clear/reorder mutate `self.items` in place. The runner no
    # longer shares that list object (see JobRunner.__init__), so none of
    # these can corrupt an in-progress run's iteration any more — but
    # letting them proceed anyway would still desync `state.items` from
    # what the runner is actually processing, which reads to the user as a
    # queue that no longer matches the run in progress. Guarded the same
    # way `start_run()` guards against a second concurrent run: raise
    # before touching anything, so the route can translate it to a 409.
    #
    # `add_paths`/`add_items` (and the upload route that calls `add_paths`)
    # are deliberately NOT guarded here: appending to `self.items` mid-run
    # is safe now that the runner holds its own copy — the new item just
    # isn't part of what's currently running and is picked up by the next
    # run. Guarding additions too would block a legitimate action for no
    # correctness reason.
    def remove(self, ids: list[str]) -> int:
        if self.runner is not None and self.runner.is_running:
            raise RuntimeError("Cannot modify the queue while a run is in progress")
        with self._lock:
            targets = [self._by_id[i] for i in ids if i in self._by_id]
            for item in targets:
                self.items.remove(item)
                del self._by_id[_item_key(item)]
            return len(targets)

    def clear(self) -> None:
        if self.runner is not None and self.runner.is_running:
            raise RuntimeError("Cannot modify the queue while a run is in progress")
        with self._lock:
            self.items.clear()
            self._by_id.clear()

    def get(self, item_id: str) -> JobItem | None:
        return self._by_id.get(item_id)

    def queue_snapshot(self) -> list[dict]:
        return [serialize_item(i) for i in self.items]

    def tropy_eligible_items(self, item_ids: list[str] | None) -> list[JobItem]:
        """Queue items that came from the Tropy JSON-LD bridge (carry
        origin = 'tropy-jsonld'), for export back."""
        pool = [self.get(i) for i in item_ids] if item_ids is not None else list(self.items)
        return [
            i for i in pool if i is not None and (i.source or {}).get("origin") == "tropy-jsonld"
        ]

    def tropy_writable_items(self, item_ids: list[str] | None) -> list[JobItem]:
        """Queue items that can be written back to Tropy: those carrying a
        numeric Tropy photo id.

        Deliberately filters on ``photo_id`` presence rather than on ``origin``.
        That is exactly what ``tropy_write.entries_from_items`` requires, so this
        states its own contract instead of encoding it as a proxy. Today only
        live-browse items (``origin == "tropy-live"``) carry one — the JSON-LD
        manifest hardcodes ``photo_id: null`` — but if that ever changes this
        keeps working.
        """
        pool = [self.get(i) for i in item_ids] if item_ids is not None else list(self.items)
        return [i for i in pool if i is not None and (i.source or {}).get("photo_id") is not None]

    # --------------------------------------------------------------- running
    def start_run(self, *, stages: set[str], output_dir: str, force: bool) -> queue.Queue:
        if self.runner is not None and self.runner.is_running:
            raise RuntimeError("A run is already in progress")
        if not self.items:
            raise ValueError("Queue is empty")
        if not stages:
            raise ValueError("No stages selected")

        config.apply_overrides({"output_dir": output_dir})
        config.save_user_settings({"output_dir": output_dir})

        # Resolve concrete model/backend pairs once, before any item runs.
        # Fails fast with a legible RuntimeError when a model cannot be
        # resolved, so a bad setup is reported up front rather than as a raw
        # provider 404 on the first file.
        from .._resolution import preflight_run, resolve_models_for_run

        resolve_models_for_run(stages=stages)
        # Preflight: one cheap probe per selected stage's backend, reusing the
        # resolution pass's cached results.  A bad endpoint (unreachable,
        # policy-rejected, wrong API shape, or a model that vanished between
        # resolution and now) raises a RuntimeError the router maps to a 409 —
        # a run must not start only to fail identically on every queued page.
        preflight_run(stages=stages)

        self.run_id = self.history.start_run(
            stages=[s for s in STAGES if s in stages],
            output_dir=output_dir,
            total=len(self.items),
        )

        events: queue.Queue = queue.Queue()
        self.runner = JobRunner(self.items, output_dir, stages=stages, force=force, events=events)
        self.runner.start()
        return events

    def record_finished_item(self, item: JobItem) -> None:
        """Persist one finished item to history. Called once per item_finished event."""
        if self.run_id is None:
            return
        if item.history_item_id is not None:
            return
        if item.state not in (State.DONE, State.FAILED):
            return
        with contextlib.suppress(Exception):
            item.history_item_id = self.history.record_item(self.run_id, item)

    def finish_run(self, payload: dict) -> None:
        if self.run_id is not None:
            self.history.finish_run(
                self.run_id,
                succeeded=payload.get("done", 0),
                failed=payload.get("failed", 0),
                elapsed=payload.get("elapsed", 0.0),
            )
        self.run_id = None

    def pause(self) -> None:
        if self.runner:
            self.runner.pause()

    def resume(self) -> None:
        if self.runner:
            self.runner.unpause()

    def cancel(self) -> None:
        if self.runner:
            self.runner.cancel()

    def skip(self, item_id: str) -> bool:
        item = self.get(item_id)
        if item is None or self.runner is None:
            return False
        self.runner.skip(item)
        return True

    def retry(self, ids: list[str]) -> bool:
        """Reset finished/failed items so the next start-run retries them."""
        if self.runner is not None and self.runner.is_running:
            return False
        for item_id in ids:
            item = self.get(item_id)
            if item is not None and item.state in (State.DONE, State.FAILED, State.CANCELLED):
                item.state = State.PENDING
                item.error = ""
                item.results = {}
                item.confidence = None
                item.language = ""
                for status in item.stages.values():
                    status.state = State.PENDING
                    status.elapsed = 0.0
                    status.chars = 0
                    status.error = ""
        return True

    def reorder(self, drag_id: str, drop_id: str, before: bool = True) -> None:
        """Move a queue item from one position to another."""
        if self.runner is not None and self.runner.is_running:
            raise RuntimeError("Cannot modify the queue while a run is in progress")
        with self._lock:
            drag_item = self._by_id.get(drag_id)
            drop_item = self._by_id.get(drop_id)
            if drag_item is None or drop_item is None:
                return
            if drag_item is drop_item:
                return
            self.items.remove(drag_item)
            idx = self.items.index(drop_item)
            if not before:
                idx += 1
            self.items.insert(idx, drag_item)

    def status(self) -> dict:
        return {
            "running": bool(self.runner and self.runner.is_running),
            "paused": bool(self.runner and self.runner.is_paused),
            "total": len(self.items),
            # Capability flag: tells the client the upload endpoint exists so
            # it can enable the dropzone. Always true once the route ships.
            "upload_enabled": True,
        }


# One instance for the process.
state = RunState()


# --------------------------------------------------------------------------- #
# PDF export (one-off, not wired into JobRunner — see
# docs/archive/handoffs/HANDOFF_PDF_EXPORT_UI.md, which is historical)
# --------------------------------------------------------------------------- #


class PdfExportState:
    """State for a single one-off PDF export operation.

    Deliberately not wired into JobRunner/STAGES — this runs outside the
    pipeline, reading finished text from disk.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | running | done | error
        self.error: str | None = None
        self.output_path: str | None = None
        self.events: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.cancel_requested = threading.Event()


pdf_export_state = PdfExportState()


def start_pdf_export(
    folder,
    *,
    stage,
    structure,
    output,
    manifest_path,
    format="pdf",
    style="readable",
    bilingual=False,
) -> bool:
    """Returns False (caller should 409) if one is already running."""
    with pdf_export_state.lock:
        if pdf_export_state.status == "running":
            return False
        pdf_export_state.status = "running"
        pdf_export_state.error = None
        pdf_export_state.output_path = None
        pdf_export_state.cancel_requested.clear()
        pdf_export_state.events = queue.Queue()
        pdf_export_state.thread = threading.Thread(
            target=_run_pdf_export,
            args=(folder, stage, structure, output, manifest_path, format, style, bilingual),
            daemon=True,
        )
        pdf_export_state.thread.start()
    return True


def _run_pdf_export(folder, stage, structure, output, manifest_path, format, style, bilingual):
    from .. import pdf_export

    def on_progress(message):
        if pdf_export_state.cancel_requested.is_set():
            raise RuntimeError("PDF export cancelled")
        pdf_export_state.events.put({"type": "log", "message": message})

    try:
        result_path = pdf_export.compile(
            folder,
            stage=stage,
            structure=structure,
            output=output,
            manifest_path=manifest_path,
            format=format,
            style=style,
            bilingual=bilingual,
            on_progress=on_progress,
        )
        with pdf_export_state.lock:
            pdf_export_state.output_path = str(result_path)
            pdf_export_state.status = "done"
            pdf_export_state.events.put({"type": "done", "output_path": str(result_path)})
    except Exception as exc:
        with pdf_export_state.lock:
            pdf_export_state.status = "error"
            pdf_export_state.error = str(exc)
            pdf_export_state.events.put({"type": "error", "message": str(exc)})
