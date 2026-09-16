# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SSE event-stream tests (bounded consumption over a live uvicorn server)."""

import json
import queue as _sync_queue
import threading
import time

import httpx
import pytest
import uvicorn
from artifice_ocr.web import server
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
from artifice_ocr.web.runtime import RunState

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
