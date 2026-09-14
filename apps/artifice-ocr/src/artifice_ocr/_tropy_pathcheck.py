# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private path validation for absolute photo paths in Tropy JSON-LD imports.

Form-driven classification (NOT ``os.name`` branching).  Every check is
segment-based — ``/etc`` must not match ``/etcfoo``.
"""

from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# closed-vocabulary frozensets
# --------------------------------------------------------------------------- #

MEDIA_SUFFIXES: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"})
# NOTE: .gif is EXCLUDED — the pipeline's SUPPORTED_EXTENSIONS doesn't include
# it; admitting it means items that die at OCR.

POSIX_BLOCKED_ROOTS: frozenset[str] = frozenset(
    {
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/var",
        "/sys",
        "/proc",
        "/dev",
        "/boot",
        "/root",
        "/run",
        "/private/etc",  # macOS symlink-collapse target
        "/private/var",  # macOS symlink-collapse target
    }
)

WINDOWS_BLOCKED_ROOTS: frozenset[str] = frozenset(
    {  # stored lowercase, posix-form
        "c:/windows",
        "c:/program files",
        "c:/program files (x86)",
        "c:/programdata",
        "c:/$recycle.bin",
        "c:/system volume information",
    }
)

BLOCKED_HOME_CHILDREN: frozenset[str] = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        "AppData",
    }
)

MAX_PATH_CHARS: int = 4096

# --------------------------------------------------------------------------- #
# result dataclass
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PhotoPathResult:
    """Result of validating an absolute photo path."""

    resolved: Path
    missing: bool
    is_symlink: bool


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def validate_absolute_photo(
    raw_path: str,
    *,
    home: Path | None = None,
) -> PhotoPathResult:
    """Validate an absolute photo path against the security ruleset.

    Raises ``ValueError`` with a **pre-sanitised** message on rejection
    (never embed resolved / normalised server-computed paths — use
    ``resolved.name`` only).

    Parameters
    ----------
    raw_path : str
        The raw photo path string from the JSON-LD (backslashes already
        normalised to ``/`` by the caller).
    home : Path | None
        Injectable home directory for testing.  Defaults to ``Path.home()``.

    Returns
    -------
    PhotoPathResult
        *resolved* — fully-resolved ``Path`` (symlinks collapsed).
        *missing* — ``True`` when the resolved path does not exist.
        *is_symlink* — ``True`` when the pre-resolution path was a symlink
        (the caller appends a warning — following is never silent).
    """
    # ---- step 1: null byte / max chars ----------------------------------
    if "\x00" in raw_path:
        raise ValueError(f"Photo path contains a null byte: '{Path(raw_path).name}'")
    if len(raw_path) > MAX_PATH_CHARS:
        raise ValueError(
            f"Photo path is too long ({len(raw_path)} characters; max {MAX_PATH_CHARS})"
        )

    # ---- step 2: form classification (done by caller with UNC already
    #     rejected — we only see posix-absolute or windows-absolute here) ---

    is_windows_abs = _is_windows_absolute(raw_path)

    # ---- step 3: normalise, expanduser, detect symlink, resolve ----------
    normalised = Path(raw_path)
    try:
        expanded = normalised.expanduser()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Could not expand user in path '{normalised.name}': {exc}") from exc

    # Symlink detection before resolve (may fail for inaccessible paths)
    try:
        is_symlink = expanded.is_symlink()
    except OSError:
        is_symlink = False

    # ---- step 3.5: blocked-root check on pre-resolved form ---------------
    # MUST run before resolve() so that inaccessible system paths (e.g.
    # /root on a regular user account) are caught by the blocklist rather
    # than dying with a generic "could not resolve" error.
    _check_blocked_roots(normalised, is_windows_abs)

    # ---- step 3.6: resolve (collapses symlinks) --------------------------
    try:
        resolved = expanded.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"Could not resolve path '{normalised.name}': {exc}") from exc

    # ---- step 4: blocked-root check on resolved form ---------------------
    # Catches symlink-collapse targets (e.g. /etc → /private/etc on macOS)
    _check_blocked_roots(resolved, is_windows_abs)

    # ---- step 5: home-children check on resolved -------------------------
    _check_home_children(resolved, home=home)

    # ---- step 6: extension allowlist on resolved suffix ------------------
    suffix = resolved.suffix.lower()
    if suffix not in MEDIA_SUFFIXES:
        raise ValueError(
            f"Unsupported file type '{suffix or '(none)'}' "
            f"for photo '{resolved.name}' — expected one of: "
            f"{_suffix_list_str()}"
        )

    # ---- step 7: symlink following ---------------------------------------
    # resolve() already followed symlinks and the full ruleset ran on the
    # target (steps 4-6).  If the pre-resolved path was a symlink we do NOT
    # reject — the caller appends a warning so following is never silent.

    # ---- step 8: existence check -----------------------------------------
    missing = not resolved.is_file()

    return PhotoPathResult(resolved=resolved, missing=missing, is_symlink=is_symlink)


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #


def _is_windows_absolute(raw_path: str) -> bool:
    """Return True when *raw_path* looks like a Windows absolute path."""
    return len(raw_path) >= 3 and raw_path[1:3] == ":/" and raw_path[0].isalpha()


def _path_parts(path: Path, is_windows: bool) -> tuple[str, ...]:
    """Return a normalised tuple of path parts for segment-based comparison.

    On POSIX a drive-letter absolute Windows path has parts like
    ``('C:', 'Windows', ...)``.  When *is_windows* is True we
    lowercase **every** part so it can compare case-insensitively
    against the lowercased blocked-root parts.
    """
    parts = path.parts
    if is_windows and parts:
        return tuple(p.lower() for p in parts)
    return parts


def _check_blocked_roots(path: Path, is_windows: bool) -> None:
    """Compare the path's leading *parts* against every blocked root.

    Raises ``ValueError`` on match.
    """
    blocked_roots: frozenset[str] = WINDOWS_BLOCKED_ROOTS if is_windows else POSIX_BLOCKED_ROOTS
    pparts = _path_parts(path, is_windows)

    for root_str in blocked_roots:
        root_path = Path(root_str)
        rparts = _path_parts(root_path, is_windows)

        if len(pparts) >= len(rparts) and pparts[: len(rparts)] == rparts:
            raise ValueError(f"Photo path '{path.name}' points into a protected system directory")


def _check_home_children(path: Path, *, home: Path | None = None) -> None:
    """Reject if the component immediately under ``Path.home()`` is in
    ``BLOCKED_HOME_CHILDREN``.

    The *home* parameter is injectable for testing — defaults to
    ``Path.home()``.
    """
    home_dir = home if home is not None else Path.home()
    try:
        rel = path.relative_to(home_dir)
    except ValueError:
        return  # not under home — nothing to check

    parts_below_home = rel.parts
    if parts_below_home and parts_below_home[0] in BLOCKED_HOME_CHILDREN:
        raise ValueError(
            f"Photo path '{path.name}' points into a protected directory under your home folder"
        )


def _suffix_list_str() -> str:
    """Human-readable sorted list of allowed suffixes."""
    return ", ".join(sorted(MEDIA_SUFFIXES))
