# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared native file-dialog service.

One correct implementation of OS file/folder dialogs for every Artifice app.
The apps used to hand-roll tkinter inside their FastAPI route handlers; this
module replaces that with a single backend-resolving service that reports one
of three outcomes — :attr:`DialogState.SELECTED`, :attr:`DialogState.CANCELLED`
or :attr:`DialogState.UNAVAILABLE` — so a caller can tell "the user chose
nothing" apart from "there is no dialog to show".  The web layers need exactly
that split: they fall back to a text ``prompt()`` only when a dialog is
*unavailable*, never when the user simply cancelled.

Backend precedence:

1. A live pywebview window, reached through ``webview.active_window()`` /
   ``webview.windows``.  pywebview's ``create_file_dialog`` is correctly
   parented, modal and marshalled to the GUI thread by pywebview itself, so it
   is safe to call from any thread.
2. tkinter — **only when the current thread is the main thread**, because
   tkinter is not thread-safe and constructing it off the main thread can hang
   or crash the process (notably on Windows).
3. ``UNAVAILABLE`` with a human-readable reason.

Both GUI toolkits are imported lazily inside functions — never at module scope
— so ``shared_ui`` stays importable in headless server runs where neither is
installed.

Threading contract
------------------
The ``pick_*`` functions are **synchronous**: they block the calling thread
until the user dismisses the dialog.  Never call them directly from an asyncio
event loop — use the ``pick_*_async`` variants, which run the blocking dialog
in a worker thread via :func:`asyncio.to_thread` so the loop (and any SSE
progress stream) keeps flowing.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "DialogResult",
    "DialogState",
    "FileType",
    "pick_files",
    "pick_files_async",
    "pick_folder",
    "pick_folder_async",
    "save_file",
    "save_file_async",
]


class DialogState(StrEnum):
    """Outcome of a file-dialog request.

    ``SELECTED`` — the user chose one or more paths.
    ``CANCELLED`` — the user dismissed the dialog without choosing.
    ``UNAVAILABLE`` — no usable backend; ``reason`` explains why.
    """

    SELECTED = "selected"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


# pywebview's filter parser (``webview.util.parse_file_type``) accepts a
# description of only word characters and spaces, and raises ``ValueError`` on
# anything else.  A description that fails that rule must not reach the
# backend: the backend is chosen by the runtime environment, so a hyphenated
# label would otherwise pass on a developer's tkinter machine and raise inside
# the frozen app a user is running.  Validate at construction so the failure is
# identical everywhere, including in tests.
_DESCRIPTION_RE = re.compile(r"[\w ]+")


@dataclass(frozen=True)
class FileType:
    """One entry in a dialog's file-type filter.

    ``description`` is the human-readable label (e.g. ``"Text files"``).
    ``patterns`` are glob patterns without a directory, e.g.
    ``("*.txt", "*.md")``.  A single pattern may be passed as a bare string.

    ``description`` is validated at construction against pywebview's filter
    rule: only word characters and spaces (letters, digits, underscore,
    space) are permitted.  Hyphens and other punctuation raise
    :class:`ValueError` here, rather than failing later and only on the
    pywebview backend.
    """

    description: str
    patterns: str | Sequence[str] = ()

    def __post_init__(self) -> None:
        if not _DESCRIPTION_RE.fullmatch(self.description):
            raise ValueError(
                f"invalid file-type description {self.description!r}: "
                "descriptions may contain only word characters and spaces"
            )
        if isinstance(self.patterns, str):
            object.__setattr__(self, "patterns", (self.patterns,))
        object.__setattr__(self, "patterns", tuple(self.patterns))


