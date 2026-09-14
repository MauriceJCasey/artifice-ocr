# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Library-level path validation.

Core ruleset for validating that a user-supplied path resides within permitted
directories.  Raises ``ValueError`` — no web-framework dependency — so it is
usable from the CLI, background threads, and the web layer alike.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

# On POSIX systems, pathlib treats ``C:/Windows`` as a relative path and
# ``resolve()`` prepends the current working directory, which would place it
# inside an allowed root. Detect the drive-letter prefix so it is rejected
# rather than silently landing inside cwd.
_WIN_DRIVE = re.compile(r"^[A-Za-z]:")


class PathValidationError(ValueError):
    """A user-supplied path failed validation, with a safe public message.

    ``public_message`` is set from a string literal at each raise site — it
    is never derived from a wrapped third-party exception — so a caller can
    read it directly instead of calling ``str(e)``/``repr(e)``, which
    CodeQL's stack-trace-exposure query treats as an information leak
    regardless of whether the message is actually sensitive. Subclasses
    ``ValueError`` so existing ``except ValueError`` call sites keep working
    unchanged.
    """

    def __init__(self, public_message: str) -> None:
        self.public_message = public_message
        super().__init__(public_message)


class OutsideAllowedRootsError(PathValidationError):
    """The path resolved outside every allowed root.

    Split out from :class:`PathValidationError` because at least one caller
    (the OCR web layer) needs to react specifically to this case — e.g. to
    add an app-specific hint about the allowed-roots env var — without
    string-matching the message text.
    """


def normalise_path(raw: str, field_name: str) -> str:
    """Normalise a raw path string: strip, replace backslashes, and — on
    POSIX — reject Windows absolute paths before they can be misinterpreted
    as relative.  Raises :class:`PathValidationError` on rejection."""
    normalised = raw.replace("\\", "/").strip()
    if not normalised:
        raise PathValidationError(f"{field_name}: path must not be empty")
    if os.name == "posix" and _WIN_DRIVE.match(normalised):
        raise PathValidationError(
            f"{field_name}: path {normalised!r} is not valid on this platform"
        )
    return normalised


def sanitise_path_component(raw: str, field_name: str = "filename") -> str:
    """Return a safe single-component filename from *raw*.

    Treats backslashes as separators (Windows path support) and rejects
    components that are empty, ``"."`` or ``".."`` after cleaning.  Raises
    :class:`PathValidationError` — no web-framework dependency — on rejection.

    The backslash replacement is load-bearing: on POSIX,
    ``Path("..\\..\\x").name`` returns the whole string because a backslash is
    not a separator there, so a Windows-style name supplied to a POSIX server
    must be normalised first.
    """
    cleaned = Path(raw.replace("\\", "/")).name
    if cleaned in ("", ".", ".."):
        raise PathValidationError(f"Invalid {field_name}: {raw!r}")
    return cleaned


def assert_contained(path: Path, container: Path, *, field_name: str = "path") -> None:
    """Raise :class:`PathValidationError` if *path* resolves outside *container*.

    A narrower, cheaper check than :func:`validate_path` — for verifying a
    path this process just constructed itself (e.g. ``upload_dir / filename``)
    didn't somehow escape the directory it was built under, not for
    validating an arbitrary user-supplied path against a general allowlist.
    """
    resolved = path.resolve()
    base = container.resolve()
    if not (base in resolved.parents or resolved == base):
        raise PathValidationError(f"{field_name}: path traversal detected")


def build_allowed_roots(env_var: str, extra_roots: Iterable[str] = ()) -> list[Path]:
    """Return the set of directory roots permitted for user-supplied paths.

    Roots are resolved at call time so ``cwd`` reflects the server process at
    the moment of the check, not import time. The env var provides the escape
    hatch for external drives, network shares, and any other location an
    individual installation needs.

    ``extra_roots`` is an optional iterable of additional roots supplied by
    the calling application — e.g. a user-approved folder list persisted in
    that app's own config. Entries are ``expanduser()``-and-``resolve()``d the
    same way as env-var entries. Blank entries and entries that fail to
    resolve are ignored rather than raising, so a stale approved folder for an
    unplugged drive cannot break validation of unrelated paths. Omitting the
    argument preserves the previous behaviour exactly.
    """
    roots: list[Path] = [
        Path.home(),
        Path(tempfile.gettempdir()),
        Path("/tmp"),
        Path.cwd(),
    ]
    extra = os.environ.get(env_var, "")
    for raw in extra.split(os.pathsep):
        raw = raw.strip()
        if raw:
            roots.append(Path(raw).expanduser().resolve())
    for raw in extra_roots:
        raw = raw.strip()
        if not raw:
            continue
        try:
            roots.append(Path(raw).expanduser().resolve())
        except Exception:
            # A stale approved folder (e.g. an unplugged external drive) must
            # not turn every path check into an error; skip it.
            continue
    return roots


def validate_path(
    raw: str,
    field_name: str,
    *,
    allowed_roots_env_var: str,
    extra_roots: Iterable[str] = (),
) -> str:
    """Return *raw* as a normalised path string after checking it resides
    within an allowed root directory.  Raises ``ValueError`` on rejection.

    Backslashes are normalised to forward slashes before processing so that a
    Windows-style path supplied from outside the web layer is not
    misinterpreted as a single filename on a POSIX server.

    ``extra_roots`` is passed through to :func:`build_allowed_roots`; omitting
    it preserves the previous behaviour exactly.
    """
    normalised_raw = normalise_path(raw, field_name)
    try:
        p = Path(normalised_raw).expanduser().resolve(strict=False)
    except Exception:
        raise PathValidationError(f"{field_name}: cannot resolve path {raw!r}") from None

    allowed = build_allowed_roots(allowed_roots_env_var, extra_roots)
    for root in allowed:
        resolved_root = root.resolve()
        try:
            relative = p.relative_to(resolved_root)
            break
        except ValueError:
            continue
    else:
        # Does NOT name the allowed roots; they include Path.home().
        raise OutsideAllowedRootsError(
            f"{field_name}: path {raw!r} is outside the directories this "
            f"server is permitted to access"
        )

    # Hidden components are checked *below* the matched root rather than across
    # the whole path, so a project that happens to live under a dotted
    # directory is not rendered unusable by its own parent.
    hidden = [part for part in relative.parts if part.startswith(".") and part not in (".", "..")]
    if hidden:
        raise PathValidationError(
            f"{field_name}: path {raw!r} descends into a hidden "
            f"directory ({hidden[0]!r}). Choose a visible directory."
        )
    return str(p)
