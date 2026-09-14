# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the web frontend's HTTP surface.

Scope is deliberately the same as the rest of this test suite: no real model
calls. The SSE stream (`/api/events`) is now covered by bounded-consumption
tests below — each test reads a fixed number of frames and exits, so none can
hang. The underlying event plumbing (`JobRunner` -> `queue.Queue`) also retains
its own coverage in `test_gui.py`.

`server.py` binds `state` at import time via `from .runtime import state`, so
patching `runtime.state` after that import would not reach the endpoints —
they resolve `state` from `server`'s own module globals. The fixture below
patches `runtime.state` directly for that reason.
"""

import json
import os
import queue as _sync_queue
import socket
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn
from artifice_ocr import config
from artifice_ocr.web import runtime, server
from artifice_ocr.web.routers import (
    events as _events_router,
)
from artifice_ocr.web.routers import (
    history as _history_router,
)
from artifice_ocr.web.routers import (
    queue as _queue_router,
)
from artifice_ocr.web.routers import (
    run as _run_router,
)
from artifice_ocr.web.routers import settings as _settings_router
from artifice_ocr.web.runtime import RunState
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock


@pytest.fixture
def client(tmp_path, monkeypatch):
    # config.save_user_settings() always targets ~/.artifice_ocr/settings.json
    # by design (it's a per-user file, not something callers parameterise) —
    # so any test that reaches it must redirect the module constant itself,
    # or it will overwrite the developer's real saved settings.
    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")

    config.reset()
    config.load_config()
    config.apply_overrides({"history_db": str(tmp_path / "history.db")})

    fresh = RunState()
    monkeypatch.setattr(_queue_router, "state", fresh)
    monkeypatch.setattr(_run_router, "state", fresh)
    monkeypatch.setattr(_events_router, "state", fresh)
    monkeypatch.setattr(_history_router, "state", fresh)
    # pdf_export router does NOT import state, only pdf_export_state
    monkeypatch.setattr("artifice_ocr.web.runtime.state", fresh)

    # Reset pdf_export_state so no test inherits a prior run's output_path,
    # status or queued events (see test_pdf_export_download_404_before_compilation
    # which was order-dependent before this reset).
    import queue as _queue_mod

    pstate = runtime.pdf_export_state
    # Wait for any thread from a previous test that didn't clean up
    if pstate.thread is not None and pstate.thread.is_alive():
        pstate.thread.join(timeout=5)
    pstate.status = "idle"
    pstate.error = None
    pstate.output_path = None
    while True:
        try:
            pstate.events.get_nowait()
        except _queue_mod.Empty:
            break

    with TestClient(server.app) as c:
        yield c


# --------------------------------------------------------------------------- #
# static frontend
# --------------------------------------------------------------------------- #


def test_index_serves_the_frontend(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "ArtificeOCR" in res.text
    assert "shell-skip" in res.text
    assert "app-shell" in res.text
    assert 'id="image-viewport"' in res.text
    assert 'id="btn-add-tropy"' in res.text
    assert 'id="btn-send-tropy"' in res.text
    assert "LudwigLang" not in res.text


def test_about_page_serves(client):
    res = client.get("/about")
    assert res.status_code == 200
    assert "About ArtificeOCR" in res.text
    assert "app-shell" in res.text


# --------------------------------------------------------------------------- #
# CORS origins (ARTIFICE_OCR_CORS_ORIGINS)
# --------------------------------------------------------------------------- #
#
# `server.app` builds its CORS middleware from the env var at *import* time,
# so exercising a non-default value means reloading the module after setting
# it. Each test restores `server` to its unmodified state afterwards so later
# tests (including the `client` fixture above, which reads `server.app`)
# see the normal default-origin app.


def test_cors_default_origins_when_env_unset(monkeypatch):
    monkeypatch.delenv("ARTIFICE_OCR_CORS_ORIGINS", raising=False)
    import importlib

    importlib.reload(server)
    try:
        with TestClient(server.app) as c:
            allowed = c.get("/", headers={"Origin": "http://localhost:8765"})
            assert allowed.headers.get("access-control-allow-origin") == "http://localhost:8765"

            rejected = c.get("/", headers={"Origin": "http://evil.example"})
            assert "access-control-allow-origin" not in rejected.headers
    finally:
        importlib.reload(server)


def test_cors_honors_env_override(monkeypatch):
    monkeypatch.setenv(
        "ARTIFICE_OCR_CORS_ORIGINS",
        "http://example.com:9999, http://foo.test:1234",
    )
    import importlib

    importlib.reload(server)
    try:
        with TestClient(server.app) as c:
            allowed = c.get("/", headers={"Origin": "http://example.com:9999"})
            assert allowed.headers.get("access-control-allow-origin") == "http://example.com:9999"

            # The old hardcoded default must no longer be allowed once an
            # override is set — this is not additive.
            rejected = c.get("/", headers={"Origin": "http://localhost:8765"})
            assert "access-control-allow-origin" not in rejected.headers
    finally:
        monkeypatch.undo()
        importlib.reload(server)


def test_static_index_html_is_gone(client):
    res = client.get("/static/index.html")
    assert res.status_code == 404


def test_static_assets_are_mounted(client):
    res = client.get("/static/css/app.css")
    assert res.status_code == 200
    assert "--paper" in res.text  # the actual design tokens, not a stub


# --------------------------------------------------------------------------- #
# SSE event stream
# --------------------------------------------------------------------------- #
# The stream (`/api/events`) is an unbounded while-True generator.  Every test
# in this section consumes a fixed number of frames and then closes the stream
# explicitly — none depends on a timeout to finish.
#
# Starlette's TestClient buffers the entire response body and therefore cannot
# drive an infinite stream — it would hang forever.  Instead we start a real
# uvicorn server in a thread and talk to it with httpx, the same pattern the
# `_wait_for_server` tests use.  No new dependency is required: both uvicorn
# and httpx are already transitive deps of FastAPI/Starlette and are present
# in the lockfile.


def _start_test_server(app, *, host="127.0.0.1", port=0):
    """Start uvicorn in a daemon thread and return the bound address.

    The caller must arrange for the server's process-global state (e.g. the
    ``runtime.state`` singleton) to be set up *before* calling this, since the
    server reads it at request time.
    """
    config = uvicorn.Config(app, host=host, port=port, log_level="error")
    server = uvicorn.Server(config)
    sock = config.bind_socket()
    port = sock.getsockname()[1]
    t = threading.Thread(target=server.run, args=([sock],), daemon=True)
    t.start()
    # Give uvicorn a moment to start accepting.
    time.sleep(0.05)
    return f"http://{host}:{port}"


@pytest.fixture
def events_server(tmp_path, monkeypatch):
    """A live uvicorn server with patched state, for streaming tests.

    Reuses the same config and state setup as the ``client`` fixture so
    that the event stream sees the fresh, isolated ``RunState``.
    """
    import artifice_ocr.config as _config

    monkeypatch.setattr(_config, "_SETTINGS_PATH", tmp_path / "settings.json")
    _config.reset()
    _config.load_config()
    _config.apply_overrides({"history_db": str(tmp_path / "history.db")})

    fresh = RunState()
    monkeypatch.setattr(_events_router, "state", fresh)
    monkeypatch.setattr(_run_router, "state", fresh)
    monkeypatch.setattr(_queue_router, "state", fresh)
    monkeypatch.setattr(_history_router, "state", fresh)
    monkeypatch.setattr("artifice_ocr.web.runtime.state", fresh)

    base = _start_test_server(server.app)
    yield base, fresh
    # State is discarded — no explicit server teardown (daemon threads
    # die with the process).


def test_events_stream_has_correct_headers(events_server):
    """`text/event-stream` content-type and both cache-control headers."""
    base, _ = events_server
    with httpx.Client() as client, client.stream("GET", f"{base}/api/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"


def test_events_no_runner_yields_waiting_message(events_server):
    """With no run in progress the stream yields the `: waiting ...` comment."""
    base, _ = events_server
    with httpx.Client() as client, client.stream("GET", f"{base}/api/events", timeout=5) as resp:
        chunk = next(resp.iter_bytes())
    assert b": waiting for a run to start" in chunk


def test_events_puts_data_frames_on_the_wire(events_server):
    """An event on the runner queue reaches the client as a `data:` frame."""
    base, state = events_server
    from artifice_ocr.jobs import JobEvent, JobRunner

    eq = _sync_queue.Queue()
    fake = JobRunner([], ".", stages={"ocr"}, events=eq)
    event = JobEvent(kind="log", stage="ocr", message="hello", tag="test")
    eq.put(event)

    state.runner = fake

    with httpx.Client() as client, client.stream("GET", f"{base}/api/events", timeout=5) as resp:
        chunk = next(resp.iter_bytes())
    assert b"data:" in chunk
    data_line = [ln for ln in chunk.decode().split("\n") if ln.startswith("data:")][0]
    payload = json.loads(data_line[len("data: ") :])
    assert payload["kind"] == "log"
    assert payload["message"] == "hello"


def test_events_heartbeat_when_queue_is_empty(events_server):
    """Empty queue yields the `: heartbeat` comment after a 1 s block.

    This is the only heartbeat test — it costs ≥1 s of wall time.  Prefer
    putting events *on* the queue in other tests so the poll returns
    immediately.
    """
    base, state = events_server
    from artifice_ocr.jobs import JobRunner

    state.runner = JobRunner([], ".", stages={"ocr"})

    with httpx.Client() as client, client.stream("GET", f"{base}/api/events", timeout=5) as resp:
        chunk = next(resp.iter_bytes())
    assert b": heartbeat" in chunk


def test_events_item_finished_triggers_record_finished_items(events_server):
    """An `item_finished` event calls `state.record_finished_item(event.item)`.

    The side-effect is observable via the history database — after the event
    is drained, DONE items are persisted.
    """
    base, state = events_server
    from artifice_ocr.jobs import JobEvent, JobItem, JobRunner, State

    # Give the state a DONE item and a run record so record_finished_item()
    # has something to persist.
    item = JobItem(path="/fake/doc.png")
    item.state = State.DONE
    state.add_items([item])
    run_id = state.history.start_run(stages=["ocr"], output_dir=".", total=1)
    state.run_id = run_id

    eq = _sync_queue.Queue()
    event = JobEvent(kind="item_finished", stage="ocr", message="done", tag="item", item=item)
    eq.put(event)

    state.runner = JobRunner([], ".", stages={"ocr"}, events=eq)

    with httpx.Client() as client, client.stream("GET", f"{base}/api/events", timeout=5) as resp:
        chunk = next(resp.iter_bytes())

    assert b"data:" in chunk
    data_line = [ln for ln in chunk.decode().split("\n") if ln.startswith("data:")][0]
    payload = json.loads(data_line[len("data: ") :])
    assert payload["kind"] == "item_finished"

    # The item should now be recorded in the history.
    rows = state.history._conn.execute(
        "SELECT name FROM run_items WHERE run_id = ?", (run_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "doc.png"


def test_record_finished_items_does_not_duplicate_across_calls(events_server):
    """`record_finished_item()` must not re-insert an item already recorded.

    Before the O(N^2) fix, one call per `item_finished` event looped over
    every item in the run — so without the `history_item_id` guard, calling
    it after each of N completions re-inserted every already-DONE item every
    time: a triangular-number blow-up (~N^2/2 rows) instead of N. This is
    what bloated a real history.db to 14GB over 37 runs (1.48M rows where a
    few thousand were expected) and made the History tab's run-items query
    effectively hang, leaving "Send to Tropy" permanently disabled. The
    per-item guard below is the direct descendant of that fix.
    """
    _, state = events_server
    from artifice_ocr.jobs import JobItem, State

    items = [JobItem(path=f"/fake/doc{i}.png") for i in range(3)]
    state.add_items(items)
    run_id = state.history.start_run(stages=["ocr"], output_dir=".", total=len(items))
    state.run_id = run_id

    # Simulate three item_finished events arriving one at a time, as they
    # would during a real run: only one more item is DONE each time
    # record_finished_item(item) is called.
    for item in items:
        item.state = State.DONE
        state.record_finished_item(item)

    rows = state.history._conn.execute(
        "SELECT item_id FROM run_items WHERE run_id = ?", (run_id,)
    ).fetchall()
    assert len(rows) == len(items), (
        f"expected {len(items)} rows (one per item), got {len(rows)} — "
        "items are being re-recorded on every call"
    )


def test_events_run_finished_triggers_finish_run(events_server):
    """A `run_finished` event calls `state.finish_run()` with the payload.

    The side-effect clears state.run_id and records the run in history.
    """
    base, state = events_server
    from artifice_ocr.jobs import JobEvent, JobRunner

    # Create a run row so finish_run has something to update.
    run_id = state.history.start_run(stages=["ocr"], output_dir=".", total=1)
    state.run_id = run_id

    eq = _sync_queue.Queue()
    event = JobEvent(
        kind="run_finished", stage="", payload={"done": 3, "failed": 1, "elapsed": 12.5}
    )
    eq.put(event)

    state.runner = JobRunner([], ".", stages={"ocr"}, events=eq)

    with httpx.Client() as client, client.stream("GET", f"{base}/api/events", timeout=5) as resp:
        chunk = next(resp.iter_bytes())

    assert b"data:" in chunk
    payload = json.loads(
        [ln for ln in chunk.decode().split("\n") if ln.startswith("data:")][0][len("data: ") :]
    )
    assert payload["kind"] == "run_finished"
    assert payload["payload"]["done"] == 3

    # run_id must be cleared after finish_run
    assert state.run_id is None

    # The run record in history must reflect the payload
    run = state.history._conn.execute(
        "SELECT succeeded, failed, elapsed FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert run is not None
    assert run[0] == 3  # succeeded
    assert run[1] == 1  # failed
    assert run[2] == 12.5  # elapsed


def test_events_client_disconnect_does_not_leave_state_broken(events_server):
    """Closing the stream does not leave the run in a broken state.

    The generator has no explicit disconnect handler — Starlette cancels it.
    This test asserts the state is still usable afterward.
    """
    base, state = events_server
    from artifice_ocr.jobs import JobRunner

    state.runner = JobRunner([], ".", stages={"ocr"})

    with httpx.Client() as client, client.stream("GET", f"{base}/api/events", timeout=5) as resp:
        # Consume one frame and then exit the context — the stream closes.
        _ = next(resp.iter_bytes())

    # State must still be cleanly usable: no exception on access.
    assert state.runner is not None
    assert state.items == []
    # Heartbeat path works again — the state is intact.
    with httpx.Client() as client2, client2.stream("GET", f"{base}/api/events", timeout=5) as resp:
        chunk = next(resp.iter_bytes())
    assert b": heartbeat" in chunk


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #


def test_empty_queue_on_startup(client):
    res = client.get("/api/queue")
    assert res.status_code == 200
    assert res.json() == {
        "items": [],
        "status": {"running": False, "paused": False, "total": 0, "upload_enabled": True},
    }


def test_add_paths_resolves_supported_extensions_only(client, tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"x")  # unsupported, must be ignored

    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": [str(tmp_path / "a.png"), str(tmp_path / "b.txt")],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["added"] == 1
    assert body["items"][0]["name"] == "a.png"


def test_add_paths_expands_a_folder(client, tmp_path):
    (tmp_path / "one.png").write_bytes(b"x")
    (tmp_path / "two.pdf").write_bytes(b"x")
    (tmp_path / "readme.md").write_bytes(b"x")

    res = client.post("/api/queue/add-paths", json={"paths": [str(tmp_path)]})
    assert res.json()["added"] == 2


def test_add_paths_deduplicates(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")

    first = client.post("/api/queue/add-paths", json={"paths": [str(f)]})
    second = client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    assert first.json()["added"] == 1
    assert second.json()["added"] == 0
    assert len(second.json()["items"]) == 1


def test_remove_items(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    res = client.post("/api/queue/remove", json={"ids": [item_id]})
    assert res.json()["removed"] == 1
    assert res.json()["items"] == []


def test_clear_queue(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    res = client.post("/api/queue/clear")
    assert res.json()["items"] == []
    assert client.get("/api/queue").json()["items"] == []


def test_reorder_moves_item_when_no_run_in_progress(client, tmp_path):
    f1 = tmp_path / "a.png"
    f2 = tmp_path / "b.png"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f1), str(f2)]}).json()
    ids = [item["id"] for item in added["items"]]

    res = client.post(
        "/api/queue/reorder",
        json={"drag_id": ids[1], "drop_id": ids[0], "before": True},
    )
    assert res.status_code == 200
    assert [item["id"] for item in res.json()["items"]] == [ids[1], ids[0]]


# --------------------------------------------------------------------------- #
# queue mutation guardrails while a run is in progress
# --------------------------------------------------------------------------- #
# `remove`/`clear`/`reorder` must 409 while `state.runner.is_running` is True
# (see `RunState.remove`/`clear`/`reorder` in runtime.py). A plain stand-in
# with `is_running = True` is used in place of a real `JobRunner`: a freshly
# constructed runner that was never `.start()`-ed reports `is_running ==
# False`, so racing a real thread to catch one mid-run would be flaky for no
# benefit — this only needs the attribute the guard actually reads.


def test_remove_409s_while_run_in_progress(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    _queue_router.state.runner = types.SimpleNamespace(is_running=True, is_paused=False)
    res = client.post("/api/queue/remove", json={"ids": [item_id]})
    assert res.status_code == 409
    assert "in progress" in res.json()["detail"].lower()
    # The guard must fire before any mutation — item is still there.
    assert len(client.get("/api/queue").json()["items"]) == 1


def test_clear_409s_while_run_in_progress(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    _queue_router.state.runner = types.SimpleNamespace(is_running=True, is_paused=False)
    res = client.post("/api/queue/clear")
    assert res.status_code == 409
    assert "in progress" in res.json()["detail"].lower()
    assert len(client.get("/api/queue").json()["items"]) == 1


def test_reorder_409s_while_run_in_progress(client, tmp_path):
    f1 = tmp_path / "a.png"
    f2 = tmp_path / "b.png"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f1), str(f2)]}).json()
    ids = [item["id"] for item in added["items"]]

    _queue_router.state.runner = types.SimpleNamespace(is_running=True, is_paused=False)
    res = client.post(
        "/api/queue/reorder",
        json={"drag_id": ids[1], "drop_id": ids[0], "before": True},
    )
    assert res.status_code == 409
    assert "in progress" in res.json()["detail"].lower()
    # Order untouched — the guard fired before any mutation.
    assert [item["id"] for item in client.get("/api/queue").json()["items"]] == ids


def test_remove_clear_reorder_still_work_when_runner_not_running(client, tmp_path):
    """The 409 guard must not fire for a runner that exists but has finished
    (or never started) — only a currently-running one blocks mutation."""
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    _queue_router.state.runner = types.SimpleNamespace(is_running=False, is_paused=False)
    res = client.post("/api/queue/remove", json={"ids": [item_id]})
    assert res.status_code == 200
    assert res.json()["removed"] == 1
    assert res.json()["items"] == []


def test_add_paths_still_succeeds_while_run_in_progress(client, tmp_path):
    """Part 2's explicit scope decision: additions are never blocked, even
    while a run is active — see the comment on RunState.remove/clear/reorder
    in runtime.py explaining why."""
    f = tmp_path / "a.png"
    f.write_bytes(b"x")

    _queue_router.state.runner = types.SimpleNamespace(is_running=True, is_paused=False)
    res = client.post("/api/queue/add-paths", json={"paths": [str(f)]})
    assert res.status_code == 200
    assert res.json()["added"] == 1


# --------------------------------------------------------------------------- #
# run control guardrails (no real run is started — no model calls in tests)
# --------------------------------------------------------------------------- #


def test_start_run_rejects_empty_queue(client):
    res = client.post("/api/run/start", json={"stages": ["ocr"]})
    assert res.status_code == 409
    assert "empty" in res.json()["detail"].lower()


def test_start_run_rejects_no_stages(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    res = client.post("/api/run/start", json={"stages": []})
    assert res.status_code == 409
    assert "ocr is required" in res.json()["detail"].lower()


def test_start_run_rejects_postprocessing_without_ocr(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    res = client.post("/api/run/start", json={"stages": ["cleanup", "translate"]})
    assert res.status_code == 409
    assert "ocr is required" in res.json()["detail"].lower()


def test_skip_unknown_item_reports_not_ok(client):
    res = client.post("/api/run/skip", json={"id": "does-not-exist"})
    assert res.json() == {"ok": False}


def test_retry_resets_finished_items(client, tmp_path):
    from artifice_ocr.jobs import State

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    runtime.state.get(item_id).state = State.DONE

    res = client.post("/api/run/retry", json=[item_id])
    assert res.json() == {"ok": True}
    assert runtime.state.get(item_id).state == State.PENDING


def test_pause_resume_cancel_are_no_ops_without_a_run(client):
    # None of these should raise just because nothing is running yet.
    for path in ("/api/run/pause", "/api/run/resume", "/api/run/cancel"):
        assert client.post(path).json() == {"ok": True}


def test_start_run_preflight_failure_returns_409_with_url(client, tmp_path, monkeypatch):
    """A preflight failure (unreachable endpoint) yields a 409 naming the URL.

    Resolution is stubbed to succeed (populating the role cache) so the failure
    provably comes from the preflight check, not from model resolution.
    """
    from artifice_ocr import _resolution
    from artifice_ocr._resolution import _RoleResolution
    from model_harness.discovery import ProbeResult
    from model_harness.resolution import ResolutionSource

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    config.apply_overrides({"ocr_backend": "ollama", "ocr_model": "llava:7b"})

    def fake_resolve(*, stages=None):
        _resolution.reset()
        _resolution._cache["vision"] = _RoleResolution(
            model="llava:7b", backend="ollama", source=ResolutionSource.USER_CHOICE
        )

    monkeypatch.setattr(_resolution, "resolve_models_for_run", fake_resolve)
    monkeypatch.setattr(
        _resolution,
        "probe_endpoint_sync",
        lambda *a, **k: ProbeResult(url="http://localhost:11434", reachable=False, hint="down"),
    )

    res = client.post("/api/run/start", json={"stages": ["ocr"]})
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "http://localhost:11434" in detail
    assert "Cannot reach OCR endpoint" in detail


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_get_config_returns_expected_keys(client):
    res = client.get("/api/config")
    body = res.json()
    assert "cleanup_model" in body
    assert "ollama_think" in body
    assert "ocr_prompt_instruction" in body


def test_set_config_only_persists_whitelisted_keys(client):
    res = client.post(
        "/api/config",
        json={
            "output_dir": "somewhere",
            "not_a_real_setting": "should be dropped",
        },
    )
    assert res.json() == {"ok": True}
    assert config.get("output_dir") == "somewhere"
    assert config.get("not_a_real_setting") is None


def test_set_config_returns_canonical_durable_values(client):
    res = client.post(
        "/api/config",
        json={"ocr_model": "  local-vision  ", "output_dir": "archive-output"},
    )
    assert res.status_code == 200
    durable = client.get("/api/config").json()
    assert durable["ocr_model"] == "local-vision"
    assert durable["output_dir"] == "archive-output"


def test_set_config_does_not_change_runtime_when_persistence_fails(client, monkeypatch):
    before = config.get("cleanup_model")
    monkeypatch.setattr(
        config,
        "save_user_settings",
        lambda _values: (_ for _ in ()).throw(PermissionError("read only")),
    )
    with pytest.raises(PermissionError, match="read only"):
        client.post("/api/config", json={"cleanup_model": "not-durable"})
    assert config.get("cleanup_model") == before


def test_config_reset_discards_overrides(client):
    client.post("/api/config", json={"cleanup_model": "a-custom-model"})
    assert client.get("/api/config").json()["cleanup_model"] == "a-custom-model"

    res = client.post("/api/config/reset")
    assert res.json()["cleanup_model"] == ""
    assert client.get("/api/config").json()["cleanup_model"] == ""


# --------------------------------------------------------------------------- #
# config — endpoint URL validation on save
# --------------------------------------------------------------------------- #


def test_set_config_rejects_link_local_ollama_url(client):
    """A link-local ollama_url is refused at save time when a backend uses it."""
    res = client.post(
        "/api/config", json={"ocr_backend": "ollama", "ollama_url": "http://169.254.169.254/"}
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "link-local" in detail.lower()


def test_set_config_explains_ollama_wildcard_bind_address(client):
    res = client.post(
        "/api/config",
        json={"ocr_backend": "ollama", "ollama_url": "http://0.0.0.0:11434"},
    )
    assert res.status_code == 400
    assert "address Ollama listens on" in res.json()["detail"]
    assert "localhost:11434" in res.json()["detail"]


def test_set_config_rejects_link_local_lm_studio_url(client):
    """A link-local lm_studio_url is refused at save time when a backend uses it."""
    res = client.post(
        "/api/config",
        json={"ocr_backend": "lm_studio", "lm_studio_url": "http://169.254.169.254/v1"},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "link-local" in detail.lower()


def test_set_config_rejects_link_local_api_base_url(client):
    """A link-local api_base_url is refused at save time when a backend uses it."""
    res = client.post(
        "/api/config",
        json={"ocr_backend": "api_key", "api_base_url": "http://169.254.169.254/v1"},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "link-local" in detail.lower()


def test_set_config_allows_loopback_urls(client):
    """Loopback URLs are accepted at save time (no rejection)."""
    res = client.post(
        "/api/config",
        json={
            "ocr_backend": "ollama",
            "cleanup_backend": "lm_studio",
            "translate_backend": "api_key",
            "ollama_url": "http://localhost:11434",
            "lm_studio_url": "http://localhost:1234/v1",
            "api_base_url": "http://localhost:8080/v1",
        },
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_set_config_rejects_public_api_base_url_without_env_var(client, monkeypatch):
    """A public api_base_url is refused at save time without the env var."""
    from artifice_ocr.web.routers import settings as settings_mod
    from model_harness.endpoint_policy import EndpointPolicy

    strict_policy = EndpointPolicy(allow_public=False)
    monkeypatch.setattr(settings_mod, "_endpoint_policy", strict_policy)
    res = client.post(
        "/api/config", json={"ocr_backend": "api_key", "api_base_url": "http://8.8.8.8/v1"}
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "public address" in detail.lower()


def test_set_config_allows_public_api_base_url_with_env_var(client, monkeypatch):
    """A public api_base_url is accepted at save time when the env var is set."""
    from artifice_ocr.web.routers import settings as settings_mod
    from model_harness.endpoint_policy import EndpointPolicy

    permissive_policy = EndpointPolicy(allow_public=True)
    monkeypatch.setattr(settings_mod, "_endpoint_policy", permissive_policy)
    res = client.post(
        "/api/config", json={"ocr_backend": "api_key", "api_base_url": "http://8.8.8.8/v1"}
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_set_config_passes_non_url_fields_through(client):
    """Non-URL config fields are not affected by endpoint validation."""
    res = client.post("/api/config", json={"output_dir": "somewhere"})
    assert res.status_code == 200
    assert config.get("output_dir") == "somewhere"


def test_set_config_trims_model_name_whitespace(client):
    """A model name posted with trailing whitespace is persisted trimmed."""
    res = client.post(
        "/api/config",
        json={"cleanup_model": "aya-expanse:8b-q8_0  "},
    )
    assert res.status_code == 200
    assert config.get("cleanup_model") == "aya-expanse:8b-q8_0"
    assert config.load_user_settings().get("cleanup_model") == "aya-expanse:8b-q8_0"


def test_set_config_normalises_ollama_url(client):
    """``ollama_url`` is stored canonical — whitespace and a trailing ``/v1``
    removed — so a later reader appends exactly one ``/v1``."""
    res = client.post(
        "/api/config",
        json={"ocr_backend": "ollama", "ollama_url": "  http://localhost:11434/v1  "},
    )
    assert res.status_code == 200
    assert config.get("ollama_url") == "http://localhost:11434"
    assert config.load_user_settings().get("ollama_url") == "http://localhost:11434"


# --------------------------------------------------------------------------- #
# config — URL validation only for active backends (pure-Ollama save regression)
# --------------------------------------------------------------------------- #


def test_set_config_pure_ollama_with_default_api_base_url_saves(client, monkeypatch):
    """The exact failure the maintainer hit: all three backends ``ollama``,
    an empty api_key, and api_base_url at its shipped public default must save
    with 200 — not be rejected by the endpoint policy for a field no backend
    is using."""
    from artifice_ocr.web.routers import settings as settings_mod
    from model_harness.endpoint_policy import EndpointPolicy

    # Fail closed deliberately: a public api_base_url must STILL be rejected
    # when api_key is active, so this test proves the save succeeds because the
    # inactive field is skipped, not because the policy was relaxed.
    monkeypatch.setattr(settings_mod, "_endpoint_policy", EndpointPolicy(allow_public=False))

    res = client.post(
        "/api/config",
        json={
            "ocr_backend": "ollama",
            "cleanup_backend": "ollama",
            "translate_backend": "ollama",
            "api_key": "",
            "api_base_url": "https://api.openai.com/v1",
            "ollama_url": "http://localhost:11434",
        },
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_set_config_skips_validation_for_inactive_url_fields(client, monkeypatch):
    """A link-local value in an *inactive* URL field is not validated at save
    time — it is deferred to use-time by the endpoint policy."""
    # Default backends are "auto"; no field maps to "auto", so none is checked.
    res = client.post("/api/config", json={"api_base_url": "http://169.254.169.254/v1"})
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_set_config_redaction_round_trip_preserves_secret(client):
    """GET → form → POST must not overwrite a real key with the placeholder."""
    from artifice_ocr.web.routers.settings import REDACTED_PLACEHOLDER

    client.post("/api/config", json={"api_key": "sk-real-secret"})
    body = client.get("/api/config").json()
    assert body["api_key"] == REDACTED_PLACEHOLDER

    res = client.post("/api/config", json=body)
    assert res.status_code == 200
    assert config.get("api_key") == "sk-real-secret"
    assert config.load_user_settings().get("api_key") == "sk-real-secret"


def test_set_config_redaction_round_trip_preserves_huggingface_token(client):
    """Same round-trip for the Hugging Face token."""
    from artifice_ocr.web.routers.settings import REDACTED_PLACEHOLDER

    client.post("/api/config", json={"huggingface_token": "hf-real-token"})
    body = client.get("/api/config").json()
    assert body["huggingface_token"] == REDACTED_PLACEHOLDER

    res = client.post("/api/config", json=body)
    assert res.status_code == 200
    assert config.get("huggingface_token") == "hf-real-token"
    assert config.load_user_settings().get("huggingface_token") == "hf-real-token"


# --------------------------------------------------------------------------- #
_removed_tropy_bridge = pytest.mark.skip(
    reason="Removed: JSON-LD bridge is no longer a supported Tropy path"
)


@_removed_tropy_bridge
def test_tropy_import_preview_rejects_bad_suffix(client, tmp_path):
    f = tmp_path / "export.txt"
    f.write_text("{}", encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 400


@_removed_tropy_bridge
def test_tropy_import_preview_reports_missing_file(client, tmp_path):
    f = tmp_path / "gone.jsonld"
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 400


@_removed_tropy_bridge
def test_tropy_import_preview_accepts_valid_jsonld(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"}
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    (tmp_path / "a.png").write_bytes(b"x")
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 200
    body = res.json()
    assert body["export_name"] == "export.json"
    assert len(body["items"]) == 1
    assert body["items"][0]["photo_count"] == 1


@_removed_tropy_bridge
def test_tropy_import_preview_accepts_bare_list(client, tmp_path):
    export = [
        {
            "@type": "Item",
            "title": "Test Item",
            "photo": [
                {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"}
            ],
        }
    ]
    f = tmp_path / "export.json"
    (tmp_path / "a.png").write_bytes(b"x")
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 200
    assert len(res.json()["items"]) == 1


@_removed_tropy_bridge
def test_tropy_import_preview_skips_non_item_nodes(client, tmp_path):
    export = {
        "@graph": [
            {"@type": "Template", "name": "Generic"},
            {
                "@type": "Item",
                "title": "Doc 1",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"}
                ],
            },
        ]
    }
    f = tmp_path / "export.json"
    (tmp_path / "a.png").write_bytes(b"x")
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 200
    assert len(res.json()["items"]) == 1


@_removed_tropy_bridge
def test_tropy_import_preview_reports_missing_photos(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "missing.jpg",
                        "checksum": "abc",
                        "mimetype": "image/jpeg",
                    }
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 200
    assert res.json()["items"][0]["missing_count"] == 1


@_removed_tropy_bridge
def test_tropy_import_preview_rejects_absolute_paths(client, tmp_path):
    """``/etc/passwd`` is still a 400 — now rejected by the blocklist.

    The detail string must reflect the actual blocklist rejection, not a
    generic "absolute" message.
    """
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "/etc/passwd", "checksum": "x", "mimetype": "text"},
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 400
    assert "protected" in res.json()["detail"].lower()


@_removed_tropy_bridge
def test_tropy_import_preview_rejects_dotdot_segments(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "../secret", "checksum": "x", "mimetype": "text"},
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 400


@_removed_tropy_bridge
def test_tropy_import_preview_error_message_does_not_leak_paths(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "../secret", "checksum": "x", "mimetype": "text"},
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    detail = res.json()["detail"]
    assert str(tmp_path) not in detail
    assert str(Path.home()) not in detail


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — import add
# --------------------------------------------------------------------------- #


@_removed_tropy_bridge
def test_tropy_import_add_adds_to_queue(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"}
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    (tmp_path / "a.png").write_bytes(b"x")
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post(
        "/api/tropy/import/add",
        json={"path": str(f), "output_dir": str(tmp_path / "out")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["added"] >= 1


@_removed_tropy_bridge
def test_tropy_import_add_reports_missing(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Missing Photos",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "gone.pdf",
                        "checksum": "x",
                        "mimetype": "application/pdf",
                        "page": 0,
                    }
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post(
        "/api/tropy/import/add",
        json={"path": str(f), "output_dir": str(tmp_path / "out")},
    )
    assert res.status_code == 200
    assert "gone.pdf  p.1" in res.json()["missing"]


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — content-based import
# --------------------------------------------------------------------------- #


@_removed_tropy_bridge
def test_tropy_import_preview_via_content(client, tmp_path):
    """Preview via ``content`` field round-trips correctly."""
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Content Item",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            }
        ]
    }
    (tmp_path / "a.png").write_bytes(b"x")
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    # via content (with relative photos, yields nothing)
    text = f.read_text(encoding="utf-8")
    res = client.post(
        "/api/tropy/import/preview",
        json={"content": text, "filename": "export.json"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["export_name"] == "export.json"


@_removed_tropy_bridge
def test_tropy_import_add_via_content_with_groups(client, safe_tmp_path):
    """``import/add`` via ``content`` adds items and respects ``groups``
    filtering — uses absolute paths in the export so content import can
    resolve photos."""
    photo_a = safe_tmp_path / "a.png"
    photo_a.write_bytes(b"x")
    photo_b = safe_tmp_path / "b.png"
    photo_b.write_bytes(b"x")

    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Group A Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": str(photo_a),
                        "checksum": "abc",
                        "mimetype": "image/png",
                    },
                ],
            },
            {
                "@type": "Item",
                "title": "Group B Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": str(photo_b),
                        "checksum": "xyz",
                        "mimetype": "image/png",
                    },
                ],
            },
        ]
    }
    f = safe_tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    # First, import via path to get group IDs (path import resolves everything)
    res_preview = client.post(
        "/api/tropy/import/preview",
        json={"path": str(f)},
    )
    groups = [item["group"] for item in res_preview.json()["items"]]
    assert len(groups) == 2

    # Now import/add via content with only the second group
    text = f.read_text(encoding="utf-8")
    res = client.post(
        "/api/tropy/import/add",
        json={
            "content": text,
            "filename": "export.json",
            "groups": groups[1:],
            "output_dir": str(safe_tmp_path / "out"),
        },
    )
    assert res.status_code == 200
    # Only the second item should be added
    assert res.json()["added"] == 1


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — request validation (422s)
# --------------------------------------------------------------------------- #


@_removed_tropy_bridge
def test_tropy_import_rejects_both_path_and_content(client):
    """Providing both ``path`` and ``content`` → 422."""
    res = client.post(
        "/api/tropy/import/preview",
        json={"path": "/tmp/e.json", "content": "{}"},
    )
    assert res.status_code == 422


@_removed_tropy_bridge
def test_tropy_import_rejects_neither_path_nor_content(client):
    """Providing neither ``path`` nor ``content`` → 422."""
    res = client.post(
        "/api/tropy/import/preview",
        json={},
    )
    assert res.status_code == 422


@_removed_tropy_bridge
def test_tropy_import_rejects_filename_without_content(client):
    """``filename`` is only valid with ``content`` → 422."""
    res = client.post(
        "/api/tropy/import/preview",
        json={"path": "/tmp/e.json", "filename": "e.json"},
    )
    assert res.status_code == 422


@_removed_tropy_bridge
def test_tropy_import_rejects_oversized_content(client, tmp_path):
    """Content exceeding ``MAX_FILE_BYTES`` → 422 from the Pydantic max_length
    constraint."""
    from artifice_ocr.tropy_jsonld import MAX_FILE_BYTES

    # Build a JSON string whose length exceeds MAX_FILE_BYTES
    big_str = "x" * (MAX_FILE_BYTES + 1)
    res = client.post(
        "/api/tropy/import/preview",
        json={"content": big_str},
    )
    assert res.status_code == 422


@_removed_tropy_bridge
def test_tropy_import_rejects_content_byte_limit_exceeded(client, tmp_path):
    """Content whose char count is under ``MAX_FILE_BYTES`` but UTF-8 byte
    count exceeds it → 400 from the loader.

    Uses a direct call to ``load_export_content`` rather than a massive
    HTTP body that can hit TestClient size limits.
    """
    from artifice_ocr.tropy_jsonld import MAX_FILE_BYTES, TropyImportError, load_export_content

    # Each → is a 3-byte UTF-8 character.  Build a string whose char count
    # is under MAX_FILE_BYTES but whose encoded byte count exceeds it.
    chunk_size = MAX_FILE_BYTES // 3 + 10
    big_content = "→" * chunk_size
    assert len(big_content) < MAX_FILE_BYTES, "char count should be under limit"
    assert len(big_content.encode("utf-8")) > MAX_FILE_BYTES, "byte count should exceed limit"

    with pytest.raises(TropyImportError, match="too large"):
        load_export_content(big_content, filename="test.jsonld")


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — export
# --------------------------------------------------------------------------- #


@_removed_tropy_bridge
def test_tropy_export_requires_tropy_origin(client, tmp_path):
    f = tmp_path / "plain.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    res = client.post("/api/tropy/export", json={"stage": "cleaned"})
    assert res.status_code == 409


@_removed_tropy_bridge
def test_tropy_export_produces_jsonld(client, tmp_path):
    # Add an item with tropy-jsonld origin
    from artifice_ocr.jobs import JobItem

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")
    item = JobItem(
        path=str(f),
        label="doc.pdf  p.1",
        source={
            "origin": "tropy-jsonld",
            "tropy_group": "abc:0",
            "item_node": {"@type": "Item", "title": "Doc"},
            "photo_index": 0,
            "photo_path_rel": "doc.pdf",
            "checksum": "abc",
            "mimetype": "application/pdf",
            "item_title": "Doc",
            "orientation": 1,
        },
    )
    item.results = {"cleaned": {"cleaned_text": "Some cleaned text"}}
    runtime.state.add_items([item])

    res = client.post("/api/tropy/export", json={"stage": "cleaned"})
    assert res.status_code == 200
    assert "application/ld+json" in res.headers["content-type"]


@_removed_tropy_bridge
def test_tropy_export_writes_to_disk_when_path_given(client, tmp_path):
    """When a `path` is provided the export writes to disk and returns JSON."""
    from artifice_ocr.jobs import JobItem

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")
    item = JobItem(
        path=str(f),
        label="doc.pdf  p.1",
        source={
            "origin": "tropy-jsonld",
            "tropy_group": "abc:0",
            "item_node": {"@type": "Item", "title": "Doc"},
            "photo_index": 0,
            "photo_path_rel": "doc.pdf",
            "checksum": "abc",
            "mimetype": "application/pdf",
            "item_title": "Doc",
            "orientation": 1,
        },
    )
    item.results = {"cleaned": {"cleaned_text": "Some cleaned text"}}
    runtime.state.add_items([item])

    out = tmp_path / "my-export.jsonld"
    res = client.post(
        "/api/tropy/export",
        json={"stage": "cleaned", "path": str(out)},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["filename"] == "my-export.jsonld"
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    data = json.loads(content)
    assert "generator" in data
    assert "@graph" in data


@_removed_tropy_bridge
def test_tropy_export_history_requires_item_node(client, tmp_path):
    run_id = _seed_history_run(runtime.state)
    rows = runtime.state.history.list_items(run_id)

    # History items seeded by _seed_history_run won't have tropy_item_node,
    # so they should be non-exportable
    if rows:
        item_id = rows[0]["item_id"]
        res = client.post(
            "/api/tropy/export/history", json={"item_ids": [item_id], "stage": "cleaned"}
        )
        assert res.status_code == 409


def test_tropy_writable_items_selects_items_with_photo_id(client):
    """An item carrying a numeric ``photo_id`` is writable back to Tropy."""
    from artifice_ocr.jobs import JobItem

    writable = JobItem(path="with-id.png", source={"photo_id": 11})
    runtime.state.add_items([writable])

    assert runtime.state.tropy_writable_items(None) == [writable]


def test_tropy_writable_items_skips_items_without_photo_id(client):
    """An item with no ``photo_id`` at all is not writable."""
    from artifice_ocr.jobs import JobItem

    plain = JobItem(path="plain.png")
    runtime.state.add_items([plain])

    assert runtime.state.tropy_writable_items(None) == []


def test_tropy_writable_items_skips_explicit_none_photo_id(client):
    """An item whose ``photo_id`` is explicitly ``None`` is not writable."""
    from artifice_ocr.jobs import JobItem

    nulled = JobItem(path="nulled.png", source={"photo_id": None})
    runtime.state.add_items([nulled])

    assert runtime.state.tropy_writable_items(None) == []


def test_tropy_writable_items_is_independent_of_origin(client):
    """Selection keys on ``photo_id``, not ``origin``.

    An item with ``origin == "tropy-jsonld"`` and a ``photo_id`` is still
    selected — the filter is on the id, not the origin.
    """
    from artifice_ocr.jobs import JobItem

    jsonld = JobItem(path="jsonld.png", source={"origin": "tropy-jsonld", "photo_id": 5})
    runtime.state.add_items([jsonld])

    assert runtime.state.tropy_writable_items(None) == [jsonld]


def test_tropy_writable_items_none_item_ids_considers_whole_queue(client):
    """``item_ids=None`` considers the whole queue, not a subset."""
    from artifice_ocr.jobs import JobItem

    a = JobItem(path="a.png", source={"photo_id": 1})
    b = JobItem(path="b.png")
    c = JobItem(path="c.png", source={"photo_id": 2})
    runtime.state.add_items([a, b, c])

    assert runtime.state.tropy_writable_items(None) == [a, c]


# --------------------------------------------------------------------------- #
# preview (in-memory queue item text)
# --------------------------------------------------------------------------- #


def test_preview_missing_item_is_404(client):
    res = client.get("/api/queue/does-not-exist/preview")
    assert res.status_code == 404


def test_preview_returns_text_confidence_and_diff(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    item = runtime.state.get(item_id)
    item.results = {
        "raw": {"extracted_text": "Der Be-\nricht war unvollstandig."},
        "cleaned": {"cleaned_text": "Der Bericht war unvollstandig."},
        "translated": {"translated_text": "The report was incomplete."},
    }
    item.confidence = 91
    item.language = "German"

    res = client.get(f"/api/queue/{item_id}/preview")
    assert res.status_code == 200
    body = res.json()
    assert body["raw"].startswith("Der Be-")
    assert body["cleaned"] == "Der Bericht war unvollstandig."
    assert body["confidence"] == 91
    assert body["confidence_tier"] == "high"
    # a word actually changed between raw and cleaned, so a range exists
    assert body["diff"]["raw_ranges"] or body["diff"]["cleaned_ranges"]


# --------------------------------------------------------------------------- #
# preview: source image (zoom/pan pane) + raw-text correction
# --------------------------------------------------------------------------- #


def test_image_route_404s_for_unknown_item(client):
    res = client.get("/api/queue/does-not-exist/image")
    assert res.status_code == 404


def test_image_route_passes_jpg_through_unchanged(client, tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"\xff\xd8\xff-fake-jpeg-bytes")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    res = client.get(f"/api/queue/{item_id}/image")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content == b"\xff\xd8\xff-fake-jpeg-bytes"


def test_image_route_converts_tiff_to_png(client, tmp_path, monkeypatch):
    # No TIFF writer is available in this environment (Pillow is deliberately
    # not a dependency), so the conversion call itself is mocked rather than
    # exercised against a real TIFF file — the same class of trade-off the
    # rest of this suite makes for real model calls.
    import fitz

    f = tmp_path / "a.tif"
    f.write_bytes(b"not-a-real-tiff")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    class FakePixmap:
        def __init__(self, path):
            assert path == str(f)

        def tobytes(self, fmt):
            assert fmt == "png"
            return b"\x89PNG-fake-bytes"

    monkeypatch.setattr(fitz, "Pixmap", FakePixmap)

    res = client.get(f"/api/queue/{item_id}/image")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content == b"\x89PNG-fake-bytes"


def test_image_route_renders_only_the_pdf_page_item_points_at(client, tmp_path):
    import fitz
    from artifice_ocr.jobs import JobItem

    pdf_path = tmp_path / "doc.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=100)  # page 0: 2:1 landscape
    doc.new_page(width=50, height=150)  # page 1: 1:3 portrait — the one requested
    doc.new_page(width=300, height=100)  # page 2: 3:1 landscape
    doc.save(str(pdf_path))
    doc.close()

    item = JobItem(path=str(pdf_path), page=1)
    runtime.state.add_items([item])
    item_id = runtime.state.queue_snapshot()[-1]["id"]

    res = client.get(f"/api/queue/{item_id}/image")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"

    rendered = fitz.Pixmap(res.content)
    # Page 1's aspect ratio (tall) is distinct from both its neighbours
    # (wide) — this would fail if page 0 or page 2 were rendered instead.
    assert rendered.height > rendered.width
    assert rendered.width not in (200 * 300 // 72, 300 * 300 // 72)


def test_image_route_caps_an_oversized_pdf_page(client, tmp_path):
    import fitz
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.web.runtime import IMAGE_MAX_LONG_EDGE

    pdf_path = tmp_path / "huge.pdf"
    doc = fitz.open()
    doc.new_page(width=2000, height=1000)  # long edge at 300dpi would be ~8333px
    doc.save(str(pdf_path))
    doc.close()

    item = JobItem(path=str(pdf_path), page=0)
    runtime.state.add_items([item])
    item_id = runtime.state.queue_snapshot()[-1]["id"]

    res = client.get(f"/api/queue/{item_id}/image")
    rendered = fitz.Pixmap(res.content)
    assert max(rendered.width, rendered.height) <= IMAGE_MAX_LONG_EDGE


def test_raw_text_route_404s_for_unknown_item(client):
    res = client.post("/api/queue/does-not-exist/raw-text", json={"text": "x"})
    assert res.status_code == 404


def test_raw_text_save_updates_in_memory_only_when_no_output_exists(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    item = runtime.state.get(item_id)
    item.results = {"raw": {"extracted_text": "origianl typo"}}

    res = client.post(f"/api/queue/{item_id}/raw-text", json={"text": "original corrected"})
    assert res.status_code == 200
    body = res.json()
    assert body["raw"] == "original corrected"
    assert item.results["raw"]["extracted_text"] == "original corrected"
    # nothing on disk to touch — no output dir was ever created for this stem
    assert not (tmp_path / "raw_ocr").exists()


def test_queue_fabricated_result_persists_to_ocr_metadata(client, tmp_path):
    output_dir = tmp_path / "output"
    json_dir = output_dir / "raw_ocr" / "json"
    json_dir.mkdir(parents=True)
    source = tmp_path / "page.png"
    source.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(source)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    item.results = {"raw": {"extracted_text": "invented prose"}}
    record_path = json_dir / f"{item.stem}.json"
    record_path.write_text('{"engine":"ollama","model":"vision"}', encoding="utf-8")
    config.apply_overrides({"output_dir": str(output_dir)})

    response = client.post(f"/api/queue/{item_id}/fabricated-result", json={"fabricated": True})
    assert response.status_code == 200
    assert response.json()["fabricated_result"] is True
    assert runtime.state.get(item_id).fabricated_result is True
    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["fabricated_result"] is True
    assert saved["fabricated_reviewed_at"]


def test_queue_fabricated_result_404s_for_unknown_item(client):
    response = client.post("/api/queue/does-not-exist/fabricated-result", json={"fabricated": True})
    assert response.status_code == 404


@pytest.mark.parametrize("invalid_json", ["{truncated", "[]"])
def test_queue_fabricated_result_survives_invalid_ocr_metadata(client, tmp_path, invalid_json):
    output_dir = tmp_path / "output"
    json_dir = output_dir / "raw_ocr" / "json"
    json_dir.mkdir(parents=True)
    source = tmp_path / "page.png"
    source.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(source)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    record_path = json_dir / f"{item.stem}.json"
    record_path.write_text(invalid_json, encoding="utf-8")
    config.apply_overrides({"output_dir": str(output_dir)})

    response = client.post(f"/api/queue/{item_id}/fabricated-result", json={"fabricated": True})
    assert response.status_code == 200
    assert response.json()["fabricated_result"] is True
    assert record_path.read_text(encoding="utf-8") == invalid_json


def test_raw_text_save_overwrites_disk_output_preserving_other_provenance(client, tmp_path):
    import json as jsonlib

    output_dir = tmp_path / "output"
    text_dir = output_dir / "raw_ocr" / "text"
    json_dir = output_dir / "raw_ocr" / "json"
    text_dir.mkdir(parents=True)
    json_dir.mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    item.results = {"raw": {"extracted_text": "garbld txt"}}

    (text_dir / f"{item.stem}.txt").write_text("garbld txt", encoding="utf-8")
    original_json = {
        "source_file": str(f),
        "stage": "raw_ocr",
        "extracted_text": "garbld txt",
        "engine": "lm-studio",
        "model": "some-vision-model",
        "ocr_prompt": "OCR: Extract all visible text...",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "page": 1,
        "total_pages": 1,
    }
    (json_dir / f"{item.stem}.json").write_text(jsonlib.dumps(original_json), encoding="utf-8")

    config.apply_overrides({"output_dir": str(output_dir)})

    res = client.post(f"/api/queue/{item_id}/raw-text", json={"text": "corrected text"})
    assert res.status_code == 200

    assert (text_dir / f"{item.stem}.txt").read_text(encoding="utf-8") == "corrected text"

    saved = jsonlib.loads((json_dir / f"{item.stem}.json").read_text(encoding="utf-8"))
    assert saved["extracted_text"] == "corrected text"
    assert saved["edited"] is True
    assert "edited_at" in saved
    # everything about the *original* OCR pass is untouched
    for key in ("engine", "model", "ocr_prompt", "timestamp", "source_file", "page", "total_pages"):
        assert saved[key] == original_json[key]


def test_raw_text_save_never_touches_cleaned_or_translated_dirs(client, tmp_path):
    output_dir = tmp_path / "output"
    (output_dir / "raw_ocr" / "text").mkdir(parents=True)
    (output_dir / "raw_ocr" / "json").mkdir(parents=True)
    (output_dir / "cleaned" / "text").mkdir(parents=True)
    (output_dir / "translated" / "text").mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)

    (output_dir / "raw_ocr" / "text" / f"{item.stem}.txt").write_text("orig", encoding="utf-8")
    (output_dir / "raw_ocr" / "json" / f"{item.stem}.json").write_text(
        '{"extracted_text": "orig"}', encoding="utf-8"
    )
    config.apply_overrides({"output_dir": str(output_dir)})

    client.post(f"/api/queue/{item_id}/raw-text", json={"text": "edited"})

    assert list((output_dir / "cleaned" / "text").iterdir()) == []
    assert list((output_dir / "translated" / "text").iterdir()) == []


# --------------------------------------------------------------------------- #
# cleaned-text + translated-text
# --------------------------------------------------------------------------- #


def test_cleaned_text_404s_for_unknown_queue_item(client):
    res = client.post("/api/queue/does-not-exist/cleaned-text", json={"text": "x"})
    assert res.status_code == 404


def test_translated_text_404s_for_unknown_queue_item(client):
    res = client.post("/api/queue/does-not-exist/translated-text", json={"text": "x"})
    assert res.status_code == 404


def test_cleaned_text_save_updates_in_memory_when_no_output_exists(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    item = runtime.state.get(item_id)
    item.results = {"cleaned": {"cleaned_text": "garbld cln"}}

    res = client.post(f"/api/queue/{item_id}/cleaned-text", json={"text": "corrected clean"})
    assert res.status_code == 200
    body = res.json()
    assert body["cleaned"] == "corrected clean"
    assert item.results["cleaned"]["cleaned_text"] == "corrected clean"


def test_translated_text_save_updates_in_memory_when_no_output_exists(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    item = runtime.state.get(item_id)
    item.results = {"translated": {"translated_text": "garbld trn"}}

    res = client.post(
        f"/api/queue/{item_id}/translated-text", json={"text": "corrected translation"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["translated"] == "corrected translation"
    assert item.results["translated"]["translated_text"] == "corrected translation"


def test_cleaned_text_save_overwrites_disk_output_preserving_provenance(client, tmp_path):
    import json as jsonlib

    output_dir = tmp_path / "output"
    text_dir = output_dir / "cleaned" / "text"
    json_dir = output_dir / "cleaned" / "json"
    text_dir.mkdir(parents=True)
    json_dir.mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    item.results = {"cleaned": {"cleaned_text": "garbld cln"}}

    (text_dir / f"{item.stem}.txt").write_text("garbld cln", encoding="utf-8")
    original_json = {
        "source_file": str(f),
        "stage": "cleaned",
        "cleaned_text": "garbld cln",
        "raw_text": "raw ocr text",
        "engine": "ollama",
        "model": "some-cleanup-model",
        "system_prompt": "Clean up the text...",
        "document_type": "default",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    (json_dir / f"{item.stem}.json").write_text(jsonlib.dumps(original_json), encoding="utf-8")

    config.apply_overrides({"output_dir": str(output_dir)})

    res = client.post(f"/api/queue/{item_id}/cleaned-text", json={"text": "corrected clean"})
    assert res.status_code == 200

    assert (text_dir / f"{item.stem}.txt").read_text(encoding="utf-8") == "corrected clean"

    saved = jsonlib.loads((json_dir / f"{item.stem}.json").read_text(encoding="utf-8"))
    assert saved["cleaned_text"] == "corrected clean"
    assert saved["edited"] is True
    assert "edited_at" in saved
    for key in ("engine", "model", "system_prompt", "timestamp", "source_file"):
        assert saved[key] == original_json[key]


def test_translated_text_save_overwrites_disk_output_preserving_provenance(client, tmp_path):
    import json as jsonlib

    output_dir = tmp_path / "output"
    text_dir = output_dir / "translated" / "text"
    json_dir = output_dir / "translated" / "json"
    text_dir.mkdir(parents=True)
    json_dir.mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    item.results = {"translated": {"translated_text": "garbld trn"}}

    (text_dir / f"{item.stem}.txt").write_text("garbld trn", encoding="utf-8")
    original_json = {
        "source_file": str(f),
        "stage": "translated",
        "translated_text": "garbld trn",
        "cleaned_text": "cleaned text",
        "source_language": "fr",
        "source_language_name": "French",
        "engine": "ollama",
        "model": "some-translate-model",
        "system_prompt": "Translate the text...",
        "document_type": "default",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    (json_dir / f"{item.stem}.json").write_text(jsonlib.dumps(original_json), encoding="utf-8")

    config.apply_overrides({"output_dir": str(output_dir)})

    res = client.post(
        f"/api/queue/{item_id}/translated-text", json={"text": "corrected translation"}
    )
    assert res.status_code == 200

    assert (text_dir / f"{item.stem}.txt").read_text(encoding="utf-8") == "corrected translation"

    saved = jsonlib.loads((json_dir / f"{item.stem}.json").read_text(encoding="utf-8"))
    assert saved["translated_text"] == "corrected translation"
    assert saved["edited"] is True
    assert "edited_at" in saved
    for key in ("engine", "model", "system_prompt", "timestamp", "source_file"):
        assert saved[key] == original_json[key]


def test_cleaned_text_save_never_touches_raw_or_translated_dirs(client, tmp_path):
    output_dir = tmp_path / "output"
    (output_dir / "cleaned" / "text").mkdir(parents=True)
    (output_dir / "cleaned" / "json").mkdir(parents=True)
    (output_dir / "raw_ocr" / "text").mkdir(parents=True)
    (output_dir / "translated" / "text").mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)

    (output_dir / "cleaned" / "text" / f"{item.stem}.txt").write_text("orig", encoding="utf-8")
    (output_dir / "cleaned" / "json" / f"{item.stem}.json").write_text(
        '{"cleaned_text": "orig"}', encoding="utf-8"
    )
    config.apply_overrides({"output_dir": str(output_dir)})

    client.post(f"/api/queue/{item_id}/cleaned-text", json={"text": "edited"})

    assert list((output_dir / "raw_ocr" / "text").iterdir()) == []
    assert list((output_dir / "translated" / "text").iterdir()) == []


def test_translated_text_save_never_touches_raw_or_cleaned_dirs(client, tmp_path):
    output_dir = tmp_path / "output"
    (output_dir / "translated" / "text").mkdir(parents=True)
    (output_dir / "translated" / "json").mkdir(parents=True)
    (output_dir / "raw_ocr" / "text").mkdir(parents=True)
    (output_dir / "cleaned" / "text").mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)

    (output_dir / "translated" / "text" / f"{item.stem}.txt").write_text("orig", encoding="utf-8")
    (output_dir / "translated" / "json" / f"{item.stem}.json").write_text(
        '{"translated_text": "orig"}', encoding="utf-8"
    )
    config.apply_overrides({"output_dir": str(output_dir)})

    client.post(f"/api/queue/{item_id}/translated-text", json={"text": "edited"})

    assert list((output_dir / "raw_ocr" / "text").iterdir()) == []
    assert list((output_dir / "cleaned" / "text").iterdir()) == []


# --------------------------------------------------------------------------- #
# settings: document types + health
# --------------------------------------------------------------------------- #


def test_document_types_lists_known_types(client):
    res = client.get("/api/document-types")
    types = res.json()["types"]
    assert "default" in types
    assert "handwritten" in types


def test_local_models_discovers_lm_studio_on_wsl_host(client, monkeypatch):
    from model_harness.discovery import ProbeResult

    calls = []

    async def fake_probe(url, **_kwargs):
        calls.append(url)
        return ProbeResult(
            url=url,
            reachable=url == "http://172.21.176.1:1234/v1",
            provider="lm-studio",
            models=("allenai/olmocr-2-7b",),
        )

    monkeypatch.setattr(_settings_router, "_wsl_host", lambda: "172.21.176.1")
    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/local-models?backend=lm_studio")

    assert res.status_code == 200
    assert res.json() == {
        "ok": True,
        "backend": "lm_studio",
        "url": "http://172.21.176.1:1234/v1",
        "models": ["allenai/olmocr-2-7b"],
    }
    assert "http://localhost:1234/v1" in calls
    assert "http://172.21.176.1:1234/v1" in calls


def test_local_models_normalises_ollama_address_and_rejects_wrong_provider(client, monkeypatch):
    from model_harness.discovery import ProbeResult

    calls = []

    async def fake_probe(url, **_kwargs):
        calls.append(url)
        return ProbeResult(
            url=url,
            reachable=True,
            provider="lm-studio",
            models=("some-model",),
        )

    monkeypatch.setattr(_settings_router, "_wsl_host", lambda: None)
    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/local-models?backend=ollama&url=http://localhost:11434/v1/")

    assert res.status_code == 200
    assert res.json()["ok"] is False
    assert calls == ["http://localhost:11434"]


def test_local_models_normalises_lm_studio_api_path():
    assert (
        _settings_router._canonical_local_url(
            "lm_studio", "http://localhost:1234/v1/models?source=settings#models"
        )
        == "http://localhost:1234/v1"
    )


def test_local_models_does_not_echo_rejected_address(client, monkeypatch):
    monkeypatch.setattr(
        _settings_router,
        "_local_endpoint_candidates",
        lambda *_args: ["https://models.example.com/v1"],
    )
    monkeypatch.setattr(
        _settings_router._endpoint_policy,
        "validate_url",
        lambda _url: (_ for _ in ()).throw(_settings_router.EndpointRejected("public endpoint")),
    )

    res = client.get("/api/local-models?backend=lm_studio")

    assert res.status_code == 200
    assert res.json()["url"] == ""
    assert res.json()["detail"] == "No permitted local endpoint address was provided."


def test_local_models_rejects_non_local_backend(client):
    res = client.get("/api/local-models?backend=api_key")
    assert res.status_code == 400


def test_health_check_reports_service_status(client, monkeypatch):
    # Use the config model names so the per-model health check matches
    from artifice_ocr import config as ocr_config
    from model_harness.discovery import ProbeResult

    cleanup_model = ocr_config.get("cleanup_model") or "llama3.2:3b"
    translate_model = ocr_config.get("translate_model") or "llama3.2:3b"
    ocr_model = ocr_config.get("ocr_model") or "llama3.2-vision:11b"

    ok_result = ProbeResult(
        url="http://localhost:11434",
        reachable=True,
        models=(cleanup_model, translate_model, ocr_model),
    )

    async def fake_probe(*a, **k):
        return ok_result

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    assert body["lm_studio"]["ok"] is True
    assert body["ollama"]["ok"] is True
    assert all(m["ok"] for m in body["models"])


def test_health_check_surfaces_unreachable_services(client, monkeypatch):
    from model_harness.discovery import ProbeResult

    fail_result = ProbeResult(
        url="http://localhost:11434",
        reachable=False,
        hint="Cannot reach Ollama",
    )

    async def fake_probe(*a, **k):
        return fail_result

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    assert body["lm_studio"]["ok"] is False
    assert body["ollama"]["ok"] is False
    assert all(not m["ok"] for m in body["models"])


def test_health_check_real_probe_returns_from_threadpool(client, httpx_mock: HTTPXMock):
    """GET /api/health exercises the real probe_endpoint against mocked HTTP.

    The route is `async def` and awaits `probe_endpoint` directly — LM Studio
    and Ollama concurrently via `asyncio.gather` rather than sequentially, so
    two 5-10s timeouts are never paid back-to-back when neither answers.
    Other tests mock `probe_endpoint` itself; this one mocks the HTTP layer
    underneath it so the full async path actually runs. The submit-to-a-
    thread-with-a-timeout shape predates the route's move to `async def`
    (it used to matter for the sync-wrapping-async bridge); kept as a cheap
    belt-and-suspenders regression guard against a future change reintroducing
    a blocking call that hangs the route.
    """
    # Default config probes LM Studio (port 1234) and Ollama (port 11434).
    # Align the Ollama model names with the config so the per-model checks pass.
    config.apply_overrides(
        {
            "ocr_model": "ollama-ocr",
            "cleanup_model": "ollama-cleanup",
            "translate_model": "ollama-translate",
        }
    )
    httpx_mock.add_response(
        url="http://localhost:1234/api/tags",
        json={"models": []},
    )
    httpx_mock.add_response(
        url="http://localhost:1234/v1/models",
        json={"data": [{"id": "lm-studio-model"}]},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/api/tags",
        json={
            "models": [
                {"name": "ollama-ocr"},
                {"name": "ollama-cleanup"},
                {"name": "ollama-translate"},
            ]
        },
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/models",
        json={"data": []},
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.get, "/api/health")
        res = future.result(timeout=15)

    assert res.status_code == 200
    body = res.json()
    # Key set consumed by ocr/web/static/js/settings.js
    assert set(body.keys()) == {"lm_studio", "ollama", "models"}
    assert set(body["lm_studio"].keys()) == {"ok", "detail", "url", "models"}
    assert set(body["ollama"].keys()) == {"ok", "detail", "url", "models"}
    assert body["lm_studio"]["models"] == ["lm-studio-model"]
    assert body["ollama"]["models"] == ["ollama-ocr", "ollama-cleanup", "ollama-translate"]
    assert body["lm_studio"]["ok"] is True
    assert body["ollama"]["ok"] is True
    assert all(m["ok"] for m in body["models"])


def test_health_check_real_probe_unreachable_shape(client, httpx_mock: HTTPXMock):
    """Failure path of /api/health keeps the same key set the JS reads."""
    httpx_mock.add_exception(
        httpx.ConnectError("Connection refused"),
        url="http://localhost:1234/api/tags",
    )
    httpx_mock.add_exception(
        httpx.ConnectError("Connection refused"),
        url="http://localhost:11434/api/tags",
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.get, "/api/health")
        res = future.result(timeout=15)

    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"lm_studio", "ollama", "models"}
    assert set(body["lm_studio"].keys()) == {"ok", "detail", "url", "models"}
    assert set(body["ollama"].keys()) == {"ok", "detail", "url", "models"}
    assert body["lm_studio"]["models"] == []
    assert body["ollama"]["models"] == []
    assert body["lm_studio"]["ok"] is False
    assert body["ollama"]["ok"] is False
    assert body["lm_studio"]["detail"] is not None
    assert body["ollama"]["detail"] is not None


def test_health_check_checks_model_against_its_own_configured_backend(client, monkeypatch):
    """A role's model must be graded against the model list of *its own*
    configured backend, not Ollama's -- the user's report was "it pings LM
    Studio, but it does not load the model": the OCR role was on lm_studio,
    but the old code always compared every model against the Ollama probe.
    """
    from model_harness.discovery import ProbeResult

    config.apply_overrides(
        {
            "ocr_backend": "lm_studio",
            "ocr_model": "allenai/olmocr-2-7b",
            "cleanup_backend": "ollama",
            "cleanup_model": "",
            "translate_backend": "ollama",
            "translate_model": "",
        }
    )

    async def fake_probe(url, *a, **k):
        if "1234" in url:
            return ProbeResult(url=url, reachable=True, models=("allenai/olmocr-2-7b",))
        return ProbeResult(url=url, reachable=True, models=("llama3.2:3b",))

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    models = {m["name"]: m for m in body["models"]}
    assert models["allenai/olmocr-2-7b"]["ok"] is True
    assert models["allenai/olmocr-2-7b"]["backend"] == "lm_studio"


def test_health_check_reports_model_missing_from_its_configured_backend(client, monkeypatch):
    """Mirror of the above: a model installed on neither server is reported
    not-OK, still graded against its own role's backend."""
    from model_harness.discovery import ProbeResult

    config.apply_overrides(
        {
            "ocr_backend": "lm_studio",
            "ocr_model": "not-installed-anywhere",
            "cleanup_backend": "ollama",
            "cleanup_model": "",
            "translate_backend": "ollama",
            "translate_model": "",
        }
    )

    async def fake_probe(url, *a, **k):
        return ProbeResult(url=url, reachable=True, models=("some-other-model",))

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    models = {m["name"]: m for m in body["models"]}
    assert models["not-installed-anywhere"]["ok"] is False
    assert models["not-installed-anywhere"]["backend"] == "lm_studio"


