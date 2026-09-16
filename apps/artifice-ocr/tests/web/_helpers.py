# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for the OCR web HTTP-surface tests."""


def _seed_history_run(state, *, failed=0):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    run_id = state.history.start_run(stages=["ocr", "cleanup"], output_dir="out", total=1)
    item = JobItem(path="C:/docs/letter.png")
    item.state = JobState.DONE if not failed else JobState.FAILED
    item.confidence = 88
    item.language = "German"
    item.results = {
        "raw": {"extracted_text": "raw text"},
        "cleaned": {"cleaned_text": "cleaned text"},
    }
    state.history.record_item(run_id, item)
    state.history.finish_run(run_id, succeeded=0 if failed else 1, failed=failed, elapsed=4.2)
    return run_id
