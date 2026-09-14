# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later
from importlib.resources import files
from pathlib import Path

import shared_ui

ROOT = Path(__file__).resolve().parents[3]


def test_shell_assets_and_template_are_packaged():
    package = files(shared_ui)
    for relative in ("assets/shell.css", "assets/shell.js", "templates/_app_shell.html"):
        assert (package / relative).is_file()


def test_shell_template_has_landmarks_and_extension_blocks():
    source = (files(shared_ui) / "templates/_app_shell.html").read_text()
    assert source.count("<main") == 1
    assert 'id="workspace"' in source
    assert "{% block workspace %}" in source
    assert "{% block inspector %}" in source
    assert "{% block activity %}" in source


def test_shell_javascript_exposes_documented_api():
    source = (files(shared_ui) / "assets/shell.js").read_text()
    members = (
        "init",
        "publishActivity",
        "removeActivity",
        "setModelStatus",
        "getPreferences",
        "setPreferences",
        "refreshSuiteApps",
    )
    for member in members:
        assert member in source


def test_frameless_window_resize_grip_has_runtime_styles():
    css = (files(shared_ui) / "assets/shell.css").read_text()
    javascript = (files(shared_ui) / "assets/window-controls.js").read_text()

    assert ".pywebview-active .window-resize-grip" in css
    assert "z-index:50" in css
    assert 'grip.id = "windowResizeGrip"' in javascript
    assert "window.pywebview.api.resize" in javascript


def test_every_app_uses_the_suite_shell():
    template_bases = (
        "apps/artifice-ocr/src/artifice_ocr/web/templates/base.html",
        "apps/artifice-draft/src/artifice_draft/web/templates/base.html",
        "apps/artifice-graph/src/artifice_graph/web/templates/base.html",
        "apps/artifice-transcribe/src/artifice_transcribe/web/templates/base.html",
    )
    for relative in template_bases:
        assert '{% extends "_app_shell.html" %}' in (ROOT / relative).read_text(encoding="utf-8")

    hub = (ROOT / "apps/artifice-hub/src/artifice_hub/web/static/index.html").read_text(
        encoding="utf-8"
    )
    assert 'class="app-shell"' in hub
    assert "/shared/shell.css" in hub
    assert "/shared/shell.js" in hub