def test_health_check_all_ollama_backends_is_unchanged(client, monkeypatch):
    """Regression guard: with every role on ``ollama`` (the pre-fix common
    case), behaviour must be unchanged -- each configured model graded
    against the single Ollama probe."""
    from model_harness.discovery import ProbeResult

    config.apply_overrides(
        {
            "ocr_backend": "ollama",
            "cleanup_backend": "ollama",
            "translate_backend": "ollama",
            "ocr_model": "llama3.2-vision:11b",
            "cleanup_model": "llama3.2:3b",
            "translate_model": "llama3.2:3b",
        }
    )

    async def fake_probe(url, *a, **k):
        return ProbeResult(
            url=url,
            reachable=True,
            models=("llama3.2-vision:11b", "llama3.2:3b"),
        )

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    assert len(body["models"]) == 3
    assert all(m["ok"] for m in body["models"])
    assert all(m["backend"] == "ollama" for m in body["models"])


def test_health_check_cloud_backend_role_not_a_false_negative(client, monkeypatch):
    """A role on a cloud backend (api_key / huggingface) has no local model
    list to check against, so it must never be reported as a plain ``ok:
    False`` -- that would misrepresent an uncheckable model as a missing one.
    """
    from model_harness.discovery import ProbeResult

    config.apply_overrides(
        {
            "ocr_backend": "ollama",
            "ocr_model": "llama3.2-vision:11b",
            "cleanup_backend": "api_key",
            "cleanup_model": "gpt-4o-mini",
            "translate_backend": "ollama",
            "translate_model": "",
        }
    )

    async def fake_probe(url, *a, **k):
        return ProbeResult(url=url, reachable=True, models=("llama3.2-vision:11b",))

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    from artifice_ocr import _backend

    class _FakeApiKeyClient:
        def health_check(self):
            return True, None

    monkeypatch.setattr(_backend, "get_client", lambda backend: _FakeApiKeyClient())

    res = client.get("/api/health")
    body = res.json()
    models = {m["name"]: m for m in body["models"]}
    cloud_entry = models["gpt-4o-mini"]
    assert cloud_entry["ok"] is not False
    assert cloud_entry.get("checkable") is False
    assert cloud_entry["backend"] == "api_key"


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #


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


