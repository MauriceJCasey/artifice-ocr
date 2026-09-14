# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the user-approved folder list (``approved_folders``).

An approved folder is an explicit, user-granted extension of the allowed-roots
list. The user picks a folder once through the native folder dialog (the
consent step); it persists to settings.json; and path validation accepts paths
beneath it. The security guarantees of the underlying validator — traversal
checks, the hidden-directory rule, and the POSIX drive-letter rejection — are
not weakened: an approved folder only adds a root, it never exempts a path
from the existing checks.
"""

from __future__ import annotations

import os

import pytest
from artifice_ocr import config
from artifice_ocr.web import server
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # config.save_user_settings() always targets ~/.artifice_ocr/settings.json
    # by design, so any test that reaches it must redirect the module constant.
    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")

    config.reset()
    config.load_config()

    with TestClient(server.app) as c:
        yield c


# --------------------------------------------------------------------------- #
# config round-trip and write validation
# --------------------------------------------------------------------------- #


def test_get_config_returns_approved_folders(client):
    body = client.get("/api/config").json()
    assert "approved_folders" in body
    assert body["approved_folders"] == []


def test_set_config_accepts_existing_directory(client, tmp_path):
    folder = tmp_path / "tropy"
    folder.mkdir()
    res = client.post("/api/config", json={"approved_folders": [str(folder)]})
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert str(folder) in config.get("approved_folders")


def test_set_config_rejects_nonexistent_directory(client, tmp_path):
    missing = tmp_path / "missing"
    res = client.post("/api/config", json={"approved_folders": [str(missing)]})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "missing" in detail
    # nothing persisted
    assert config.get("approved_folders") == []


def test_set_config_rejects_non_list(client):
    res = client.post("/api/config", json={"approved_folders": "/tmp"})
    assert res.status_code == 400
    assert "list" in res.json()["detail"].lower()


def test_set_config_rejects_blank_entry(client, tmp_path):
    res = client.post("/api/config", json={"approved_folders": ["   "]})
    assert res.status_code == 400
    assert "not a valid folder path" in res.json()["detail"]


def test_approved_folders_persists_across_config_reload(client, tmp_path):
    folder = tmp_path / "tropy"
    folder.mkdir()
    res = client.post("/api/config", json={"approved_folders": [str(folder)]})
    assert res.status_code == 200

    # Simulate a server restart: reload config and re-apply persisted settings,
    # mirroring the sequence in artifice_ocr.web.runtime.RunState.__init__.
    config.reset()
    config.load_config()
    config.apply_overrides(config.load_user_settings())
    assert str(folder) in config.get("approved_folders")


# --------------------------------------------------------------------------- #
# validation: approved folders extend the roots without weakening the rules
# --------------------------------------------------------------------------- #


def test_approved_folder_makes_path_valid(client):
    """A path outside every default root is accepted once its folder is approved."""
    from artifice_ocr.validation import validate_path

    approved = "/opt/_artifice_approved_root"
    target = f"{approved}/project/x.tpy"

    with pytest.raises(ValueError, match="is outside the directories"):
        validate_path(target, "path")

    config.apply_overrides({"approved_folders": [approved]})
    assert os.path.isabs(validate_path(target, "path"))


def test_approved_folder_does_not_defeat_hidden_directory_rule(client):
    from artifice_ocr.validation import validate_path

    approved = "/opt/_artifice_approved_root"
    config.apply_overrides({"approved_folders": [approved]})
    with pytest.raises(ValueError, match="descends into a hidden directory"):
        validate_path(f"{approved}/.secret/x.tpy", "path")


def test_stale_approved_folder_does_not_break_validation(client):
    """A stale (nonexistent) approved folder is ignored and does not break
    validation of paths covered by another, valid approved folder."""
    from artifice_ocr.validation import validate_path

    config.apply_overrides(
        {"approved_folders": ["/nonexistent/stale/drive", "/opt/_artifice_approved_valid"]}
    )
    assert os.path.isabs(validate_path("/opt/_artifice_approved_valid/project/x.tpy", "path"))


# --------------------------------------------------------------------------- #
# Windows external-drive case — the gap that let this ship
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(os.name != "nt", reason="Windows drive letters only resolve on Windows")
def test_windows_external_drive_rejected_without_approval(client):
    from artifice_ocr.validation import validate_path

    config.apply_overrides({"approved_folders": []})
    with pytest.raises(ValueError, match="is outside the directories"):
        validate_path("E:\\Projects\\Tropy\\x.tpy", "path")


@pytest.mark.skipif(os.name != "nt", reason="Windows drive letters only resolve on Windows")
def test_windows_external_drive_accepted_when_approved(client):
    from artifice_ocr.validation import validate_path

    config.apply_overrides({"approved_folders": ["E:\\Projects\\Tropy"]})
    result = validate_path("E:\\Projects\\Tropy\\x.tpy", "path")
    assert result.lower().startswith("e:")


# --------------------------------------------------------------------------- #
# Browse Project routes through the same actionable error message
# --------------------------------------------------------------------------- #


def test_tropy_browse_outside_root_names_settings_remedy(client):
    """Browse Project on a path outside the allowed roots must surface the
    same 'approve the folder in Settings' remedy as every other endpoint."""
    config.apply_overrides({"tropy_live_browse_enabled": True})

    res = client.post(
        "/api/tropy/browse/projects",
        json={"path": "/etc/passwd.tpy"},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "Approve the folder in Settings" in detail
    assert "ARTIFICE_OCR_ALLOWED_ROOTS" in detail
