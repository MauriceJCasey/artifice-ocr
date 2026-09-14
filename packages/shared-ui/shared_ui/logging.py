# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reusable rotating-file + console logging setup, shared across the suite's apps.

**Why there is a file handler here.** Each app ships as a windowed executable
(``console=False`` in its ``.spec``), which means ``sys.stderr`` goes nowhere
on Windows. A module that logs to stderr alone produces no diagnostics at all
in that build: a real failure — a non-ASCII page in OCR, a redacted token in
transcribe — reaches the user as nothing but an opaque error with no
surrounding context, and finding the cause means re-running the executable
with its stderr redirected to a file. Everything needed had been logged —
there was simply nowhere for it to land. The rotating file handler is that
somewhere.

Each app instantiates its own :class:`AppLogger` with its own root logger
name, environment variable, filename, and default directory, and exposes its
methods as module-level functions (``log_dir``, ``log_path``,
``setup_logging``, ``reset``, ``get_logger``) so existing call sites do not
change. Configuration state (``_configured``) lives on the instance, not as a
module global here, so that two apps sharing this module in the same process
never mark each other as configured.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s [%(name)-10s] %(levelname)-5s %(message)s"
_DATEFMT = "%H:%M:%S"

# The file gets the full logger name and a date, because a log read days later
# out of context needs both. The console keeps its short, scannable form.
_FILE_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

# ~2 MB x 3 backups. Large enough to hold a long multi-page/multi-job run,
# small enough that a user can attach one to an issue report.
_MAX_BYTES = 2_000_000
_BACKUP_COUNT = 3


class AppLogger:
    """Rotating-file + console logging setup for one app's root logger.

    Instantiate once per app (module-level, at import time) with that app's
    root logger name, log-dir environment variable, log filename, and default
    log directory, then expose the bound methods as that app's public
    ``_logging`` API. See ``artifice_ocr._logging`` and
    ``artifice_transcribe._logging`` for the wiring.
    """

    def __init__(
        self,
        *,
        package_name: str,
        log_dir_env: str,
        log_filename: str,
        default_log_dir: Callable[[], Path],
    ) -> None:
        self._package_name = package_name
        self._log_dir_env = log_dir_env
        self._log_filename = log_filename
        self._default_log_dir = default_log_dir
        self._configured = False

    def log_dir(self) -> Path:
        """Directory the log file lives in.

        Override with this app's log-dir environment variable (the test suite
        does, to stay out of a real developer's log).
        """
        override = os.environ.get(self._log_dir_env, "").strip()
        if override:
            return Path(override)
        return self._default_log_dir()

    def log_path(self) -> Path:
        """Full path of the current log file."""
        return self.log_dir() / self._log_filename

    def _add_file_handler(self, root: logging.Logger, level: int) -> bool:
        """Attach the rotating file handler. Returns False if it could not be made.

        Failing to open a log must never be the reason a run fails, so every
        error here is swallowed — the app degrades to console-only logging,
        which is exactly the behaviour that existed before this handler.

        ``encoding="utf-8"`` is not optional. Without it the handler uses the
        locale codec (cp1252 on a stock Windows install) and raises
        ``UnicodeEncodeError`` on the first umlaut, curly quote, or non-ASCII
        transcript — on the very content most likely to be under
        investigation. A log that cannot record the failing input is worse
        than no log.
        """
        try:
            directory = self.log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                directory / self._log_filename,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError:
            return False
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_FILE_DATEFMT))
        root.addHandler(handler)
        return True

    def _install_exception_hooks(self, logger: logging.Logger) -> None:
        """Route otherwise-unhandled exceptions into the log as well as stderr.

        Both default hooks print to ``sys.stderr``, which the frozen build
        discards. Chaining rather than replacing keeps normal console
        behaviour intact.
        """
        previous_sys_hook = sys.excepthook
        previous_thread_hook = threading.excepthook

        def _sys_hook(exc_type, exc, tb):
            if not issubclass(exc_type, KeyboardInterrupt):
                logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
            previous_sys_hook(exc_type, exc, tb)

        def _thread_hook(args):
            if args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
                logger.critical(
                    "Unhandled exception in thread %s",
                    getattr(args.thread, "name", "?"),
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            previous_thread_hook(args)

        sys.excepthook = _sys_hook
        threading.excepthook = _thread_hook

    def setup_logging(self, level: int = logging.INFO) -> None:
        if self._configured:
            return
        self._configured = True

        root = logging.getLogger(self._package_name)
        root.setLevel(level)

        # ``pythonw.exe`` sets both std streams to None before any of our code
        # runs. Whether ``sys.stderr`` has been restored by this point (e.g.
        # via ``shared_ui.ensure_std_streams()``) depends on the calling app's
        # own entry-point code, not on this module.
        if sys.stderr is not None:
            stream = logging.StreamHandler(sys.stderr)
            stream.setLevel(level)
            stream.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
            root.addHandler(stream)

        if self._add_file_handler(root, level):
            self._install_exception_hooks(root)

        root.propagate = False

    def reset(self) -> None:
        """Drop every handler and allow :meth:`setup_logging` to run again.

        Exists for the test suite, which needs each test to log into its own
        temporary directory rather than inheriting the first test's handler.
        """
        root = logging.getLogger(self._package_name)
        for handler in list(root.handlers):
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
        self._configured = False

    def get_logger(self, name: str) -> logging.Logger:
        self.setup_logging()
        return logging.getLogger(f"{self._package_name}.{name}")