def test_history_runs_lists_finished_runs(client):
    _seed_history_run(runtime.state)

    res = client.get("/api/history/runs")
    runs = res.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["total"] == 1
    assert runs[0]["succeeded"] == 1


def test_history_run_items_lists_documents(client):
    run_id = _seed_history_run(runtime.state)

    res = client.get(f"/api/history/runs/{run_id}/items")
    items = res.json()["items"]
    assert items[0]["name"] == "letter.png"
    assert items[0]["language"] == "German"


def test_history_item_detail_includes_text_and_diff(client):
    run_id = _seed_history_run(runtime.state)
    item_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]

    res = client.get(f"/api/history/items/{item_id}")
    body = res.json()
    assert body["raw"] == "raw text"
    assert body["cleaned"] == "cleaned text"
    assert body["confidence_tier"] == "high"
    assert "diff" in body


def test_history_item_detail_404_for_unknown_id(client):
    res = client.get("/api/history/items/999999")
    assert res.status_code == 404


def test_history_item_detail_includes_page(client):
    run_id = _seed_history_run(runtime.state)
    item_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]
    body = client.get(f"/api/history/items/{item_id}").json()
    assert "page" in body


def test_history_fabricated_result_can_be_flagged_and_exported(client):
    run_id = _seed_history_run(runtime.state)
    item_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]

    flagged = client.post(
        f"/api/history/items/{item_id}/fabricated-result", json={"fabricated": True}
    )
    assert flagged.status_code == 200
    assert flagged.json()["fabricated_result"] is True
    listed = client.get(f"/api/history/runs/{run_id}/items").json()["items"]
    assert listed[0]["fabricated_result"] is True

    export = client.get("/api/history/fabricated-results")
    assert export.status_code == 200
    body = export.json()
    assert body["schema_version"] == 1
    assert body["items"][0]["item_id"] == item_id
    assert body["items"][0]["raw_text"] == "raw text"
    assert body["items"][0]["source_file"] == "letter.png"
    assert "tropy_item_node" not in body["items"][0]


