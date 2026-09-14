# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Scale tests for sending large batches of pages to Tropy.

Nothing else in the suite exercises the Tropy Developer API at more than a
handful of items: the live interop test (`test_tropy_live.py`) sends one
photo, `test_tropy_api.py`'s unit tests use one to three, and the
deterministic UI stress harness (`tests/stress/`) seeds four items and never
even reaches a live Tropy backend for its Send-to-Tropy action — there's no
Developer API listening in that harness, so `connect()` fails before the
per-photo loop runs at all. A 944-page batch that hung and appeared to crash
in the field had nowhere to be caught by any of that.

These tests run a synthetic Tropy Developer API stand-in — a real
`ThreadingHTTPServer`, not a mock — at hundreds of photos, the scale that
actually broke, to catch three regression classes directly:

1. Wall-clock blowing up (a return to one connection per HTTP call, or one
   discovery round trip per item during commit, rather than the fix's O(1)
   connection reuse and O(1) discovery).
2. One bad photo discarding every other already-checked result, instead of
   being isolated.
3. The server actually seeing O(1) TCP connections and a bounded number of
   discovery ("/") hits, not O(n) — checked structurally, not inferred from
   timing alone.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from artifice_ocr import config
from artifice_ocr.jobs import JobItem
from artifice_ocr.web.routers import tropy_notes
from artifice_ocr.web.runtime import state

_BATCH_SIZE = 600
_FAILING_PHOTO_IDS = {137, 314, 500}


@pytest.fixture(autouse=True)
def _isolated_state():
    state.clear()
    yield
    state.clear()


class _ScaleTropyHandler(BaseHTTPRequestHandler):
    """Stands in for Tropy's Developer API at real batch scale.

    Counts new TCP connections (`setup()` runs once per accepted connection,
    not per request — HTTP/1.1 keep-alive can carry many requests over one),
    so a regression to "one connection per call" is caught by inspecting the
    server's own count rather than by inferring it from wall-clock time.
    """

    server_version = "ArtificeScale/1"
    # BaseHTTPRequestHandler defaults to HTTP/1.0, which closes the
    # connection after every response — that alone would force a new TCP
    # connection per request regardless of whether the *client* reuses one
    # httpx.Client, making the connection-count test meaningless. Real
    # Tropy's Node HTTP server keeps connections alive; match that.
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        # Without this, Nagle's algorithm on this socket stalls each small
        # keep-alive request/response pair by ~40ms waiting to see if more
        # data will be coalesced — invisible with HTTP/1.0 (each request got
        # its own connection, so the stall never had anything to wait for),
        # but murders throughput the moment keep-alive is on. Real Tropy is a
        # Node HTTP server, and Node sets this by default.
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.server.app["connections"] += 1

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
        if self.path in ("/", "/project/current/"):
            app["discovery_hits"] += 1
            return self._json({"project": str(app["project"]), "id": "archive", "version": "1.0"})
        if self.path.startswith("/project/current/photos/"):
            photo_id = int(self.path.rsplit("/", 1)[-1])
            if photo_id in app["fail_photo_ids"]:
                return self._json({"detail": "synthetic failure"}, 500)
            return self._json({"id": photo_id, "item": 1, "notes": []})
        if self.path.startswith("/project/current/notes/"):
            note_id = int(self.path.rsplit("/", 1)[-1])
            return self._json({"id": note_id, "text": app["notes"].get(note_id, "")})
        self._json({}, 404)

    def do_POST(self):  # noqa: N802
        app = self.server.app
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if self.path == "/project/current/notes":
            form = parse_qs(raw.decode())
            note_id = 1000 + len(app["writes"])
            app["writes"].append(form)
            return self._json({"id": [note_id]})
        self._json({}, 404)