@dataclass(frozen=True)
class DialogResult:
    """Result of a file-dialog request.

    ``state`` is one of :class:`DialogState`.  ``paths`` is the selection as
    :class:`pathlib.Path` objects — a single-element tuple for folder/save
    dialogs, possibly several for a multi-select open dialog — and is empty for
    ``CANCELLED`` and ``UNAVAILABLE``.  ``reason`` carries a human-readable
    explanation and is populated only for ``UNAVAILABLE``.
    """

    state: DialogState
    paths: tuple[Path, ...] = ()
    reason: str = ""

    @property
    def path(self) -> Path | None:
        """The single selected path, or ``None`` when nothing was selected.

        Convenience for single-select dialogs (folder/save); for a multi-select
        open dialog read :attr:`paths` directly.
        """
        return self.paths[0] if self.paths else None

    def as_dict(self) -> dict[str, str | list[str]]:
        """Serialise to the wire shape the web layers emit.

        Returns ``{"state": ..., "paths": [...], "reason": ...}`` — the shared
        file-dialog contract.  ``state`` is the enum's string value, ``paths``
        are the selected paths as strings (empty unless ``SELECTED``), and
        ``reason`` is the raw explanation (empty unless ``UNAVAILABLE``).
        """
        return {
            "state": self.state.value,
            "paths": [str(p) for p in self.paths],
            "reason": self.reason,
        }


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _live_webview_window():
    """Return the live pywebview window, or ``None`` when none is open.

    Imports pywebview lazily so headless installs never pay for it.  ``None``
    is returned (rather than raising) when pywebview is missing or its GUI
    loop has not started.  Once the loop is running, the active window is
    returned when one exists, otherwise the most recently created window in
    ``webview.windows``.
    """
    try:
        import webview  # noqa: PLC0415 — lazy import, see module docstring
    except ImportError:
        return None

    # A GUI loop only exists after webview.start() succeeds.  If it failed (or
    # was never called), guilib stays None and any entry in webview.windows is
    # a stale object that create_file_dialog would block on for up to 20 s.
    if getattr(webview, "guilib", None) is None:
        return None

    try:
        window = webview.active_window()
        if window is not None:
            return window
    except Exception:
        pass

    windows = getattr(webview, "windows", None)
    return windows[-1] if windows else None


def _select_backend() -> tuple[str, object | None, str]:
    """Choose a backend in precedence order.

    Returns ``(backend, handle, reason)`` where ``backend`` is ``"webview"``,
    ``"tkinter"`` or ``""`` (unavailable).  ``handle`` is the live pywebview
    window for the webview backend and ``None`` otherwise; ``reason`` is
    populated only when unavailable.
    """
    window = _live_webview_window()
    if window is not None:
        return "webview", window, ""

    if not _on_main_thread():
        return (
            "",
            None,
            "no native file dialog is available: there is no live window, and "
            "tkinter requires the main thread",
        )

    try:
        import tkinter  # noqa: F401 — availability probe only
    except ImportError:
        return "", None, "no native file dialog is available: tkinter is not installed"
    return "tkinter", None, ""


def _to_tk_filetypes(file_types: Sequence[FileType]) -> list[tuple[str, str]]:
    """Convert :class:`FileType` entries to tkinter's ``(label, patterns)`` form."""
    return [(ft.description, " ".join(ft.patterns)) for ft in file_types]


def _to_webview_filetypes(file_types: Sequence[FileType]) -> tuple[str, ...]:
    """Convert :class:`FileType` entries to pywebview's ``"label (*.a;*.b)"`` form."""
    return tuple(f"{ft.description} ({';'.join(ft.patterns)})" for ft in file_types)


def _run_webview_dialog(
    window,
    *,
    mode: str,
    initial_dir: str,
    file_types: Sequence[FileType],
    multiple: bool,
    default_name: str,
) -> tuple[str, ...]:
    import webview  # noqa: PLC0415 — lazy import, see module docstring

    if mode == "files":
        dialog_type = webview.FileDialog.OPEN
        allow_multiple = multiple
        save_filename = ""
    elif mode == "folder":
        dialog_type = webview.FileDialog.FOLDER
        allow_multiple = False
        save_filename = ""
    elif mode == "save":
        dialog_type = webview.FileDialog.SAVE
        allow_multiple = False
        save_filename = default_name
    else:
        raise AssertionError(f"unknown dialog mode {mode!r}")

    # pywebview's create_file_dialog has no ``title`` parameter.
    result = window.create_file_dialog(
        dialog_type=dialog_type,
        directory=initial_dir,
        allow_multiple=allow_multiple,
        save_filename=save_filename,
        file_types=_to_webview_filetypes(file_types),
    )
    # None == user cancelled; otherwise a tuple of selected paths.
    return () if result is None else tuple(result)


def _run_tk_dialog(
    mode: str,
    *,
    title: str,
    initial_dir: str,
    file_types: Sequence[tuple[str, str]],
    multiple: bool,
    default_name: str,
) -> tuple[str, ...]:
    import tkinter as tk  # noqa: PLC0415 — lazy import, see module docstring
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if mode == "files":
            if multiple:
                return tuple(
                    filedialog.askopenfilenames(
                        title=title,
                        initialdir=initial_dir or None,
                        filetypes=list(file_types),
                    )
                )
            single = filedialog.askopenfilename(
                title=title,
                initialdir=initial_dir or None,
                filetypes=list(file_types),
            )
            return (single,) if single else ()
        if mode == "folder":
            directory = filedialog.askdirectory(title=title, initialdir=initial_dir or None)
            return (directory,) if directory else ()
        if mode == "save":
            chosen = filedialog.asksaveasfilename(
                title=title,
                initialdir=initial_dir or None,
                initialfile=default_name or None,
                filetypes=list(file_types),
            )
            return (chosen,) if chosen else ()
        raise AssertionError(f"unknown dialog mode {mode!r}")
    finally:
        root.destroy()