def test_history_fabricated_result_404s_for_unknown_item(client):
    response = client.post("/api/history/items/999999/fabricated-result", json={"fabricated": True})
    assert response.status_code == 404


def test_history_image_route_404_for_unknown_item(client):
    res = client.get("/api/history/items/999999/image")
    assert res.status_code == 404


def test_history_image_route_404_when_source_file_gone(client, tmp_path):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path=str(tmp_path / "nope.png"))
    item.state = JobState.DONE
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)
    hist_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]
    res = client.get(f"/api/history/items/{hist_id}/image")
    assert res.status_code == 404


def test_history_image_route_passes_jpg_through_unchanged(client, tmp_path):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    f = tmp_path / "scan.jpg"
    f.write_bytes(b"\xff\xd8\xff-fake-jpeg")
    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path=str(f))
    item.state = JobState.DONE
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)
    hist_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]
    res = client.get(f"/api/history/items/{hist_id}/image")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content == b"\xff\xd8\xff-fake-jpeg"


def test_history_image_route_renders_page_parsed_from_name_when_page_col_null(client, tmp_path):
    import fitz
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    pdf_path = tmp_path / "doc.pdf"
    doc = fitz.open()
    doc.new_page(width=50, height=200)  # page 0 — tall
    doc.new_page(width=200, height=50)  # page 1 — wide
    doc.save(str(pdf_path))
    doc.close()

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=2)
    for idx in (0, 1):
        item = JobItem(path=str(pdf_path))
        item.state = JobState.DONE
        item.label = f"Eberhard KV 3.pdf  p.{idx + 1}"
        runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=2, failed=0, elapsed=1.0)

    items = client.get(f"/api/history/runs/{run_id}/items").json()["items"]
    img0 = client.get(f"/api/history/items/{items[0]['item_id']}/image").content
    img1 = client.get(f"/api/history/items/{items[1]['item_id']}/image").content
    pix0 = fitz.Pixmap(img0)
    pix1 = fitz.Pixmap(img1)
    assert pix0.height > pix0.width  # tall (page 0)
    assert pix1.width > pix1.height  # wide (page 1)
    assert img0 != img1  # different pages rendered


