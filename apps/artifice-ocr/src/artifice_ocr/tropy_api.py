# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Safe client for Tropy's loopback-only Developer API.

Tropy exposes its API only when the user enables ``Developer API`` in its
preferences (or starts Tropy with ``--port``).  Stable releases use port 2019;
preview/beta releases use 2029.  This module discovers both without ever
accepting a host name from a request, verifies the currently open project, and
uses Tropy's note endpoint so Tropy itself constructs and indexes note state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

import httpx

from . import config
from .tropy_db import resolve_project_db_path, tropy_config_dir

_DEFAULT_PORTS = (2019, 2029)
_TIMEOUT = 2.0


class TropyAPIError(RuntimeError):
    """A user-actionable Developer API failure."""


@dataclass(frozen=True)
class TropyConnection:
    port: int
    project_id: str
    project_name: str
    project_db: Path
    version: str
    project_prefix: str = "/project/current"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _clean_port(value: Any) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def candidate_ports() -> list[int]:
    """Return unique candidate ports, most explicit/recent first."""
    values: list[Any] = [config.get("tropy_api_port", 0)]
    try:
        state = json.loads((tropy_config_dir() / "state.json").read_text(encoding="utf-8"))
        if isinstance(state, dict):
            values.append(state.get("port"))
    except (OSError, ValueError, TypeError):
        pass
    values.extend(_DEFAULT_PORTS)

    ports: list[int] = []
    for value in values:
        port = _clean_port(value)
        if port is not None and port not in ports:
            ports.append(port)
    return ports


def _canonical_project(path: str | Path) -> Path:
    return resolve_project_db_path(path).resolve()


def _same_project(left: Path, right: Path) -> bool:
    # Windows paths are case-insensitive. ``casefold`` is harmless on the two
    # POSIX platforms and also handles mocked Windows paths in cross-platform CI.
    return str(left).replace("\\", "/").casefold() == str(right).replace("\\", "/").casefold()


def _project_name(path: Path) -> str:
    return path.parent.stem if path.name == "project.tpy" else path.stem


def connect(project_path: str | Path) -> TropyConnection:
    """Find Tropy's API and prove its current project is ``project_path``."""
    expected = _canonical_project(project_path)
    saw_api = False
    wrong_project: Path | None = None

    with httpx.Client(timeout=_TIMEOUT, trust_env=False, follow_redirects=False) as client:
        for port in candidate_ports():
            try:
                # Tropy <= 1.17 reports the current project at the API root and
                # exposes /project/* routes. Tropy >= 1.18 additionally exposes
                # named-project routes such as /project/current/*. Start with
                # the root because it is the common discovery endpoint.
                response = client.get(f"http://127.0.0.1:{port}/")
            except httpx.HTTPError:
                continue
            saw_api = True
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
                actual = _canonical_project(payload["project"])
            except (KeyError, TypeError, ValueError, OSError):
                continue
            if not _same_project(expected, actual):
                wrong_project = actual
                continue

            # Prefer the named-project routes when the running Tropy supports
            # them. Do not follow redirects: on 1.18 the legacy routes redirect
            # to these routes, while on 1.17 this probe simply returns 404.
            project_prefix = "/project"
            project_id = "current"
            try:
                named = client.get(f"http://127.0.0.1:{port}/project/current/")
                if named.status_code == 200:
                    named_payload = named.json()
                    named_project = _canonical_project(named_payload["project"])
                    if _same_project(expected, named_project):
                        project_prefix = "/project/current"
                        project_id = str(named_payload.get("id") or "current")
            except (httpx.HTTPError, KeyError, TypeError, ValueError, OSError):
                pass
            return TropyConnection(
                port=port,
                project_id=project_id,
                project_name=_project_name(actual),
                project_db=actual,
                version=str(payload.get("version") or "unknown"),
                project_prefix=project_prefix,
            )

    if wrong_project is not None:
        raise TropyAPIError(
            f"Tropy has '{_project_name(wrong_project)}' open; open "
            f"'{_project_name(expected)}' and try again"
        )
    if saw_api:
        raise TropyAPIError("Tropy's Developer API responded, but no project is open")
    raise TropyAPIError(
        "Tropy's Developer API is unavailable. In Tropy Preferences, enable "
        "Developer API, then try again"
    )


def note_html(text: str) -> str:
    """Convert plain OCR text to conservative paragraph HTML for Tropy."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "".join(f"<p>{escape(line)}</p>" if line else "<p><br></p>" for line in lines)


def normalise_note_text(text: str) -> str:
    """Return the comparison form exposed by Tropy's plain-text API.

    Tropy stores our paragraph HTML correctly, but its API flattens paragraph
    and hard-break boundaries to runs of spaces in the returned ``text``
    field. Collapse all whitespace so a multiline result is recognised as the
    same note after Tropy's HTML-to-text conversion.
    """
    return " ".join(text.split())


class TropyAPIClient:
    """Operations bound to one already-verified Tropy project.

    Owns one persistent HTTP connection for its whole lifetime instead of
    opening and tearing down a fresh one per call. A batch operation
    (checking hundreds of photos for a large Tropy send) previously paid a
    full connect/close cycle for every single request; reusing one
    connection removes that overhead entirely. Call :meth:`close` (or use
    as a context manager) once the batch is done.
    """

    def __init__(self, connection: TropyConnection):
        self.connection = connection
        self._client = httpx.Client(timeout=_TIMEOUT, trust_env=False, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TropyAPIClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, f"{self.connection.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise TropyAPIError("Lost the connection to Tropy") from exc
        return response

    def verify_current(self) -> None:
        """Recheck the target immediately before a write."""
        current = connect(self.connection.project_db)
        if current.port != self.connection.port:
            raise TropyAPIError("Tropy's Developer API changed; preview again")

    def photo(self, photo_id: int) -> dict | None:
        response = self._request("GET", f"{self.connection.project_prefix}/photos/{photo_id}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise TropyAPIError(f"Tropy could not inspect photo {photo_id}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TropyAPIError("Tropy returned an invalid photo response") from exc
        return payload if isinstance(payload, dict) else None

    def note_text(self, note_id: int) -> str | None:
        response = self._request("GET", f"{self.connection.project_prefix}/notes/{note_id}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise TropyAPIError(f"Tropy could not inspect note {note_id}")
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload.get("text") if isinstance(payload, dict) else None

    def has_identical_note(self, photo: dict, text: str) -> bool:
        expected = normalise_note_text(text)
        for raw_id in photo.get("notes") or []:
            try:
                existing = self.note_text(int(raw_id))
            except (TypeError, ValueError):
                continue
            if existing is not None and normalise_note_text(existing) == expected:
                return True
        return False

    def create_note(self, photo_id: int, text: str, language: str) -> list[int]:
        response = self._request(
            "POST",
            f"{self.connection.project_prefix}/notes",
            data={"photo": str(photo_id), "html": note_html(text), "language": language},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise TropyAPIError(f"Tropy rejected the note for photo {photo_id}")
        try:
            ids = response.json().get("id", [])
        except (ValueError, AttributeError) as exc:
            raise TropyAPIError("Tropy returned an invalid note response") from exc
        return [int(value) for value in ids]