def _pick(
    mode: str,
    *,
    title: str,
    initial_dir: str | os.PathLike[str] | None,
    file_types: Sequence[FileType] | None,
    multiple: bool,
    default_name: str,
) -> DialogResult:
    directory = os.fspath(initial_dir) if initial_dir is not None else ""
    backend, handle, reason = _select_backend()
    if backend == "":
        return DialogResult(DialogState.UNAVAILABLE, reason=reason)

    filters = tuple(file_types) if file_types else ()
    try:
        if backend == "webview":
            paths = _run_webview_dialog(
                handle,
                mode=mode,
                initial_dir=directory,
                file_types=filters,
                multiple=multiple,
                default_name=default_name,
            )
        else:
            paths = _run_tk_dialog(
                mode,
                title=title,
                initial_dir=directory,
                file_types=_to_tk_filetypes(filters),
                multiple=multiple,
                default_name=default_name,
            )
    except Exception as exc:
        msg = str(exc).strip() or type(exc).__name__
        return DialogResult(DialogState.UNAVAILABLE, reason=f"Native dialog failed: {msg}")

    if not paths:
        return DialogResult(DialogState.CANCELLED)
    return DialogResult(DialogState.SELECTED, paths=tuple(Path(p) for p in paths))


def pick_files(
    *,
    title: str = "Select files",
    initial_dir: str | os.PathLike[str] | None = None,
    file_types: Sequence[FileType] | None = None,
    multiple: bool = True,
) -> DialogResult:
    """Open a file picker and return the selected path(s).

    ``multiple=True`` (the default) allows selecting several files at once,
    matching OCR's batch workflow.  Blocking — see the module docstring.
    """
    return _pick(
        "files",
        title=title,
        initial_dir=initial_dir,
        file_types=file_types,
        multiple=multiple,
        default_name="",
    )


def pick_folder(
    *,
    title: str = "Select a folder",
    initial_dir: str | os.PathLike[str] | None = None,
) -> DialogResult:
    """Open a folder picker.  Single selection.  Blocking."""
    return _pick(
        "folder",
        title=title,
        initial_dir=initial_dir,
        file_types=None,
        multiple=False,
        default_name="",
    )


def save_file(
    *,
    title: str = "Save",
    initial_dir: str | os.PathLike[str] | None = None,
    default_name: str = "",
    file_types: Sequence[FileType] | None = None,
) -> DialogResult:
    """Open a save dialog.  Single selection.  Blocking.

    The chosen path is a location the user *named*, not one that has been
    created — this module never touches the filesystem.
    """
    return _pick(
        "save",
        title=title,
        initial_dir=initial_dir,
        file_types=file_types,
        multiple=False,
        default_name=default_name,
    )


async def pick_files_async(
    *,
    title: str = "Select files",
    initial_dir: str | os.PathLike[str] | None = None,
    file_types: Sequence[FileType] | None = None,
    multiple: bool = True,
) -> DialogResult:
    """Awaitable :func:`pick_files` — runs the blocking dialog off the loop."""
    return await asyncio.to_thread(
        pick_files,
        title=title,
        initial_dir=initial_dir,
        file_types=file_types,
        multiple=multiple,
    )


async def pick_folder_async(
    *,
    title: str = "Select a folder",
    initial_dir: str | os.PathLike[str] | None = None,
) -> DialogResult:
    """Awaitable :func:`pick_folder` — runs the blocking dialog off the loop."""
    return await asyncio.to_thread(
        pick_folder,
        title=title,
        initial_dir=initial_dir,
    )


async def save_file_async(
    *,
    title: str = "Save",
    initial_dir: str | os.PathLike[str] | None = None,
    default_name: str = "",
    file_types: Sequence[FileType] | None = None,
) -> DialogResult:
    """Awaitable :func:`save_file` — runs the blocking dialog off the loop."""
    return await asyncio.to_thread(
        save_file,
        title=title,
        initial_dir=initial_dir,
        default_name=default_name,
        file_types=file_types,
    )