def test_history_image_route_honours_page_column_when_set(client, tmp_path):
    import fitz
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    pdf_path = tmp_path / "doc.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=50)  # page 0
    doc.new_page(width=50, height=200)  # page 1
    doc.save(str(pdf_path))
    doc.close()

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path=str(pdf_path), page=1)  # explicitly page 1
    item.state = JobState.DONE
    item.label = "Eberhard KV 3.pdf  p.1"  # name says page 1, but column should win
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)

    hist_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]
    img_bytes = client.get(f"/api/history/items/{hist_id}/image").content
    pix = fitz.Pixmap(img_bytes)
    assert pix.height > pix.width  # page 1 is tall — column beats name parse


def test_history_raw_text_save_updates_text(client):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path="C:/docs/report.png")
    item.state = JobState.DONE
    item.results = {"raw": {"extracted_text": "original text"}}
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)
    hist_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]

    res = client.post(f"/api/history/items/{hist_id}/raw-text", json={"text": "corrected text"})
    assert res.status_code == 200
    body = res.json()
    assert body["raw"] == "corrected text"

    re_read = client.get(f"/api/history/items/{hist_id}").json()
    assert re_read["raw"] == "corrected text"


def test_history_raw_text_save_404_for_unknown_item(client):
    res = client.post("/api/history/items/999999/raw-text", json={"text": "x"})
    assert res.status_code == 404


