# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json

import pytest
from shared_ui.suite import DEFAULT_PREFERENCES, get_preferences, suite_apps, update_preferences


def test_preferences_default_when_missing(tmp_path):
    assert get_preferences(tmp_path / "missing.json") == DEFAULT_PREFERENCES


def test_preferences_round_trip_and_validate(tmp_path):
    path = tmp_path / "prefs.json"
    assert update_preferences({"theme": "dark", "reduced_motion": True}, path) == {
        "theme": "dark",
        "reduced_motion": True,
    }
    assert json.loads(path.read_text()) == {"theme": "dark", "reduced_motion": True}
    with pytest.raises(ValueError, match="Unknown"):
        update_preferences({"secret": "never"}, path)
    with pytest.raises(ValueError, match="theme"):
        update_preferences({"theme": "sepia"}, path)


def test_suite_apps_only_returns_safe_loopback_urls(monkeypatch):
    monkeypatch.setattr("shared_ui.suite.read_discovery", lambda slug: {"port": 8123})
    apps = suite_apps()
    assert len(apps) == 5
    assert all(app["url"] == "http://127.0.0.1:8123/" for app in apps)
    assert {app["slug"] for app in apps} == {
        "artifice-hub",
        "artifice-ocr",
        "artifice-draft",
        "artifice-transcribe",
        "artifice-graph",
    }


@pytest.mark.parametrize("record", [{"port": 0}, {"port": 70000}, {"port": "8123"}, None])
def test_suite_apps_rejects_invalid_ports(monkeypatch, record):
    monkeypatch.setattr("shared_ui.suite.read_discovery", lambda slug: record)
    assert all(app["url"] is None and not app["running"] for app in suite_apps())
