# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared view models for suite navigation and non-sensitive UI preferences."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir

from .handoff import read_discovery

SUITE_APPS: tuple[dict[str, str], ...] = (
    {"slug": "artifice-hub", "name": "Hub", "accent": "#2f7d45"},
    {"slug": "artifice-ocr", "name": "OCR", "accent": "#017259"},
    {"slug": "artifice-draft", "name": "Draft", "accent": "#892254"},
    {"slug": "artifice-transcribe", "name": "Transcribe", "accent": "#715993"},
    {"slug": "artifice-graph", "name": "Graph", "accent": "#066a9c"},
)
DEFAULT_PREFERENCES: dict[str, Any] = {"theme": "system", "reduced_motion": False}
_THEMES = frozenset({"system", "light", "dark"})


def _preferences_path() -> Path:
    return Path(user_data_dir("artifice-suite", "ArtificeSuite")) / "ui-preferences.json"


def get_preferences(path: Path | None = None) -> dict[str, Any]:
    """Return validated preferences, safely falling back to suite defaults."""
    target = path or _preferences_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_PREFERENCES.copy()
    if not isinstance(value, dict):
        return DEFAULT_PREFERENCES.copy()
    return {
        "theme": value.get("theme") if value.get("theme") in _THEMES else "system",
        "reduced_motion": value.get("reduced_motion") is True,
    }


def update_preferences(patch: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Validate and atomically persist an allowed preference patch."""
    unknown = set(patch) - set(DEFAULT_PREFERENCES)
    if unknown:
        raise ValueError(f"Unknown UI preference: {sorted(unknown)[0]}")
    current = get_preferences(path)
    if "theme" in patch:
        if patch["theme"] not in _THEMES:
            raise ValueError("theme must be system, light, or dark")
        current["theme"] = patch["theme"]
    if "reduced_motion" in patch:
        if not isinstance(patch["reduced_motion"], bool):
            raise ValueError("reduced_motion must be a boolean")
        current["reduced_motion"] = patch["reduced_motion"]
    target = path or _preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    with suppress(OSError):
        os.chmod(temporary, 0o600)
    temporary.replace(target)
    return current

def suite_apps() -> list[dict[str, Any]]:
    """Return a JSON-ready, loopback-only suite-switcher view model."""
    result: list[dict[str, Any]] = []
    for definition in SUITE_APPS:
        record = read_discovery(definition["slug"])
        port = record.get("port") if isinstance(record, dict) else None
        running = isinstance(port, int) and 1 <= port <= 65535
        result.append(
            {
                **definition,
                "running": running,
                "installed": None,
                "url": f"http://127.0.0.1:{port}/" if running else None,
            }
        )
    return result
