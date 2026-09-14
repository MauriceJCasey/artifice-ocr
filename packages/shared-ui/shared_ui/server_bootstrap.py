# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared local-server bootstrap utilities.

Port discovery, server-thread startup, and startup-failure reporting — extracted
from ``artifice-ocr`` so every app in the suite can share one implementation.
"""

from __future__ import annotations

import os
import socket
import sys
import threading


def free_port() -> int:
    """Return an available TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_available(port: int) -> bool:
    """Return True if *port* can be bound on 127.0.0.1 right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_for_server(port: int, *, timeout: float = 10.0) -> bool:
    """Block until a TCP listener accepts on *port* or *timeout* seconds elapse.

    ``uvicorn`` starts in a background thread and takes a moment to bind the
    port.  This poll loop bridges the gap so callers can reliably determine
    whether the server came up before reporting failure.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(("127.0.0.1", port))
                return True
            except OSError:
                _time.sleep(0.1)
    return False


def start_server_thread(app, port: int) -> tuple[threading.Thread, list[BaseException]]:
    """Start *app* via uvicorn in a daemon thread on *port*.

    Returns the thread and a shared ``errors`` list that callers should inspect
    after ``wait_for_server`` to determine whether startup succeeded.
    """
    import uvicorn

    errors: list[BaseException] = []

    def _serve() -> None:
        try:
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return thread, errors


def report_startup_failure(
    app_name: str,
    port: int,
    thread,
    errors: list[BaseException],
    *,
    timeout: float = 10.0,
) -> None:
    """Print an error and show a best-effort dialog when the server did not start.

    The ``tkinter`` dialog is wrapped in ``try/except Exception`` because some
    environments (server-only, headless, CI) have no display or no tkinter.
    """
    if errors:
        detail = f"{type(errors[0]).__name__}: {errors[0]}"
    elif thread.is_alive():
        detail = f"No response within {timeout:g}s, though the server thread is still running."
    else:
        detail = "The server thread exited without ever starting to listen."
    message = (
        f"{app_name}'s local server could not start on port {port}.\n\n"
        f"{detail}\n\n"
        f"Close any other {app_name} window and try again."
    )
    print(f"ERROR: {message}")
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(f"{app_name} — server did not start", message)
        root.destroy()
    except Exception:
        pass


def ensure_std_streams() -> None:
    """Replace ``sys.stdout`` / ``sys.stderr`` with ``os.devnull`` when either is
    ``None``.

    ``pythonw.exe`` on Windows sets both streams to ``None``, and uvicorn's
    logging formatter calls ``.isatty()`` on them — which raises
    ``AttributeError`` and crashes the server before it can start.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115
