# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local SQLite history of completed pipeline runs.

Kept deliberately small: two tables, no ORM, no migrations framework. SQLite
keeps run history searchable and makes aggregate diagnostics over a few
thousand rows effectively instant.
"""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._logging import get_logger
from .config import get as cfg

log = get_logger("history")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started     TEXT NOT NULL,
    finished    TEXT,
    stages      TEXT NOT NULL,
    output_dir  TEXT NOT NULL,
    doc_type    TEXT,
    ocr_model   TEXT,
    cleanup_model   TEXT,
    translate_model TEXT,
    total       INTEGER NOT NULL DEFAULT 0,
    succeeded   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    elapsed     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_items (
    item_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs ON DELETE CASCADE,
    source_file TEXT NOT NULL,
    name        TEXT NOT NULL,
    state       TEXT NOT NULL,
    language    TEXT,
    confidence  INTEGER,
    error       TEXT,
    fabricated_result INTEGER NOT NULL DEFAULT 0,
    fabricated_at TEXT,
    ocr_engine TEXT,
    ocr_model_resolved TEXT,
    stage_json  TEXT NOT NULL,
    raw_text        TEXT,
    cleaned_text    TEXT,
    translated_text TEXT,
    page        INTEGER,
    edited      INTEGER NOT NULL DEFAULT 0,
    edited_at   TEXT,
    photo_id        INTEGER,
    tropy_item_id   INTEGER,
    tropy_item_title TEXT,
    tropy_project_path TEXT,
    created     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_items_run ON run_items (run_id);
"""

# Columns added after the original schema shipped. `CREATE TABLE IF NOT
# EXISTS` only covers brand-new databases, so an existing on-disk
# history.db (the user's real run history) needs these added explicitly —
# additive-only ALTER TABLEs, never a drop/recreate, so past runs survive.
_MIGRATED_COLUMNS = {
    "page": "INTEGER",
    "edited": "INTEGER NOT NULL DEFAULT 0",
    "edited_at": "TEXT",
    "original_raw_text": "TEXT",
    "original_cleaned_text": "TEXT",
    "original_translated_text": "TEXT",
    "photo_id": "INTEGER",
    "tropy_item_id": "INTEGER",
    "tropy_item_title": "TEXT",
    "tropy_project_path": "TEXT",
    "tropy_group": "TEXT",
    "tropy_photo_path": "TEXT",
    "tropy_item_node": "TEXT",
    "fabricated_result": "INTEGER NOT NULL DEFAULT 0",
    "fabricated_at": "TEXT",
    "ocr_engine": "TEXT",
    "ocr_model_resolved": "TEXT",
}

_ITEM_NODE_MAX_BYTES = 256 * 1024  # 256 KB


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(run_items)")}
    for column, decl in _MIGRATED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE run_items ADD COLUMN {column} {decl}")
    conn.commit()


