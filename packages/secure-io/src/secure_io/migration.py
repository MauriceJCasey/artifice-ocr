# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared legacy-path migration utilities for the Artifice Suite.

Provides two functions extracted from the three real call sites across
``artifice-transcribe`` and ``artifice-graph``:

- ``migrate_legacy_file`` — consolidates transcribe's
  ``_migrate_legacy_db`` (single-file ``shutil.move`` with collision
  awareness).
- ``migrate_legacy_directory`` — consolidates transcribe's
  ``_migrate_legacy_uploads`` (files-only directory migration with
  per-file collision) and graph's ``_resolve_user_data_dir``
  (whole-directory ``shutil.move`` with symlink refusal and
  post-move permission hardening).

The two shapes are deliberately kept as separate functions rather than
one over-parametrised ``migrate_legacy_path()``, because the three real
call sites split cleanly into single-file and directory moves and a
unified function would itself be the kind of over-engineering this
refactor exists to eliminate.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Literal

from secure_io import ensure_restricted


def migrate_legacy_file(
    legacy_path: Path,
    default_path: Path,
    *,
    user_overrode_default: bool,
    logger: logging.Logger,
) -> None:
    """Move *legacy_path* → *default_path* if the user hasn't overridden
    the default and *default_path* doesn't already exist.

    Logs a warning (does **not** raise) if both exist; keeps the existing
    default in place and leaves the legacy file where it is.  No-op if
    *legacy_path* doesn't exist.
    """
    if user_overrode_default:
        return

    if default_path.exists():
        if legacy_path.exists():
            logger.warning(
                "Legacy file found at %s but file already exists at %s. "
                "Using the existing file. To recover data from the legacy "
                "file, copy it manually to the new location.",
                legacy_path,
                default_path,
            )
        return

    if not legacy_path.exists():
        return

    try:
        logger.info("Migrating legacy file from %s to %s", legacy_path, default_path)
        default_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_path), str(default_path))
        logger.info("Migration complete — file is now at %s", default_path)
    except Exception:
        logger.warning(
            "Failed to migrate legacy file from %s to %s",
            legacy_path,
            default_path,
            exc_info=True,
        )


def migrate_legacy_directory(
    legacy_path: Path,
    default_path: Path,
    *,
    user_overrode_default: bool,
    move_mode: Literal["whole_dir", "files_only"],
    collision_is_silent: bool,
    refuse_symlink: bool = False,
    restrict_filename: str | None = None,
    cleanup_empty_legacy: bool = False,
    logger: logging.Logger,
) -> Path:
    """Move *legacy_path* → *default_path* per *move_mode*.

    ``whole_dir``
        ``shutil.move`` on the whole directory.  The only valid collision
        handling is "does *default_path* already exist?" → skip entirely.
        *collision_is_silent* controls whether that skip is logged.

    ``files_only``
        Move only files directly under *legacy_path* (not subdirectories).
        Collision is per-file: if a destination file exists, skip that one
        file (*collision_is_silent* controls whether that's logged) and
        continue with the rest.

    *refuse_symlink*
        If ``True`` and *legacy_path* is a symlink, log a warning and
        return *default_path* without moving anything.  The symlink check
        is performed before any filesystem mutation (TOCTOU-safe —
        checked, not raced).

    *restrict_filename*
        If set, after a successful ``whole_dir`` move, call
        ``secure_io.ensure_restricted(default_path / restrict_filename)``.
        Only meaningful with ``move_mode=\"whole_dir\"``; ignored for
        ``files_only``.

    *cleanup_empty_legacy*
        If ``True`` (only meaningful with ``move_mode=\"files_only\"``),
        attempt to ``rmdir()`` the legacy directory after moving if it is
        now empty (or was already empty/absent).  Swallows ``OSError``.

    Always returns *default_path* — on any exception during the move,
    logs a warning and returns *legacy_path* instead as a fallback (the
    caller's app must still be able to start).
    """
    if user_overrode_default:
        return default_path

    # TOCTOU: symlink check must happen before any filesystem mutation.
    if refuse_symlink and legacy_path.is_symlink():
        logger.warning(
            "Legacy path %s is a symlink — refusing to move it. "
            "Symlink targets are likely outside the app's jurisdiction.",
            legacy_path,
        )
        return default_path

    if move_mode == "whole_dir":
        return _migrate_whole_dir(
            legacy_path,
            default_path,
            collision_is_silent=collision_is_silent,
            restrict_filename=restrict_filename,
            logger=logger,
        )

    # files_only
    return _migrate_files_only(
        legacy_path,
        default_path,
        collision_is_silent=collision_is_silent,
        cleanup_empty_legacy=cleanup_empty_legacy,
        logger=logger,
    )


