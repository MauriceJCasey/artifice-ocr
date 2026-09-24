# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FastAPI backend for the web frontend.

This module owns the FastAPI ``app`` and the bootstrap code (CLI, port
discovery, browser launch). Individual route groups live under ``routers/``
and are included here.
"""

import contextlib
import importlib.resources
import json
import logging
import os
import time
from html import escape
from pathlib import Path
from typing import Any

import shared_ui
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import ChoiceLoader, Environment, PackageLoader, select_autoescape
from shared_ui.server_bootstrap import (
    ensure_std_streams,
    free_port,
    port_available,
    report_startup_failure,
    start_server_thread,
    wait_for_server,
)

from .routers import byom as byom_router
from .routers import events as events_router
from .routers import history as history_router
from .routers import native_dialogs as native_dialogs_router
from .routers import pdf_export as pdf_export_router
from .routers import queue as queue_router
from .routers import run as run_router
from .routers import segmentation as segmentation_router
from .routers import settings as settings_router
from .routers import suite as suite_router
from .routers import tropy_browse as tropy_browse_router
from .routers import tropy_notes as tropy_notes_router
from .runtime import PdfExportState, RunState

logger = logging.getLogger(__name__)


_core_router = APIRouter()

# CORS origins default to the app's own standard host:port pair, but are
# overridable via ARTIFICE_OCR_CORS_ORIGINS (comma-separated) for
# contributors running on a non-standard port — the hardcoded default alone
# left no way to fix a CORS rejection short of editing source.
_DEFAULT_CORS_ORIGINS = [
    "http://localhost:8765",
    "http://127.0.0.1:8765",
]
_cors_origins_env = os.environ.get("ARTIFICE_OCR_CORS_ORIGINS", "")
_cors_origins = (
    [origin.strip() for origin in _cors_origins_env.split(",") if origin.strip()]
    if _cors_origins_env
    else _DEFAULT_CORS_ORIGINS
)


async def no_cache_static(request: Request, call_next):
    response: Response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path.startswith("/shared/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ── Static assets (resolved through importlib.resources — freeze-safe) ─────
# Resolved through importlib.resources, NOT a __file__-relative path.  This
# app is distributed as a frozen .exe/.dmg, where __file__ points inside a
# temporary extraction directory.  Using importlib keeps the path correct in
# every environment — source checkout, installed wheel, and frozen bundle.
STATIC_DIR = importlib.resources.files("artifice_ocr.web") / "static"

# Shared design system (resolved from installed shared-ui package)
_SHARED_UI = importlib.resources.files(shared_ui) / "assets"

# ── Jinja2 — PackageLoader resolves through importlib (freeze-safe), and
# ChoiceLoader lets templates include shared-ui’s masthead partial.
_JINJA = Environment(
    loader=ChoiceLoader(
        [
            PackageLoader("artifice_ocr.web", "templates"),
            PackageLoader("shared_ui", "templates"),
        ]
    ),
    autoescape=select_autoescape(["html", "xml"]),
)

# ── Masthead context for shared _masthead.html partial ──────────────────
_OCR_NAV_ITEMS = [
    {"href": "/?view=main", "label": "Source", "key": "pipeline"},
    {"href": "/?view=preview", "label": "Review", "key": "review"},
    {"href": "/?view=history", "label": "History", "key": "history"},
    {"href": "/?view=settings", "label": "Settings", "key": "settings"},
    {"href": "/about", "label": "About", "key": "about"},
]

_MASTHEAD_CTX = {
    "app_slug": "artifice-ocr",
    "shell_variant": "research",
    "brand_accent": "OCR",
    "page_title": "Document workspace",
    "nav_items": _OCR_NAV_ITEMS,
    "show_inspector": False,
    "show_activity": True,
}


def _asset_version() -> str:
    """Cache-busting version for the /static and /shared links.

    Derived from the newest mtime across both asset trees and recomputed on
    every request to "/", so an asset edited while the server is running is
    picked up immediately and the version changes only when an asset
    actually did. The cost is a directory walk (stat only, no file reads)
    once per page load — negligible for a static tree this size, but it
    would not scale to a very large one.
    """
    roots = (STATIC_DIR, Path(str(_SHARED_UI)))
    mtimes = [p.stat().st_mtime for root in roots for p in root.rglob("*") if p.is_file()]
    return str(int(max(mtimes))) if mtimes else "0"


def _render(template_name: str, **extra) -> str:
    """Build template context and render a Jinja template."""
    ctx: dict[str, Any] = {
        "asset_v": int(time.time()),
    }
    ctx.update(_MASTHEAD_CTX)
    ctx.update(extra)
    return _JINJA.get_template(template_name).render(**ctx)


@_core_router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_render("index.html", active_tab="pipeline"))


@_core_router.get("/about", response_class=HTMLResponse)
def about() -> HTMLResponse:
    return HTMLResponse(
        _render(
            "about.html",
            active_tab="about",
            page_title="About",
            show_inspector=False,
            show_activity=False,
        )
    )


# ── BYOM dev-only preview (phase6) ──────────────────────────────────────────
#
# Renders packages/shared-ui's byom.css/byom.js against fixture data, with
# no dependency on the real /api/byom/* routes — those are Step 4 and do not
# exist yet. Gated behind ARTIFICE_DEV_PREVIEW=1 so it 404s (rather than
# merely being unlinked) unless a developer opts in, and can never ship
# enabled by accident.


_BYOM_PREVIEW_APPS = (
    "artifice-ocr",
    "artifice-draft",
    "artifice-graph",
    "artifice-transcribe",
)
# Apps the dev-only preview can render. Matches the four ``app`` slugs
# ``GET /api/byom/state`` will actually return — see
# ``model_harness.registry._RECOMMENDATIONS``, which already carries all four
# (artifice-transcribe's entries cover its optional post-transcription
# endpoint only, per the docstring on ``recommendations_for_app``).

_BYOM_PREVIEW_APP_NAMES = {
    "artifice-ocr": "Artifice OCR",
    "artifice-draft": "Draft",
    "artifice-graph": "Knowledge Graph",
    "artifice-transcribe": "Transcribe",
}

# Roles each app's GET /api/byom/state publishes, in the same stable order the
# real routers serve. OCR derives its list from the router's own _ROLE_SETTING
# so this dev-only copy can never drift from it; the other three are hand-kept
# in sync with their routers' _ROLE_SETTING (ocr cannot import another app).
_PREVIEW_ROLES = {
    "artifice-ocr": list(byom_router._ROLE_SETTING),
    "artifice-draft": ["chat"],
    "artifice-graph": ["chat", "embedding"],
    "artifice-transcribe": ["chat"],
}


def _byom_recommendations(app: str) -> dict:
    """Serialise model_harness.registry recommendations for *app*.

    Reads the real ModelRecommendation fields (model_name, provider, vision,
    min_vram_gb, ethos_badges, role, notes) — NOT the {name, why, size_bytes}
    shape the phase6 brief's illustrative GET /api/byom/state JSON shows,
    which does not match the dataclass. See the KNOWN CONTRACT MISMATCH note
    atop packages/shared-ui/shared_ui/assets/byom.js.

    This is a dev-only duplicate of routers/byom.py's own
    _byom_recommendations(), kept in sync by hand rather than imported,
    because the preview route's fixture data lives in this file. PR #56
    added ethos_badges/role/notes to the real router but missed this copy —
    the exact "half-applied change" failure mode this codebase's HANDOVER
    keeps recording. Whenever ModelRecommendation's serialised shape
    changes, update both.
    """
    from model_harness.registry import HardwareTier, recommendations_for_app

    tier_keys = {
        "laptop": HardwareTier.LAPTOP,
        "desktop": HardwareTier.DESKTOP,
        "mac_unified": HardwareTier.MAC_UNIFIED,
    }
    return {
        key: [
            {
                "model_name": r.model_name,
                "provider": r.provider,
                "vision": r.vision,
                "min_vram_gb": r.min_vram_gb,
                "ethos_badges": list(r.ethos_badges),
                "role": r.role,
                "notes": r.notes,
            }
            for r in recommendations_for_app(app, tier)
        ]
        for key, tier in tier_keys.items()
    }


def _byom_base_state(app: str) -> dict:
    """Build the {app, configured, endpoint, model, recommendations[, embedding]}
    shape for *app*. Only artifice-graph carries the `embedding` key — the
    other three omit it entirely, matching the frozen contract byom.js
    branches on (see APP_GRAPH handling in packages/shared-ui/shared_ui
    /assets/byom.js). The embedding block itself has no registry data of
    its own to source from (model_harness.registry has no embedding-model
    table), so its endpoint/model here are the frozen contract's own
    literal example, not a real lookup.
    """
    state = {
        "app": app,
        "configured": False,
        "endpoint": None,
        "model": None,
        "roles": _PREVIEW_ROLES.get(app, ["chat"]),
        "recommendations": _byom_recommendations(app),
    }
    if app == "artifice-graph":
        state["embedding"] = {
            "configured": False,
            "endpoint": "http://localhost:11434",
            "model": "bge-m3",
        }
    return state


def _byom_preview_fixture(app: str, state: str) -> dict:
    """Return the fixture bundle {state, detect, test, testEmbedding,
    initialTab, autoTest} for one (app, state) pair. Falls back to
    "artifice-ocr" for an unrecognised ``app`` and to "not-found" for an
    absent or unrecognised ``state`` — the real first-run case.

    Hint strings mirror (but do not import — they are private module
    constants) the wording model_harness.discovery actually produces, so
    the preview reads like the real thing without reaching into
    discovery.py's underscore-prefixed internals.
    """
    if app not in _BYOM_PREVIEW_APPS:
        app = "artifice-ocr"
    base_state = _byom_base_state(app)

    runner_down_hint = (
        "Ensure your local model runner (Ollama, LM Studio, vLLM) is running. "
        "Run 'ollama serve' to start the Ollama server"
    )
    lm_studio_down_hint = "Ensure the LM Studio server is running and accessible"

    not_found_detect = {
        "endpoints": [
            {
                "url": "http://localhost:11434",
                "name": "Ollama",
                "provider": "ollama",
                "reachable": False,
                "models": [],
                "hint": runner_down_hint,
            },
            {
                "url": "http://localhost:1234/v1",
                "name": "LM Studio",
                "provider": "lm-studio",
                "reachable": False,
                "models": [],
                "hint": lm_studio_down_hint,
            },
        ]
    }
    found_detect = {
        "endpoints": [
            {
                "url": "http://localhost:11434",
                "name": "Ollama",
                "provider": "ollama",
                "reachable": True,
                "models": ["llava:7b"],
                "hint": None,
            },
            {
                "url": "http://localhost:1234/v1",
                "name": "LM Studio",
                "provider": "lm-studio",
                "reachable": False,
                "models": [],
                "hint": lm_studio_down_hint,
            },
        ]
    }
    ok_test = {
        "reachable": True,
        "provider": "ollama",
        "models": ["llava:7b", "minicpm-v:8b"],
        "hint": None,
    }
    fail_test = {"reachable": False, "provider": "ollama", "models": [], "hint": runner_down_hint}
    # POST /api/byom/test-embedding fixtures — graph only exercises these,
    # but they are harmless to include for every app since byom.js never
    # calls that endpoint unless state.embedding is present.
    embedding_ok_test = {
        "reachable": True,
        "provider": "ollama",
        "models": ["bge-m3"],
        "hint": None,
    }
    embedding_fail_test = {
        "reachable": False,
        "provider": "ollama",
        "models": [],
        "hint": runner_down_hint,
    }

    scenarios = {
        "detecting": {
            "state": base_state,
            "detect": None,
            "test": None,
            "testEmbedding": None,
            "initialTab": None,
            "autoTest": None,
        },
        "not-found": {
            "state": base_state,
            "detect": not_found_detect,
            "test": fail_test,
            "testEmbedding": embedding_fail_test,
            "initialTab": None,
            "autoTest": None,
        },
        "found": {
            "state": base_state,
            "detect": found_detect,
            "test": ok_test,
            "testEmbedding": embedding_ok_test,
            "initialTab": None,
            "autoTest": None,
        },
        "test-ok": {
            "state": base_state,
            "detect": not_found_detect,
            "test": ok_test,
            "testEmbedding": embedding_ok_test,
            "initialTab": None,
            "autoTest": {"url": "http://localhost:11434", "apiKey": ""},
        },
        "test-fail": {
            "state": base_state,
            "detect": not_found_detect,
            "test": fail_test,
            "testEmbedding": embedding_fail_test,
            "initialTab": None,
            "autoTest": {"url": "http://localhost:9999", "apiKey": ""},
        },
        "advanced": {
            "state": base_state,
            "detect": not_found_detect,
            "test": fail_test,
            "testEmbedding": embedding_fail_test,
            "initialTab": "advanced",
            "autoTest": None,
        },
    }
    return scenarios.get(state, scenarios["not-found"])


@_core_router.get("/byom-preview")
def byom_preview(app: str = "artifice-ocr", state: str = "not-found") -> HTMLResponse:
    """Dev-only preview of the shared BYOM onboarding screen. 404s unless
    ARTIFICE_DEV_PREVIEW=1 is set in the environment.

    `app` and `state` are orthogonal: `app` selects which of the four apps'
    GET /api/byom/state payload is served, `state` keeps selecting the
    detect/test scenario, exactly as before this parameter was added.
    """
    if os.environ.get("ARTIFICE_DEV_PREVIEW") != "1":
        raise HTTPException(status_code=404)

    html = (STATIC_DIR / "byom-preview.html").read_text(encoding="utf-8")
    fixture = _byom_preview_fixture(app, state)
    resolved_app = fixture["state"]["app"]  # normalised: falls back to artifice-ocr for a bad `app`
    app_name = _BYOM_PREVIEW_APP_NAMES.get(resolved_app, resolved_app)
    # "</" -> "<\/" defensively: none of the fixture strings above contain
    # it today, but this is embedded into a <script> block by string
    # substitution rather than a templating engine that would escape it,
    # and the fixture text is free-form enough (hints, URLs) that a future
    # edit could introduce "</script>" by accident.
    fixture_json = json.dumps(fixture).replace("</", "<\\/")
    # `state` and `app` are both attacker-influenceable (query parameters)
    # even though this route is dev-flag-gated, so each gets two
    # separately-escaped substitutions rather than one shared placeholder:
    # HTML-escaped for the text node, JSON-encoded (implies quoting) for the
    # JS string literal.
    #
    # json.dumps() does NOT escape "/" — a `state` of
    # "</script><script>alert(1)</script>" survives dumps() intact and,
    # embedded verbatim into the <script> block below, terminates it early:
    # the HTML tokenizer matches the literal bytes "</script" regardless of
    # JS string-literal context, so the browser closes the tag mid-string
    # and the remainder becomes live markup. Confirmed by curling this
    # route with that value during review. `resolved_app`/`app_name` are
    # already constrained to the four-item allowlist below and cannot
    # carry attacker input, but get the same treatment for defense in depth
    # against a future edit adding a name with "</" in it.
    html = html.replace("__ASSET_V__", _asset_version())
    html = html.replace("__STATE_HTML__", escape(state))
    html = html.replace("__STATE_JS__", json.dumps(state).replace("</", "<\\/"))
    html = html.replace("__APP_HTML__", escape(resolved_app))
    html = html.replace("__APP_JS__", json.dumps(resolved_app).replace("</", "<\\/"))
    html = html.replace("__APP_NAME_JS__", json.dumps(app_name).replace("</", "<\\/"))
    html = html.replace("__FIXTURE_JSON__", fixture_json)
    return HTMLResponse(html)


# ── Handoff discovery: check if another app is running ──────────────────


def create_app(
    *,
    runtime_state: RunState | None = None,
    pdf_export_state: PdfExportState | None = None,
) -> FastAPI:
    """Build an Artifice OCR FastAPI app, wired to injectable state.

    Callers that pass an explicit RunState/PdfExportState (e.g. a test
    fixture's own fresh instance) get every router module that references
    that state by name rewired to it — the single place that happens,
    replacing the five-plus scattered monkeypatches
    apps/artifice-ocr/tests/test_web.py used to need, and fixing a gap that
    fixture had: tropy_browse and tropy_notes import `state` too but were
    never among the patched targets.

    Callers that omit the state arguments (including the module-level
    ``app = create_app()`` below) reuse whatever ``runtime.state`` /
    ``runtime.pdf_export_state`` already are, rather than constructing new
    ones. This matters: some callers bind ``state`` by name at import time
    (``from .runtime import state``, e.g.
    apps/artifice-ocr/tests/test_live_ui_model_interop.py) before this
    module is imported. Building a *new* RunState here unconditionally would
    silently detach their reference from the object every router actually
    uses — reproduced by a failing live-interop run before this comment was
    written. Reusing the existing singleton by default keeps that binding
    valid, exactly as it was before this factory existed.
    """
    from . import runtime

    rs = runtime_state if runtime_state is not None else runtime.state
    pes = pdf_export_state if pdf_export_state is not None else runtime.pdf_export_state

    runtime.state = rs
    runtime.pdf_export_state = pes
    queue_router.state = rs
    run_router.state = rs
    events_router.state = rs
    history_router.state = rs
    tropy_browse_router.state = rs
    tropy_notes_router.state = rs
    pdf_export_router.pdf_export_state = pes

    new_app = FastAPI(title="Artifice OCR")
    new_app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    new_app.middleware("http")(no_cache_static)

    new_app.include_router(byom_router.router)
    new_app.include_router(queue_router.router)
    new_app.include_router(run_router.router)
    new_app.include_router(segmentation_router.router)
    new_app.include_router(events_router.router)
    new_app.include_router(settings_router.router)
    new_app.include_router(history_router.router)
    new_app.include_router(native_dialogs_router.router)
    new_app.include_router(tropy_browse_router.router)
    new_app.include_router(tropy_notes_router.router)
    new_app.include_router(pdf_export_router.router)
    new_app.include_router(suite_router.router)
    new_app.include_router(_core_router)

    new_app.mount("/shared", StaticFiles(directory=str(_SHARED_UI)), name="shared")
    new_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return new_app


app = create_app()


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #

# Re-export under the private names that `main()` and the test suite expect.
_free_port = free_port
_port_available = port_available
_wait_for_server = wait_for_server
_ensure_std_streams = ensure_std_streams

# ── Loopback-only guard ──────────────────────────────────────────────
# Security item 5.2b: Tropy routes would be reachable without auth in a
# deployed instance if the server bound to a non-loopback address.

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _assert_loopback_host() -> None:
    """Refuse to start if the server binds to a non-loopback address.
    This is a defense-in-depth guard — start_server_thread() hardcodes
    127.0.0.1 today, but if a future change adds a configurable host
    this check catches it before the server listens.
    """
    host = "127.0.0.1"  # current value in shared_ui.server_bootstrap.start_server_thread
    if host not in _LOOPBACK_HOSTS:
        print(
            f"artifice-ocr binds to loopback only for security; "
            f"refusing to start on {host}. Set host to 127.0.0.1.",
            flush=True,
        )
        raise SystemExit(1)


def _start_server_thread(port: int):
    return start_server_thread(app, port)


def _report_startup_failure(port: int, thread, errors: list[BaseException]) -> None:
    report_startup_failure("Artifice OCR", port, thread, errors)


def _report_window_failure(reason: str) -> None:
    """Tell desktop users why the native window could not be opened."""
    message = (
        "Artifice OCR could not open its desktop window.\n\n"
        f"{reason}\n\n"
        "The browser fallback is disabled."
    )
    print(f"ERROR: {message}", flush=True)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Artifice OCR — window unavailable", message)
        root.destroy()
    except Exception:
        pass


def main() -> None:
    _ensure_std_streams()
    _assert_loopback_host()

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for the local server (default: 8765, or a free port if busy)",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        default=False,
        help="Server-only mode: print the URL and wait, do not open a window or browser",
    )
    args = parser.parse_args()

    # Distinguish "user said --port 8765" from "the default happened to be 8765".
    # An explicit port that is busy is a deliberate choice — fail, don't fall back.
    is_explicit_port = args.port is not None

    # Try the requested port; fall back to a free port only when the user
    # did NOT specify one and the default (8765) is busy.
    for attempt in range(2):
        if attempt == 0:
            port = args.port if is_explicit_port else 8765
        else:
            port = _free_port()
            print(f"Port 8765 is busy — using port {port} instead.", flush=True)

        if not _port_available(port):
            if is_explicit_port or attempt == 1:
                _report_startup_failure(port, None, [OSError(f"Port {port} is already in use")])
                return
            continue

        server_thread, server_errors = _start_server_thread(port)
        if _wait_for_server(port):
            break

        if is_explicit_port or attempt == 1:
            _report_startup_failure(port, server_thread, server_errors)
            return

    # Guard against the race where another process grabbed the port between
    # our availability check and the server thread binding.
    if server_errors or not server_thread.is_alive():
        _report_startup_failure(port, server_thread, server_errors)
        return

    url = f"http://127.0.0.1:{port}"

    # ── Discovery: register this running instance for handoff ──────────
    # ── Server-only mode (--no-window) ────────────────────────────────────
    if args.no_window:
        print(f"Artifice OCR running at {url}  (Ctrl+C to stop)", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            server_thread.join()
        return

    # ── Desktop mode: require a native window ────────────────────────────
    # This applies equally to frozen releases and Hub/uv installations.
    from .window import open_native_window  # noqa: PLC0415

    try:
        result = open_native_window(url, title="Artifice OCR")
        if result.opened:
            # Window closed by user — exit cleanly.
            # The daemon server thread dies with the process.
            return

        reason = result.reason
    except Exception as exc:
        reason = f"Native window failed: {exc}"

    _report_window_failure(reason)


if __name__ == "__main__":
    main()
