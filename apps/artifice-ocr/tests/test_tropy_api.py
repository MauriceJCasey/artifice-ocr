# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract tests for official Tropy Developer API note write-back."""

import json
import sqlite3
from pathlib import Path

import pytest
from artifice_ocr import config
from artifice_ocr.jobs import JobItem
from artifice_ocr.tropy_api import (
    TropyAPIClient,
    TropyAPIError,
    TropyConnection,
    candidate_ports,
    connect,
    normalise_note_text,
    note_html,
)
from artifice_ocr.web.runtime import state


def _project(tmp_path: Path, name: str = "Archive.tropy") -> Path:
    root = tmp_path / name
    root.mkdir()
    db = root / "project.tpy"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE project (project_id TEXT, name TEXT, created TEXT, base TEXT)")
    con.execute("INSERT INTO project VALUES ('id', 'Archive', '', 'project')")
    con.commit()
    con.close()
    return root


@pytest.fixture(autouse=True)
def clean_state():
    config.reset()
    config.load_config()
    state.clear()
    yield
    state.clear()
    config.reset()


def test_candidate_ports_prefers_override_and_tropy_state(tmp_path, monkeypatch):
    (tmp_path / "state.json").write_text(json.dumps({"port": 2029}), encoding="utf-8")
    monkeypatch.setattr("artifice_ocr.tropy_api.tropy_config_dir", lambda: tmp_path)
    config.apply_overrides({"tropy_api_port": 3456})
    assert candidate_ports() == [3456, 2029, 2019]


