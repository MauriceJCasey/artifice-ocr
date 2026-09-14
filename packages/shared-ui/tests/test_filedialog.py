# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for shared_ui.filedialog.

No test opens a real dialog: the backends are injected or monkeypatched so the
suite runs headless in CI.
"""

from __future__ import annotations

import asyncio
import builtins
import threading
from pathlib import Path

import pytest
from shared_ui import filedialog
from shared_ui.filedialog import (
    DialogResult,
    DialogState,
    FileType,
    pick_files,
    pick_files_async,
    pick_folder,
    save_file,
)

try:
    import webview
except ImportError:  # pragma: no cover — pywebview is a dev-only dependency
    webview = None

_needs_webview = pytest.mark.skipif(webview is None, reason="pywebview not installed")


class FakeWindow:
    """A pywebview window stand-in whose dialog result is scripted."""

    def __init__(self, result=None, *, exc: BaseException | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[dict] = []
        self.thread: threading.Thread | None = None

    def create_file_dialog(self, **kwargs):
        self.calls.append(kwargs)
        self.thread = threading.current_thread()
        if self._exc is not None:
            raise self._exc
        return self._result


def _use_webview(monkeypatch, window: FakeWindow | None) -> None:
    monkeypatch.setattr(filedialog, "_live_webview_window", lambda: window)


def _use_main_thread(monkeypatch, *, on_main: bool) -> None:
    monkeypatch.setattr(filedialog, "_on_main_thread", lambda: on_main)


class TestDialogResult:
    """DialogResult carries state, paths and a reason."""

    def test_selected_single_path(self) -> None:
        r = DialogResult(DialogState.SELECTED, paths=(Path("/a/b.txt"),))
        assert r.state is DialogState.SELECTED
        assert r.path == Path("/a/b.txt")

    def test_cancelled_has_no_paths(self) -> None:
        r = DialogResult(DialogState.CANCELLED)
        assert r.paths == ()
        assert r.path is None

    def test_unavailable_carries_reason(self) -> None:
        r = DialogResult(DialogState.UNAVAILABLE, reason="no display")
        assert r.reason == "no display"

    def test_as_dict_selected(self) -> None:
        r = DialogResult(DialogState.SELECTED, paths=(Path("/a/b.txt"), Path("/c/d.txt")))
        assert r.as_dict() == {
            "state": "selected",
            "paths": ["/a/b.txt", "/c/d.txt"],
            "reason": "",
        }

    def test_as_dict_cancelled(self) -> None:
        r = DialogResult(DialogState.CANCELLED)
        assert r.as_dict() == {"state": "cancelled", "paths": [], "reason": ""}

    def test_as_dict_unavailable(self) -> None:
        r = DialogResult(DialogState.UNAVAILABLE, reason="no display")
        assert r.as_dict() == {"state": "unavailable", "paths": [], "reason": "no display"}


class TestFileType:
    """FileType normalises a bare string to a one-pattern tuple."""

    def test_bare_string_pattern(self) -> None:
        ft = FileType("Text", "*.txt")
        assert ft.patterns == ("*.txt",)

    def test_tuple_patterns(self) -> None:
        ft = FileType("Text", ("*.txt", "*.md"))
        assert ft.patterns == ("*.txt", "*.md")

    def test_to_tk_and_webview_formats(self) -> None:
        ft = FileType("Text files", ("*.txt", "*.md"))
        assert filedialog._to_tk_filetypes([ft]) == [("Text files", "*.txt *.md")]
        assert filedialog._to_webview_filetypes([ft]) == ("Text files (*.txt;*.md)",)

    def test_hyphenated_description_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            FileType("JSON-LD export", "*.jsonld")
        assert "JSON-LD export" in str(excinfo.value)

    def test_word_and_space_description_constructs(self) -> None:
        ft = FileType("JSON export", "*.jsonld")
        assert ft.description == "JSON export"
        assert ft.patterns == ("*.jsonld",)

    @pytest.mark.parametrize("description", ["JSON-LD export", "JSON/LD export", "JSON.LD export"])
    def test_disallowed_description_characters_rejected(self, description: str) -> None:
        with pytest.raises(ValueError):
            FileType(description, "*.jsonld")


class TestSelected:
    """A live window returns SELECTED with the chosen paths."""

    def test_selected_single(self, monkeypatch) -> None:
        _use_webview(monkeypatch, FakeWindow(("/home/user/scan.jpg",)))
        result = pick_files(multiple=False)
        assert result.state is DialogState.SELECTED
        assert result.paths == (Path("/home/user/scan.jpg"),)

    def test_selected_multiple(self, monkeypatch) -> None:
        _use_webview(monkeypatch, FakeWindow(("/a/one.jpg", "/a/two.jpg")))
        result = pick_files(multiple=True)
        assert result.state is DialogState.SELECTED
        assert result.paths == (Path("/a/one.jpg"), Path("/a/two.jpg"))

    def test_folder_selected(self, monkeypatch) -> None:
        _use_webview(monkeypatch, FakeWindow(("/data/project",)))
        result = pick_folder()
        assert result.state is DialogState.SELECTED
        assert result.path == Path("/data/project")

    def test_save_selected(self, monkeypatch) -> None:
        _use_webview(monkeypatch, FakeWindow(("/out/export.jsonld",)))
        result = save_file()
        assert result.state is DialogState.SELECTED
        assert result.path == Path("/out/export.jsonld")


class TestCancelled:
    """A dialog returning nothing maps to CANCELLED, not UNAVAILABLE."""

    def test_webview_none_is_cancelled(self, monkeypatch) -> None:
        _use_webview(monkeypatch, FakeWindow(None))
        result = pick_files()
        assert result.state is DialogState.CANCELLED
        assert result.paths == ()

    def test_tk_empty_is_cancelled(self, monkeypatch) -> None:
        _use_webview(monkeypatch, None)
        _use_main_thread(monkeypatch, on_main=True)
        monkeypatch.setattr(filedialog, "_run_tk_dialog", lambda *a, **k: ())
        result = pick_folder()
        assert result.state is DialogState.CANCELLED


class TestUnavailable:
    """No backend, or a non-main thread without a window, is UNAVAILABLE."""

    def test_no_window_off_main_thread_is_unavailable(self, monkeypatch) -> None:
        _use_webview(monkeypatch, None)
        _use_main_thread(monkeypatch, on_main=False)
        result = pick_files()
        assert result.state is DialogState.UNAVAILABLE
        assert "main thread" in result.reason

    def test_no_window_and_no_tkinter_is_unavailable(self, monkeypatch) -> None:
        _use_webview(monkeypatch, None)
        _use_main_thread(monkeypatch, on_main=True)

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "tkinter":
                raise ImportError("No module named 'tkinter' (simulated)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        result = pick_folder()
        assert result.state is DialogState.UNAVAILABLE
        assert "tkinter" in result.reason

    def test_backend_exception_is_unavailable(self, monkeypatch) -> None:
        _use_webview(monkeypatch, FakeWindow(None, exc=RuntimeError("boom")))
        result = pick_files()
        assert result.state is DialogState.UNAVAILABLE
        assert "boom" in result.reason


class TestBackendPrecedence:
    """A live pywebview window wins over tkinter."""

    def test_window_wins_over_tkinter(self, monkeypatch) -> None:
        window = FakeWindow(("/chosen.txt",))
        _use_webview(monkeypatch, window)
        # tkinter would be permitted (main thread), but must not be reached.
        _use_main_thread(monkeypatch, on_main=True)
        monkeypatch.setattr(
            filedialog,
            "_run_tk_dialog",
            lambda *a, **k: pytest.fail("tkinter reached despite a live window"),
        )
        result = pick_files()
        assert result.state is DialogState.SELECTED
        assert window.calls, "webview dialog was not invoked"

    @_needs_webview
    def test_webview_gets_open_dialog_type_and_multiple(self, monkeypatch) -> None:
        window = FakeWindow(("/x/a", "/x/b"))
        _use_webview(monkeypatch, window)
        pick_files(multiple=True)
        (call,) = window.calls
        assert call["dialog_type"] == webview.FileDialog.OPEN
        assert call["allow_multiple"] is True

    @_needs_webview
    def test_webview_folder_and_save_are_single(self, monkeypatch) -> None:
        folder_win = FakeWindow(("/d",))
        _use_webview(monkeypatch, folder_win)
        pick_folder()
        (call,) = folder_win.calls
        assert call["dialog_type"] == webview.FileDialog.FOLDER
        assert call["allow_multiple"] is False

        save_win = FakeWindow(("/out.json",))
        _use_webview(monkeypatch, save_win)
        save_file(default_name="out.json")
        (call,) = save_win.calls
        assert call["dialog_type"] == webview.FileDialog.SAVE
        assert call["allow_multiple"] is False
        assert call["save_filename"] == "out.json"


class TestTkinterPath:
    """When only tkinter is available (main thread), the tk backend is used."""

    def test_tk_backend_used_on_main_thread(self, monkeypatch) -> None:
        _use_webview(monkeypatch, None)
        _use_main_thread(monkeypatch, on_main=True)
        captured: dict = {}

        def _fake_tk(mode, **kwargs):
            captured["mode"] = mode
            captured["kwargs"] = kwargs
            return ("/picked.txt",)

        monkeypatch.setattr(filedialog, "_run_tk_dialog", _fake_tk)
        result = pick_files(multiple=False)
        assert result.state is DialogState.SELECTED
        assert result.path == Path("/picked.txt")
        assert captured["mode"] == "files"

    def test_tk_file_types_converted(self, monkeypatch) -> None:
        _use_webview(monkeypatch, None)
        _use_main_thread(monkeypatch, on_main=True)
        captured: dict = {}

        def _fake_tk(mode, **kwargs):
            captured.update(kwargs)
            return ("/picked.txt",)

        monkeypatch.setattr(filedialog, "_run_tk_dialog", _fake_tk)
        pick_files(file_types=[FileType("Images", ("*.png", "*.jpg"))])
        assert captured["file_types"] == [("Images", "*.png *.jpg")]


class TestAsync:
    """The awaitable form runs the blocking dialog off the event loop."""

    @pytest.mark.asyncio
    async def test_async_does_not_block_loop(self, monkeypatch) -> None:
        entered = threading.Event()
        release = threading.Event()
        window = FakeWindow(("/chosen.txt",))

        def _blocking_dialog(self, **kwargs):
            self.thread = threading.current_thread()
            entered.set()
            assert release.wait(timeout=2.0), "dialog was never released"
            return self._result

        monkeypatch.setattr(FakeWindow, "create_file_dialog", _blocking_dialog)
        _use_webview(monkeypatch, window)

        task = asyncio.create_task(pick_files_async())
        # The worker thread enters the dialog and blocks; the event loop stays
        # responsive — reaching this line at all proves it wasn't blocked.
        assert await asyncio.to_thread(entered.wait, 2.0)
        assert window.thread is not threading.main_thread()

        release.set()
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result.state is DialogState.SELECTED
        assert result.path == Path("/chosen.txt")

    @pytest.mark.asyncio
    async def test_async_folder_and_save(self, monkeypatch) -> None:
        _use_webview(monkeypatch, FakeWindow(("/d",)))
        folder = await pick_files_async()  # reuse pick_files_async for a smoke check
        assert folder.state is DialogState.SELECTED
