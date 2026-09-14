# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression checks for the Windows Defender false-positive surface.

These are intentionally small source/build-contract checks. They prevent a
future maintenance change from reintroducing the two behaviours that caused
the strongest defense-evasion signal in the frozen OCR executable.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OCR_SRC = ROOT / "apps" / "artifice-ocr" / "src" / "artifice_ocr"


def test_ocr_runtime_does_not_enumerate_processes_or_strip_download_metadata():
    window = (ROOT / "packages" / "shared-ui" / "shared_ui" / "window.py").read_text(
        encoding="utf-8"
    )

    assert not (OCR_SRC / "tropy_write.py").exists()
    assert "Zone.Identifier" not in window
    assert "os.remove" not in window


def test_ocr_pyinstaller_bundle_is_uncompressed_and_windowed():
    spec = (ROOT / "apps" / "artifice-ocr" / "artifice-ocr.spec").read_text(encoding="utf-8")
    assert "upx=False" in spec
    assert "console=False" in spec
    assert "uac_admin=False" in spec
    assert "uac_uiaccess=False" in spec


def test_ocr_pyinstaller_bundle_contains_both_windows_webview_renderers():
    """Keep the desktop window usable with and without WebView2 installed."""
    spec = (ROOT / "apps" / "artifice-ocr" / "artifice-ocr.spec").read_text(encoding="utf-8")
    assert '"webview.platforms.edgechromium"' in spec
    assert '"webview.platforms.mshtml"' in spec


def test_ocr_distribution_has_no_ludwiglang_surface():
    """The retired exporter must not return through source or packaged UI."""
    package = ROOT / "apps" / "artifice-ocr" / "src" / "artifice_ocr"
    index = (package / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    base = (package / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    server = (package / "web" / "server.py").read_text(encoding="utf-8")

    assert not (package / "export_ludwiglang.py").exists()
    assert not (package / "web" / "routers" / "ludwiglang.py").exists()
    assert not (package / "web" / "static" / "js" / "ludwiglang.js").exists()
    assert "LudwigLang" not in index
    assert "ludwiglang.js" not in base
    assert "ludwiglang_router" not in server


def test_canonical_stage_mapping(tmp_path):
    from artifice_ocr.output import stage_dir
    from artifice_output import ProjectLayout

    layout = ProjectLayout(tmp_path, "Archive", create=True)
    assert stage_dir(layout.project_dir, "raw_ocr") == layout.project_dir / "pipeline" / "raw-ocr"
    assert stage_dir(tmp_path, "raw_ocr") == tmp_path / "raw_ocr"
