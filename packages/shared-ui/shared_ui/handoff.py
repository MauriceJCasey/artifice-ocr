# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""File-based handoff mechanism for the Artifice Suite.

Apps write opaque-token handoff packages to a shared platformdirs directory.
The sender writes a JSON manifest + body file, then notifies the receiver
via a localhost URL carrying only the UUID.  The receiver reads, validates,
imports, and deletes the handoff.

Security: all apps bind 127.0.0.1.  The handoff UUID is unguessable.
Manifests expire after 5 minutes.  Source apps are allowlisted.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from platformdirs import user_data_dir

_ALLOWED_SOURCES: frozenset[str] = frozenset(
    {"artifice-ocr", "artifice-draft", "artifice-graph", "artifice-transcribe"}
)
_MAX_AGE_SECONDS: int = 300  # 5 minutes


def _suite_dir() -> Path:
    """Return the shared ArtificeSuite user-data directory."""
    return Path(user_data_dir("artifice-suite", "ArtificeSuite"))


def handoff_dir() -> Path:
    """Return the handoff directory, creating it if missing."""
    p = _suite_dir() / "handoff"
    p.mkdir(parents=True, exist_ok=True)
    return p


def discovery_dir() -> Path:
    """Return the discovery directory, creating it if missing."""
    p = _suite_dir() / "discovery"
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_discovery(slug: str, port: int, pid: int) -> None:
    """Write a ``<slug>.json`` discovery file so other apps can find this one."""
    data: dict[str, object] = {
        "port": port,
        "pid": pid,
        "started": datetime.now(UTC).isoformat(),
    }
    (discovery_dir() / f"{slug}.json").write_text(json.dumps(data))


def read_discovery(slug: str) -> dict[str, object] | None:
    """Read a target app's discovery file, returning ``None`` if not found."""
    path = discovery_dir() / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


class HandoffError(ValueError):
    """A handoff request was rejected, with a safe public message.

    ``public_message`` is set from a string literal at each raise site — it
    is never derived from a wrapped third-party exception — so a caller can
    read it directly instead of calling ``str(e)``, which CodeQL's
    stack-trace-exposure query treats as unsafe. Subclasses ``ValueError``
    so existing ``except ValueError`` call sites keep working unchanged.
    """

    def __init__(self, public_message: str) -> None:
        self.public_message = public_message
        super().__init__(public_message)


def create_handoff(source: str, target: str, body: str, kind: str = "plain-text") -> str:
    """Write a manifest + body file to the handoff directory.

    Returns the UUID token the sender should pass to the receiver.
    """
    if source not in _ALLOWED_SOURCES:
        raise HandoffError(f"Unknown source app: {source}")
    if target not in _ALLOWED_SOURCES:
        raise HandoffError(f"Unknown target app: {target}")

    uid = uuid.uuid4().hex
    d = handoff_dir()

    manifest: dict[str, object] = {
        "version": 1,
        "source": source,
        "target": target,
        "kind": kind,
        "created": datetime.now(UTC).isoformat(),
        "body_file": f"{uid}.txt",
    }
    (d / f"{uid}.json").write_text(json.dumps(manifest), encoding="utf-8")
    (d / f"{uid}.txt").write_text(body, encoding="utf-8")
    return uid


def read_handoff(uuid_str: str, expected_target: str) -> dict[str, str] | None:
    """Read and validate a handoff manifest.

    Returns ``{"source": ..., "kind": ..., "body": ...}`` on success,
    or ``None`` if the handoff is missing, expired, or invalid.
    """
    if not _UUID_HEX_RE.match(uuid_str):
        return None

    d = handoff_dir()
    manifest_path = d / f"{uuid_str}.json"
    if not manifest_path.exists():
        return None

    try:
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(manifest_raw, dict):
        return None
    if manifest_raw.get("version") != 1:
        return None

    source = str(manifest_raw.get("source", ""))
    if source not in _ALLOWED_SOURCES:
        return None
    if manifest_raw.get("target") != expected_target:
        return None

    # Check age — expire after _MAX_AGE_SECONDS
    created_str = str(manifest_raw.get("created", ""))
    try:
        created = datetime.fromisoformat(created_str)
        age = datetime.now(UTC) - created
        if age > timedelta(seconds=_MAX_AGE_SECONDS):
            return None
    except ValueError:
        return None

    # Read body file
    body_file = str(manifest_raw.get("body_file", ""))
    body_path = d / body_file
    if not body_path.exists():
        return None

    body = body_path.read_text(encoding="utf-8")
    return {
        "source": source,
        "kind": str(manifest_raw.get("kind", "plain-text")),
        "body": body,
    }


def delete_handoff(uuid_str: str) -> None:
    """Delete the manifest and body files for a handoff."""
    if not _UUID_HEX_RE.match(uuid_str):
        return
    d = handoff_dir()
    manifest_path = d / f"{uuid_str}.json"
    body_path = d / f"{uuid_str}.txt"
    for path in (manifest_path, body_path):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def cleanup_expired() -> None:
    """Delete handoffs older than ``_MAX_AGE_SECONDS``."""
    d = handoff_dir()
    if not d.exists():
        return
    cutoff = datetime.now(UTC) - timedelta(seconds=_MAX_AGE_SECONDS)
    for mf in d.glob("*.json"):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data.get("created", ""))
            if created < cutoff:
                mf.unlink(missing_ok=True)
                body_file = data.get("body_file", "")
                if body_file:
                    bf = d / str(body_file)
                    bf.unlink(missing_ok=True)
        except (json.JSONDecodeError, ValueError, OSError):
            # Corrupt or unparseable — remove it
            with contextlib.suppress(OSError):
                mf.unlink(missing_ok=True)
