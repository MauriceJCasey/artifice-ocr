# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the BYOM onboarding endpoints in artifice-ocr."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import httpx
import pytest
from artifice_ocr import config
from artifice_ocr.web import server
from fastapi.testclient import TestClient
from model_harness.discovery import ProbeResult


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolate settings so tests never touch the real ~/.artifice_ocr/settings.json."""
    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
    config.reset()
    config.load_config()
    config.apply_overrides({"history_db": str(tmp_path / "history.db")})

    from artifice_ocr.web.routers import (
        events as _events_router,
    )
    from artifice_ocr.web.routers import (
        history as _history_router,
    )
    from artifice_ocr.web.routers import (
        queue as _queue_router,
    )
    from artifice_ocr.web.routers import (
        run as _run_router,
    )
    from artifice_ocr.web.runtime import RunState

    fresh = RunState()
    monkeypatch.setattr(_queue_router, "state", fresh)
    monkeypatch.setattr(_run_router, "state", fresh)
    monkeypatch.setattr(_events_router, "state", fresh)
    monkeypatch.setattr(_history_router, "state", fresh)
    monkeypatch.setattr("artifice_ocr.web.runtime.state", fresh)
    # pdf_export uses its own separate state
    import artifice_ocr.web.routers.pdf_export as _pe

    monkeypatch.setattr(_pe, "pdf_export_state", fresh)
    import queue as _queue_mod

    from artifice_ocr.web import runtime as web_runtime

    pstate = web_runtime.pdf_export_state
    if pstate.thread is not None and pstate.thread.is_alive():
        pstate.thread.join(timeout=5)
    pstate.thread = None
    pstate.events = _queue_mod.Queue()
    pstate.output_path = None
    pstate.status = None

    return TestClient(server.app)


# ── GET /api/byom/state ───────────────────────────────────────────────────


class TestByomState:
    """`GET /api/byom/state` returns config state — no network, no probing."""

    def test_returns_all_keys_in_default_state(self, client):
        """A fresh install returns configured=False."""
        r = client.get("/api/byom/state")
        assert r.status_code == 200
        body = r.json()

        assert body["app"] == "artifice-ocr"
        assert body["configured"] is False
        assert body["endpoint"] == "http://localhost:11434"
        # The default model is empty (resolved at run time), so the state
        # reports it as None — not a concrete name.
        assert body["model"] is None
        assert "recommendations" in body

    def test_configured_true_when_api_key_set(self, client):
        """Setting api_key makes configured=True."""
        config.apply_overrides({"api_key": "sk-fake"})
        config.save_user_settings({"api_key": "sk-fake"})

        r = client.get("/api/byom/state")
        assert r.status_code == 200
        assert r.json()["configured"] is True

    def test_configured_true_when_base_url_changed(self, client):
        """Changing api_base_url from the default makes configured=True."""
        config.apply_overrides({"api_base_url": "http://localhost:9999/v1"})
        config.save_user_settings({"api_base_url": "http://localhost:9999/v1"})

        r = client.get("/api/byom/state")
        assert r.status_code == 200
        assert r.json()["configured"] is True

    def test_lm_studio_backend_is_reported_as_the_configured_endpoint(self, client):
        config.apply_overrides(
            {
                "ocr_backend": "lm_studio",
                "lm_studio_url": "http://localhost:1234/v1",
            }
        )

        body = client.get("/api/byom/state").json()

        assert body["configured"] is True
        assert body["endpoint"] == "http://localhost:1234/v1"

    def test_selected_lm_studio_endpoint_takes_precedence_over_stored_api_key(self, client):
        config.apply_overrides(
            {
                "api_key": "sk-stale",
                "api_base_url": "https://api.example.com/v1",
                "ocr_backend": "lm_studio",
                "lm_studio_url": "http://localhost:1234/v1",
            }
        )

        body = client.get("/api/byom/state").json()

        assert body["endpoint"] == "http://localhost:1234/v1"

    def test_recommendations_have_correct_fields(self, client):
        """Recommendations use model_name, provider, vision, min_vram_gb, ethos_badges, role, notes."""
        r = client.get("/api/byom/state")
        body = r.json()
        recs = body["recommendations"]

        for tier in ("laptop", "desktop", "mac_unified"):
            assert tier in recs
            for entry in recs[tier]:
                assert "model_name" in entry
                assert "provider" in entry
                assert "vision" in entry
                assert "min_vram_gb" in entry
                assert "ethos_badges" in entry
                assert "role" in entry
                assert "notes" in entry

    def test_recommendations_is_serialisable(self, client):
        """The full response round-trips through JSON without error."""
        r = client.get("/api/byom/state")
        assert r.status_code == 200
        json.dumps(r.json())

    def test_state_roles_match_role_setting(self, client):
        """state.roles is derived from _ROLE_SETTING keys — derive one from the
        other and assert equality, so the two can never drift apart."""
        from artifice_ocr.web.routers.byom import _ROLE_SETTING

        r = client.get("/api/byom/state")
        assert r.status_code == 200
        assert r.json()["roles"] == list(_ROLE_SETTING)
        # Stable, sensible order the picker renders in.
        assert r.json()["roles"] == ["vision", "chat", "translation"]


# ── GET /api/byom/detect ──────────────────────────────────────────────────


class TestByomDetect:
    """`GET /api/byom/detect` probes known endpoints.

    The route handler transforms ProbeResults into the JSON shape byom.js
    expects.  We mock the probe so we can test serialisation (including the
    `name` derived from the registry) without depending on a running server.
    """

    def test_returns_endpoints_array(self, client):
        with patch("artifice_ocr.web.routers.byom.detect_local_servers") as mock_detect:
            mock_detect.return_value = [
                ProbeResult(
                    url="http://localhost:11434/v1",
                    reachable=True,
                    provider="ollama",
                    models=("llava:7b", "minicpm-v:8b"),
                    hint=None,
                ),
            ]

            r = client.get("/api/byom/detect")
            assert r.status_code == 200
            body = r.json()

            assert "endpoints" in body
            eps = body["endpoints"]
            assert len(eps) == 1
            ep = eps[0]
            assert ep["url"] == "http://localhost:11434/v1"
            assert ep["name"] == "Ollama"
            assert ep["provider"] == "ollama"
            assert ep["reachable"] is True
            assert ep["models"] == ["llava:7b", "minicpm-v:8b"]
            assert ep["hint"] is None

    def test_unreachable_endpoint_has_hint(self, client):
        with patch("artifice_ocr.web.routers.byom.detect_local_servers") as mock_detect:
            mock_detect.return_value = [
                ProbeResult(
                    url="http://localhost:1234/v1",
                    reachable=False,
                    provider="lm-studio",
                    models=(),
                    hint="Ensure the LM Studio server is running",
                ),
            ]

            r = client.get("/api/byom/detect")
            assert r.status_code == 200
            ep = r.json()["endpoints"][0]
            assert ep["reachable"] is False
            assert ep["hint"] == "Ensure the LM Studio server is running"


# ── POST /api/byom/test ───────────────────────────────────────────────────


class TestByomTest:
    """`POST /api/byom/test` validates, probes, and persists."""

    def test_rejects_empty_url(self, client):
        """A blank URL is rejected by validate_url."""
        r = client.post("/api/byom/test", json={"url": "", "api_key": ""})
        assert r.status_code == 400
        body = r.json()
        assert "hint" in body
        assert "error" in body

    def test_rejects_bad_scheme(self, client):
        """Only http/https are permitted."""
        r = client.post("/api/byom/test", json={"url": "ftp://localhost/v1", "api_key": ""})
        assert r.status_code == 400
        body = r.json()
        assert "hint" in body
        assert "error" in body

    def test_rejects_public_url_by_default(self, client):
        """Without ARTIFICE_ALLOW_PUBLIC_MODELS, a public URL is rejected."""
        r = client.post("/api/byom/test", json={"url": "https://api.example.com/v1", "api_key": ""})
        assert r.status_code == 400
        body = r.json()
        assert "hint" in body
        assert "error" in body

    def test_successful_probe_saves_config(self, client):
        """A reachable endpoint persists its URL so configured=True."""
        with patch("artifice_ocr.web.routers.byom.probe_endpoint") as mock_probe:
            mock_probe.return_value = ProbeResult(
                url="http://localhost:9999",
                reachable=True,
                provider="ollama",
                models=("llava:7b",),
                hint=None,
            )

            r = client.post(
                "/api/byom/test",
                json={
                    "url": "http://localhost:9999",
                    "api_key": "",
                },
            )
            assert r.status_code == 200
            body = r.json()
            assert body["reachable"] is True
            assert body["provider"] == "ollama"

            # Config was saved — a subsequent state call reports configured=True.
            state_r = client.get("/api/byom/state")
            assert state_r.json()["configured"] is True

    def test_unreachable_probe_does_not_mark_configured(self, client):
        """A failed probe does NOT overwrite the config."""
        with patch("artifice_ocr.web.routers.byom.probe_endpoint") as mock_probe:
            mock_probe.return_value = ProbeResult(
                url="http://localhost:1234/v1",
                reachable=False,
                provider="lm-studio",
                models=(),
                hint="Not running",
            )

            r = client.post(
                "/api/byom/test",
                json={
                    "url": "http://localhost:1234/v1",
                    "api_key": "sk-test",
                },
            )
            assert r.status_code == 200
            assert r.json()["reachable"] is False

            # Config was NOT saved.
            state_r = client.get("/api/byom/state")
            assert state_r.json()["configured"] is False

    def test_successful_lm_studio_probe_selects_the_local_backend(self, client):
        with patch("artifice_ocr.web.routers.byom.probe_endpoint") as mock_probe:
            mock_probe.return_value = ProbeResult(
                url="http://localhost:1234/v1",
                reachable=True,
                provider="lm-studio",
                models=("local-vision-model",),
                hint=None,
            )

            response = client.post(
                "/api/byom/test",
                json={"url": "http://localhost:1234/v1", "api_key": ""},
            )

        assert response.status_code == 200
        saved = config.load_user_settings()
        assert saved["lm_studio_url"] == "http://localhost:1234/v1"
        assert saved["ocr_backend"] == "lm_studio"
        assert saved["cleanup_backend"] == "lm_studio"
        assert saved["translate_backend"] == "lm_studio"
        assert "api_base_url" not in saved
        assert client.get("/api/byom/state").json()["endpoint"] == "http://localhost:1234/v1"

    def test_api_key_persisted_on_success(self, client):
        """The api_key is saved via save_user_settings → secure_io."""
        with patch("artifice_ocr.web.routers.byom.probe_endpoint") as mock_probe:
            mock_probe.return_value = ProbeResult(
                url="http://localhost:9999/v1",
                reachable=True,
                provider="generic-api",
                models=(),
                hint=None,
            )

            client.post(
                "/api/byom/test",
                json={
                    "url": "http://localhost:9999/v1",
                    "api_key": "sk-secret-123",
                },
            )

            saved = config.load_user_settings()
            assert saved.get("api_key") == "sk-secret-123"
            assert saved.get("api_base_url") == "http://localhost:9999/v1"

    def test_successful_ollama_probe_does_not_rewrite_api_base_url(self, client):
        """A successful Ollama probe persists ollama_url but must not restore
        the shipped cloud ``api_base_url`` default — that re-poisons a field
        the user deliberately cleared."""
        with patch("artifice_ocr.web.routers.byom.probe_endpoint") as mock_probe:
            mock_probe.return_value = ProbeResult(
                url="http://localhost:11434/v1",
                reachable=True,
                provider="ollama",
                models=("llava:7b",),
                hint=None,
            )

            # Give api_base_url a non-default value so a poisoned write back to
            # https://api.openai.com/v1 would be observable.
            config.apply_overrides({"api_base_url": "http://localhost:9999/v1"})
            config.save_user_settings({"api_base_url": "http://localhost:9999/v1"})

            r = client.post(
                "/api/byom/test",
                json={"url": "http://localhost:11434/v1", "api_key": ""},
            )
            assert r.status_code == 200

            saved = config.load_user_settings()
            assert saved.get("ollama_url") == "http://localhost:11434"
            assert saved.get("api_base_url") == "http://localhost:9999/v1"


# ── POST /api/byom/model ───────────────────────────────────────────────────


class TestByomModel:
    """`POST /api/byom/model` persists each role to the correct settings key.

    OCR has three roles (vision, chat, translation) mapping to three settings
    (ocr_model, cleanup_model, translate_model) — the BYOM screen must be able
    to set all three, not just the chat/cleanup one.
    """

    def test_vision_role_sets_ocr_model(self, client):
        r = client.post("/api/byom/model", json={"model": "llava:7b", "role": "vision"})
        assert r.status_code == 200
        assert r.json() == {"model": "llava:7b", "role": "vision"}
        assert config.get("ocr_model") == "llava:7b"

    def test_translation_role_sets_translate_model(self, client):
        r = client.post("/api/byom/model", json={"model": "qwen2.5:7b", "role": "translation"})
        assert r.status_code == 200
        assert r.json() == {"model": "qwen2.5:7b", "role": "translation"}
        assert config.get("translate_model") == "qwen2.5:7b"

    def test_chat_role_sets_cleanup_model(self, client):
        r = client.post("/api/byom/model", json={"model": "mistral:7b", "role": "chat"})
        assert r.status_code == 200
        assert r.json() == {"model": "mistral:7b", "role": "chat"}
        assert config.get("cleanup_model") == "mistral:7b"

    def test_unknown_role_is_rejected(self, client):
        """No embedding role may leak into ocr's picker — the server rejects it."""
        r = client.post("/api/byom/model", json={"model": "bge-m3", "role": "embedding"})
        assert r.status_code == 400
        assert "embedding" in r.json()["error"]


