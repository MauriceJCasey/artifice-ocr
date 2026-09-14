# -*- mode: python ; coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PyInstaller spec for artifice-ocr — Linux standalone executable.
#
# onedir (not onefile) was chosen deliberately:
#
#   1. Startup speed — onedir maps files directly from disk instead of
#      extracting a self-contained archive to a temp directory on every launch.
#
#   2. Debuggability — you can inspect the _internal/ tree to see exactly
#      what shipped, which onefile compresses into an opaque .pak.
#
#   3. __file__ safety — onefile extracts to a different temp directory each
#      launch, which breaks __file__-relative paths harder.  We already fixed
#      the two remaining __file__ sites, but onedir is the safer choice for
#      a codebase that carries a fossil record of the pattern.
#
#   4. Distribution model — on macOS this becomes an .app bundle; on Windows
#      it is a folder you zip.  Both are standard.  onefile is primarily for
#      CLI tools with no bundled data, which this is not.
#
# Usage (from the repo root):
#   uv run --with pyinstaller pyinstaller apps/artifice-ocr/artifice-ocr.spec

import sys
from pathlib import Path

from PyInstaller.compat import is_win
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# ---------------------------------------------------------------------------
# Configuration — paths relative to the repo root
# ---------------------------------------------------------------------------
APP_NAME = "artifice-ocr"
PACKAGE = "artifice_ocr"
ENTRY_POINT = "artifice_ocr.web.server:main"

# ---------------------------------------------------------------------------
# Hidden imports — uvicorn, fastapi, and friends load things dynamically
# that PyInstaller's static analysis misses.
# ---------------------------------------------------------------------------
HIDDEN_IMPORTS = [
    # uvicorn
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # fastapi
    "fastapi",
    "fastapi.middleware",
    "fastapi.middleware.cors",
    "fastapi.staticfiles",
    # starlette
    "starlette",
    "starlette.routing",
    "starlette.middleware",
    # jinja2 — PackageLoader loads templates at runtime
    "jinja2.ext",
    # Other dynamic loaders
    "multipart",
    "multipart.multipart",
    # artisecure_io — dynamic import in config.py
    "secure_io",
    # model_harness — only loaded when a model is actually called;
    # include it so the BYOM onboarding screen works from a cold bundle.
    "model_harness",
    "model_harness.contract",
    "model_harness.registry",
    # pywebview — dynamically loads platform backends at runtime;
    # PyInstaller's static analysis cannot discover these.
    "webview",
    "webview.dom",
    "webview.dom.element",
    "webview.dom.event",
    "webview.dom.dom",
    "webview.dom.propdict",
    "webview.dom.classlist",
    "webview.menu",
    "webview.http",
    "webview.event",
    "webview.screen",
    "webview.localization",
    "webview.guilib",
    "webview.util",
    "webview.state",
    "webview.models",
    "webview.errors",
    "webview._version",
]

# Freeze only the pywebview backend for the target platform. The previous
# spec pulled every backend (GTK, Qt, CEF, WinForms, WebView2 and Cocoa) and
# pythonnet into every executable, producing a much larger and less
# transparent binary. Core webview modules are discovered normally; these
# are the platform modules whose imports are selected dynamically at runtime.
if sys.platform == "win32":
    HIDDEN_IMPORTS.extend(
        [
            "webview.platforms.winforms",
            "webview.platforms.win32",
            "webview.platforms.edgechromium",
            # winforms falls back to MSHTML when the WebView2 runtime is not
            # available.  This import is selected dynamically, so PyInstaller
            # cannot discover it and an omitted module makes pywebview fail
            # outright (which sends the frozen app to its browser fallback).
            "webview.platforms.mshtml",
            "clr",
        ]
    )
elif sys.platform == "darwin":
    HIDDEN_IMPORTS.append("webview.platforms.cocoa")
else:
    HIDDEN_IMPORTS.append("webview.platforms.gtk")

# ---------------------------------------------------------------------------
# Data files — collect_data_files() picks up everything declared in
# pyproject.toml [tool.setuptools.package-data] for both artifice_ocr and
# shared_ui, plus the shared_ui templates (hatchling include rule).
# ---------------------------------------------------------------------------
datas = []
datas.extend(collect_data_files("artifice_ocr"))
datas.extend(collect_data_files("shared_ui"))

# pywebview ships js/ and lib/ directories that its backends need at runtime.
# On Windows we also need the WebView2 loader DLLs.
datas.extend(collect_data_files("webview", subdir="js"))
if is_win:
    datas.extend(collect_data_files("webview", subdir="lib"))

# ---------------------------------------------------------------------------
# Analysis
#
# We point PyInstaller at a dedicated freeze-entry script (imports
# server.main() through normal Python import machinery) rather than at
# server.py directly.  Running server.py as a bare script breaks relative
# imports ("from .routers import ..." → "attempted relative import with no
# known parent package").  The wrapper uses absolute imports and preserves
# the package context.
#
# The pathex list gives PyInstaller the source directories it needs to find
# artifice_ocr and its dependencies (shared_ui, model_harness, secure_io).
# ---------------------------------------------------------------------------
_SPEC_DIR = Path(SPECPATH)
_REPO_ROOT = _SPEC_DIR.parent.parent
_FREEZE_ENTRY = str(_REPO_ROOT / "apps" / APP_NAME / "src" / PACKAGE / "_freeze_entry.py")

# Hookspath: include pywebview's own PyInstaller hook so its js/ and lib/
# data files are automatically collected on every platform.
_WEBVIEW_HOOKSPATH = []
try:
    import webview as _wv
    import os as _os

    _hook_dir = _os.path.join(_os.path.dirname(_wv.__file__), "__pyinstaller")
    if _os.path.isdir(_hook_dir):
        _WEBVIEW_HOOKSPATH.append(_hook_dir)
except Exception:
    pass

a = Analysis(
    [_FREEZE_ENTRY],
    pathex=[
        str(_REPO_ROOT / "apps" / "artifice-ocr" / "src"),
        str(_REPO_ROOT / "apps" / "artifice-graph" / "src"),
        str(_REPO_ROOT / "apps" / "artifice-draft" / "src"),
        str(_REPO_ROOT / "apps" / "artifice-transcribe" / "src"),
        str(_REPO_ROOT / "packages" / "shared-ui"),
        str(_REPO_ROOT / "packages" / "model-harness" / "src"),
        str(_REPO_ROOT / "packages" / "secure-io" / "src"),
    ],
    binaries=[],
    datas=datas,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=_WEBVIEW_HOOKSPATH,
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# ---------------------------------------------------------------------------
# onedir EXE
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX compression is disabled for Windows distributions. Packed Python
    # bootloaders and compressed extension modules are a frequent source of
    # Defender ML false positives, while an uncompressed onedir bundle is
    # easier to inspect and sign.
    upx=False,
    # The webview app reports startup failures through its native dialog. A
    # console-bearing GUI executable is unnecessary and looks like a launcher
    # wrapper to endpoint heuristics.
    console=False,
    # Keep the standard least-privilege Windows manifest explicit. The app
    # stores its data under the user's profile and never needs elevation or
    # UIAccess; requesting either would be both unsafe and suspicious.
    uac_admin=False,
    uac_uiaccess=False,
    icon='../../packages/shared-ui/shared_ui/assets/logos/artifice-ocr.png',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
