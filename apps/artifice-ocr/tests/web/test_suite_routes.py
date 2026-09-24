# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The suite-shell endpoints shell.js calls on every page.

OCR was the only app without them, so its app switcher always read "Suite
status is unavailable" and the theme choice never persisted server-side.
"""

import pytest
import shared_ui.suite


@pytest.fixture(autouse=True)
def _isolated_preferences(tmp_path, monkeypatch):
    target = tmp_path / "ui-preferences.json"
    monkeypatch.setattr(shared_ui.suite, "_preferences_path", lambda: target)


def test_suite_apps_lists_the_suite(client):
    res = client.get("/api/suite/apps")
    assert res.status_code == 200
    apps = res.json()
    assert isinstance(apps, list) and apps


def test_preferences_default_then_persist(client):
    assert client.get("/api/ui/preferences").json()["theme"] == "system"
    res = client.patch("/api/ui/preferences", json={"theme": "dark"})
    assert res.status_code == 200
    assert client.get("/api/ui/preferences").json()["theme"] == "dark"


def test_invalid_preference_is_rejected_not_a_server_error(client):
    assert client.patch("/api/ui/preferences", json={"theme": "neon"}).status_code == 422
    assert client.patch("/api/ui/preferences", json={"colour": "red"}).status_code == 422
