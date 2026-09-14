# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for HistoryStore — preserved from test_gui.py.

These cover the SQLite-backed history layer. They were co-located in
test_gui.py alongside GUI-specific tests but never imported anything
from the gui/ tree.
"""

import pytest

from artifice_ocr.history import HistoryStore
from artifice_ocr.jobs import JobItem, State


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(tmp_path / "history.db")
    yield s
    s.close()


def _finished_item(confidence=90, state=State.DONE):
    item = JobItem(path="C:/docs/letter.png")
    item.state = state
    item.confidence = confidence
    item.language = "German"
    item.stages["ocr"].state = State.DONE
    item.stages["ocr"].chars = 1200
    item.stages["ocr"].elapsed = 4.0
    item.stages["cleanup"].state = State.DONE
    item.stages["cleanup"].chars = 1150
    item.stages["cleanup"].elapsed = 2.0
    item.results = {
        "raw": {"extracted_text": "raw"},
        "cleaned": {"cleaned_text": "clean"},
        "translated": {"translated_text": "trans"},
    }
    return item


# --------------------------------------------------------------------------- #
# HistoryStore
# --------------------------------------------------------------------------- #


def test_history_round_trip(store):
    run_id = store.start_run(stages=["ocr", "cleanup"], output_dir="out", total=1)
    store.record_item(run_id, _finished_item())
    store.finish_run(run_id, succeeded=1, failed=0, elapsed=6.0)

    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["total"] == 1
    assert runs[0]["succeeded"] == 1
    assert runs[0]["finished"] is not None

    items = store.list_items(run_id)
    assert len(items) == 1
    assert items[0]["name"] == "letter.png"
    assert items[0]["language"] == "German"
    assert items[0]["confidence"] == 90
    assert items[0]["raw_text"] == "raw"
    assert items[0]["translated_text"] == "trans"


def test_fabricated_result_round_trip_records_model_provenance(store):
    run_id = store.start_run(stages=["ocr"], output_dir="out", total=1)
    item = _finished_item()
    item.fabricated_result = True
    item.results["raw"].update({"engine": "ollama", "model": "vision:latest"})

    item_id = store.record_item(run_id, item)
    row = store.get_item(item_id)
    assert row["fabricated_result"] == 1
    assert row["fabricated_at"] is not None
    assert row["ocr_engine"] == "ollama"
    assert row["ocr_model_resolved"] == "vision:latest"
    assert store.list_fabricated_results()[0]["item_id"] == item_id

    store.set_fabricated_result(item_id, False)
    assert store.get_item(item_id)["fabricated_result"] == 0
    assert store.get_item(item_id)["fabricated_at"] is None
    assert store.list_fabricated_results() == []


def test_history_search_and_delete(store):
    run_id = store.start_run(stages=["ocr"], output_dir="out", total=1)
    store.record_item(run_id, _finished_item())
    store.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)

    assert len(store.search_items("letter")) == 1
    assert len(store.search_items("nothing-matches")) == 0

    store.delete_run(run_id)
    assert store.list_runs() == []
    assert store.list_items(run_id) == []


def test_history_stats_aggregates_throughput_and_confidence(store):
    run_id = store.start_run(stages=["ocr", "cleanup"], output_dir="out", total=2)
    store.record_item(run_id, _finished_item(confidence=90))
    store.record_item(run_id, _finished_item(confidence=40))
    store.finish_run(run_id, succeeded=2, failed=0, elapsed=12.0)

    stats = store.stats()
    assert stats["runs"] == 1
    assert stats["files"] == 2
    assert sorted(stats["confidences"]) == [40, 90]
    # 1200 chars over 4s, twice
    assert stats["stage_totals"]["ocr"]["chars"] == 2400
    assert stats["stage_totals"]["ocr"]["elapsed"] == 8.0
    assert stats["stage_totals"]["ocr"]["n"] == 2
    assert len(stats["recent"]) == 1


def test_history_survives_item_with_no_results(store):
    run_id = store.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path="broken.png")
    item.state = State.FAILED
    item.error = "RuntimeError: nope"
    store.record_item(run_id, item)

    rows = store.list_items(run_id)
    assert rows[0]["state"] == "failed"
    assert rows[0]["raw_text"] is None
    assert rows[0]["confidence"] is None


def test_history_records_the_pdf_page_a_tropy_item_came_from(store):
    run_id = store.start_run(stages=["ocr"], output_dir="out", total=1)
    item = _finished_item()
    item.page = 4
    store.record_item(run_id, item)

    row = store.list_items(run_id)[0]
    assert row["page"] == 4
    assert row["edited"] == 0
    assert row["edited_at"] is None


def test_history_update_raw_text_marks_the_row_edited(store):
    run_id = store.start_run(stages=["ocr"], output_dir="out", total=1)
    store.record_item(run_id, _finished_item())
    item_id = store.list_items(run_id)[0]["item_id"]

    store.update_raw_text(item_id, "corrected transcription")

    row = store.get_item(item_id)
    assert row["raw_text"] == "corrected transcription"
    assert row["edited"] == 1
    assert row["edited_at"] is not None
    # only raw_text changed — cleaned/translated are left alone
    assert row["cleaned_text"] == "clean"
    assert row["translated_text"] == "trans"


def test_history_migrates_a_database_missing_the_new_columns(tmp_path):
    """A real on-disk history.db from before this feature shipped has no
    page/edited/edited_at columns — HistoryStore must add them in place
    (never drop/recreate) so past runs survive."""
    import sqlite3

    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL,
            finished TEXT, stages TEXT NOT NULL, output_dir TEXT NOT NULL,
            doc_type TEXT, ocr_model TEXT, cleanup_model TEXT, translate_model TEXT,
            total INTEGER NOT NULL DEFAULT 0, succeeded INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE run_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            source_file TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
            language TEXT, confidence INTEGER, error TEXT, stage_json TEXT NOT NULL,
            raw_text TEXT, cleaned_text TEXT, translated_text TEXT, created TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO runs (started, stages, output_dir, total) VALUES (?, ?, ?, ?)",
        ("2020-01-01T00:00:00", "ocr", "out", 1),
    )
    conn.execute(
        "INSERT INTO run_items "
        "(run_id, source_file, name, state, stage_json, raw_text, created) "
        "VALUES (1, 'a.png', 'a.png', 'done', '{}', 'old raw text', '2020-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    store = HistoryStore(db_path)
    try:
        rows = store.list_items(1)
        assert len(rows) == 1
        assert rows[0]["raw_text"] == "old raw text"
        assert rows[0]["page"] is None
        assert rows[0]["edited"] == 0
        assert rows[0]["edited_at"] is None
        assert rows[0]["fabricated_result"] == 0
        assert rows[0]["fabricated_at"] is None

        store.update_raw_text(rows[0]["item_id"], "fixed")
        assert store.get_item(rows[0]["item_id"])["raw_text"] == "fixed"
    finally:
        store.close()