def test_history_search_finds_by_filename(client):
    _seed_history_run(runtime.state)

    hit = client.get("/api/history/search", params={"q": "letter"})
    miss = client.get("/api/history/search", params={"q": "nonexistent"})

    assert len(hit.json()["items"]) == 1
    assert miss.json()["items"] == []


def test_history_fulltext_search_finds_text(client):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path="C:/docs/report.png")
    item.state = JobState.DONE
    item.results = {
        "raw": {"extracted_text": "This is a confidential report about quantum computing."},
        "cleaned": {"cleaned_text": "This is a cleaned confidential report."},
    }
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)

    res = client.get("/api/history/fulltext", params={"q": "quantum"})
    results = res.json()["results"]
    assert len(results) == 1
    assert results[0]["name"] == "report.png"


def test_history_search_with_no_query_returns_nothing(client):
    _seed_history_run(runtime.state)
    res = client.get("/api/history/search")
    assert res.json()["items"] == []


def test_delete_run_removes_it_but_not_output_files(client):
    run_id = _seed_history_run(runtime.state)

    res = client.delete(f"/api/history/runs/{run_id}")
    assert res.json() == {"ok": True}
    assert client.get("/api/history/runs").json()["runs"] == []


# --------------------------------------------------------------------------- #
# removed workspace surfaces
# --------------------------------------------------------------------------- #


def test_unrelated_workspace_routes_are_removed(client):
    assert client.get("/api/analytics/stats").status_code == 404
    assert client.get("/api/templates").status_code == 404


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — serialiser fields
# --------------------------------------------------------------------------- #


def test_history_detail_includes_tropy_exportable(client):
    run_id = _seed_history_run(runtime.state)
    rows = runtime.state.history.list_items(run_id)
    if rows:
        item_id = rows[0]["item_id"]
        res = client.get(f"/api/history/items/{item_id}")
        data = res.json()
        assert "tropy_exportable" in data
        assert "tropy_group" in data
        assert "tropy_photo_path" in data


# --------------------------------------------------------------------------- #
# bootstrap: waiting for the background uvicorn thread before opening a window
# --------------------------------------------------------------------------- #
#
# `main()` starts uvicorn in a background thread and then immediately opens a
# window (native or browser) at its URL. Caught live: the window can win that
# race and load before the socket is bound, showing a connection-refused
# error on first launch. `_wait_for_server` is the fix; these pin the two
# outcomes it has to get right.


def test_wait_for_server_returns_true_once_something_is_listening():
    import threading

    from artifice_ocr.web.server import _wait_for_server

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted = threading.Event()

    # Delay the "server" coming up, the same shape as uvicorn's own startup
    # lag, to prove this actually polls rather than checking once.
    def open_late():
        import time

        time.sleep(0.3)
        conn, _ = srv.accept()
        conn.close()
        accepted.set()

    threading.Thread(target=open_late, daemon=True).start()
    try:
        assert _wait_for_server(port, timeout=3.0) is True
        accepted.wait(timeout=2.0)  # let accept() finish before srv.close()
    finally:
        srv.close()


def test_wait_for_server_gives_up_after_timeout():
    from artifice_ocr.web.server import _wait_for_server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        never_listening_port = s.getsockname()[1]
    # The socket is closed again immediately, so nothing is listening on this
    # port — a short timeout keeps the test itself fast.
    assert _wait_for_server(never_listening_port, timeout=0.5) is False


# --------------------------------------------------------------------------- #
# bootstrap: reporting it when the server thread never comes up
# --------------------------------------------------------------------------- #
#
# `_wait_for_server` correctly detects the timeout above, but `main()` used to
# open the browser window anyway with just a print() — invisible in
# a `.pyw` process, which has no console. That's exactly the "OCR Pipeline"
# window showing a connection-refused page with zero
# explanation. These pin the fix: the exception (if any) is captured off the
# background thread and actually surfaced instead of silently discarded.


def test_start_server_thread_captures_an_exception_from_uvicorn(monkeypatch):
    import uvicorn
    from artifice_ocr.web.server import _start_server_thread

    monkeypatch.setattr(
        uvicorn, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("port already in use"))
    )

    thread, errors = _start_server_thread(59999)
    thread.join(timeout=2.0)
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "port already in use" in str(errors[0])


@pytest.fixture
def stub_tkinter(monkeypatch):
    """Install a fake ``tkinter`` for the duration of a test.

    Injected into ``sys.modules`` rather than patched onto the real module,
    because patching requires tkinter to be importable and that cannot be
    assumed either way:

    - CI runners have no tkinter at all, so
      ``monkeypatch.setattr("tkinter.messagebox.showerror", ...)`` fails at
      import time and the test errors before it reaches its assertions.
    - A machine that has both tkinter *and* a display — WSLg, for instance —
      would run the real ``showerror``, which is modal and would block the
      suite until a human dismissed it.

    Returns the list of calls made to ``showerror`` so a test can assert the
    dialog was attempted.
    """
    tk = types.ModuleType("tkinter")
    messagebox = types.ModuleType("tkinter.messagebox")
    calls: list[tuple] = []

    class _Root:
        def withdraw(self):
            pass

        def destroy(self):
            pass

    tk.Tk = _Root
    messagebox.showerror = lambda *a, **k: calls.append(a)
    tk.messagebox = messagebox

    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", messagebox)
    return calls


def test_report_startup_failure_reports_even_with_no_tkinter(monkeypatch, capsys):
    """The console message is the guarantee; the dialog is a bonus.

    ``_report_startup_failure`` is the only feedback a user gets who launched a
    packaged build by double-clicking an icon and saw nothing happen. If a
    missing tkinter could take that path down, the error handler would hide the
    very error it exists to report.

    Setting the ``sys.modules`` entry to ``None`` makes ``import tkinter``
    raise ImportError, which is how a runner without tkinter behaves.
    """
    from artifice_ocr.web.server import _report_startup_failure

    monkeypatch.setitem(sys.modules, "tkinter", None)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", None)

    class FakeThread:
        def is_alive(self):
            return False

    _report_startup_failure(5099, FakeThread(), [ValueError("bad config")])
    out = capsys.readouterr().out
    assert "5099" in out
    assert "bad config" in out


