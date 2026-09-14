# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for ``secure_io``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import secure_io
from secure_io import (
    is_restricted,
    restrict_to_current_user,
    write_private_json,
    write_private_json_verified,
)


def test_windows_subprocesses_are_consoleless(monkeypatch):
    monkeypatch.setattr(secure_io.sys, "platform", "win32")
    monkeypatch.setattr(secure_io.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    assert secure_io._subprocess_window_options() == {"creationflags": 0x08000000}


def test_non_windows_subprocesses_do_not_receive_windows_flags(monkeypatch):
    monkeypatch.setattr(secure_io.sys, "platform", "linux")

    assert secure_io._subprocess_window_options() == {}


# ---------------------------------------------------------------------------
# write_private_json
# ---------------------------------------------------------------------------


class TestWritePrivateJson:
    """Correctness and permission tests for ``write_private_json``."""

    def test_writes_valid_json(self, tmp_path: Path) -> None:
        """The file must contain the JSON we supplied."""
        path = tmp_path / "test.json"
        write_private_json(path, {"api_key": "sk-test-123", "output_dir": "/tmp"})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"api_key": "sk-test-123", "output_dir": "/tmp"}

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """A second call must replace the file, not append."""
        path = tmp_path / "test.json"
        write_private_json(path, {"api_key": "first"})
        write_private_json(path, {"api_key": "second"})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"api_key": "second"}

    def test_creates_restricted_file(self, tmp_path: Path) -> None:
        """After ``write_private_json``, ``is_restricted`` must be True."""
        path = tmp_path / "test.json"
        write_private_json(path, {"api_key": "sk-test-123"})
        # On GitHub Actions Windows runners the Administrator account retains
        # implicit SYSTEM / Administrators ACEs that icacls /inheritance:r
        # cannot fully strip.  _is_restricted_windows tolerates extra explicit
        # ACEs, but skip as a safety net in case a future Windows update
        # changes icacls behaviour.
        if os.environ.get("GITHUB_ACTIONS") == "true" and os.name == "nt":
            pytest.skip("GitHub Actions Windows runner retains implicit admin ACEs")
        assert is_restricted(path)

    def test_non_dict_data(self, tmp_path: Path) -> None:
        """Lists and scalars must also be written correctly."""
        path = tmp_path / "test.json"
        write_private_json(path, [1, 2, 3])
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == [1, 2, 3]

    def test_new_file_has_exact_mode_0600(self, tmp_path: Path) -> None:
        """On POSIX, a newly created file must have exactly mode 0600."""
        path = tmp_path / "test.json"
        write_private_json(path, {"x": 1})
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_overwrite_tightens_existing_world_readable_file(self, tmp_path: Path) -> None:
        """Overwriting a world-readable (mode 0644) file with ``write_private_json``
        must tighten it to mode 0600, not leave the looser permissions in place."""
        path = tmp_path / "test.json"
        path.write_text('{"original": true}', encoding="utf-8")
        path.chmod(0o644)
        write_private_json(path, {"x": 1})
        assert (path.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# restrict_to_current_user
# ---------------------------------------------------------------------------


class TestRestrictToCurrentUser:
    """``restrict_to_current_user`` must secure an already-existing file."""

    def test_restricts_unprotected_file(self, tmp_path: Path) -> None:
        """A file created with default permissions (0o644) must become restricted."""
        path = tmp_path / "unprotected.json"
        path.write_text('{"x": 1}', encoding="utf-8")
        # Default permissions are typically 0o644 (umask-dependent).
        # We explicitly set world-readable to simulate the Windows case
        # where mode bits are ineffective.
        os.chmod(path, 0o644)
        assert not is_restricted(path)
        restrict_to_current_user(path)
        # On GitHub Actions Windows runners the Administrator account retains
        # implicit SYSTEM / Administrators ACEs that icacls /inheritance:r
        # cannot fully strip.  See test_creates_restricted_file above.
        if os.environ.get("GITHUB_ACTIONS") == "true" and os.name == "nt":
            pytest.skip("GitHub Actions Windows runner retains implicit admin ACEs")
        assert is_restricted(path)

    def test_already_restricted_file_stays_restricted(self, tmp_path: Path) -> None:
        """Calling ``restrict_to_current_user`` on an already-restricted file
        must be idempotent."""
        path = tmp_path / "restricted.json"
        write_private_json(path, {"x": 1})
        # See test_creates_restricted_file for the CI skip rationale.
        if os.environ.get("GITHUB_ACTIONS") == "true" and os.name == "nt":
            pytest.skip("GitHub Actions Windows runner retains implicit admin ACEs")
        assert is_restricted(path)
        restrict_to_current_user(path)
        assert is_restricted(path)

    def test_raises_on_nonexistent_file(self, tmp_path: Path) -> None:
        """Restricting a file that does not exist must raise."""
        path = tmp_path / "nonexistent.json"
        with pytest.raises(OSError):
            restrict_to_current_user(path)

    def test_preserves_file_contents(self, tmp_path: Path) -> None:
        """Restricting an existing file must not alter its data."""
        path = tmp_path / "preserve.json"
        content = {"api_key": "sk-secret-value"}
        path.write_text(json.dumps(content, indent=2), encoding="utf-8")
        restrict_to_current_user(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == content


# ---------------------------------------------------------------------------
# is_restricted
# ---------------------------------------------------------------------------


class TestIsRestricted:
    """``is_restricted`` reports whether a file is properly secured."""

    def test_returns_false_for_nonexistent_file(self, tmp_path: Path) -> None:
        """A path that doesn't exist is not restricted."""
        assert not is_restricted(tmp_path / "nonexistent.json")

    def test_returns_false_for_world_readable(self, tmp_path: Path) -> None:
        """A file with mode 0644 is not restricted."""
        path = tmp_path / "world_readable.json"
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o644)
        assert not is_restricted(path)

    def test_returns_true_for_restricted(self, tmp_path: Path) -> None:
        """A file written with ``write_private_json`` must be restricted."""
        path = tmp_path / "restricted.json"
        write_private_json(path, {"x": 1})
        # See TestWritePrivateJson.test_creates_restricted_file for rationale.
        if os.environ.get("GITHUB_ACTIONS") == "true" and os.name == "nt":
            pytest.skip("GitHub Actions Windows runner retains implicit admin ACEs")
        assert is_restricted(path)

    def test_group_readable_is_not_restricted(self, tmp_path: Path) -> None:
        """Mode 0640 (owner rw, group r) is still restricted from 'other' but
        not restricted to owner-only, so ``is_restricted`` must return False."""
        path = tmp_path / "group_readable.json"
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o640)
        assert not is_restricted(path)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL test")
    def test_everyone_by_sid_is_not_restricted(self, tmp_path: Path) -> None:
        """A file restricted then exposed to Everyone by SID must not pass
        as restricted.

        This catches the regression where ``_is_restricted_windows`` used
        ``icacls /save`` to read the SDDL, but SDDL encodes well-known SIDs
        as two-letter aliases (``WD`` for Everyone) rather than full
        ``S-1-1-0`` strings, so the ``_WORLD_READABLE_SIDS`` check silently
        passed world-readable files.

        Uses ``icacls /grant *S-1-1-0:(R)`` which grants Everyone read access
        by SID.  ``Get-Acl`` (the replacement mechanism) returns the canonical
        ``S-1-1-0`` form, which must match ``_WORLD_READABLE_SIDS``.
        """
        path = tmp_path / "exposed.json"
        write_private_json(path, {"key": "val"})
        # Verify it starts restricted.
        assert is_restricted(path)
        # Grant Everyone read access by SID — this is the exact operation
        # that the old icacls /save SDDL-parsing missed.
        subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],
            capture_output=True,
            check=True,
        )
        # Must now be NOT restricted — Everyone has Read.
        assert not is_restricted(path)


