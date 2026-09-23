# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later
from importlib.resources import files
from pathlib import Path

import shared_ui

ROOT = Path(__file__).resolve().parents[3]


def test_shell_assets_and_template_are_packaged():
    package = files(shared_ui)
    for relative in (
        "assets/shell.css",
        "assets/shell.js",
        "assets/research.css",
        "assets/research-fonts.css",
        "assets/fonts/SourceSans3.woff2",
        "assets/fonts/SourceSans3-Italic.woff2",
        "assets/fonts/OFL-SourceSans3.txt",
        "templates/_app_shell.html",
    ):
        assert (package / relative).is_file()


def test_shell_template_has_landmarks_and_extension_blocks():
    source = (files(shared_ui) / "templates/_app_shell.html").read_text()
    assert source.count("<main") == 1
    assert 'id="workspace"' in source
    assert "{% block workspace %}" in source
    assert "{% block inspector %}" in source
    assert "{% block activity %}" in source
    assert 'data-shell-action="activity"' in source
    assert 'shell_variant == "research"' in source
    javascript = (files(shared_ui) / "assets/shell.js").read_text()
    assert "quarantineLegacyTabs" in javascript
    assert 'setAttribute("tabindex", "-1")' in javascript


def test_research_styles_are_variant_scoped_and_retain_legacy_tokens():
    css = (files(shared_ui) / "assets/research.css").read_text()
    font_css = (files(shared_ui) / "assets/research-fonts.css").read_text()
    assert '[data-shell-variant="research"]' in css
    assert '[data-shell-variant="research"] .progress-bar' in css
    assert "transition: none;" in css
    assert "grid-template: var(--shell-title) minmax(0, 1fr) auto" in css
    assert '[data-shell-variant="research"] .shell-titlebar-nav' in css
    assert 'font-family: "Source Sans 3"' in font_css
    shell = (files(shared_ui) / "templates/_app_shell.html").read_text()
    assert '{% if shell_variant == "research" %}' in shell
    assert "/shared/research-fonts.css" in shell
    tokens = (files(shared_ui) / "assets/tokens.css").read_text()
    assert '"Libre Baskerville"' in tokens
    assert '"Playfair Display"' in tokens


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


