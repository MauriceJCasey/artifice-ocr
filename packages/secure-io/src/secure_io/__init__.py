# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Write JSON files with OS-appropriate access controls.

``write_private_json(path, data)`` persists *data* as JSON so that only the
current user can read the file.

- **POSIX**: uses ``os.open(..., 0o600)``, which creates the file with
  restricted permissions atomically — the file is never world-readable.
- **Windows**: creates the file empty, applies an ACL via ``icacls``, verifies
  the ACL (via ``Get-Acl``, for locale-independent SID comparison), and only
  then writes the contents.  The file exists empty and unprotected between
  creation and the ACL call, but no secret is exposed because the file is
  still empty during that window.

``restrict_to_current_user(path)`` applies the same restriction to an
**existing** file, which closes the live-defect window for installations that
already have unprotected files on disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path


def _subprocess_window_options() -> dict[str, int]:
    """Keep Windows security helpers from flashing console windows."""
    if sys.platform != "win32":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def write_private_json(path: Path, data: object) -> None:
    """Persist *data* as JSON at *path*, restricting access to the current user.

    The secret never touches an unprotected file:
    - POSIX: mode 0600 is set atomically at creation time via ``os.open(..., 0o600)``.
    - Windows: the file is created empty, the ACL is applied and verified, and
      only then are the contents written.
    """
    if sys.platform == "win32":
        _write_private_json_windows(path, data)
    else:
        _write_private_json_posix(path, data)


def restrict_to_current_user(path: Path) -> None:
    """Restrict *path* so that only the current user can read or write it.

    POSIX: ``os.chmod(path, 0o600)``.
    Windows: strip inherited ACEs and grant the current user's SID read+write.
    """
    if sys.platform == "win32":
        _restrict_windows(path)
    else:
        os.chmod(path, 0o600)
        if not is_restricted(path):
            raise PermissionError(
                f"Failed to restrict permissions on {path}: "
                f"st_mode is {oct(path.stat().st_mode & 0o777)}"
            )


def write_private_json_verified(path: Path, data: object, *, label: str = "file") -> None:
    """Write *data* as private JSON to *path*, verifying the restriction
    actually took effect and retrying once before giving up.

    ``write_private_json`` should already restrict access at creation time,
    but callers that treat the secret as genuinely sensitive (API tokens,
    inference credentials, anything written to a per-user settings file)
    have historically wanted a belt-and-suspenders check: read back
    ``is_restricted`` immediately, retry the write once if it somehow
    wasn't applied, and raise rather than silently leave a secret on an
    unprotected file. This was independently reimplemented three times
    across the suite before being extracted here.

    Raises ``PermissionError`` if the file still isn't restricted after
    one retry. *label* appears in that error message (e.g. "settings
    file", "HF token file") to keep it specific to the caller.
    """
    write_private_json(path, data)
    if not is_restricted(path):
        write_private_json(path, data)
        if not is_restricted(path):
            raise PermissionError(f"Failed to secure {label} after retry: {path}")


def is_restricted(path: Path) -> bool:
    """Return ``True`` if *path* is readable/writable only by the current user.

    POSIX: checks ``st_mode & 0o777 == 0o600``.
    Windows: verifies via ``Get-Acl`` (PowerShell) that the ACL grants the
    current user read+write access, no inherited ACEs are present, and no
    well-known world-readable SIDs appear.
    """
    if not path.exists():
        return False
    if sys.platform == "win32":
        return _is_restricted_windows(path)
    else:
        return (path.stat().st_mode & 0o777) == 0o600


def ensure_restricted(path: Path) -> None:
    """Check whether *path* is restricted and repair it if not.

    This is a load-time guard: it warns and continues on failure rather than
    raising, because a repair failure must never prevent the app from
    starting.  (A filesystem without permission support, such as exFAT, will
    hit the repair failure path; the app still loads, the file remains
    unprotected, and the warning documents the trade-off.)

    If *path* does not exist this is a silent no-op — there is nothing to
    repair and ``is_restricted`` returns ``False`` for non-existent paths.
    """
    import logging

    if not path.exists():
        return
    try:
        if not is_restricted(path):
            restrict_to_current_user(path)
    except Exception:
        logging.warning("Could not restrict permissions on %s — continuing anyway", path)


# ---------------------------------------------------------------------------
# POSIX implementation
# ---------------------------------------------------------------------------


def _write_private_json_posix(path: Path, data: object) -> None:
    """Write *data* as JSON at *path*, ensuring mode 0600 afterward.

    ``os.open(..., 0o600)`` makes the file private when created; ``os.chmod``
    afterward tightens an already-existing file that was upgraded from a looser
    mode (e.g. a file that shipped world-readable in an earlier version).
    """
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Windows implementation
# ---------------------------------------------------------------------------


