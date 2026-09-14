# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Release-blocking contracts for the two core OCR paths and Tropy return.

These tests use real SDK clients over loopback HTTP.  Only the external model
and desktop applications are simulated; Artifice's backend selection, image
encoding, pipeline runner, provenance, Tropy client, duplicate check, and note
commit all execute as production code.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from artifice_ocr import _resolution, config
from artifice_ocr.jobs import JobRunner, State
from artifice_ocr.tropy_db import TropyItem, TropyPhoto, items_to_job_items
from artifice_ocr.web.routers import tropy_notes
from artifice_ocr.web.runtime import state
from PIL import Image, ImageDraw


class _ProtocolHandler(BaseHTTPRequestHandler):
    server_version = "ArtificeContract/1"

    def log_message(self, _format, *args):
        return

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        app = self.server.app
        if app["kind"] == "tropy":
            if self.path in ("/", "/project/current/"):
                return self._json(
                    {
                        "project": str(app["project"]),
                        "id": "archive",
                        "version": "contract",
                    }
                )
            if self.path == "/project/current/photos/41":
                return self._json({"id": 41, "item": 7, "notes": app["note_ids"]})
            if self.path.startswith("/project/current/notes/"):
                note_id = int(self.path.rsplit("/", 1)[-1])
                return self._json({"id": note_id, "text": app["notes"].get(note_id, "")})
        self._json({}, 404)

    def do_POST(self):  # noqa: N802
        app = self.server.app
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if app["kind"] == "ollama" and self.path == "/api/chat":
            request = json.loads(raw)
            app["requests"].append(request)
            return self._json(
                {
                    "model": request["model"],
                    "created_at": "2026-09-04T00:00:00Z",
                    "message": {"role": "assistant", "content": "Archive text via Ollama"},
                    "done": True,
                    "done_reason": "stop",
                    "total_duration": 1,
                    "load_duration": 1,
                    "prompt_eval_count": 1,
                    "prompt_eval_duration": 1,
                    "eval_count": 4,
                    "eval_duration": 1,
                }
            )
        if app["kind"] == "lm_studio" and self.path == "/v1/chat/completions":
            request = json.loads(raw)
            app["requests"].append(request)
            return self._json(
                {
                    "id": "chatcmpl-contract",
                    "object": "chat.completion",
                    "created": 1,
                    "model": request["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Archive text via LM Studio",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 4, "total_tokens": 5},
                }
            )
        if app["kind"] == "tropy" and self.path == "/project/current/notes":
            form = parse_qs(raw.decode())
            note_id = 91
            html = form["html"][0]
            text = html.replace("</p><p>", " ").replace("<p>", "").replace("</p>", "")
            app["note_ids"].append(note_id)
            app["notes"][note_id] = text
            app["writes"].append(form)
            return self._json({"id": [note_id]})
        self._json({}, 404)


@contextmanager
def _server(kind: str, **values):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProtocolHandler)
    server.app = {
        "kind": kind,
        "requests": [],
        "note_ids": [],
        "notes": {},
        "writes": [],
        **values,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "Archive.tropy"
    project.mkdir()
    db = project / "project.tpy"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE project (project_id TEXT, name TEXT, created TEXT, base TEXT)")
    con.execute("INSERT INTO project VALUES ('archive', 'Archive', '', 'project')")
    con.commit()
    con.close()
    image = project / "page.png"
    # Real content, not a blank sheet: the near-blank short-circuit in
    # _ocr_single_image must not skip this fixture's OCR call.
    page = Image.new("RGB", (200, 100), "white")
    ImageDraw.Draw(page).text((20, 20), "Archive page with real content", fill="black")
    page.save(image)
    return project, image


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
    config.reset()
    config.load_config()
    config.apply_overrides(
        {
            "history_db": str(tmp_path / "history.db"),
            "resume": False,
            "ocr_repetition_guard": True,
        }
    )
    _resolution.reset()
    state.clear()
    yield
    state.clear()
    _resolution.reset()
    config.reset()


@pytest.mark.parametrize(
    ("backend", "kind", "url_key", "suffix", "expected"),
    [
        ("ollama", "ollama", "ollama_url", "", "Archive text via Ollama"),
        ("lm_studio", "lm_studio", "lm_studio_url", "/v1", "Archive text via LM Studio"),
    ],
)
def test_tropy_page_ocr_and_note_round_trip_over_real_protocols(
    tmp_path, monkeypatch, backend, kind, url_key, suffix, expected
):
    project, image = _project(tmp_path)
    item = TropyItem(
        item_id=7,
        title="Archive page",
        photos=[
            TropyPhoto(
                photo_id=41,
                path=str(image),
                item_id=7,
                page=None,
                mimetype="image/png",
                checksum="sha-contract",
                orientation=1,
                missing=False,
            )
        ],
    )
    jobs = items_to_job_items(
        [item], project_db=project / "project.tpy", output_dir=str(tmp_path / "output")
    )
    state.add_items(jobs)

    with _server(kind) as model_server:
        model_url = f"http://127.0.0.1:{model_server.server_port}{suffix}"
        config.apply_overrides(
            {
                "ocr_backend": backend,
                "ocr_model": "vision-contract",
                url_key: model_url,
            }
        )
        runner = JobRunner(
            jobs,
            str(tmp_path / "output"),
            stages={"ocr"},
            force=True,
            max_workers=1,
        )
        for job in jobs:
            job.reset({"ocr"})
        runner._run()

        assert jobs[0].state is State.DONE
        assert jobs[0].results["raw"]["extracted_text"] == expected
        assert jobs[0].results["raw"]["engine"] == backend
        request = model_server.app["requests"][0]
        if backend == "ollama":
            assert request["messages"][0]["images"]
            assert isinstance(request["messages"][0]["content"], str)
        else:
            assert request["messages"][0]["content"][1]["type"] == "image_url"

    with _server("tropy", project=project) as tropy_server:
        config.apply_overrides({"tropy_api_port": tropy_server.server_port})
        selected_id = str(id(jobs[0]))
        request = tropy_notes.TropyNotesRequest(
            source="queue", item_ids=[selected_id], stage="raw_ocr"
        )
        preview = tropy_notes.tropy_notes_preview(request)
        assert preview["write_count"] == 1
        assert preview["counts"] == {
            "selected": 1,
            "ready": 1,
            "duplicate": 0,
            "empty": 0,
            "foreign": 0,
            "missing_photo": 0,
            "item_mismatch": 0,
            "error": 0,
            "ineligible": 0,
        }
        result = tropy_notes.tropy_notes_commit(
            tropy_notes.TropyNotesCommitRequest(
                source="queue",
                item_ids=[selected_id],
                stage="raw_ocr",
                expected_write_count=1,
            )
        )
        assert result["written"] == 1
        assert tropy_server.app["writes"][0]["photo"] == ["41"]
        assert expected in tropy_server.app["writes"][0]["html"][0]

        duplicate = tropy_notes.tropy_notes_preview(request)
        assert duplicate["write_count"] == 0
        assert duplicate["counts"]["duplicate"] == 1
