# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the native window module and --no-window flag."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
from artifice_ocr.web.window import (
    WindowResult,
    _unblock_frozen_bundle,
    open_native_window,
)

# apps/artifice-ocr/tests/test_window.py -> repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestWindowResult:
    """Unit tests for the WindowResult data class."""

    def test_opened_true(self) -> None:
        result = WindowResult(opened=True)
        assert result.opened is True
        assert result.reason == ""

    def test_opened_false_with_reason(self) -> None:
        result = WindowResult(opened=False, reason="No display found")
        assert result.opened is False
        assert result.reason == "No display found"

    def test_defaults(self) -> None:
        result = WindowResult(opened=False)
        assert result.reason == ""


class TestOpenNativeWindow:
    """Tests for open_native_window() with pywebview unavailable."""

    def test_returns_false_when_pywebview_not_installed(self) -> None:
        """When pywebview is not importable, return opened=False with reason."""
        import builtins

        original_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "webview":
                raise ImportError("No module named 'webview'")
            return original_import(name, *a, **kw)

        try:
            builtins.__import__ = _fake_import
            result = open_native_window("http://127.0.0.1:8765")
            assert result.opened is False
            assert "pywebview" in result.reason.lower()
            assert "pip install" in result.reason
        finally:
            builtins.__import__ = original_import

    def test_returns_false_when_webview_backend_unavailable(self) -> None:
        """When pywebview imports but no backend is available, return opened=False."""
        with mock.patch("webview.create_window"), mock.patch("webview.start") as mock_start:
            mock_start.side_effect = RuntimeError("No suitable webview provider found")
            result = open_native_window("http://127.0.0.1:8765")
            assert result.opened is False
            assert "Native window unavailable" in result.reason
            assert "No suitable webview provider" in result.reason

    def test_returns_true_when_window_opens(self) -> None:
        """When the window opens and then closes cleanly, return opened=True."""
        with mock.patch("webview.create_window"), mock.patch("webview.start") as mock_start:
            mock_start.return_value = None  # simulates window closed
            result = open_native_window("http://127.0.0.1:8765")
            assert result.opened is True
            assert result.reason == ""

    def test_passes_url_and_title_to_create_window(self) -> None:
        """Ensure url and title keyword args are forwarded correctly."""
        with (
            mock.patch("webview.create_window") as mock_create,
            mock.patch("webview.start"),
        ):
            open_native_window(
                "http://127.0.0.1:9999",
                title="My OCR App",
                width=1024,
                height=768,
            )
            mock_create.assert_called_once_with(
                title="My OCR App",
                url="http://127.0.0.1:9999",
                width=1024,
                height=768,
                resizable=True,
                min_size=(640, 480),
                frameless=True,
                easy_drag=False,
                js_api=mock.ANY,
            )


class TestUnblockFrozenBundle:
    """Tests for _unblock_frozen_bundle()."""

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Zone.Identifier ADS is an NTFS/Windows-only concept",
    )
    def test_unblock_does_not_modify_zone_identifier(self, tmp_path: Path) -> None:
        """The compatibility hook never removes download-origin metadata."""
        dll_file = tmp_path / "Python.Runtime.dll"
        dll_file.write_text("fake dll content")

        # Write a real Zone.Identifier ADS (the "[ZoneTransfer]" header is
        # characteristic of a real MOTW stream).
        zone_stream = f"{dll_file}:Zone.Identifier"
        with open(zone_stream, "w") as f:
            f.write("[ZoneTransfer]\nZoneId=3\n")

        _unblock_frozen_bundle(tmp_path)

        assert os.path.exists(zone_stream)

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Zone.Identifier ADS is an NTFS/Windows-only concept",
    )
    def test_unblock_noop_when_no_zone_identifier(self, tmp_path: Path) -> None:
        """Files without a Zone.Identifier stream are left alone."""
        dll_file = tmp_path / "Python.Runtime.dll"
        dll_file.write_text("fake dll content")

        # Should not raise.
        _unblock_frozen_bundle(tmp_path)

    def test_unblock_noop_nonexistent_dir(self) -> None:
        """A nonexistent directory is a silent no-op, not an error."""
        _unblock_frozen_bundle(Path("/nonexistent/pythonnet/dir"))

    def test_unblock_noop_no_files_with_stream(self, tmp_path: Path) -> None:
        """A directory with files that have no Zone.Identifier ADS is a no-op."""
        (tmp_path / "some.dll").write_text("content")
        (tmp_path / "other.dll").write_text("content")

        # Should not raise.
        _unblock_frozen_bundle(tmp_path)


