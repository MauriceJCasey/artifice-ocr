# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for ``secure_io.migration`` — ``migrate_legacy_file`` and
``migrate_legacy_directory``."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest import mock

import pytest
from secure_io.migration import migrate_legacy_directory, migrate_legacy_file

# ---------------------------------------------------------------------------
# migrate_legacy_file
# ---------------------------------------------------------------------------


class TestMigrateLegacyFile:
    """``migrate_legacy_file`` — single-file migration with collision awareness."""

    def test_moves_when_legacy_exists_and_default_does_not(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Legacy file exists, default does not → the file is moved."""
        legacy = tmp_path / "legacy" / "myfile.db"
        default = tmp_path / "default" / "myfile.db"
        legacy.parent.mkdir()
        legacy.write_text("legacy content")

        caplog.set_level(logging.INFO)
        migrate_legacy_file(
            legacy, default, user_overrode_default=False, logger=logging.getLogger("test")
        )

        assert not legacy.exists()
        assert default.exists()
        assert default.read_text() == "legacy content"
        assert "Migrating legacy file" in caplog.text
        assert "Migration complete" in caplog.text

    def test_warns_when_both_exist(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Both exist → warning logged, default untouched, legacy left in place."""
        legacy = tmp_path / "legacy.db"
        default = tmp_path / "default.db"
        legacy.write_text("legacy")
        default.write_text("existing")

        caplog.set_level(logging.WARNING)
        migrate_legacy_file(
            legacy, default, user_overrode_default=False, logger=logging.getLogger("test")
        )

        assert legacy.exists()
        assert default.exists()
        assert default.read_text() == "existing"  # untouched
        assert "Legacy file found" in caplog.text
        assert "file already exists" in caplog.text

    def test_noop_when_legacy_does_not_exist(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Legacy file absent → nothing happens."""
        legacy = tmp_path / "nonexistent.db"
        default = tmp_path / "default.db"

        caplog.set_level(logging.INFO)
        migrate_legacy_file(
            legacy, default, user_overrode_default=False, logger=logging.getLogger("test")
        )

        assert not default.exists()
        assert "Migrating" not in caplog.text

    def test_noop_when_user_overrode_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``user_overrode_default=True`` → no-op regardless of file state."""
        legacy = tmp_path / "legacy.db"
        default = tmp_path / "default.db"
        legacy.write_text("legacy")

        caplog.set_level(logging.INFO)
        migrate_legacy_file(
            legacy, default, user_overrode_default=True, logger=logging.getLogger("test")
        )

        assert not default.exists()
        assert legacy.exists()
        assert "Migrating" not in caplog.text

    def test_noop_when_default_exists_and_legacy_absent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Default exists, legacy doesn't → no-op (nothing to migrate)."""
        legacy = tmp_path / "nonexistent.db"
        default = tmp_path / "default.db"
        default.write_text("existing")

        caplog.set_level(logging.INFO)
        migrate_legacy_file(
            legacy, default, user_overrode_default=False, logger=logging.getLogger("test")
        )

        assert default.read_text() == "existing"
        assert "Migrating" not in caplog.text

    def test_noop_when_both_absent(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Neither legacy nor default exist → no-op."""
        legacy = tmp_path / "nonexistent.db"
        default = tmp_path / "default.db"

        caplog.set_level(logging.INFO)
        migrate_legacy_file(
            legacy, default, user_overrode_default=False, logger=logging.getLogger("test")
        )

        assert not default.exists()
        assert "Migrating" not in caplog.text

    def test_does_not_raise_when_move_fails(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An exception during ``shutil.move`` → logged as warning, not propagated."""
        legacy = tmp_path / "legacy" / "myfile.db"
        default = tmp_path / "default" / "myfile.db"
        legacy.parent.mkdir()
        legacy.write_text("legacy content")

        caplog.set_level(logging.WARNING)
        with mock.patch(
            "secure_io.migration.shutil.move", side_effect=OSError("simulated failure")
        ):
            migrate_legacy_file(
                legacy, default, user_overrode_default=False, logger=logging.getLogger("test")
            )

        # The call must complete without raising.
        assert legacy.exists()
        assert legacy.read_text() == "legacy content"
        assert "Failed to migrate" in caplog.text


# ---------------------------------------------------------------------------
# migrate_legacy_directory — whole_dir (graph shape)
# ---------------------------------------------------------------------------


class TestMigrateLegacyDirectoryWholeDir:
    """``migrate_legacy_directory`` with ``move_mode=\"whole_dir\"`` —
    the graph ``_resolve_user_data_dir`` shape.

    ``collision_is_silent=True``, ``refuse_symlink=True``,
    ``restrict_filename=\"config.json\"``.
    """

    def test_moves_whole_directory(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A legacy directory with a config file is moved to the default location."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "config.json").write_text('{"key": "val"}')
        (legacy / "other.txt").write_text("data")

        caplog.set_level(logging.INFO)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="whole_dir",
            collision_is_silent=True,
            refuse_symlink=True,
            restrict_filename="config.json",
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert not legacy.exists()
        assert default.exists()
        assert (default / "config.json").read_text() == '{"key": "val"}'
        assert (default / "other.txt").read_text() == "data"
        assert "Migrating user data" in caplog.text
        assert "migrated successfully" in caplog.text

    def test_calls_ensure_restricted_after_move(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After a successful whole_dir move, ``ensure_restricted`` is called
        on the restricted file."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "config.json").write_text('{"x": 1}')

        with mock.patch("secure_io.migration.ensure_restricted") as mock_restrict:
            caplog.set_level(logging.INFO)
            result = migrate_legacy_directory(
                legacy,
                default,
                user_overrode_default=False,
                move_mode="whole_dir",
                collision_is_silent=True,
                refuse_symlink=True,
                restrict_filename="config.json",
                logger=logging.getLogger("test"),
            )

        assert result == default
        mock_restrict.assert_called_once_with(default / "config.json")

    def test_does_not_call_ensure_restricted_when_file_absent(self, tmp_path: Path) -> None:
        """If ``restrict_filename`` is set but the file doesn't exist after
        the move, ``ensure_restricted`` is not called."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "other.txt").write_text("data")  # no config.json

        with mock.patch("secure_io.migration.ensure_restricted") as mock_restrict:
            migrate_legacy_directory(
                legacy,
                default,
                user_overrode_default=False,
                move_mode="whole_dir",
                collision_is_silent=True,
                refuse_symlink=True,
                restrict_filename="config.json",
                logger=logging.getLogger("test"),
            )

        mock_restrict.assert_not_called()

    def test_silent_skip_when_destination_exists(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Destination already exists → silent skip, no move, no log."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        default.mkdir()
        (legacy / "config.json").write_text("legacy")
        (default / "config.json").write_text("existing")

        caplog.set_level(logging.INFO)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="whole_dir",
            collision_is_silent=True,
            refuse_symlink=True,
            restrict_filename="config.json",
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert legacy.exists()
        assert (default / "config.json").read_text() == "existing"
        # Silent: no move log, no collision log.
        assert "Migrating" not in caplog.text

    def test_refuses_symlink(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Legacy path is a symlink → refused, not moved, logged with warning."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "config.json").write_text("data")

        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        try:
            os.symlink(str(real_dir), str(legacy))
        except OSError:
            pytest.skip("symlink creation not permitted on this platform/user")

        caplog.set_level(logging.WARNING)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="whole_dir",
            collision_is_silent=True,
            refuse_symlink=True,
            restrict_filename="config.json",
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert not default.exists()
        assert "symlink" in caplog.text
        assert "refusing to move" in caplog.text

    def test_fallback_on_move_exception(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An exception during ``shutil.move`` → logs warning, returns legacy_path."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "config.json").write_text("data")

        caplog.set_level(logging.WARNING)
        with mock.patch("secure_io.migration.shutil.move", side_effect=OSError("disk full")):
            result = migrate_legacy_directory(
                legacy,
                default,
                user_overrode_default=False,
                move_mode="whole_dir",
                collision_is_silent=True,
                refuse_symlink=True,
                restrict_filename="config.json",
                logger=logging.getLogger("test"),
            )

        assert result == legacy
        assert not default.exists()
        assert legacy.exists()
        assert "Failed to migrate" in caplog.text

    def test_restrict_failure_does_not_revert_move(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If ``ensure_restricted`` raises after the move has already
        succeeded, the function must still return *default_path* — the data
        is provably no longer at *legacy_path* and the caller must not be
        told otherwise."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "config.json").write_text("data")
        (legacy / "other.txt").write_text("other")

        caplog.set_level(logging.WARNING)
        with mock.patch(
            "secure_io.migration.ensure_restricted",
            side_effect=OSError("simulated ACL failure"),
        ):
            result = migrate_legacy_directory(
                legacy,
                default,
                user_overrode_default=False,
                move_mode="whole_dir",
                collision_is_silent=True,
                refuse_symlink=True,
                restrict_filename="config.json",
                logger=logging.getLogger("test"),
            )

        # The move succeeded — must return default_path, not legacy_path.
        assert result == default
        assert not legacy.exists()
        assert default.exists()
        assert (default / "config.json").read_text() == "data"
        assert (default / "other.txt").read_text() == "other"
        # Must log a restrict-specific warning, not the generic move-failure one.
        assert "Could not re-restrict" in caplog.text
        assert "Failed to migrate" not in caplog.text

    def test_toctou_symlink_before_mutation(self, tmp_path: Path) -> None:
        """The symlink check must happen before any filesystem mutation.

        Verify that when *legacy_path* is a symlink, no directories or files
        are created as a side effect — the function must return *default_path*
        before reaching the ``mkdir`` call.
        """
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "config.json").write_text("data")

        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        try:
            os.symlink(str(real_dir), str(legacy))
        except OSError:
            pytest.skip("symlink creation not permitted on this platform/user")

        # Record pre-existing paths
        pre_existing = set(tmp_path.rglob("*"))

        migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="whole_dir",
            collision_is_silent=True,
            refuse_symlink=True,
            restrict_filename="config.json",
            logger=logging.getLogger("test"),
        )

        post_existing = set(tmp_path.rglob("*"))
        assert post_existing == pre_existing  # No filesystem mutation at all

    def test_noop_when_user_overrode_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``user_overrode_default=True`` → returns default_path, no move."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "config.json").write_text("data")

        caplog.set_level(logging.INFO)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=True,
            move_mode="whole_dir",
            collision_is_silent=True,
            refuse_symlink=True,
            restrict_filename="config.json",
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert legacy.exists()
        assert not default.exists()
        assert "Migrating" not in caplog.text

    def test_noop_when_legacy_absent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Legacy directory doesn't exist → returns default_path, no move."""
        legacy = tmp_path / "nonexistent"
        default = tmp_path / "default"

        caplog.set_level(logging.INFO)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="whole_dir",
            collision_is_silent=True,
            refuse_symlink=True,
            restrict_filename="config.json",
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert not default.exists()
        assert "Migrating" not in caplog.text


# ---------------------------------------------------------------------------
# migrate_legacy_directory — files_only (transcribe shape)
# ---------------------------------------------------------------------------


class TestMigrateLegacyDirectoryFilesOnly:
    """``migrate_legacy_directory`` with ``move_mode=\"files_only\"`` —
    the transcribe ``_migrate_legacy_uploads`` shape.

    ``collision_is_silent=False``, ``cleanup_empty_legacy=True``.
    """

    def test_moves_files_leaves_subdirectories(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Files are moved; subdirectories are left behind."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "file_a.txt").write_text("A")
        (legacy / "file_b.txt").write_text("B")
        subdir = legacy / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested")

        caplog.set_level(logging.INFO)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="files_only",
            collision_is_silent=False,
            cleanup_empty_legacy=True,
            logger=logging.getLogger("test"),
        )

        assert result == default
        # Files moved
        assert not (legacy / "file_a.txt").exists()
        assert not (legacy / "file_b.txt").exists()
        assert (default / "file_a.txt").read_text() == "A"
        assert (default / "file_b.txt").read_text() == "B"
        # Subdirectory left behind
        assert subdir.exists()
        assert (subdir / "nested.txt").read_text() == "nested"
        # Migration logged
        assert "moved 2 file(s)" in caplog.text

    def test_per_file_collision_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Per-file collision: colliding file skipped with warning, others move."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        default.mkdir()
        (legacy / "file_a.txt").write_text("A-legacy")
        (legacy / "file_b.txt").write_text("B-legacy")
        (default / "file_a.txt").write_text("A-existing")  # collision

        caplog.set_level(logging.WARNING)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="files_only",
            collision_is_silent=False,
            cleanup_empty_legacy=True,
            logger=logging.getLogger("test"),
        )

        assert result == default
        # file_a: collision → left in legacy, default untouched
        assert (legacy / "file_a.txt").exists()
        assert (default / "file_a.txt").read_text() == "A-existing"
        # file_b: no collision → moved
        assert not (legacy / "file_b.txt").exists()
        assert (default / "file_b.txt").read_text() == "B-legacy"
        # Warning logged
        assert "already exists at destination" in caplog.text

    def test_per_file_collision_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``collision_is_silent=True`` → collision skips file without logging."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        default.mkdir()
        (legacy / "file_a.txt").write_text("A-legacy")
        (legacy / "file_b.txt").write_text("B-legacy")
        (default / "file_a.txt").write_text("A-existing")

        caplog.set_level(logging.INFO)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="files_only",
            collision_is_silent=True,
            cleanup_empty_legacy=True,
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert (legacy / "file_a.txt").exists()
        assert (default / "file_a.txt").read_text() == "A-existing"
        assert not (legacy / "file_b.txt").exists()
        # No collision warning — the point of silent mode
        assert "already exists at destination" not in caplog.text
        # Still logs the success for the moved file
        assert "moved 1 file(s)" in caplog.text

    def test_cleanup_empty_legacy_after_migration(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After moving all files, the empty legacy directory is ``rmdir``'d."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "file.txt").write_text("data")

        caplog.set_level(logging.INFO)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="files_only",
            collision_is_silent=False,
            cleanup_empty_legacy=True,
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert not legacy.exists()  # directory was removed

    def test_cleanup_does_not_remove_nonempty_legacy(self, tmp_path: Path) -> None:
        """If legacy still has subdirectories after migration, ``rmdir`` is
        not called (it would raise and be swallowed, but we assert the dir survives)."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "file.txt").write_text("data")
        subdir = legacy / "subdir"
        subdir.mkdir()

        migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="files_only",
            collision_is_silent=False,
            cleanup_empty_legacy=True,
            logger=logging.getLogger("test"),
        )

        assert legacy.exists()
        assert subdir.exists()

    def test_cleanup_when_legacy_already_empty(self, tmp_path: Path) -> None:
        """An already-empty legacy directory is removed by cleanup."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()

        migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="files_only",
            collision_is_silent=False,
            cleanup_empty_legacy=True,
            logger=logging.getLogger("test"),
        )

        assert not legacy.exists()

    def test_cleanup_when_legacy_absent(self, tmp_path: Path) -> None:
        """When legacy doesn't exist at all, cleanup is a silent no-op."""
        legacy = tmp_path / "nonexistent"
        default = tmp_path / "default"

        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="files_only",
            collision_is_silent=False,
            cleanup_empty_legacy=True,
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert not default.exists()

    def test_noop_when_user_overrode_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``user_overrode_default=True`` → returns default_path, no files moved."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "file.txt").write_text("data")

        caplog.set_level(logging.INFO)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=True,
            move_mode="files_only",
            collision_is_silent=False,
            cleanup_empty_legacy=True,
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert legacy.exists()
        assert not default.exists()
        assert "Migrating" not in caplog.text

    def test_noop_when_legacy_absent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Legacy absent → returns default_path, no-op."""
        legacy = tmp_path / "nonexistent"
        default = tmp_path / "default"

        caplog.set_level(logging.INFO)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="files_only",
            collision_is_silent=False,
            cleanup_empty_legacy=False,
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert not default.exists()

    def test_per_file_move_exception_warns_and_continues(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An exception during a single file's ``shutil.move`` is caught,
        logged as a warning, and does not stop migration of other files."""
        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        legacy.mkdir()
        (legacy / "file_a.txt").write_text("A")
        (legacy / "file_b.txt").write_text("B")
        (legacy / "file_c.txt").write_text("C")

        def _move_side_effect(src: str, dst: str, /) -> None:
            if "file_b.txt" in src:
                raise OSError("simulated locked file")
            # Simulate a real shutil.move by renaming
            Path(src).rename(Path(dst))

        caplog.set_level(logging.WARNING)
        with mock.patch("secure_io.migration.shutil.move", side_effect=_move_side_effect):
            result = migrate_legacy_directory(
                legacy,
                default,
                user_overrode_default=False,
                move_mode="files_only",
                collision_is_silent=False,
                cleanup_empty_legacy=True,
                logger=logging.getLogger("test"),
            )

        # Exception must not propagate — we got here.
        assert result == default

        # file_a and file_c migrated successfully
        assert not (legacy / "file_a.txt").exists()
        assert (default / "file_a.txt").read_text() == "A"
        assert not (legacy / "file_c.txt").exists()
        assert (default / "file_c.txt").read_text() == "C"

        # file_b failed → legacy copy remains, not in default
        assert (legacy / "file_b.txt").exists()
        assert (legacy / "file_b.txt").read_text() == "B"
        assert not (default / "file_b.txt").exists()

        # Distinct failure warning logged (not the collision warning)
        assert "Failed to migrate file" in caplog.text
        assert "already exists at destination" not in caplog.text
        assert "Failed to migrate 1 file(s)" in caplog.text

    def test_refuses_symlink_with_files_only(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``refuse_symlink=True`` with ``move_mode=\"files_only\"`` refuses
        the symlink and returns ``default_path`` without moving anything."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "file.txt").write_text("data")

        legacy = tmp_path / "legacy"
        default = tmp_path / "default"
        try:
            os.symlink(str(real_dir), str(legacy))
        except OSError:
            pytest.skip("symlink creation not permitted on this platform/user")

        caplog.set_level(logging.WARNING)
        result = migrate_legacy_directory(
            legacy,
            default,
            user_overrode_default=False,
            move_mode="files_only",
            collision_is_silent=False,
            refuse_symlink=True,
            cleanup_empty_legacy=True,
            logger=logging.getLogger("test"),
        )

        assert result == default
        assert not default.exists()
        assert "symlink" in caplog.text
        assert "refusing to move" in caplog.text