def test_report_startup_failure_prints_the_captured_exception(stub_tkinter, capsys):
    from artifice_ocr.web.server import _report_startup_failure

    class FakeThread:
        def is_alive(self):
            return False

    _report_startup_failure(5099, FakeThread(), [ValueError("bad config")])
    out = capsys.readouterr().out
    assert "5099" in out
    assert "ValueError" in out
    assert "bad config" in out


def test_report_startup_failure_explains_a_plain_timeout(stub_tkinter, capsys):
    from artifice_ocr.web.server import _report_startup_failure

    class FakeThread:
        def is_alive(self):
            return True

    _report_startup_failure(5099, FakeThread(), [])
    out = capsys.readouterr().out
    assert "No response within 10s" in out


# --------------------------------------------------------------------------- #
# bootstrap: sys.stdout/stderr are None in a real (no-terminal) .pyw launch
# --------------------------------------------------------------------------- #
#
# Confirmed live: a genuine double-click of the desktop shortcut (fresh
# reboot, nothing else holding the port) crashed with
# "ValueError: Unable to configure formatter 'default'" — uvicorn's logging
# setup tries to attach a StreamHandler to sys.stderr, which is None (not
# just quiet) in a truly consoleless process. Reproduced directly by setting
# sys.stdout/stderr to None and calling logging.config.dictConfig on
# uvicorn's own LOGGING_CONFIG.


def test_ensure_std_streams_replaces_none_streams(monkeypatch):
    from artifice_ocr.web.server import _ensure_std_streams

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    _ensure_std_streams()

    assert sys.stdout is not None
    assert sys.stderr is not None
    sys.stdout.write("this must not raise\n")
    sys.stderr.write("neither must this\n")


def test_ensure_std_streams_leaves_real_streams_alone(monkeypatch, capsys):
    from artifice_ocr.web.server import _ensure_std_streams

    _ensure_std_streams()
    print("still visible to capsys")
    assert "still visible to capsys" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# pdf export
# --------------------------------------------------------------------------- #


def _make_pdf_text_folder(tmp_path, n=2):
    """Create a cleaned/text folder with n .txt files."""
    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    for i in range(n):
        (text_dir / f"page{i + 1}.txt").write_text(
            f"Page {i + 1} text.\nSome content here.",
            encoding="utf-8",
        )
    return text_dir


@pytest.fixture
def pdf_text_folder(tmp_path):
    return _make_pdf_text_folder(tmp_path, n=2)