@contextmanager
def _scale_server(project: Path, fail_photo_ids: set[int] = frozenset()):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScaleTropyHandler)
    server.app = {
        "project": project,
        "fail_photo_ids": fail_photo_ids,
        "connections": 0,
        "discovery_hits": 0,
        "notes": {},
        "writes": [],
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "Archive.tropy"
    root.mkdir()
    db = root / "project.tpy"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE project (project_id TEXT, name TEXT, created TEXT, base TEXT)")
    conn.execute("INSERT INTO project VALUES ('archive', 'Archive', '', 'project')")
    conn.commit()
    conn.close()
    return root


def _seed_queue(project: Path, count: int) -> list[JobItem]:
    state.clear()
    items = []
    for photo_id in range(1, count + 1):
        item = JobItem(
            path=str(project / "assets" / f"page-{photo_id}.jpg"),
            language="en",
            source={
                "origin": "tropy-live",
                "photo_id": photo_id,
                "tropy_item_id": 1,
                "tropy_project": str(project / "project.tpy"),
                "item_title": "Archive item",
            },
            results={"cleaned": {"cleaned_text": f"Page {photo_id} transcription"}},
        )
        items.append(item)
    state.add_items(items)
    return items


def test_large_batch_preview_isolates_failures_and_completes_quickly(tmp_path):
    project = _project(tmp_path)
    items = _seed_queue(project, _BATCH_SIZE)

    with _scale_server(project, fail_photo_ids=_FAILING_PHOTO_IDS) as server:
        config.apply_overrides({"tropy_api_port": server.server_port})

        started = time.monotonic()
        request = tropy_notes.TropyNotesRequest(
            source="queue",
            item_ids=[str(id(item)) for item in items],
            stage="cleaned",
            project_path=str(project),
        )
        result = tropy_notes.tropy_notes_preview(request)
        elapsed = time.monotonic() - started

    assert result["blockers"] == []
    assert result["counts"]["error"] == len(_FAILING_PHOTO_IDS)
    assert result["counts"]["ready"] == _BATCH_SIZE - len(_FAILING_PHOTO_IDS)
    assert result["write_count"] == _BATCH_SIZE - len(_FAILING_PHOTO_IDS)
    assert len(result["item_errors"]) == len(_FAILING_PHOTO_IDS)

    # Generous ceiling: a stub server on loopback answers in low-single-digit
    # milliseconds, so this batch should clear in well under a second. This
    # is a regression tripwire for gross O(n)-with-latency behaviour, not a
    # tight performance benchmark — it exists to fail loudly, not to be
    # precise.
    assert elapsed < 15.0, f"preview of {_BATCH_SIZE} items took {elapsed:.1f}s"


def test_large_batch_preview_reuses_a_small_number_of_connections(tmp_path):
    project = _project(tmp_path)
    items = _seed_queue(project, _BATCH_SIZE)

    with _scale_server(project) as server:
        config.apply_overrides({"tropy_api_port": server.server_port})

        request = tropy_notes.TropyNotesRequest(
            source="queue",
            item_ids=[str(id(item)) for item in items],
            stage="cleaned",
            project_path=str(project),
        )
        tropy_notes.tropy_notes_preview(request)
        connections = server.app["connections"]

    # Before the connection-reuse fix, every one of the 600 photo checks
    # opened its own httpx client and therefore its own TCP connection to
    # this server. A handful of connections (keep-alive plus whatever the
    # threading server's accept loop happens to interleave) is the reused-
    # client signature; anywhere near 600 is the regression this catches.
    assert connections <= 5, f"expected a handful of reused connections, saw {connections}"


def test_large_batch_commit_verifies_the_connection_once_not_per_note(tmp_path):
    project = _project(tmp_path)
    commit_size = 200
    items = _seed_queue(project, commit_size)

    with _scale_server(project) as server:
        config.apply_overrides({"tropy_api_port": server.server_port})

        item_ids = [str(id(item)) for item in items]
        preview_request = tropy_notes.TropyNotesRequest(
            source="queue", item_ids=item_ids, stage="cleaned", project_path=str(project)
        )
        preview = tropy_notes.tropy_notes_preview(preview_request)
        assert preview["write_count"] == commit_size

        commit_request = tropy_notes.TropyNotesCommitRequest(
            source="queue",
            item_ids=item_ids,
            stage="cleaned",
            project_path=str(project),
            expected_write_count=commit_size,
        )
        result = tropy_notes.tropy_notes_commit(commit_request)
        discovery_hits = server.app["discovery_hits"]

    assert result["written"] == commit_size
    assert result["status"] == "complete"
    # Each connect() does up to two discovery probes (root, then the named
    # /project/current/ route). This test makes three connect()-equivalent
    # calls: the standalone preview, commit's own internal _preview(), and
    # commit's verify_current() — 6 hits total, not proportional to
    # commit_size. verify_current() used to run once per "ready" note inside
    # the commit loop instead of once before it, which would have added
    # ~2 * commit_size more here.
    assert discovery_hits <= 8, f"expected ~6 discovery hits total, saw {discovery_hits}"