# -- whole_dir ----------------------------------------------------------------


def _migrate_whole_dir(
    legacy_path: Path,
    default_path: Path,
    *,
    collision_is_silent: bool,
    restrict_filename: str | None,
    logger: logging.Logger,
) -> Path:
    if not legacy_path.exists():
        return default_path

    if default_path.exists():
        if not collision_is_silent:
            logger.info(
                "Default path %s already exists — legacy path %s left in place.",
                default_path,
                legacy_path,
            )
        return default_path

    try:
        logger.info("Migrating user data from %s to %s", legacy_path, default_path)
        default_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_path), str(default_path))
        logger.info("User data migrated successfully to %s", default_path)
    except Exception:
        logger.warning(
            "Failed to migrate user data from %s to %s",
            legacy_path,
            default_path,
            exc_info=True,
        )
        # Fall back to the legacy location — the app must still start.
        return legacy_path

    # The move has already succeeded by this point — a restrict failure
    # must not make the function claim the migration failed, since the
    # data is provably no longer at legacy_path.
    if restrict_filename is not None:
        restrict_target = default_path / restrict_filename
        if restrict_target.exists():
            try:
                ensure_restricted(restrict_target)
            except Exception:
                logger.warning(
                    "Could not re-restrict migrated file at %s",
                    restrict_target,
                    exc_info=True,
                )

    return default_path


# -- files_only ---------------------------------------------------------------


def _migrate_files_only(
    legacy_path: Path,
    default_path: Path,
    *,
    collision_is_silent: bool,
    cleanup_empty_legacy: bool,
    logger: logging.Logger,
) -> Path:
    if not legacy_path.exists():
        if cleanup_empty_legacy:
            # Nothing to clean — the directory doesn't exist.
            pass
        return default_path

    legacy_files = [f for f in legacy_path.iterdir() if f.is_file()]

    if not legacy_files:
        if cleanup_empty_legacy:
            _rmdir_if_empty(legacy_path)
        return default_path

    default_path.mkdir(parents=True, exist_ok=True)

    migrated = 0
    skipped = 0
    failed = 0
    for src in legacy_files:
        dst = default_path / src.name
        if dst.exists():
            if not collision_is_silent:
                logger.warning(
                    "File %s already exists at destination %s — leaving legacy copy in place",
                    src.name,
                    dst,
                )
            skipped += 1
            continue
        try:
            shutil.move(str(src), str(dst))
        except Exception:
            logger.warning(
                "Failed to migrate file %s to %s",
                src.name,
                dst,
                exc_info=True,
            )
            failed += 1
            continue
        migrated += 1

    if migrated > 0:
        logger.info("Migration complete — moved %d file(s) to %s", migrated, default_path)
    if skipped > 0 and not collision_is_silent:
        logger.warning(
            "Skipped %d file(s) due to name collisions. Legacy copies remain at %s",
            skipped,
            legacy_path,
        )
    if failed > 0:
        logger.warning(
            "Failed to migrate %d file(s). Legacy copies remain at %s",
            failed,
            legacy_path,
        )

    if cleanup_empty_legacy:
        _rmdir_if_empty(legacy_path)

    return default_path


def _rmdir_if_empty(path: Path) -> None:
    """Remove *path* if it is empty.  Swallows OSError."""
    try:
        remaining = list(path.iterdir())
        if not remaining:
            path.rmdir()
    except OSError:
        pass