# ── Contract + SSRF + first-paint tests (pytest-httpx) ────────────────────


class TestByomContractAndSsrf:
    """Drive the real HTTP layer so the JSON keys match what byom.js reads.

    These tests use pytest-httpx to mock the model-server probes. A rejected
    URL must be rejected *before* any network call; the absence of recorded
    requests is the evidence.
    """

    # -- GET /api/byom/state ---------------------------------------------

    def test_state_json_keys_default(self, client):
        r = client.get("/api/byom/state")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {
            "app",
            "configured",
            "endpoint",
            "model",
            "roles",
            "recommendations",
        }
        assert body["app"] == "artifice-ocr"
        assert body["configured"] is False

    def test_state_json_keys_configured(self, client):
        config.apply_overrides({"api_key": "sk-fake"})
        config.save_user_settings({"api_key": "sk-fake"})
        r = client.get("/api/byom/state")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {
            "app",
            "configured",
            "endpoint",
            "model",
            "roles",
            "recommendations",
        }
        assert body["configured"] is True

    # -- GET /api/byom/detect --------------------------------------------

    def test_detect_json_keys_all_down(self, client, httpx_mock):
        for url in ("http://localhost:11434", "http://localhost:1234", "http://localhost:8080"):
            httpx_mock.add_exception(
                httpx.ConnectError("Connection refused"), url=url + "/api/tags"
            )

        r = client.get("/api/byom/detect")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"endpoints"}
        assert len(body["endpoints"]) == 3
        for ep in body["endpoints"]:
            assert set(ep.keys()) == {"url", "name", "provider", "reachable", "models", "hint"}
            assert ep["reachable"] is False
            assert isinstance(ep["hint"], str)

    def test_detect_json_keys_one_up(self, client, httpx_mock):
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags", json={"models": [{"name": "llava:7b"}]}
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models", json={"data": [{"id": "llava:7b"}]}
        )
        for url in ("http://localhost:1234", "http://localhost:8080"):
            httpx_mock.add_exception(
                httpx.ConnectError("Connection refused"), url=url + "/api/tags"
            )

        r = client.get("/api/byom/detect")
        assert r.status_code == 200
        body = r.json()
        reachable = [ep for ep in body["endpoints"] if ep["reachable"]]
        assert len(reachable) == 1
        assert reachable[0]["provider"] == "ollama"
        assert reachable[0]["models"] == ["llava:7b"]

    # -- POST /api/byom/test ---------------------------------------------

    def test_test_json_keys_reachable(self, client, httpx_mock):
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags", json={"models": [{"name": "llava:7b"}]}
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models", json={"data": [{"id": "llava:7b"}]}
        )

        r = client.post("/api/byom/test", json={"url": "http://localhost:11434/v1", "api_key": ""})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"reachable", "provider", "models", "hint"}
        assert body["reachable"] is True
        assert body["provider"] == "ollama"

    def test_test_json_keys_refused(self, client, httpx_mock):
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"), url="http://localhost:11434/api/tags"
        )

        r = client.post("/api/byom/test", json={"url": "http://localhost:11434/v1", "api_key": ""})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"reachable", "provider", "models", "hint"}
        assert body["reachable"] is False
        assert isinstance(body["hint"], str)

    def test_test_json_keys_timeout(self, client, httpx_mock):
        httpx_mock.add_exception(
            httpx.TimeoutException("Timed out"), url="http://localhost:11434/api/tags"
        )

        r = client.post("/api/byom/test", json={"url": "http://localhost:11434/v1", "api_key": ""})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"reachable", "provider", "models", "hint"}
        assert body["reachable"] is False
        assert "timeout" in body["hint"].lower() or "did not respond" in body["hint"].lower()

    # -- SSRF surface -----------------------------------------------------

    def _assert_rejected_before_network(self, r, httpx_mock):
        assert r.status_code == 400
        body = r.json()
        assert "hint" in body
        assert "error" in body
        assert isinstance(body["hint"], str)
        assert not httpx_mock.get_requests(), "a request was issued for a rejected URL"

    def test_rejects_link_local_before_network(self, client, httpx_mock):
        r = client.post(
            "/api/byom/test",
            json={"url": "http://169.254.169.254/latest/meta-data/", "api_key": ""},
        )
        self._assert_rejected_before_network(r, httpx_mock)
        assert "link-local" in r.json()["hint"].lower()

    def test_rejects_public_host_before_network(self, client, httpx_mock):
        r = client.post("/api/byom/test", json={"url": "https://api.openai.com/v1", "api_key": ""})
        self._assert_rejected_before_network(r, httpx_mock)
        assert "public" in r.json()["hint"].lower()

    def test_rejects_file_scheme_before_network(self, client, httpx_mock):
        r = client.post("/api/byom/test", json={"url": "file:///etc/passwd", "api_key": ""})
        self._assert_rejected_before_network(r, httpx_mock)

    def test_rejects_ftp_scheme_before_network(self, client, httpx_mock):
        r = client.post("/api/byom/test", json={"url": "ftp://localhost/v1", "api_key": ""})
        self._assert_rejected_before_network(r, httpx_mock)

    def test_rejects_no_host_before_network(self, client, httpx_mock):
        r = client.post("/api/byom/test", json={"url": "http:///v1", "api_key": ""})
        self._assert_rejected_before_network(r, httpx_mock)

    # -- First paint must not block ---------------------------------------

    def test_root_does_not_probe_and_links_assets(self, client, httpx_mock):
        for url in ("http://localhost:11434", "http://localhost:1234", "http://localhost:8080"):
            httpx_mock.add_exception(
                httpx.ConnectError("Connection refused"), url=url + "/api/tags", is_optional=True
            )
        for url in (
            "http://localhost:11434/v1",
            "http://localhost:1234/v1",
            "http://localhost:8080/v1",
        ):
            httpx_mock.add_exception(
                httpx.ConnectError("Connection refused"), url=url + "/models", is_optional=True
            )

        start = time.perf_counter()
        r = client.get("/")
        elapsed = time.perf_counter() - start

        assert r.status_code == 200
        assert elapsed < 0.5, f"GET / took {elapsed:.3f}s, root must not probe"
        assert not httpx_mock.get_requests(), "root route made a network request"
        html = r.text
        assert "/shared/byom.css" in html
        assert "/shared/byom.js" in html
