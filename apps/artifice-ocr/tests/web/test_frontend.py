# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Static frontend, CORS, and static-asset route tests."""

from artifice_ocr.web import server
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# static frontend
# --------------------------------------------------------------------------- #


def test_index_serves_the_frontend(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "ArtificeOCR" in res.text
    assert "shell-skip" in res.text
    assert "app-shell" in res.text
    assert 'id="image-viewport"' in res.text
    assert 'id="btn-add-tropy"' in res.text
    assert 'id="btn-send-tropy"' in res.text
    assert "LudwigLang" not in res.text


def test_about_page_serves(client):
    res = client.get("/about")
    assert res.status_code == 200
    assert "About ArtificeOCR" in res.text
    assert "app-shell" in res.text


def test_about_page_is_clean(client):
    html = client.get("/about").text
    # Workspace scripts threw 11 uncaught exceptions here: nothing to bind to.
    assert "/static/js/intake.js" not in html
    assert "/static/js/settings.js" not in html
    # The connection dialog still works from the titlebar.
    assert "/shared/byom.js" in html
    # One main landmark (the shell's), and no request to another server.
    assert html.count("<main") == 1
    assert "mauricejcasey.com/favicon" not in html


# --------------------------------------------------------------------------- #
# CORS origins (ARTIFICE_OCR_CORS_ORIGINS)
# --------------------------------------------------------------------------- #
#
# `server.app` builds its CORS middleware from the env var at *import* time,
# so exercising a non-default value means reloading the module after setting
# it. Each test restores `server` to its unmodified state afterwards so later
# tests (including the `client` fixture above, which reads `server.app`)
# see the normal default-origin app.


def test_cors_default_origins_when_env_unset(monkeypatch):
    monkeypatch.delenv("ARTIFICE_OCR_CORS_ORIGINS", raising=False)
    import importlib

    importlib.reload(server)
    try:
        with TestClient(server.app) as c:
            allowed = c.get("/", headers={"Origin": "http://localhost:8765"})
            assert allowed.headers.get("access-control-allow-origin") == "http://localhost:8765"

            rejected = c.get("/", headers={"Origin": "http://evil.example"})
            assert "access-control-allow-origin" not in rejected.headers
    finally:
        importlib.reload(server)


def test_cors_honors_env_override(monkeypatch):
    monkeypatch.setenv(
        "ARTIFICE_OCR_CORS_ORIGINS",
        "http://example.com:9999, http://foo.test:1234",
    )
    import importlib

    importlib.reload(server)
    try:
        with TestClient(server.app) as c:
            allowed = c.get("/", headers={"Origin": "http://example.com:9999"})
            assert allowed.headers.get("access-control-allow-origin") == "http://example.com:9999"

            # The old hardcoded default must no longer be allowed once an
            # override is set — this is not additive.
            rejected = c.get("/", headers={"Origin": "http://localhost:8765"})
            assert "access-control-allow-origin" not in rejected.headers
    finally:
        monkeypatch.undo()
        importlib.reload(server)


def test_static_index_html_is_gone(client):
    res = client.get("/static/index.html")
    assert res.status_code == 404


def test_static_assets_are_mounted(client):
    res = client.get("/static/css/app.css")
    assert res.status_code == 200
    assert "--paper" in res.text  # the actual design tokens, not a stub