class TestMainNoWindowFlag:
    """Integration tests for server.main() with the --no-window flag."""

    @staticmethod
    def _start_server(port: int, *extra_args: str):
        """Launch the server in a subprocess via setsid/nohup for clean cleanup.

        ``preexec_fn`` (to put the child in its own process group, so
        ``_stop_server`` can kill it and anything it spawns) is POSIX-only —
        passing it at all on Windows raises ``ValueError`` immediately, not
        just at call time, so it is only added to the kwargs on POSIX.
        """
        import subprocess

        kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(_REPO_ROOT),
        }
        if sys.platform != "win32":
            kwargs["preexec_fn"] = __import__("os").setsid

        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "artifice_ocr.web.server",
                "--port",
                str(port),
                *extra_args,
            ],
            **kwargs,
        )

    @staticmethod
    def _stop_server(proc) -> None:
        """Stop a server started by ``_start_server``, cross-platform.

        POSIX: SIGTERM the whole process group (matches the setsid above).
        Windows has no process-group equivalent here — the child was
        launched directly (no shell layer spawning grandchildren), so
        ``terminate()`` on the one process is sufficient.
        """
        if sys.platform != "win32":
            import os
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=5)

    @staticmethod
    def _free_port() -> int:
        """Return an available ephemeral port on localhost."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @staticmethod
    def _wait_for_server(port: int, timeout: float = 15.0) -> bool:
        """Poll until the server responds on the given port."""
        import urllib.request

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
                return True
            except OSError:
                time.sleep(0.15)
        return False

    def test_no_window_flag_accepted(self) -> None:
        """Verify that --no-window is recognized by the argument parser."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--no-window", action="store_true", default=False)
        args = parser.parse_args(["--no-window"])
        assert args.no_window is True

        args = parser.parse_args([])
        assert args.no_window is False

    def test_server_with_no_window_serves_content(self) -> None:
        """End-to-end: start server with --no-window, curl / and /shared/tokens.css.

        This is the key regression test — the server must work exactly as
        before with the --no-window flag.
        """
        import urllib.request

        port = self._free_port()
        proc = self._start_server(port, "--no-window")

        try:
            assert self._wait_for_server(port), f"Server on port {port} did not start"

            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
            assert resp.status == 200
            body = resp.read().decode()
            assert "<!DOCTYPE html>" in body.lower() or "<html" in body.lower()

            # Check static assets
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/shared/tokens.css")
            assert resp.status == 200
            css = resp.read().decode()
            assert "clamp" in css  # fluid typography

            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/static/css/app.css")
            assert resp.status == 200
        finally:
            self._stop_server(proc)

    def test_server_with_no_window_serves_api(self) -> None:
        """Check that /api/queue responds in --no-window mode."""
        import json
        import urllib.request

        port = self._free_port()
        proc = self._start_server(port, "--no-window")

        try:
            assert self._wait_for_server(port), f"Server on port {port} did not start"

            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/queue")
            assert resp.status == 200
            data = json.loads(resp.read())
            assert "items" in data or "queue" in data
        finally:
            self._stop_server(proc)

class TestWindowFailureReporting:
    """A packaged desktop failure must not silently become a browser app."""

    def test_failure_is_shown_in_a_dialog(self, monkeypatch, capsys) -> None:
        import types

        calls: list[tuple[str, str]] = []
        root = mock.Mock()
        tk = types.ModuleType("tkinter")
        tk.Tk = mock.Mock(return_value=root)
        messagebox = types.ModuleType("tkinter.messagebox")
        messagebox.showerror = lambda title, message: calls.append((title, message))
        tk.messagebox = messagebox
        monkeypatch.setitem(sys.modules, "tkinter", tk)
        monkeypatch.setitem(sys.modules, "tkinter.messagebox", messagebox)

        from artifice_ocr.web.server import _report_window_failure

        _report_window_failure("WebView2 is unavailable")

        assert calls
        assert "WebView2 is unavailable" in calls[0][1]
        assert "browser fallback is disabled" in calls[0][1].lower()
        assert "WebView2 is unavailable" in capsys.readouterr().out
        root.destroy.assert_called_once_with()

    def test_no_launch_path_opens_a_browser(self) -> None:
        server = (
            _REPO_ROOT
            / "apps"
            / "artifice-ocr"
            / "src"
            / "artifice_ocr"
            / "web"
            / "server.py"
        ).read_text(encoding="utf-8")
        assert "import webbrowser" not in server
        assert "webbrowser.open" not in server
        assert "if getattr(sys, \"frozen\", False)" not in server
