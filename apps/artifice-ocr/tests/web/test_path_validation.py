# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generic path-validation tests: allowed roots, temp dir, malformed input rejection."""

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

# --------------------------------------------------------------------------- #
# Cause A regression — temp dir from TMPDIR is always an allowed root
# --------------------------------------------------------------------------- #


def test_platform_temp_dir_is_always_an_allowed_root():
    """The platform's own temp directory is an allowed root on every platform.

    This is the invariant that the macOS breakage violated, and it is checked
    unconditionally so that Windows and macOS both assert it rather than
    skipping. Before the fix the list carried a bare ``Path("/tmp")``, which is
    not the platform temp directory on macOS (``/var/folders/…``) and does not
    exist at all on Windows.
    """
    import tempfile

    from shared_ui.path_validation import build_allowed_roots

    temp_root = Path(tempfile.gettempdir()).resolve()
    assert temp_root in [r.resolve() for r in build_allowed_roots("ARTIFICE_OCR_ALLOWED_ROOTS")]


@pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "Reproduces a POSIX-only failure mode. On Windows the platform temp "
        "directory lives under %LOCALAPPDATA%, i.e. below Path.home(), so it is "
        "already covered by the home root and this failure cannot arise. "
        "/var/tmp also does not exist on Windows. The platform-neutral "
        "invariant is asserted unconditionally in the test above."
    ),
)
def test_validate_directory_accepts_temp_dir_from_custom_tmpdir(monkeypatch):
    """A temp directory under a relocated ``TMPDIR`` is accepted.

    This reproduces the macOS failure *on Linux*: on macOS
    ``tempfile.gettempdir()`` returns a per-user directory under
    ``/var/folders/…`` that is neither ``/tmp`` nor ``$HOME``, so without
    ``gettempdir()`` in the roots list every path in a macOS ``tmp_path`` is
    refused. Pointing ``TMPDIR`` at ``/var/tmp`` puts the temp directory outside
    both ``/tmp`` and ``$HOME``, which is the same shape of problem.
    """
    import tempfile

    from artifice_ocr.web.validation import validate_directory

    custom_root = Path("/var/tmp")
    if not custom_root.is_dir():
        pytest.skip("/var/tmp is absent on this POSIX host")

    monkeypatch.setenv("TMPDIR", str(custom_root))
    # setattr, not assignment: monkeypatch restores the cache afterwards, so a
    # later test does not inherit a cleared tempdir.
    monkeypatch.setattr(tempfile, "tempdir", None)

    d = Path(tempfile.mkdtemp(dir=str(custom_root)))
    try:
        # Compare against the RESOLVED path: validate_directory returns str(p)
        # after resolve(). On Linux /var/tmp is a real directory so resolve() is
        # a no-op and either form passes — but on macOS /var is a symlink to
        # /private/var, so the unresolved form fails there and only there.
        assert validate_directory(str(d), "input_dir") == str(d.resolve())
    finally:
        if d.exists():
            d.rmdir()


# --------------------------------------------------------------------------- #
# validate_contained — malformed input rejection (regression)
# --------------------------------------------------------------------------- #
# ``normalise_path`` raises ``ValueError`` for empty/whitespace-only strings
# and, on POSIX, for Windows drive-letter paths.  ``validate_contained`` called
# it outside any try/except, so those errors propagated as unhandled 500s
# rather than 400s.  These tests assert that both failure modes now return
# HTTP 400, matching the pattern ``validate_directory`` already follows.


def test_validate_contained_rejects_empty_string(tmp_path):
    """An empty raw path string must be 400, not an unhandled ValueError."""
    from artifice_ocr.web.validation import validate_contained

    container = str(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        validate_contained("", container, "path")
    assert exc_info.value.status_code == 400
    assert "must not be empty" in str(exc_info.value.detail)


@pytest.mark.skipif(
    os.name != "posix",
    reason="Windows drive-letter detection only activates on POSIX",
)
def test_validate_contained_rejects_windows_drive_letter_on_posix(tmp_path):
    """A Windows drive-letter path on POSIX must be 400, not 500."""
    from artifice_ocr.web.validation import validate_contained

    container = str(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        validate_contained("C:\\Windows", container, "path")
    assert exc_info.value.status_code == 400
    assert "not valid on this platform" in str(exc_info.value.detail)
