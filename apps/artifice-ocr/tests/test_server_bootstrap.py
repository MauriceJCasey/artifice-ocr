# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""server.py bootstrap helpers: server-thread startup, failure reporting, std-stream repair."""

import socket
import sys
import types

import pytest

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
