# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Path validation for web endpoints.

Thin web-facing wrapper around the library-level ``artifice_ocr.validation``
ruleset.  Translates ``ValueError`` into ``HTTPException(400)`` at the
boundary so the core module stays free of any web-framework dependency.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from shared_ui.path_validation import OutsideAllowedRootsError, PathValidationError, normalise_path

from ..validation import validate_path


def validate_directory(raw: str, field_name: str) -> str:
    """Return *raw* as a normalised path string after checking it resides
    within an allowed root directory.  Raises HTTP 400 on rejection.

    Backslashes are normalised to forward slashes before processing so that a
    Windows-style path supplied from a browser is not misinterpreted as a
    single filename on a POSIX server (pathlib does not treat ``\\`` as a
    separator on POSIX, and ``Path("C:\\Windows").resolve()`` would land
    inside the current working directory instead of being rejected).
    """
    try:
        return validate_path(raw, field_name)
    except OutsideAllowedRootsError:
        # The library-level message deliberately omits the env-var hint that
        # the web layer includes. Reconstruct it so the caller sees the full
        # guidance — distinguished by exception TYPE, not by string-matching
        # the message text.
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_name}: path {raw!r} is outside the directories "
                f"this server is permitted to access. Approve the folder in "
                f"Settings, or widen the allowed roots with the "
                f"ARTIFICE_OCR_ALLOWED_ROOTS environment variable."
            ),
        ) from None
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.public_message) from None


def validate_contained(
    raw: str,
    container: str,
    field_name: str,
    *,
    must_exist: bool = True,
) -> str:
    """Return *raw* as a normalised path after checking it resolves within
    *container*.  Raises HTTP 400 on rejection.

    Backslashes are normalised to forward slashes (same rationale as
    ``validate_directory``).

    ``must_exist=False`` resolves non-strictly, so a path that is inside
    *container* but absent is returned rather than refused. A caller wanting to
    answer 404 for a missing file needs this: with strict resolution the
    nonexistent path raises first and every miss becomes a 400, which collapses
    "not there" and "not permitted" into one answer.

    **Containment is still decided after resolution either way**, so relaxing
    existence does not relax the security check — and it must not be reordered
    to test existence first. An endpoint that answered 404 for a path outside
    *container* would become an existence oracle for arbitrary filesystem
    paths.
    """
    try:
        normalised_raw = normalise_path(raw, field_name)
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.public_message) from None
    try:
        p = Path(normalised_raw).expanduser().resolve(strict=must_exist)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name}: cannot resolve path {raw!r}",
        ) from None

    base = Path(container).expanduser().resolve()
    try:
        p.relative_to(base)
    except ValueError:
        # Names neither the resolved path nor the container's absolute path;
        # both disclose server filesystem layout to an unauthenticated caller.
        raise HTTPException(
            status_code=400,
            detail=(f"{field_name}: path {raw!r} is not within the permitted output directory"),
        ) from None

    return str(p)
