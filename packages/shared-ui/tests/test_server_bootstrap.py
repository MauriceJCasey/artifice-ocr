# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for shared_ui.server_bootstrap."""

from __future__ import annotations

import socket
import sys
import threading
import time

from shared_ui.server_bootstrap import (
    ensure_std_streams,
    free_port,
    port_available,
    report_startup_failure,
    start_server_thread,
    wait_for_server,
)


class TestFreePort:
    """Tests for free_port()."""

    def test_returns_an_int(self) -> None:
        port = free_port()
        assert isinstance(port, int)

    def test_returned_port_can_be_bound(self) -> None:
        port = free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            # If we get here without OSError, the port was truly free.


class TestPortAvailable:
    """Tests for port_available()."""

    def test_free_port_reported_available(self) -> None:
        port = free_port()
        assert port_available(port) is True

    def test_bound_port_reported_unavailable(self) -> None:
        port = free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            s.listen(0)
            assert port_available(port) is False
        # After closing the socket it should be free again.
        # Give the OS a moment to release it.
        time.sleep(0.05)
        assert port_available(port) is True


class TestWaitForServer:
    """Tests for wait_for_server()."""

    def test_returns_true_when_listening(self) -> None:
        port = free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", port))
            server.listen(1)
            assert wait_for_server(port, timeout=1.0) is True

    def test_returns_false_when_nothing_listening(self) -> None:
        port = free_port()
        assert wait_for_server(port, timeout=0.5) is False


# --------------------------------------------------------------------------- #
# Minimal ASGI app for start_server_thread tests
# --------------------------------------------------------------------------- #


async def _minimal_asgi(scope, receive, send):
    """Trivial ASGI app that responds 200 to everything."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


def _failing_asgi(scope, receive, send):
    """ASGI app that raises immediately — it lacks ``async`` on purpose."""
    raise RuntimeError("startup failure")


class TestStartServerThread:
    """Tests for start_server_thread()."""

    def test_successful_startup(self) -> None:
        port = free_port()
        thread, errors = start_server_thread(_minimal_asgi, port)

        assert isinstance(thread, threading.Thread)
        assert thread.is_alive()
        # Wait for the server to actually start listening.
        assert wait_for_server(port, timeout=5.0), "Server did not start listening within timeout"
        assert errors == []

    def test_failure_populates_errors(self, monkeypatch) -> None:
        """When uvicorn.run raises, the exception lands in errors.

        ``start_server_thread`` imports ``uvicorn`` locally, so the patch
        targets ``uvicorn.run`` directly on the cached module.
        """
        port = free_port()
        monkeypatch.setattr(
            "uvicorn.run",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated uvicorn failure")),
        )

        thread, errors = start_server_thread(_minimal_asgi, port)
        thread.join(timeout=3.0)
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert str(errors[0]) == "simulated uvicorn failure"


class TestEnsureStdStreams:
    """Tests for ensure_std_streams()."""

    def test_replaces_none_streams(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        ensure_std_streams()
        assert sys.stdout is not None
        assert sys.stderr is not None
        # Verify they are writable.
        sys.stdout.write("")  # should not raise
        sys.stderr.write("")  # should not raise

    def test_leaves_real_streams_untouched(self, monkeypatch) -> None:
        import io

        fake_out = io.StringIO()
        fake_err = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", fake_err)
        ensure_std_streams()
        assert sys.stdout is fake_out
        assert sys.stderr is fake_err


class TestReportStartupFailure:
    """Tests for report_startup_failure().

    Every test mocks ``tkinter`` in ``sys.modules`` so the tkinter dialog code
    path runs through mock objects instead of trying to connect to an X11
    display (which hangs indefinitely in headless environments).
    """

    @staticmethod
    def _mock_tkinter(monkeypatch):
        from unittest.mock import MagicMock

        mock_tk = MagicMock()
        monkeypatch.setitem(sys.modules, "tkinter", mock_tk)

    def test_does_not_raise_with_empty_errors(self, monkeypatch) -> None:
        self._mock_tkinter(monkeypatch)
        thread = threading.Thread(target=lambda: None)
        thread.start()
        try:
            report_startup_failure("TestApp", 9999, thread, [])
        finally:
            thread.join(timeout=1.0)

    def test_does_not_raise_with_populated_errors(self, monkeypatch) -> None:
        self._mock_tkinter(monkeypatch)
        thread = threading.Thread(target=lambda: None)
        thread.start()
        try:
            report_startup_failure("TestApp", 9999, thread, [RuntimeError("boom")])
        finally:
            thread.join(timeout=1.0)

    def test_detail_when_thread_still_alive(self, capsys, monkeypatch) -> None:
        """When errors is empty and the thread is alive, the detail mentions
        no response within the given timeout."""
        self._mock_tkinter(monkeypatch)
        alive_flag = threading.Event()

        def _stay_alive():
            alive_flag.set()
            while True:
                time.sleep(0.1)

        thread = threading.Thread(target=_stay_alive, daemon=True)
        thread.start()
        alive_flag.wait(timeout=1.0)
        try:
            report_startup_failure("TestApp", 9999, thread, [], timeout=20.0)
            captured = capsys.readouterr()
            assert "No response within 20s" in captured.out
        finally:
            # daemon — nothing to join
            pass

    def test_detail_when_thread_exited(self, capsys, monkeypatch) -> None:
        """When errors is empty and the thread is no longer alive, the
        detail says the thread exited without listening."""
        self._mock_tkinter(monkeypatch)
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join(timeout=1.0)
        report_startup_failure("TestApp", 9999, thread, [])
        captured = capsys.readouterr()
        assert "thread exited without ever starting to listen" in captured.out