def _write_private_json_windows(path: Path, data: object) -> None:
    """Create empty file, apply ACL, verify, then write.

    The file exists empty and unprotected between its creation and the ACL
    application — no secret is exposed during that window because the file is
    still empty.
    """
    # Create an empty file first.
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
    os.close(fd)
    try:
        _restrict_windows(path)
    except Exception:
        # If we cannot protect the file, remove the empty file we created
        # and fail loudly.  Silently writing a plaintext secret is exactly
        # the bug being fixed here, so a hard failure is the intended
        # trade-off.  (A filesystem without ACL support, such as exFAT,
        # will hit this.)
        with suppress(OSError):
            path.unlink()
        raise
    _write_json(path, data)


def _restrict_windows(path: Path) -> None:
    """Apply a restrictive ACL to *path* on Windows.

    Uses ``icacls`` (ships with Windows) to strip inherited ACEs and grant
    the current user read+write access.  The user is addressed by SID, not
    username, so the ACL works on non-English Windows installs.
    """
    sid = _get_current_user_sid()
    _run_icacls(
        path,
        [
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:(R,W)",
        ],
    )
    # Verify the ACL took effect before returning.
    ok, reason = _windows_acl_verdict(path)
    if not ok:
        raise PermissionError(
            f"Failed to verify ACL on {path} — the file may still be unprotected. {reason}"
        )


# Well-known security principals whose presence in an ACL means the file is
# effectively world-readable.  These are matched by SID using the canonical
# form returned by ``Get-Acl``, which is locale-independent and always emits
# full SID strings (``icacls /save`` emits two-letter SDDL aliases like
# ``WD`` for Everyone, so matching SID strings against its output silently
# passes world-readable files — the same class of defect as matching
# localised display names).
_WORLD_READABLE_SIDS = frozenset(
    {
        "S-1-1-0",  # Everyone
        "S-1-5-32-545",  # BUILTIN\Users
        "S-1-5-11",  # NT AUTHORITY\Authenticated Users
        "S-1-5-32-546",  # BUILTIN\Guests
    }
)


