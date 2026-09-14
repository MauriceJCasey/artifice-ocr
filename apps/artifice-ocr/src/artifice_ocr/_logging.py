# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Centralized logging for the OCR pipeline.

Usage:
    from artifice_ocr._logging import get_logger
    log = get_logger("ocr")
    log.info("Starting OCR for %s", filename)

Thin wrapper around :class:`shared_ui.logging.AppLogger`. See that module for
the shared rotating-file + console implementation; this file only supplies
OCR's app-specific configuration (root logger name, environment variable,
filename, and default log directory).
"""

from pathlib import Path

from shared_ui.logging import AppLogger

LOG_DIR_ENV = "ARTIFICE_OCR_LOG_DIR"
LOG_FILENAME = "artifice-ocr.log"

# Defaults to ``~/.artifice_ocr/logs``, beside the settings and history the
# app already keeps there, so a user has one place to look.
_logger = AppLogger(
    package_name="artifice_ocr",
    log_dir_env=LOG_DIR_ENV,
    log_filename=LOG_FILENAME,
    default_log_dir=lambda: Path.home() / ".artifice_ocr" / "logs",
)

log_dir = _logger.log_dir
log_path = _logger.log_path
setup_logging = _logger.setup_logging
reset = _logger.reset
get_logger = _logger.get_logger