def test_connect_uses_stable_port_and_verifies_project(tmp_path, httpx_mock, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr("artifice_ocr.tropy_api.tropy_config_dir", lambda: tmp_path / "missing")
    httpx_mock.add_response(
        url="http://127.0.0.1:2019/",
        json={"project": str(project), "id": "Archive", "version": "1.17", "status": "ok"},
    )
    httpx_mock.add_response(url="http://127.0.0.1:2019/project/current/", status_code=404)
    connection = connect(project)
    assert connection.port == 2019
    assert connection.project_name == "Archive"
    assert connection.project_prefix == "/project"


def test_connect_uses_named_project_routes_when_available(tmp_path, httpx_mock, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr("artifice_ocr.tropy_api.tropy_config_dir", lambda: tmp_path / "missing")
    httpx_mock.add_response(
        url="http://127.0.0.1:2019/",
        json={"project": str(project), "version": "1.18", "status": "ok"},
    )
    httpx_mock.add_response(
        url="http://127.0.0.1:2019/project/current/",
        json={"project": str(project), "id": "Archive", "version": "1.18", "status": "ok"},
    )
    connection = connect(project)
    assert connection.project_id == "Archive"
    assert connection.project_prefix == "/project/current"


def test_connect_blocks_wrong_open_project(tmp_path, httpx_mock, monkeypatch):
    expected = _project(tmp_path, "Expected.tropy")
    other = _project(tmp_path, "Other.tropy")
    monkeypatch.setattr("artifice_ocr.tropy_api.tropy_config_dir", lambda: tmp_path / "missing")
    for port in (2019, 2029):
        httpx_mock.add_response(
            url=f"http://127.0.0.1:{port}/",
            json={"project": str(other), "id": "Other", "version": "1.17"},
        )
    with pytest.raises(TropyAPIError, match="Other.*Expected"):
        connect(expected)


def test_note_client_uses_current_note_endpoint_and_form_data(tmp_path, httpx_mock):
    project = _project(tmp_path)
    connection = TropyConnection(2019, "Archive", "Archive", project / "project.tpy", "1.17")
    httpx_mock.add_response(
        method="POST", url="http://127.0.0.1:2019/project/current/notes", json={"id": [77]}
    )
    ids = TropyAPIClient(connection).create_note(10, "A < B", "en")
    assert ids == [77]
    request = httpx_mock.get_request()
    assert b"photo=10" in request.content
    assert b"%26lt%3B" in request.content
    assert "/project/import" not in str(request.url)
    assert note_html("A < B") == "<p>A &lt; B</p>"


def test_note_client_uses_stable_note_endpoint(tmp_path, httpx_mock):
    project = _project(tmp_path)
    connection = TropyConnection(
        2019, "current", "Archive", project / "project.tpy", "1.17", "/project"
    )
    httpx_mock.add_response(
        method="POST", url="http://127.0.0.1:2019/project/notes", json={"id": [78]}
    )
    assert TropyAPIClient(connection).create_note(10, "Text", "en") == [78]


def test_note_comparison_matches_tropy_paragraph_flattening():
    assert normalise_note_text("First paragraph.\n\nSecond paragraph.") == normalise_note_text(
        "First paragraph.   Second paragraph."
    )


class _FakeClient:
    duplicate = False
    writes = []

    def __init__(self, connection):
        self.connection = connection

    def photo(self, photo_id):
        return {"id": photo_id, "item": 1, "notes": [90] if self.duplicate else []}

    def note_text(self, note_id):
        return "Clean text"

    def has_identical_note(self, photo, text):
        return self.duplicate and text == "Clean text"

    def verify_current(self):
        return None

    def create_note(self, photo_id, text, language):
        self.writes.append((photo_id, text, language))
        return [100 + len(self.writes)]

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None


def _queue_item(project: Path) -> JobItem:
    return JobItem(
        path=str(project / "assets" / "page.jpg"),
        language="en",
        source={
            "origin": "tropy-live",
            "photo_id": 10,
            "tropy_item_id": 1,
            "tropy_project": str(project / "project.tpy"),
            "item_title": "Letter",
        },
        results={"cleaned": {"cleaned_text": "Clean text"}},
    )


def test_notes_routes_preview_commit_and_skip_duplicate(tmp_path, monkeypatch):
    project = _project(tmp_path)
    connection = TropyConnection(2019, "Archive", "Archive", project / "project.tpy", "1.17")
    monkeypatch.setattr("artifice_ocr.web.routers.tropy_notes.connect", lambda path: connection)
    monkeypatch.setattr("artifice_ocr.web.routers.tropy_notes.TropyAPIClient", _FakeClient)
    _FakeClient.writes = []
    _FakeClient.duplicate = False
    item = _queue_item(project)
    state.add_items([item])
    selected = [str(id(item))]

    from artifice_ocr.web.routers.tropy_notes import (
        TropyNotesCommitRequest,
        TropyNotesRequest,
        tropy_notes_commit,
        tropy_notes_preview,
    )

    preview = tropy_notes_preview(
        TropyNotesRequest(
            source="queue", item_ids=selected, stage="cleaned", project_path=str(project)
        )
    )
    assert preview["write_count"] == 1
    assert preview["project"]["name"] == "Archive"

    commit = tropy_notes_commit(
        TropyNotesCommitRequest(
            source="queue",
            item_ids=selected,
            stage="cleaned",
            project_path=str(project),
            expected_write_count=1,
        )
    )
    assert commit["written"] == 1
    assert _FakeClient.writes == [(10, "Clean text", "en")]

    _FakeClient.duplicate = True
    duplicate = tropy_notes_preview(
        TropyNotesRequest(
            source="queue", item_ids=selected, stage="cleaned", project_path=str(project)
        )
    )
    assert duplicate["write_count"] == 0
    assert duplicate["counts"]["duplicate"] == 1


class _FlakyOnOneClient:
    """Stands in for a Tropy Developer API that trips on exactly one photo.

    Mirrors the failure a large batch is statistically bound to hit: hundreds
    of sequential requests to a real (possibly momentarily overloaded) Tropy
    instance mean *some* single request is likely to fail even when the
    project itself is fine. That single failure must not cost every other
    already-checked item in the batch its result.
    """

    writes: list[tuple[int, str, str]] = []
    failing_photo_id = 20

    def __init__(self, connection):
        self.connection = connection

    def photo(self, photo_id):
        if photo_id == self.failing_photo_id:
            raise TropyAPIError("Tropy could not inspect photo 20")
        return {"id": photo_id, "item": 1, "notes": []}

    def note_text(self, note_id):
        return "Clean text"

    def has_identical_note(self, photo, text):
        return False

    def verify_current(self):
        return None

    def create_note(self, photo_id, text, language):
        self.writes.append((photo_id, text, language))
        return [100 + len(self.writes)]

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None


def test_notes_preview_isolates_one_bad_photo_from_the_rest_of_a_large_batch(tmp_path, monkeypatch):
    project = _project(tmp_path)
    connection = TropyConnection(2019, "Archive", "Archive", project / "project.tpy", "1.17")
    monkeypatch.setattr("artifice_ocr.web.routers.tropy_notes.connect", lambda path: connection)
    monkeypatch.setattr("artifice_ocr.web.routers.tropy_notes.TropyAPIClient", _FlakyOnOneClient)
    _FlakyOnOneClient.writes = []

    items = []
    for photo_id in range(10, 40, 10):  # 10, 20, 30 — 20 is the flaky one
        item = JobItem(
            path=str(project / "assets" / f"page-{photo_id}.jpg"),
            language="en",
            source={
                "origin": "tropy-live",
                "photo_id": photo_id,
                "tropy_item_id": 1,
                "tropy_project": str(project / "project.tpy"),
                "item_title": "Letter",
            },
            results={"cleaned": {"cleaned_text": "Clean text"}},
        )
        items.append(item)
    state.add_items(items)

    from artifice_ocr.web.routers.tropy_notes import TropyNotesRequest, tropy_notes_preview

    data = tropy_notes_preview(
        TropyNotesRequest(
            source="queue",
            item_ids=[str(id(item)) for item in items],
            stage="cleaned",
            project_path=str(project),
        )
    )
    assert data["blockers"] == []
    assert data["counts"]["error"] == 1
    assert data["counts"]["ready"] == 2
    assert data["write_count"] == 2
    assert data["item_errors"] == [
        {"label": "page-20.jpg", "message": "Tropy could not inspect photo 20"}
    ]


def test_notes_route_never_falls_back_to_another_stage(tmp_path, monkeypatch):
    project = _project(tmp_path)
    connection = TropyConnection(2019, "Archive", "Archive", project / "project.tpy", "1.17")
    monkeypatch.setattr("artifice_ocr.web.routers.tropy_notes.connect", lambda path: connection)
    monkeypatch.setattr("artifice_ocr.web.routers.tropy_notes.TropyAPIClient", _FakeClient)
    item = _queue_item(project)
    item.results = {"raw": {"extracted_text": "Raw only"}}
    state.add_items([item])

    from artifice_ocr.web.routers.tropy_notes import TropyNotesRequest, tropy_notes_preview

    data = tropy_notes_preview(
        TropyNotesRequest(
            source="queue",
            item_ids=[str(id(item))],
            stage="cleaned",
            project_path=str(project),
        )
    )
    assert data["write_count"] == 0
    assert data["counts"]["empty"] == 1


def test_notes_route_requires_an_explicit_selection(tmp_path):
    project = _project(tmp_path)
    state.add_items([_queue_item(project)])

    from artifice_ocr.web.routers.tropy_notes import TropyNotesRequest, tropy_notes_preview

    data = tropy_notes_preview(
        TropyNotesRequest(source="queue", stage="cleaned", project_path=str(project))
    )
    assert data["write_count"] == 0
    assert data["blockers"] == ["No results were selected"]


def test_fabricated_queue_result_is_never_written_to_tropy(tmp_path):
    project = _project(tmp_path)
    item = _queue_item(project)
    item.fabricated_result = True
    state.add_items([item])

    from artifice_ocr.web.routers.tropy_notes import TropyNotesRequest, tropy_notes_preview

    data = tropy_notes_preview(
        TropyNotesRequest(
            source="queue",
            item_ids=[str(id(item))],
            stage="cleaned",
            project_path=str(project),
        )
    )
    assert data["write_count"] == 0
    assert data["counts"]["ineligible"] == 1