def _run_powershell(cmd: str) -> subprocess.CompletedProcess[str]:
    """Run *cmd* under Windows PowerShell, falling back to PowerShell 7.

    Two environment defences cover different reasons a normal PowerShell
    invocation may fail to reach ``Get-Acl``:

    - **PSModulePath stripped** — the variable is inherited from the parent
      process, and a value aimed at a different PowerShell edition (as CI
      runners set) leaves Windows PowerShell unable to find its own bundled
      modules.  Removing it lets PowerShell rebuild its default.
    - **pwsh fallback** — if Windows PowerShell is absent or broken,
      PowerShell 7 runs the identical command.

    ``-NonInteractive`` guarantees no prompt can block the call.

    Raises the last ``FileNotFoundError`` / ``CalledProcessError`` if every
    candidate fails, so the caller can still report *why*.
    """
    env = os.environ.copy()
    env.pop("PSModulePath", None)

    last_exc: Exception | None = None
    for exe in ("powershell", "pwsh"):
        try:
            return subprocess.run(
                [
                    exe,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    cmd,
                ],
                capture_output=True,
                text=True,
                check=True,
                env=env,
                **_subprocess_window_options(),
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _get_acl_via_powershell(path: Path) -> list[dict[str, object]]:
    """Return parsed ACE entries for *path* from ``Get-Acl`` via PowerShell.

    Each returned dict has the keys ``sid`` (str), ``rights`` (comma-separated
    str, e.g. ``"Read, Synchronize"``), ``is_inherited`` (bool), and
    ``access_type`` (``"Allow"`` or ``"Deny"``).

    ``Get-Acl`` returns canonical SIDs and exposes ``IsInherited`` directly,
    avoiding both the localised-display-name problem and the SDDL-alias
    problem that ``icacls`` and ``icacls /save`` suffer from respectively.
    """
    # Pipe-separated fields, one line per ACE.
    #
    # Two things here are load-bearing and were each got wrong once:
    #
    # 1. This must NOT be an f-string.  PowerShell's -f operator uses the same
    #    {0}{1} placeholder syntax as Python's str.format, so an f-string
    #    consumes them and the format string collapses to the constant
    #    "0|1|2|3" — every ACE then reports identical junk and verification
    #    rejects a correctly restricted file.
    # 2. IdentityReference.Value returns an NTAccount *display name*
    #    ("BUILTIN\\Administrators"), which is localised — the very bug this
    #    function exists to avoid.  .Translate(SecurityIdentifier) is what
    #    yields the canonical SID ("S-1-5-32-544").  Translate() throws for
    #    an unresolvable account, so it is guarded; such an ACE is reported
    #    with its raw value and simply will not match a well-known SID.
    #
    # -LiteralPath avoids glob interpretation; embedded single quotes are
    # doubled, which is PowerShell's escape inside a single-quoted string.
    ps_path = str(path).replace("'", "''")
    cmd = (
        # Load Get-Acl's module explicitly rather than relying on autoload.
        # On the GitHub windows-latest runner autoload fails outright —
        # "the module could not be loaded" — and Get-Acl then exits 1, which
        # made every ACL verification fail and stopped the apps saving
        # settings at all.  An explicit Import-Module is not redundant with
        # Explicit module loading is independent of PSModulePath; the
        # latter is cleared above so Windows PowerShell can rebuild its
        # default search path. SilentlyContinue because a
        # session that already has the module loaded is fine.
        "Import-Module Microsoft.PowerShell.Security -ErrorAction SilentlyContinue; "
        "Get-Acl -LiteralPath '" + ps_path + "' | "
        "Select-Object -ExpandProperty Access | ForEach-Object { "
        "$id = $_.IdentityReference; "
        "try { $sid = $id.Translate([System.Security.Principal.SecurityIdentifier]).Value } "
        "catch { $sid = $id.Value }; "
        "'{0}|{1}|{2}|{3}' -f $sid, $_.FileSystemRights, $_.IsInherited, $_.AccessControlType }"
    )
    result = _run_powershell(cmd)
    entries: list[dict[str, object]] = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        entries.append(
            {
                "sid": parts[0],
                "rights": parts[1],
                "is_inherited": parts[2] == "True",
                "access_type": parts[3] if len(parts) > 3 else "Allow",
            }
        )
    return entries


def _is_restricted_windows(path: Path) -> bool:
    """Check the ACL on *path* using ``Get-Acl`` via PowerShell.

    Returns ``True`` when:
    - No inherited ACEs are present (``IsInherited`` is ``False`` for every entry).
    - No well-known world-readable SID (Everyone, Users,
      Authenticated Users, Guests) has a non-Deny ACE that includes Read access.
    - At least one explicit Allow ACE grants Read+Write or FullControl.

    ``Get-Acl`` returns canonical SIDs (locale-independent) and exposes
    ``IsInherited`` directly, avoiding both the SDDL-alias problem (where
    ``icacls /save`` encodes ``Everyone`` as ``WD`` rather than ``S-1-1-0``)
    and the localised-display-name problem (where ``icacls`` displays
    ``Jeder`` on German, ``Tout le monde`` on French).

    Additional explicit ACEs for SYSTEM and Administrators are tolerated
    because on Administrator accounts Windows retains them even after
    ``/inheritance:r``, and those principals already have full machine access.
    """
    return _windows_acl_verdict(path)[0]


def _format_ace(entry: dict[str, object]) -> str:
    """Render one ACE compactly for a diagnostic message."""
    inherited = " inherited" if entry["is_inherited"] else ""
    return f"[{entry['access_type']} {entry['sid']} ({entry['rights']}){inherited}]"


def _windows_acl_verdict(path: Path) -> tuple[bool, str]:
    """Return ``(is_restricted, reason)`` for *path*'s ACL on Windows.

    The reason is what makes a failure actionable.  A verification that can
    only say "no" is indistinguishable from one that could not read the ACL
    at all, and the caller turns both into a hard failure that stops the app
    saving settings — so the two cases must be told apart in the message.
    """
    try:
        entries = _get_acl_via_powershell(path)
    except FileNotFoundError:
        return False, "could not run powershell (not found on PATH)."
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip().replace("\n", " ")[:300]
        return False, f"Get-Acl failed (exit {exc.returncode}): {stderr}"

    if not entries:
        return False, "Get-Acl returned no ACEs."

    seen = " ".join(_format_ace(e) for e in entries)

    has_explicit_allow_rw = False
    for entry in entries:
        sid = entry["sid"]
        rights = str(entry["rights"])
        is_inherited = bool(entry["is_inherited"])
        access_type = str(entry["access_type"])

        # Inherited ACEs must not be present — we strip them in _restrict_windows.
        if is_inherited:
            return False, f"inherited ACE survived /inheritance:r. ACEs: {seen}"

        if access_type == "Deny":
            continue

        # Reject if a world-readable SID has any Read access.
        if sid in _WORLD_READABLE_SIDS and "Read" in rights:
            return False, f"world-readable SID {sid} has Read. ACEs: {seen}"

        # At least one Allow ACE must grant Read+Write (or FullControl).
        if "FullControl" in rights or ("Read" in rights and "Write" in rights):
            has_explicit_allow_rw = True

    if not has_explicit_allow_rw:
        return False, f"no Allow ACE grants Read+Write. ACEs: {seen}"

    return True, "ok"


def _get_current_user_sid() -> str:
    """Return the current user's SID string (``S-1-5-...``).

    Uses ``whoami /user``, which is built into every supported Windows version.
    """
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        **_subprocess_window_options(),
    )
    # Output format:  "DOMAIN\\username","S-1-5-21-..."
    parts = result.stdout.strip().rsplit(",", 1)
    if len(parts) < 2:
        raise RuntimeError(f"Unexpected whoami output: {result.stdout!r}")
    sid = parts[-1].strip().strip('"')
    if not sid.startswith("S-1-"):
        raise RuntimeError(f"Unexpected SID format from whoami: {sid!r}")
    return sid


def _run_icacls(path: Path, args: list[str]) -> None:
    """Run ``icacls`` with *args* and raise on failure."""
    subprocess.run(
        ["icacls", str(path)] + args,
        capture_output=True,
        text=True,
        check=True,
        **_subprocess_window_options(),
    )


def _write_json(path: Path, data: object) -> None:
    """Write *data* as JSON to *path* (no permission handling)."""
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
