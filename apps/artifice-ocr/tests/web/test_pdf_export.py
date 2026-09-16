# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""PDF export route tests: start/status/download, SSE events, and path validation."""

from pathlib import Path

import pytest

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
