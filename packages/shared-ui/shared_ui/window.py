# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared native desktop window via pywebview (WebView2 / WKWebView / WebKitGTK).

All pywebview imports are local to ``open_native_window()`` so that users who
do not have a webview backend can still import ``WindowResult``, ``WindowApi``
and ``WindowError`` without pywebview installed.
"""

from __future__ import annotations

from pathlib import Path


class WindowError(Exception):
    """Raised when a native window cannot be opened."""


class WindowResult:
    """Return value from :func:`open_native_window`.

    Carries whether the window opened and, if not, a human-readable reason
    suitable for printing to the user.
    """

    def __init__(self, opened: bool, reason: str = "") -> None:
        self.opened = opened
        self.reason = reason


class WindowApi:
    """Exposed to JS as window.pywebview.api.* after the pywebviewready event.

    Methods run on a pywebview bridge thread; Window.minimize()/destroy()
    marshal to the GUI thread internally on every backend.  The underscore
    attribute is invisible to pywebview's function introspection.
    """

    def __init__(self) -> None:
        self._window = None
        self._maximized = False

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def maximize(self) -> None:
        if self._window is not None:
            self._window.maximize()
            self._maximized = True

    def restore(self) -> None:
        if self._window is not None:
            self._window.restore()
            self._maximized = False

    def toggle_maximize(self) -> None:
        if self._window is not None:
            if self._maximized:
                self.restore()
            else:
                self.maximize()

    def resize(self, width: int, height: int) -> None:
        if self._window is not None:
            self._window.resize(width, height)

    def destroy(self) -> None:
        if self._window is not None:
            self._window.destroy()


def _unblock_frozen_bundle(bundle_dir: Path) -> None:
    """Deprecated compatibility hook; never alter downloaded files.

    Older builds recursively removed Windows download-origin metadata from
    extracted files. That is indistinguishable from security-software
    evasion. Users should use Windows' documented *Unblock* action on the
    downloaded archive, or install a signed distribution, before launching.
    """
    return


def open_native_window(
    url: str,
    *,
    title: str = "Artifice",
    width: int = 1280,
    height: int = 800,
) -> WindowResult:
    """Open *url* in a native desktop window.

    Returns a :class:`WindowResult`.  If the window opened, this call blocks
    until the user closes it and then returns ``opened=True``.  If no webview
    backend is available (headless session, missing system libraries, or
    pywebview not installed) it returns ``opened=False`` with a short human
    reason.

    This function is designed to be called *after* the local server is already
    listening, so the user sees a populated page immediately.
    """
    # ------------------------------------------------------------------
    # Try to import pywebview.  Both the import itself and the subsequent
    # ``webview.start()`` can fail in headless / missing-backend scenarios.
    # We catch every case here so the caller only has one place to handle.
    # ------------------------------------------------------------------
    try:
        import webview  # noqa: PLC0415 — deliberate lazy import
    except ImportError:
        return WindowResult(
            opened=False,
            reason="pywebview is not installed — run `pip install pywebview`.",
        )

    # webview.start() raises WebViewException when no backend is found, and a
    # bare RuntimeError / ImportError when a system dependency (pythonnet,
    # GTK, etc.) is missing at runtime.
    try:
        api = WindowApi()
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
        api._window = webview.create_window(
            title=title,
            url=url,
            width=width,
            height=height,
            resizable=True,
            min_size=(640, 480),
            frameless=True,
            easy_drag=False,  # default is True — window-wide drag would hijack page interactions
            js_api=api,
        )
        # webview.start() blocks until the window is closed.
        webview.start(gui=None)  # let pywebview auto-detect
    except Exception as exc:
        msg = str(exc).strip() or type(exc).__name__
        return WindowResult(opened=False, reason=f"Native window unavailable: {msg}")

    return WindowResult(opened=True, reason="")