def default_db_path() -> Path:
    """Where history lives. Override with the `history_db` config key."""
    configured = cfg("history_db")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".artifice_ocr" / "history.db"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class HistoryStore:
    """Thread-safe-enough wrapper around the history database.

    All writes happen on the tk main thread (the GUI records from drained
    events), but the lock keeps it honest if that ever changes.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            _migrate(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------------- write
    def start_run(self, *, stages: list[str], output_dir: str, total: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO runs
                   (started, stages, output_dir, doc_type,
                    ocr_model, cleanup_model, translate_model, total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now(),
                    ",".join(stages),
                    output_dir,
                    cfg("document_type"),
                    cfg("ocr_model"),
                    cfg("cleanup_model"),
                    cfg("translate_model"),
                    total,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def record_item(self, run_id: int, item) -> int:
        """Persist one finished :class:`jobs.JobItem`."""
        stage_json = json.dumps(
            {
                name: {
                    "state": s.state.value,
                    "elapsed": round(s.elapsed, 3),
                    "chars": s.chars,
                    "error": s.error,
                }
                for name, s in item.stages.items()
            }
        )
        results = item.results
        raw_result = results.get("raw") or {}
        src = item.source or {}

        # Tropy JSON-LD bridge columns (additive — do not drop old columns)
        tropy_group = src.get("tropy_group")
        tropy_photo_path = src.get("photo_path_rel")
        item_node = src.get("item_node")
        if item_node is not None:
            item_node_json = json.dumps(item_node, ensure_ascii=False)
            if len(item_node_json.encode("utf-8")) > _ITEM_NODE_MAX_BYTES:
                log.warning(
                    "Item node for '%s' exceeds %d KB — storing as non-exportable",
                    item.name,
                    _ITEM_NODE_MAX_BYTES // 1024,
                )
                item_node_json = None
        else:
            item_node_json = None

        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO run_items
                   (run_id, source_file, name, state, language, confidence,
                    error, fabricated_result, fabricated_at, ocr_engine,
                    ocr_model_resolved, stage_json, raw_text, cleaned_text, translated_text,
                    page, photo_id, tropy_item_id, tropy_item_title,
                    tropy_project_path, tropy_group, tropy_photo_path,
                    tropy_item_node, created)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    item.path,
                    item.name,
                    item.state.value,
                    item.language,
                    item.confidence,
                    item.error,
                    int(bool(item.fabricated_result)),
                    _now() if item.fabricated_result else None,
                    raw_result.get("engine"),
                    raw_result.get("model"),
                    stage_json,
                    raw_result.get("extracted_text"),
                    (results.get("cleaned") or {}).get("cleaned_text"),
                    (results.get("translated") or {}).get("translated_text"),
                    item.page,
                    src.get("photo_id"),
                    src.get("tropy_item_id"),
                    src.get("item_title"),
                    src.get("tropy_project"),
                    tropy_group,
                    tropy_photo_path,
                    item_node_json,
                    _now(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def set_fabricated_result(self, item_id: int, value: bool) -> None:
        """Persist a human review label for an invented OCR transcription."""
        with self._lock:
            self._conn.execute(
                "UPDATE run_items SET fabricated_result = ?, fabricated_at = ? WHERE item_id = ?",
                (int(value), _now() if value else None, item_id),
            )
            self._conn.commit()

    def list_fabricated_results(self, limit: int = 200) -> list[sqlite3.Row]:
        """Return flagged examples with model provenance for rule development."""
        with self._lock:
            return self._conn.execute(
                """SELECT i.*, r.started AS run_started, r.doc_type,
                          r.ocr_model AS configured_ocr_model
                   FROM run_items i JOIN runs r ON r.run_id = i.run_id
                   WHERE i.fabricated_result = 1
                   ORDER BY i.fabricated_at DESC, i.item_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def update_raw_text(self, item_id: int, text: str) -> None:
        """Persist a manual correction made from the History pane.

        This rewrites the historical record's `raw_text` rather than the
        on-disk `raw_ocr/` files a live run produced it from — the DB row
        doesn't carry enough to safely reconstruct the original output
        filename for a Tropy page sharing a PDF (no `output_stem` is stored),
        so touching disk here risks writing the correction to the wrong
        file. `edited`/`edited_at` record that this row no longer reflects
        the original OCR pass untouched, same honesty principle as the
        live-queue correction path.
        """
        self._update_stage_text("raw_text", item_id, text)

    def _update_stage_text(self, column: str, item_id: int, text: str) -> None:
        """Internal: update a text column with edited flag.

        On first edit, the original text is preserved in the corresponding
        ``original_{column}`` column so the user can audit what changed.
        """
        with self._lock:
            row = self._conn.execute(
                f"SELECT {column}, original_{column} FROM run_items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                return
            current = row[column] or ""
            stored_original = row[1]  # original_{column}
            # Preserve original on first edit
            if current and current != text and not stored_original:
                self._conn.execute(
                    f"UPDATE run_items SET original_{column} = ? WHERE item_id = ?",
                    (current, item_id),
                )
            self._conn.execute(
                f"UPDATE run_items SET {column} = ?, edited = 1, edited_at = ? WHERE item_id = ?",
                (text, _now(), item_id),
            )
            self._conn.commit()

    def update_cleaned_text(self, item_id: int, text: str) -> None:
        """Persist a manual correction to cleaned text from the History pane."""
        self._update_stage_text("cleaned_text", item_id, text)

    def update_translated_text(self, item_id: int, text: str) -> None:
        """Persist a manual correction to translated text from the History pane."""
        self._update_stage_text("translated_text", item_id, text)

    def finish_run(self, run_id: int, *, succeeded: int, failed: int, elapsed: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished = ?, succeeded = ?, failed = ?, elapsed = ? "
                "WHERE run_id = ?",
                (_now(), succeeded, failed, elapsed, run_id),
            )
            self._conn.commit()

    def delete_run(self, run_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM run_items WHERE run_id = ?", (run_id,))
            self._conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ read
    def list_runs(self, limit: int = 200) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
            ).fetchall()

    def list_items(self, run_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? ORDER BY item_id", (run_id,)
            ).fetchall()

    def get_item(self, item_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM run_items WHERE item_id = ?", (item_id,)
            ).fetchone()

    def search_items(self, term: str, limit: int = 200) -> list[sqlite3.Row]:
        like = f"%{term}%"
        with self._lock:
            return self._conn.execute(
                "SELECT item_id, name, state, language, confidence, fabricated_result "
                "FROM run_items WHERE name LIKE ? OR source_file LIKE ? "
                "ORDER BY item_id DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()

    def fulltext_search(self, query: str, limit: int = 200) -> list[sqlite3.Row]:
        like = f"%{query}%"
        with self._lock:
            return self._conn.execute(
                """SELECT item_id, name, source_file,
                          raw_text, cleaned_text, translated_text
                   FROM run_items
                   WHERE raw_text LIKE ? OR cleaned_text LIKE ? OR translated_text LIKE ?
                   ORDER BY item_id
                   LIMIT ?""",
                (like, like, like, limit),
            ).fetchall()

    # ------------------------------------------------------------ diagnostics
    def stats(self) -> dict[str, Any]:
        """Aggregate run diagnostics retained for internal reporting."""
        with self._lock:
            totals = self._conn.execute(
                """SELECT COUNT(*) AS runs,
                          COALESCE(SUM(total), 0) AS files,
                          COALESCE(SUM(failed), 0) AS failed,
                          COALESCE(SUM(elapsed), 0) AS elapsed
                   FROM runs"""
            ).fetchone()
            confidences = [
                r[0]
                for r in self._conn.execute(
                    "SELECT confidence FROM run_items WHERE confidence IS NOT NULL"
                )
            ]
            stage_rows = self._conn.execute("SELECT stage_json FROM run_items").fetchall()
            recent = self._conn.execute(
                """SELECT run_id, started, total, failed, elapsed
                   FROM runs WHERE finished IS NOT NULL
                   ORDER BY run_id DESC LIMIT 20"""
            ).fetchall()
            by_model = self._conn.execute(
                """SELECT r.translate_model AS model,
                          AVG(i.confidence) AS avg_conf,
                          COUNT(i.item_id)  AS n
                   FROM run_items i JOIN runs r ON r.run_id = i.run_id
                   WHERE i.confidence IS NOT NULL
                   GROUP BY r.translate_model
                   ORDER BY n DESC"""
            ).fetchall()

        # Per-stage throughput, aggregated in Python so the JSON blob stays opaque to SQL.
        stage_totals: dict[str, dict[str, float]] = {}
        for row in stage_rows:
            try:
                stages = json.loads(row["stage_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            for name, s in stages.items():
                if s.get("state") != "done":
                    continue
                acc = stage_totals.setdefault(name, {"chars": 0.0, "elapsed": 0.0, "n": 0})
                acc["chars"] += s.get("chars", 0)
                acc["elapsed"] += s.get("elapsed", 0.0)
                acc["n"] += 1

        return {
            "runs": totals["runs"],
            "files": totals["files"],
            "failed": totals["failed"],
            "elapsed": totals["elapsed"],
            "confidences": confidences,
            "stage_totals": stage_totals,
            "recent": [dict(r) for r in recent],
            "by_model": [dict(r) for r in by_model],
        }