def test_pdf_export_start_returns_ok(client, pdf_text_folder):
    folder = str(pdf_text_folder.parent.parent)
    res = client.post(
        "/api/pdf-export/start",
        json={
            "folder": folder,
            "stage": "cleaned",
            "structure": False,
        },
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # Wait for the thread to finish
    import time

    for _ in range(50):
        status = client.get("/api/pdf-export/status").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert status["status"] == "done"
    assert status["output_path"] is not None


def test_pdf_export_409_on_concurrent_start(client, pdf_text_folder, monkeypatch):
    """A second start is refused with 409 while the first is still running.

    The first export must be *provably* in flight when the second request
    arrives. The guard lives exactly as long as the worker thread, and the
    two-file fixture compiles in milliseconds — on a fast machine the first
    export can finish in the gap between the two POSTs, in which case the
    second start is correctly accepted with 200 (this failed intermittently
    on the macOS CI runners, the fastest in the matrix). Gate the worker
    inside compile() on events this test controls instead of assuming speed.
    """
    import threading

    from artifice_ocr import pdf_export as pdf_export_module

    folder = str(pdf_text_folder.parent.parent)

    entered = threading.Event()
    release = threading.Event()
    real_compile = pdf_export_module.compile

    def gated_compile(*args, **kwargs):
        entered.set()
        release.wait(timeout=10)
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(pdf_export_module, "compile", gated_compile)

    first = client.post(
        "/api/pdf-export/start",
        json={
            "folder": folder,
            "stage": "cleaned",
            "structure": False,
        },
    )
    assert first.status_code == 200

    # The worker is now blocked inside compile(); the export is in flight.
    assert entered.wait(timeout=5), "export worker never entered compile()"

    try:
        second = client.post(
            "/api/pdf-export/start",
            json={
                "folder": folder,
                "stage": "cleaned",
                "structure": False,
            },
        )
        assert second.status_code == 409
        assert "already running" in second.json()["detail"].lower()
    finally:
        release.set()

    # Wait for the first to finish so we don't leave state dirty
    import time

    for _ in range(50):
        status = client.get("/api/pdf-export/status").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert status["status"] == "done"


def test_pdf_export_400_on_missing_folder(client, tmp_path):
    """A folder inside allowed roots that contains no text passes web validation
    but fails in the worker thread — confirming the thread still catches the
    error after the web layer validates."""
    empty = tmp_path / "empty_folder"
    empty.mkdir()
    res = client.post(
        "/api/pdf-export/start",
        json={
            "folder": str(empty),
            "stage": "cleaned",
            "structure": False,
        },
    )
    assert res.status_code == 200  # start returns ok; error surfaces on thread
    assert res.json()["ok"] is True

    import time

    for _ in range(50):
        status = client.get("/api/pdf-export/status").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert status["status"] == "error"


def test_pdf_export_400_when_folder_is_a_file(client, tmp_path):
    """Pointing the input at a file (not a folder) is rejected synchronously
    with guidance — the exact failure a user hit selecting a .pdf inside
    output/cleaned/text/ and getting the generic 'No pages found'."""
    a_file = tmp_path / "some.pdf"
    a_file.write_bytes(b"%PDF-1.4\n")
    res = client.post(
        "/api/pdf-export/start",
        json={"folder": str(a_file), "stage": "cleaned", "structure": False},
    )
    assert res.status_code == 400
    assert "folder, not a file" in res.json()["detail"]


def test_pdf_export_400_when_folder_absent(client, tmp_path):
    """A path inside allowed roots that does not exist is rejected up front
    rather than surfacing later as 'No pages found'."""
    ghost = tmp_path / "does_not_exist"
    res = client.post(
        "/api/pdf-export/start",
        json={"folder": str(ghost), "stage": "cleaned", "structure": False},
    )
    assert res.status_code == 400
    assert "Folder not found" in res.json()["detail"]


def test_pdf_export_download_404_before_compilation(client):
    res = client.get("/api/pdf-export/download")
    assert res.status_code == 404


def test_pdf_export_download_returns_pdf_after_done(client, pdf_text_folder):
    folder = str(pdf_text_folder.parent.parent)
    client.post(
        "/api/pdf-export/start",
        json={
            "folder": folder,
            "stage": "cleaned",
            "structure": False,
        },
    )

    import time

    for _ in range(50):
        status = client.get("/api/pdf-export/status").json()
        if status["status"] == "done":
            break
        time.sleep(0.05)

    res = client.get("/api/pdf-export/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert len(res.content) > 0


def test_pdf_export_events_sse_streams_log_then_done(client, pdf_text_folder):
    """SSE stream should emit log events then a done event."""
    folder = str(pdf_text_folder.parent.parent)
    res = client.post(
        "/api/pdf-export/start",
        json={
            "folder": folder,
            "stage": "cleaned",
            "structure": False,
        },
    )
    assert res.status_code == 200

    sse_res = client.get("/api/pdf-export/events")
    assert sse_res.status_code == 200
    assert sse_res.headers.get("content-type", "").startswith("text/event-stream")

    events_text = sse_res.text
    assert "log" in events_text or "done" in events_text


def test_pdf_export_terminal_event_not_leaked_to_next_stream(client, pdf_text_folder, monkeypatch):
    """A finished export's terminal 'done' event must not appear in the next
    export's event stream.

    The race window: worker A finishes ``compile()``, publishes terminal state
    and its terminal event, while ``start_pdf_export`` replaces the event queue.
    If the terminal-event push is not inside the same lock that guards the queue
    swap, A's "done" event can land in B's brand-new stream.

    This test uses a custom queue whose ``put()`` signals a ``threading.Event``
    so the main thread knows A is inside the terminal-write section (now under
    ``pdf_export_state.lock``).  A concurrent ``start_pdf_export`` call then
    blocks on that lock until A releases — guaranteeing A's terminal event is
    published before B's queue is created.

    Runs 50 iterations to exercise the interleaving window.  No sleeps, retries,
    or timing tolerances in the critical path — all coordination is via Events
    and the lock itself.
    """
    import queue
    import threading
    import time

    from artifice_ocr import pdf_export as pdf_export_module
    from artifice_ocr.web import runtime as runtime_module

    folder = str(pdf_text_folder.parent.parent)

    # Distinctive output path so we can identify A's done event even when B
    # produces its own done event on the same queue.
    A_OUTPUT = "/tmp/export_a_distinctive_output.pdf"

    # Gating events for compile
    entered = threading.Event()
    release = threading.Event()

    # Signalled by the custom queue when the worker calls .put()
    put_called = threading.Event()

    # Both A and B return instant fake paths — the test is about lock
    # coordination, not PDF generation.  Running real compile 50× would
    # make the test needlessly slow.
    B_OUTPUT = "/tmp/export_b_output.pdf"
    call_count = [0]

    def gated_compile(*args, **kwargs):
        call_count[0] += 1
        entered.set()
        release.wait(timeout=10)
        if call_count[0] == 1:
            return A_OUTPUT
        return B_OUTPUT

    monkeypatch.setattr(pdf_export_module, "compile", gated_compile)

    # ``Queue.put`` on an unbounded ``queue.Queue`` never blocks, so
    # inheriting and adding a ``set()`` call carries zero deadlock risk.
    class SignalingQueue(queue.Queue):
        def put(self, item, block=True, timeout=None):
            put_called.set()
            super().put(item, block, timeout)

    state = runtime_module.pdf_export_state

    leak_detected = False
    ok_iterations = 0

    for iteration in range(50):
        # Reset state for this iteration.
        # NOTE: start_pdf_export itself replaces pdf_export_state.events with a
        # fresh queue.Queue(), so we set our SignalingQueue AFTER the call
        # returns but while the worker is still gated inside compile.
        state.status = "idle"
        state.error = None
        state.output_path = None
        entered.clear()
        release.clear()
        put_called.clear()
        call_count[0] = 0

        # --- Start export A (gated inside compile) ---
        ok = runtime_module.start_pdf_export(
            folder,
            stage="cleaned",
            structure=False,
            output=None,
            manifest_path=None,
            format="pdf",
            style="readable",
            bilingual=False,
        )
        assert ok, f"Iteration {iteration}: start_pdf_export A returned False"
        assert entered.wait(timeout=5), f"Iteration {iteration}: A never entered compile()"

        # A is blocked inside compile — it's safe to swap in our
        # SignalingQueue because the worker hasn't reached events.put() yet.
        state.events = SignalingQueue()
        a_queue = state.events

        # Release A — it finishes compile and proceeds to terminal writes
        release.set()

        # Wait for A to reach events.put() (inside the lock, with the fix).
        # This tells us A is provably in the terminal-write section.
        if not put_called.wait(timeout=10):
            # Worker never reached put — drain and continue
            for _ in range(50):
                if state.status in ("done", "error"):
                    break
                time.sleep(0.05)
            continue

        # A is now inside events.put() and therefore inside the lock (with
        # the fix).  Call start_pdf_export — it will block on the lock
        # until A releases, then see status="done" and create B's queue.
        ok2 = runtime_module.start_pdf_export(
            folder,
            stage="cleaned",
            structure=False,
            output=None,
            manifest_path=None,
            format="pdf",
            style="readable",
            bilingual=False,
        )

        if not ok2:
            # B was rejected — the main thread won the race to the lock
            # before A's status="done" was visible.  Wait for A to finish
            # and continue to the next iteration.
            if state.thread is not None and state.thread.is_alive():
                state.thread.join(timeout=5)
            continue

        # B was accepted.  B's queue must not contain A's distinctive
        # output path — that would mean A's terminal event leaked.
        b_queue = state.events
        assert b_queue is not a_queue, f"Iteration {iteration}: B did not get a fresh queue"

        # Drain B's queue quickly.  B's thread has just started and cannot
        # have produced a "done" event yet (compile returns instantly with
        # our fake, but the worker still needs to acquire the lock).  Any
        # "done" event with A_OUTPUT is a stale leak from A.
        while True:
            try:
                event = b_queue.get(timeout=0.5)
            except queue.Empty:
                break
            if event.get("type") == "done" and event.get("output_path") == A_OUTPUT:
                leak_detected = True
                break

        if leak_detected:
            break

        ok_iterations += 1

        # Wait for B to finish before the next iteration (keeps state clean)
        if state.thread is not None and state.thread.is_alive():
            state.thread.join(timeout=5)

    assert not leak_detected, (
        "A's terminal 'done' event leaked into B's event queue — "
        "terminal writes are not atomic with respect to queue swap"
    )
    assert ok_iterations > 0, (
        "No iteration reached the window; B was always rejected. "
        "The test did not exercise the interleaving."
    )


# --------------------------------------------------------------------------- #
# path validation — pdf export
# --------------------------------------------------------------------------- #


def test_pdf_export_refuses_folder_outside_allowed_roots(client):
    res = client.post(
        "/api/pdf-export/start",
        json={
            "folder": "/opt/rejected/scans",
            "stage": "cleaned",
            "structure": False,
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "outside the directories this server is permitted" in detail.lower()
    assert str(Path.home()) not in detail


def test_pdf_export_refuses_output_outside_allowed_roots(client):
    res = client.post(
        "/api/pdf-export/start",
        json={
            "folder": "/tmp/scans",
            "stage": "cleaned",
            "structure": False,
            "output": "/opt/rejected/out.pdf",
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "outside the directories this server is permitted" in detail.lower()
    assert str(Path.home()) not in detail


def test_pdf_export_refuses_manifest_outside_allowed_roots(client):
    res = client.post(
        "/api/pdf-export/start",
        json={
            "folder": "/tmp/scans",
            "stage": "cleaned",
            "structure": False,
            "manifest": "/opt/rejected/manifest.json",
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "outside the directories this server is permitted" in detail.lower()
    assert str(Path.home()) not in detail


def test_pdf_export_accepts_valid_paths(client, pdf_text_folder):
    folder = str(pdf_text_folder.parent.parent)
    res = client.post(
        "/api/pdf-export/start",
        json={
            "folder": folder,
            "stage": "cleaned",
            "structure": False,
        },
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # Wait for the thread to finish
    import time

    for _ in range(50):
        status = client.get("/api/pdf-export/status").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert status["status"] == "done"


# --------------------------------------------------------------------------- #
# PAGE export
# --------------------------------------------------------------------------- #


def _write_persisted_page(out: Path, stem: str) -> Path:
    """Write a dummy persisted PAGE file at its canonical persistence path.

    The export endpoints copy these files verbatim — they never parse the XML,
    so a stub document is sufficient to exercise the routing surface.
    """
    from artifice_ocr.output import page_path

    path = page_path(out, stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<Page stem='{stem}'/>", encoding="utf-8")
    return path


def test_page_export_start_exports_persisted_pages(client, tmp_path):
    from artifice_ocr.output import page_path

    out = tmp_path / "out"
    _write_persisted_page(out, "page")

    res = client.post(
        "/api/page-export/start",
        json={"output_dir": str(out), "stems": ["page"]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["skipped"] == []
    assert len(body["exported"]) == 1

    exported = Path(body["exported"][0])
    assert exported.is_file()
    assert exported.read_text(encoding="utf-8") == "<Page stem='page'/>"
    # The export is a copy, distinct from the untouched persistence file.
    assert exported.resolve() != page_path(out, "page").resolve()


def test_page_export_start_reports_skipped_as_partial_success(client, tmp_path):
    out = tmp_path / "out"
    _write_persisted_page(out, "page")

    res = client.post(
        "/api/page-export/start",
        json={"output_dir": str(out), "stems": ["page", "missing"]},
    )
    # A missing PAGE file is a partial result, not a failure.
    assert res.status_code == 200
    body = res.json()
    assert body["skipped"] == ["missing"]
    assert len(body["exported"]) == 1


def test_page_export_start_rejects_empty_stems(client, tmp_path):
    out = tmp_path / "out"
    res = client.post(
        "/api/page-export/start",
        json={"output_dir": str(out), "stems": []},
    )
    assert res.status_code == 400


def test_page_export_start_refuses_output_dir_outside_roots(client):
    res = client.post(
        "/api/page-export/start",
        json={"output_dir": "/opt/rejected", "stems": ["page"]},
    )
    assert res.status_code == 400
    assert "outside the directories this server is permitted" in res.json()["detail"].lower()


def test_page_export_download_serves_persisted_page(client, tmp_path):
    out = tmp_path / "out"
    _write_persisted_page(out, "page")

    res = client.get(
        "/api/page-export/download",
        params={"output_dir": str(out), "stem": "page"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/xml")
    assert res.content == b"<Page stem='page'/>"


def test_page_export_download_404_for_missing_page(client, tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)

    res = client.get(
        "/api/page-export/download",
        params={"output_dir": str(out), "stem": "missing"},
    )
    assert res.status_code == 404
    detail = res.json()["detail"]
    assert "No PAGE document found" in detail
    # The resolved absolute path must not leak into the error message.
    assert str(out) not in detail


def test_page_export_download_refuses_output_dir_outside_roots(client):
    res = client.get(
        "/api/page-export/download",
        params={"output_dir": "/opt/rejected", "stem": "page"},
    )
    assert res.status_code == 400


# --------------------------------------------------------------------------- #
# path validation — add_paths
# --------------------------------------------------------------------------- #


def test_add_paths_refuses_path_outside_allowed_roots(client, tmp_path):
    """A path outside the permitted root directories is refused with 400."""
    # Pick a directory that is not /home, /tmp, or the working directory.
    # resolve(strict=False) does not require the path to exist, so a
    # nonexistent path under /opt is sufficient.
    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": ["/opt/rejected/scan.png"],
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "outside the directories this server is permitted" in detail.lower()
    # The rejection must not disclose the server's filesystem layout. The
    # allowed roots include Path.home(), so naming them would hand the OS
    # username to an unauthenticated caller.
    assert str(Path.home()) not in detail
    assert "allowed:" not in detail.lower()


def test_add_paths_refuses_hidden_directory(client, tmp_path):
    """A path that descends into a hidden directory is refused even when the
    root itself is permitted."""
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "scan.png").write_bytes(b"x")

    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": [str(hidden / "scan.png")],
        },
    )
    assert res.status_code == 400
    assert "hidden" in res.json()["detail"].lower()


def test_add_paths_accepts_normal_path(client, tmp_path):
    """A normal folder of scans in a normal location still queues and
    processes — the security fix does not break the actual workflow."""
    (tmp_path / "scan.png").write_bytes(b"x")
    (tmp_path / "scan.pdf").write_bytes(b"x")

    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": [str(tmp_path)],
        },
    )
    assert res.status_code == 200
    assert res.json()["added"] == 2


def test_add_paths_refuses_windows_style_path(client, tmp_path):
    """A Windows-style path with backslashes that points outside allowed
    roots is rejected.

    The *reason* differs by platform and the assertion has to follow, or this
    test passes on POSIX and fails on Windows. On POSIX a drive letter is
    refused outright by `normalise_path`, because pathlib would otherwise treat
    "C:/SystemFolder" as relative and `resolve()` would prepend the cwd,
    landing it inside an allowed root. On Windows the same string is a
    perfectly valid absolute path, so it resolves and is then refused by the
    containment check instead. Either way it must be a 400 — that is the
    property under test.
    """
    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": ["C:\\SystemFolder\\file.png"],
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"].lower()
    if os.name == "posix":
        assert "not valid on this platform" in detail
    else:
        assert "outside the directories this server is permitted" in detail


# --------------------------------------------------------------------------- #
# path validation — output_dir
# --------------------------------------------------------------------------- #


def test_start_run_refuses_output_dir_outside_allowed_roots(client, tmp_path):
    """An output directory outside the permitted roots is refused before
    any run is started."""
    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": "/opt/rejected/output",
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "outside the directories this server is permitted" in detail.lower()
    assert str(Path.home()) not in detail
    assert "allowed:" not in detail.lower()


def test_start_run_refuses_hidden_output_dir(client, tmp_path):
    """A hidden output directory is refused at validation time."""
    hidden = tmp_path / ".hidden_out"
    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": str(hidden),
        },
    )
    assert res.status_code == 400
    assert "hidden" in res.json()["detail"].lower()


def test_start_run_accepts_normal_output_dir(client, tmp_path):
    """A normal output directory passes validation (the run then fails because
    the queue is empty, but that means the path check succeeded)."""
    out = tmp_path / "out"
    out.mkdir()

    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": str(out),
        },
    )
    # 409 = queue is empty, but validation passed (otherwise 400)
    assert res.status_code == 409
    assert "empty" in res.json()["detail"].lower()


def test_start_run_refuses_windows_style_output_dir(client):
    """A Windows-style output directory path is rejected.

    Platform-dependent for the same reason as
    ``test_add_paths_refuses_windows_style_path`` — see its docstring. The 400
    is the invariant; the message is not.
    """
    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": "C:\\SystemFolder\\output",
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"].lower()
    if os.name == "posix":
        assert "not valid on this platform" in detail
    else:
        assert "outside the directories this server is permitted" in detail


# --------------------------------------------------------------------------- #
# Cause A regression — temp dir from TMPDIR is always an allowed root
# --------------------------------------------------------------------------- #


def test_platform_temp_dir_is_always_an_allowed_root():
    """The platform's own temp directory is an allowed root on every platform.

    This is the invariant that the macOS breakage violated, and it is checked
    unconditionally so that Windows and macOS both assert it rather than
    skipping. Before the fix the list carried a bare ``Path("/tmp")``, which is
    not the platform temp directory on macOS (``/var/folders/…``) and does not
    exist at all on Windows.
    """
    import tempfile

    from shared_ui.path_validation import build_allowed_roots

    temp_root = Path(tempfile.gettempdir()).resolve()
    assert temp_root in [r.resolve() for r in build_allowed_roots("ARTIFICE_OCR_ALLOWED_ROOTS")]


@pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "Reproduces a POSIX-only failure mode. On Windows the platform temp "
        "directory lives under %LOCALAPPDATA%, i.e. below Path.home(), so it is "
        "already covered by the home root and this failure cannot arise. "
        "/var/tmp also does not exist on Windows. The platform-neutral "
        "invariant is asserted unconditionally in the test above."
    ),
)
def test_validate_directory_accepts_temp_dir_from_custom_tmpdir(monkeypatch):
    """A temp directory under a relocated ``TMPDIR`` is accepted.

    This reproduces the macOS failure *on Linux*: on macOS
    ``tempfile.gettempdir()`` returns a per-user directory under
    ``/var/folders/…`` that is neither ``/tmp`` nor ``$HOME``, so without
    ``gettempdir()`` in the roots list every path in a macOS ``tmp_path`` is
    refused. Pointing ``TMPDIR`` at ``/var/tmp`` puts the temp directory outside
    both ``/tmp`` and ``$HOME``, which is the same shape of problem.
    """
    import tempfile

    from artifice_ocr.web.validation import validate_directory

    custom_root = Path("/var/tmp")
    if not custom_root.is_dir():
        pytest.skip("/var/tmp is absent on this POSIX host")

    monkeypatch.setenv("TMPDIR", str(custom_root))
    # setattr, not assignment: monkeypatch restores the cache afterwards, so a
    # later test does not inherit a cleared tempdir.
    monkeypatch.setattr(tempfile, "tempdir", None)

    d = Path(tempfile.mkdtemp(dir=str(custom_root)))
    try:
        # Compare against the RESOLVED path: validate_directory returns str(p)
        # after resolve(). On Linux /var/tmp is a real directory so resolve() is
        # a no-op and either form passes — but on macOS /var is a symlink to
        # /private/var, so the unresolved form fails there and only there.
        assert validate_directory(str(d), "input_dir") == str(d.resolve())
    finally:
        if d.exists():
            d.rmdir()


# --------------------------------------------------------------------------- #
# validate_contained — malformed input rejection (regression)
# --------------------------------------------------------------------------- #
# ``normalise_path`` raises ``ValueError`` for empty/whitespace-only strings
# and, on POSIX, for Windows drive-letter paths.  ``validate_contained`` called
# it outside any try/except, so those errors propagated as unhandled 500s
# rather than 400s.  These tests assert that both failure modes now return
# HTTP 400, matching the pattern ``validate_directory`` already follows.


def test_validate_contained_rejects_empty_string(tmp_path):
    """An empty raw path string must be 400, not an unhandled ValueError."""
    from artifice_ocr.web.validation import validate_contained

    container = str(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        validate_contained("", container, "path")
    assert exc_info.value.status_code == 400
    assert "must not be empty" in str(exc_info.value.detail)


@pytest.mark.skipif(
    os.name != "posix",
    reason="Windows drive-letter detection only activates on POSIX",
)
def test_validate_contained_rejects_windows_drive_letter_on_posix(tmp_path):
    """A Windows drive-letter path on POSIX must be 400, not 500."""
    from artifice_ocr.web.validation import validate_contained

    container = str(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        validate_contained("C:\\Windows", container, "path")
    assert exc_info.value.status_code == 400
    assert "not valid on this platform" in str(exc_info.value.detail)


# --------------------------------------------------------------------------- #
# config reset — credential redaction
# --------------------------------------------------------------------------- #


def test_config_reset_does_not_leak_credentials(client, monkeypatch):
    """POST /api/config/reset must not return api_key or huggingface_token
    verbatim.

    ``config.reset()`` clears the in-memory cache; we prevent that here by
    making it a no-op so the secrets survive into the response path.  On the
    unfixed code the response includes them verbatim; with the fix
    ``_redact_config`` replaces them with the shared placeholder.
    """
    # Populate secrets in the live config cache.
    config.apply_overrides(
        {
            "api_key": "sk-secret-test-key",
            "huggingface_token": "hf-secret-test-token",
        }
    )

    # Prevent reset from clearing the cache so the secrets survive into the
    # response dict — this reproduces the leak scenario from the review.
    monkeypatch.setattr(config, "reset", lambda: None)

    res = client.post("/api/config/reset")
    assert res.status_code == 200
    body = res.json()

    assert body.get("api_key") != "sk-secret-test-key", (
        "api_key was returned verbatim in reset_config response"
    )
    assert body.get("huggingface_token") != "hf-secret-test-token", (
        "huggingface_token was returned verbatim in reset_config response"
    )
    # Optionally confirm the placeholder appears (only true if the values
    # are truthy — they are here).
    assert body.get("api_key") == "************", (
        f"Expected placeholder for api_key, got: {body.get('api_key')}"
    )
    assert body.get("huggingface_token") == "************", (
        f"Expected placeholder for huggingface_token, got: {body.get('huggingface_token')}"
    )


def test_tesseract_status_route_returns_shape(client, monkeypatch):
    """The detection endpoint always returns a stable shape, whether or not a
    real Tesseract binary is present on the machine running the test."""
    from artifice_ocr import _tesseract

    monkeypatch.setattr(_tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(_tesseract, "version", lambda binary=None: "tesseract 5.3.3")

    res = client.get("/api/tesseract/status")
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"available", "path", "version", "lang"}
    assert body["available"] is True
    assert body["version"] == "tesseract 5.3.3"
