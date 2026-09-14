# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job runner: per-file pipeline execution with live status, pause and skip.

The pipeline runs three strictly sequential passes — OCR, then Cleanup,
then Translate — so that only one inference engine is active at a time.
Progress is published as :class:`JobEvent` objects on a ``queue.Queue``
which the caller drains at its own pace (the GUI polls it from the tk
main loop).

Retry is deliberately *not* handled here — a retry is simply a fresh runner
over the selected items. Because completed stages leave outputs on disk,
``resume`` makes the retry pick up where the failure happened.
"""

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._logging import get_logger
from .config import get as cfg
from .pipeline import (
    SKIP_ALREADY_EXISTS,
    SKIP_NOT_SELECTED,
    run_cleanup_step,
    run_ocr_step,
    run_title_step,
    run_translate_step,
)

if TYPE_CHECKING:
    from .pagexml import PageDocument

log = get_logger("jobs")

STAGES = ("ocr", "cleanup", "title", "translate")

STAGE_LABELS = {
    "ocr": "OCR",
    "cleanup": "Cleanup",
    "title": "Title",
    "translate": "Translate",
}


class State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StageStatus:
    state: State = State.PENDING
    elapsed: float = 0.0
    chars: int = 0
    error: str = ""
    # Why `state` is SKIPPED — SKIP_NOT_SELECTED (the user didn't enable this
    # stage) or SKIP_ALREADY_EXISTS (its output was reused; `skip_key` is the
    # matched output key). Empty for any non-skipped state. Without this, a
    # page whose text was reused from a prior run looked identical to one the
    # user deliberately turned off.
    skip_reason: str = ""
    skip_key: str = ""


@dataclass
class JobItem:
    """One unit of work moving through the pipeline.

    Usually a file, but a Tropy page is one page *inside* a shared PDF — hence
    `page` (0-based, PDFs only) and `output_stem`, which decouples the output
    key from the filename. Without that, every page of a checksum-named PDF
    would write to the same place.
    """

    path: str
    stages: dict[str, StageStatus] = field(default_factory=dict)
    state: State = State.PENDING
    confidence: int | None = None
    language: str = ""
    attempts: int = 0
    results: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    page: int | None = None
    output_stem: str = ""
    label: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    guard_rejected: bool = False
    fabricated_result: bool = False
    history_item_id: int | None = None
    # The authoritative PAGE document for this item, layered up as each stage
    # executes. Distinct from `page` (the 0-based PDF page index) — this is a
    # PageDocument, not a page number. Reset like the other per-run fields.
    page_document: "PageDocument | None" = None

    def __post_init__(self):
        if not self.stages:
            self.stages = {s: StageStatus() for s in STAGES}

    @property
    def name(self) -> str:
        return self.label or Path(self.path).name

    @property
    def stem(self) -> str:
        return self.output_stem or Path(self.path).stem

    @property
    def elapsed(self) -> float:
        return sum(s.elapsed for s in self.stages.values())

    def reset(self, enabled_stages: set[str]) -> None:
        """Prepare for a (re)run, clearing prior state."""
        self.state = State.PENDING
        self.error = ""
        self.results = {}
        self.confidence = None
        self.language = ""
        self.guard_rejected = False
        self.fabricated_result = False
        self.history_item_id = None
        self.page_document = None
        for name, status in self.stages.items():
            status.state = State.PENDING if name in enabled_stages else State.SKIPPED
            status.elapsed = 0.0
            status.chars = 0
            status.error = ""
            # Set here so a stage whose phase never calls `_run_stage` at all
            # (title/translate return early when deselected — see
            # `_phase_title`/`_phase_translate`) still reports why it's
            # skipped. A stage that *does* run (ocr/cleanup always do, with
            # their own skip_* flag) overwrites this with whatever
            # `_run_stage` actually observes.
            status.skip_reason = SKIP_NOT_SELECTED if name not in enabled_stages else ""
            status.skip_key = ""


@dataclass
class JobEvent:
    """Something the runner wants the UI to know about."""

    kind: str  # run_started | item_started | stage_started | stage_finished
    #            item_finished | run_finished | paused | resumed | log
    item: JobItem | None = None
    stage: str = ""
    message: str = ""
    tag: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class JobRunner:
    """Runs a batch of :class:`JobItem` through the pipeline.

    The pipeline executes three strictly sequential passes — OCR, Cleanup,
    then Translate — so only one inference engine is active at a time.
    """

    def __init__(
        self,
        items: list[JobItem],
        output_dir: str,
        *,
        stages: set[str],
        force: bool = False,
        events: queue.Queue | None = None,
        max_workers: int | None = None,
    ):
        # A defensive copy, not an alias: the caller (RunState) keeps
        # mutating its own `items` list from the HTTP request thread while
        # this runner iterates its own copy on a background thread for as
        # long as the run takes. Sharing the list object let `remove`/
        # `clear`/`reorder` corrupt an in-progress iteration out from under
        # this thread with no synchronisation at all.
        self.items = list(items)
        self.output_dir = output_dir
        self.stages = set(stages)
        self.force = force
        self.events: queue.Queue = events or queue.Queue()
        self.max_workers = max_workers or cfg("max_ocr_workers")

        self._resume_gate = threading.Event()
        self._resume_gate.set()
        self._cancelled = False
        self._skip_ids: set[int] = set()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        for item in self.items:
            item.reset(self.stages)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return not self._resume_gate.is_set()

    def pause(self) -> None:
        """Pause between stages. An in-flight model call is allowed to finish."""
        if not self.is_paused:
            self._resume_gate.clear()
            self._emit("paused", message="Paused — finishing in-flight requests")

    def unpause(self) -> None:
        if self.is_paused:
            self._resume_gate.set()
            self._emit("resumed", message="Resumed")

    def cancel(self) -> None:
        self._cancelled = True
        self._resume_gate.set()  # release anything blocked at the gate

    def skip(self, item: JobItem) -> None:
        """Skip an item that has not finished yet."""
        self._skip_ids.add(id(item))

    # ---------------------------------------------------------------- events
    def _emit(self, kind: str, **kwargs) -> None:
        self.events.put(JobEvent(kind=kind, **kwargs))

    def _gate(self) -> bool:
        """Block while paused. Returns False if the run was cancelled."""
        self._resume_gate.wait()
        return not self._cancelled

    def _should_skip(self, item: JobItem) -> bool:
        return self._cancelled or id(item) in self._skip_ids

    # ------------------------------------------------------------------- run
    def _run(self) -> None:
        t0 = time.monotonic()
        self._emit(
            "run_started",
            message=f"Pipeline start — {len(self.items)} file(s), "
            f"stages: {', '.join(s for s in STAGES if s in self.stages)}",
            tag="accent",
        )

        try:
            self._phase_ocr()
            self._phase_cleanup()
            self._phase_title()
            self._phase_translate()

            for item in self.items:
                if item.state not in (
                    State.DONE,
                    State.FAILED,
                    State.SKIPPED,
                    State.CANCELLED,
                ):
                    self._finish_item(item, State.DONE)
        finally:
            elapsed = time.monotonic() - t0
            done = sum(1 for i in self.items if i.state is State.DONE)
            failed = sum(1 for i in self.items if i.state is State.FAILED)
            # Items that finished normally but had at least one stage reuse
            # existing output — the "every page shows skipped and the run
            # finishes instantly" case this whole feature exists to surface.
            reused = sum(
                1
                for i in self.items
                if any(s.skip_reason == SKIP_ALREADY_EXISTS for s in i.stages.values())
            )
            message = f"Run finished in {elapsed:.1f}s — {done} ok, {failed} failed"
            if reused:
                message += f", {reused} reused existing output (tick Force re-run to redo)"
            self._emit(
                "run_finished",
                message=message,
                tag="success" if not failed else "warning",
                payload={"elapsed": elapsed, "done": done, "failed": failed, "skipped": reused},
            )

    # ---------------------------------------------------------- phase helpers
    def _begin_item(self, item: JobItem) -> bool:
        """Prepare an item for processing. Returns False if it should be skipped."""
        if self._should_skip(item) or not self._gate():
            self._finish_item(item, State.CANCELLED if self._cancelled else State.SKIPPED)
            return False
        if item.state in (State.DONE, State.FAILED, State.SKIPPED, State.CANCELLED):
            return False
        if item.state is State.PENDING:
            item.state = State.RUNNING
            item.attempts += 1
            self._emit("item_started", item=item)
        return True

    def _phase_ocr(self) -> None:
        """Pass 1: OCR every file strictly sequentially."""
        for item in self.items:
            if not self._begin_item(item):
                continue
            raw = self._run_stage(
                item,
                "ocr",
                # Default-bind `item` — this loop body is the only place the
                # lambda is called (synchronously, this iteration), but the
                # extra `source=` reference tipped B023's per-lambda count
                # over its baseline, and a default arg is the standard fix.
                lambda item=item: run_ocr_step(
                    item.path,
                    self.output_dir,
                    skip_ocr="ocr" not in self.stages,
                    resume=self._resume_enabled,
                    force=self.force,
                    page=item.page,
                    stem=item.output_stem or None,
                    orientation=(item.source or {}).get("orientation", 1),
                    source=item.source or None,
                ),
                chars_key="extracted_text",
            )
            if raw is None:
                self._finish_item(item, State.FAILED)
            else:
                item.results["raw"] = raw
                if raw.get("_page_document") is not None:
                    item.page_document = raw["_page_document"]

    def _phase_cleanup(self) -> None:
        """Pass 2: Cleanup every file strictly sequentially."""
        for item in self.items:
            if not self._begin_item(item):
                continue
            raw = item.results.get("raw")
            if raw is None:
                self._finish_item(item, State.FAILED)
                continue
            cleaned = self._run_stage(
                item,
                "cleanup",
                lambda: run_cleanup_step(
                    raw,
                    item.stem,
                    self.output_dir,
                    skip_cleanup="cleanup" not in self.stages,
                    resume=self._resume_enabled,
                    force=self.force,
                ),
                chars_key="cleaned_text",
            )
            if cleaned is None:
                self._finish_item(item, State.FAILED)
            else:
                item.results["cleaned"] = cleaned
                if cleaned.get("_page_document") is not None:
                    item.page_document = cleaned["_page_document"]

    def _phase_title(self) -> None:
        """Pass 3: Title generation every file strictly sequentially."""
        if "title" not in self.stages:
            return
        for item in self.items:
            if not self._begin_item(item):
                continue
            cleaned = item.results.get("cleaned")
            if cleaned is None:
                self._finish_item(item, State.FAILED)
                continue
            title_result = self._run_stage(
                item,
                "title",
                lambda: run_title_step(
                    cleaned,
                    item.stem,
                    self.output_dir,
                    skip_title="title" not in self.stages,
                    resume=self._resume_enabled,
                    force=self.force,
                ),
                chars_key="title",
            )
            if title_result is None:
                self._finish_item(item, State.FAILED)
            else:
                item.results["title"] = title_result
                item.language = title_result.get("language", item.language)
                if title_result.get("_page_document") is not None:
                    item.page_document = title_result["_page_document"]

    def _phase_translate(self) -> None:
        """Pass 4: Translate every file strictly sequentially."""
        if "translate" not in self.stages:
            return
        for item in self.items:
            if not self._begin_item(item):
                continue
            cleaned = item.results.get("cleaned")
            if cleaned is None:
                self._finish_item(item, State.FAILED)
                continue
            translated = self._run_stage(
                item,
                "translate",
                lambda: run_translate_step(
                    cleaned,
                    item.stem,
                    self.output_dir,
                    resume=self._resume_enabled,
                    force=self.force,
                ),
                chars_key="translated_text",
            )
            if translated is None:
                self._finish_item(item, State.FAILED)
            else:
                item.results["translated"] = translated
                item.language = translated.get("source_language_name", "")
                conf = translated.get("confidence") or {}
                item.confidence = conf.get("overall_score")
                if translated.get("_page_document") is not None:
                    item.page_document = translated["_page_document"]

    # --------------------------------------------------------------- helpers
    @property
    def _resume_enabled(self) -> bool:
        return bool(cfg("resume")) and not self.force

    def _run_stage(self, item: JobItem, stage: str, fn, *, chars_key: str) -> dict | None:
        """Run one stage, updating status and emitting events. None on failure."""
        status = item.stages[stage]
        status.state = State.RUNNING
        self._emit("stage_started", item=item, stage=stage)

        try:
            data = fn()
        except Exception as exc:
            status.state = State.FAILED
            status.error = str(exc)
            item.error = status.error
            log.warning("%s failed for %s: %s", STAGE_LABELS[stage], item.name, exc)
            self._emit(
                "stage_finished",
                item=item,
                stage=stage,
                message=f"[{STAGE_LABELS[stage]}] {item.name} — {status.error}",
                tag="error",
            )
            return None

        status.elapsed = data.get("_elapsed", 0.0)
        status.chars = len(data.get(chars_key) or "")
        skipped = data.get("_skipped", False)
        status.state = State.SKIPPED if skipped else State.DONE
        status.skip_reason = data.get("_skip_reason", "") if skipped else ""
        status.skip_key = data.get("_skip_key", "") if skipped else ""

        if skipped:
            if status.skip_reason == SKIP_ALREADY_EXISTS:
                detail = "already has output"
                if status.skip_key:
                    detail += f" ({status.skip_key})"
            elif status.skip_reason == SKIP_NOT_SELECTED:
                detail = "not selected"
            else:
                detail = "skipped"
            suffix = f" — {detail}"
        else:
            suffix = f" -> {status.chars} chars ({status.elapsed:.1f}s)"
        self._emit(
            "stage_finished",
            item=item,
            stage=stage,
            message=f"[{STAGE_LABELS[stage]}] {item.name}{suffix}",
            tag="warning" if skipped else "success",
        )

        # A guarded cleanup that kept the raw text is a silent no-op unless we
        # say so — the user needs to know the page was left unrepaired.
        guard = data.get("guard") or {}
        if guard.get("ok") is False:
            item.guard_rejected = True
            self._emit(
                "log",
                item=item,
                stage=stage,
                message=f"    guard kept raw text — {'; '.join(guard.get('reasons', []))}",
                tag="warning",
            )
        return data

    def _finish_item(self, item: JobItem, state: State) -> None:
        item.state = state
        for status in item.stages.values():
            if status.state in (State.PENDING, State.RUNNING):
                status.state = State.SKIPPED if state is not State.FAILED else State.PENDING
        self._emit("item_finished", item=item, payload={"state": state.value})