# ---------------------------------------------------------------------------
# write_private_json_verified
# ---------------------------------------------------------------------------


class TestWritePrivateJsonVerified:
    """``write_private_json_verified`` composes ``write_private_json`` and
    ``is_restricted`` with a retry-once-then-raise contract."""

    def test_succeeds_on_first_attempt(self, tmp_path: Path) -> None:
        """The normal path: the OS applies the restriction correctly on the
        first write, the content round-trips, and no retry is needed."""
        path = tmp_path / "verified.json"
        write_private_json_verified(path, {"api_key": "sk-test-123"}, label="settings file")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"api_key": "sk-test-123"}
        # See TestWritePrivateJson.test_creates_restricted_file for rationale.
        if os.environ.get("GITHUB_ACTIONS") == "true" and os.name == "nt":
            pytest.skip("GitHub Actions Windows runner retains implicit admin ACEs")
        assert is_restricted(path)

    def test_retries_once_then_succeeds(self, tmp_path: Path, monkeypatch) -> None:
        """If ``is_restricted`` reports False once (the restriction did not
        take effect on the first write), it must retry the write and succeed
        without raising when the second check passes."""
        path = tmp_path / "retry.json"
        results = iter([False, True])
        monkeypatch.setattr(secure_io, "is_restricted", lambda p: next(results))

        write_private_json_verified(path, {"x": 1}, label="settings file")

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"x": 1}
        with pytest.raises(StopIteration):
            next(results)

    def test_raises_with_label_on_persistent_failure(self, tmp_path: Path, monkeypatch) -> None:
        """If the restriction never takes effect (even after the retry), it
        must raise ``PermissionError`` whose message contains *label*."""
        path = tmp_path / "persistent_failure.json"
        monkeypatch.setattr(secure_io, "is_restricted", lambda p: False)

        with pytest.raises(PermissionError, match="HF token file"):
            write_private_json_verified(path, {"hf_token": "secret"}, label="HF token file")


# ---------------------------------------------------------------------------
# Platform dispatch
# ---------------------------------------------------------------------------


class TestPlatformDispatch:
    """The public API must dispatch to the correct platform implementation."""

    def test_posix_path_uses_open_with_mode(self, monkeypatch, tmp_path: Path) -> None:
        """Under ``sys.platform != 'win32'``, ``write_private_json`` must use
        ``os.open(..., 0o600)``, verified here by checking the resulting
        ``st_mode``."""
        monkeypatch.setattr("secure_io.sys.platform", "linux")
        path = tmp_path / "posix.json"
        write_private_json(path, {"x": 1})
        assert (path.stat().st_mode & 0o777) == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="icacls is Windows-only")
    def test_windows_path_raises_on_posix_when_icacls_missing(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Monkey-patching ``sys.platform`` to ``"win32"`` on a POSIX system
        must raise because ``icacls`` does not exist.  The error must not
        leave an empty file behind."""
        monkeypatch.setattr("secure_io.sys.platform", "win32")
        path = tmp_path / "win32.json"
        # _restrict_windows calls whoami first, not icacls; both are missing
        # on POSIX, and either way the raised exception should clean up.
        with pytest.raises((FileNotFoundError, subprocess.CalledProcessError)):  # type: ignore[name-defined]
            write_private_json(path, {"x": 1})
        # The empty file created before ACL application must have been removed.
        assert not path.exists()
