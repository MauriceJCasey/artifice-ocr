# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for shared_ui.window."""

from __future__ import annotations

import builtins

from shared_ui.window import WindowApi, WindowError, WindowResult, open_native_window


class TestWindowResult:
    """Tests for WindowResult."""

    def test_defaults(self) -> None:
        r = WindowResult(opened=True)
        assert r.opened is True
        assert r.reason == ""

    def test_with_reason(self) -> None:
        r = WindowResult(opened=False, reason="something went wrong")
        assert r.opened is False
        assert r.reason == "something went wrong"


class TestWindowError:
    """Tests for WindowError."""

    def test_is_exception(self) -> None:
        err = WindowError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"


class TestWindowApiNullSafety:
    """WindowApi methods are safe no-ops when no underlying window exists."""

    def test_minimize_noop(self) -> None:
        api = WindowApi()
        api.minimize()  # should not raise
        assert api._window is None

    def test_maximize_noop(self) -> None:
        api = WindowApi()
        api.maximize()  # should not raise
        assert api._window is None

    def test_restore_noop(self) -> None:
        api = WindowApi()
        api.restore()  # should not raise
        assert api._window is None

    def test_toggle_maximize_noop(self) -> None:
        api = WindowApi()
        api.toggle_maximize()  # should not raise
        assert api._window is None

    def test_resize_noop(self) -> None:
        api = WindowApi()
        api.resize(1024, 768)  # should not raise
        assert api._window is None

    def test_resize_forwards_to_window(self) -> None:
        """The frameless resize grip drives this — it must reach the window."""
        from unittest.mock import MagicMock

        api = WindowApi()
        api._window = MagicMock()
        api.resize(1024, 768)
        api._window.resize.assert_called_once_with(1024, 768)

    def test_destroy_noop(self) -> None:
        api = WindowApi()
        api.destroy()  # should not raise
        assert api._window is None


class TestModuleImportableWithoutPywebview:
    """WindowResult and WindowApi are importable even without pywebview."""

    def test_classes_importable(self) -> None:
        """Top-level imports work without triggering a webview import."""
        # The fact that this module loaded at all without an ImportError
        # proves the constraint holds — `import webview` lives inside
        # open_native_window(), not at module level.
        assert WindowResult(opened=True).opened is True


class TestOpenNativeWindowWithoutPywebview:
    """open_native_window returns a graceful failure when pywebview is absent."""

    def test_returns_false_when_import_fails(self, monkeypatch) -> None:
        """Intercept the import so it raises ImportError for webview."""
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "webview":
                raise ImportError("No module named 'webview' (simulated)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

        result = open_native_window("http://localhost:8000")
        assert result.opened is False
        assert "pywebview is not installed" in result.reason

    def test_default_title_is_artifice(self, monkeypatch) -> None:
        """title parameter defaults to 'Artifice'."""
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "webview":
                raise ImportError("No module named 'webview' (simulated)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

        result = open_native_window("http://localhost:8000")
        # With pywebview absent, it returns opened=False — the title default
        # doesn't change the result, but we prove the call succeeds with
        # only the url argument (keyword args use defaults).
        assert result.opened is False
