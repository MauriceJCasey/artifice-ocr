# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded state-machine fuzzing of the queue/review API."""

import os

import pytest
from artifice_ocr.jobs import JobItem, State
from artifice_ocr.web.runtime import state
from artifice_ocr.web.server import app
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test


class QueueMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.client = TestClient(app)
        state.clear()
        self._replenish()

    def teardown(self):
        self.client.close()
        state.clear()

    @staticmethod
    def _replenish() -> None:
        if state.items:
            return
        items = []
        for number in range(3):
            item = JobItem(
                path=f"/stress/page-{number}.png",
                state=State.DONE,
                results={
                    "raw": {"extracted_text": f"old text {number}"},
                    "cleaned": {"cleaned_text": f"old cleaned {number}"},
                    "translated": {"translated_text": f"translated {number}"},
                },
            )
            items.append(item)
        state.add_items(items)

    def _queue(self) -> list[dict]:
        response = self.client.get("/api/queue")
        assert response.status_code == 200
        return response.json()["items"]

    @rule(before=st.booleans())
    def reorder(self, before):
        items = self._queue()
        if len(items) >= 2:
            response = self.client.post(
                "/api/queue/reorder",
                json={"drag_id": items[0]["id"], "drop_id": items[-1]["id"], "before": before},
            )
            assert response.status_code == 200

    @rule(use_real_id=st.booleans(), fabricated=st.booleans())
    def label_fabrication(self, use_real_id, fabricated):
        items = self._queue()
        item_id = items[0]["id"] if use_real_id and items else "unknown"
        response = self.client.post(
            f"/api/queue/{item_id}/fabricated-result", json={"fabricated": fabricated}
        )
        assert response.status_code == (200 if use_real_id and items else 404)

    @rule(text=st.text(max_size=80))
    def edit_raw_text(self, text):
        items = self._queue()
        if items:
            response = self.client.post(
                f"/api/queue/{items[0]['id']}/raw-text", json={"text": text}
            )
            assert response.status_code == 200
            assert response.json()["raw"] == text

    @rule(find=st.sampled_from(["old", "text", "missing"]), replacement=st.text(max_size=20))
    def batch_replace(self, find, replacement):
        response = self.client.post(
            "/api/queue/batch-replace",
            json={
                "find": find,
                "replace": replacement,
                "stages": ["raw", "cleaned", "translated"],
            },
        )
        assert response.status_code == 200

    @rule(remove_known=st.booleans())
    def remove(self, remove_known):
        items = self._queue()
        ids = [items[0]["id"]] if remove_known and items else ["unknown", "", "-1"]
        response = self.client.post("/api/queue/remove", json={"ids": ids})
        assert response.status_code == 200
        self._replenish()

    @rule()
    def clear_and_recover(self):
        response = self.client.post("/api/queue/clear")
        assert response.status_code == 200
        self._replenish()

    @invariant()
    def queue_is_consistent(self):
        items = self._queue()
        ids = [item["id"] for item in items]
        assert len(ids) == len(set(ids))
        assert all(item["state"] in {member.value for member in State} for item in items)


@pytest.mark.ui_stress
def test_queue_api_state_machine():
    run_state_machine_as_test(
        QueueMachine,
        settings=settings(
            max_examples=int(os.environ.get("ARTIFICE_STATEFUL_EXAMPLES", "50")),
            stateful_step_count=int(os.environ.get("ARTIFICE_STATEFUL_STEPS", "20")),
            derandomize=True,
            database=None,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow],
        ),
    )
