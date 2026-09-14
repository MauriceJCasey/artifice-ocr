# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The supported Tropy HTTP surface is live browse plus Developer API notes."""

from pathlib import Path

from artifice_ocr import config
from artifice_ocr.web.routers.settings import _CONFIG_KEYS
from artifice_ocr.web.server import app


def test_legacy_tropy_routes_are_not_registered():
    paths = set(app.openapi()["paths"])

    assert "/api/tropy/import/preview" not in paths
    assert "/api/tropy/import/add" not in paths
    assert "/api/tropy/export" not in paths
    assert "/api/tropy/export/history" not in paths
    assert "/api/tropy/writeback/preview" not in paths
    assert "/api/tropy/writeback/commit" not in paths
    assert "/api/analytics/stats" not in paths
    assert "/api/templates" not in paths
    assert "/api/handoff/create" not in paths
    assert "/api/handoff/discovery/{slug}" not in paths


def test_supported_tropy_routes_remain_registered():
    paths = set(app.openapi()["paths"])

    assert "/api/tropy/browse/projects" in paths
    assert "/api/tropy/browse/enqueue" in paths
    assert "/api/tropy/notes/preview" in paths
    assert "/api/tropy/notes/commit" in paths


def test_deprecated_tropy_settings_are_ignored():
    for key in ("tropy_writeback_enabled", "tropy_last_export_path", "run_templates"):
        assert key not in config.PERSISTED_KEYS
        assert key not in _CONFIG_KEYS


def test_removed_integrations_are_not_packaged():
    package = Path(__file__).resolve().parents[1] / "src" / "artifice_ocr"
    removed = (
        package / "tropy_write.py",
        package / "web" / "routers" / "tropy_bridge.py",
        package / "web" / "routers" / "tropy_writeback.py",
        package / "web" / "routers" / "analytics.py",
        package / "web" / "static" / "js" / "analytics.js",
        package / "web" / "static" / "js" / "handoff.js",
    )
    assert not [path for path in removed if path.exists()]


def test_windows_build_is_blocked_by_critical_workflows():
    root = Path(__file__).resolve().parents[3]
    workflow = (root / ".github" / "workflows" / "build-exe.yml").read_text(encoding="utf-8")
    gate = "Gate OCR on Ollama, LM Studio, and Tropy round trips"
    assert gate in workflow
    assert "test_critical_workflows.py" in workflow
    assert "test_tropy_browse.py" in workflow
    assert "test_tropy_api.py" in workflow
    assert workflow.index(gate) < workflow.index("- name: Freeze")
