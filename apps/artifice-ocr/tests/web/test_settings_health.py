# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Document types, local model discovery, and health-check tests."""

from concurrent.futures import ThreadPoolExecutor

import httpx
from artifice_ocr import config
from artifice_ocr.web.routers import settings as _settings_router
from pytest_httpx import HTTPXMock

# --------------------------------------------------------------------------- #
# settings: document types + health
# --------------------------------------------------------------------------- #


def test_document_types_lists_known_types(client):
    res = client.get("/api/document-types")
    types = res.json()["types"]
    assert "default" in types
    assert "handwritten" in types


def test_local_models_discovers_lm_studio_on_wsl_host(client, monkeypatch):
    from model_harness.discovery import ProbeResult

    calls = []

    async def fake_probe(url, **_kwargs):
        calls.append(url)
        return ProbeResult(
            url=url,
            reachable=url == "http://172.21.176.1:1234/v1",
            provider="lm-studio",
            models=("allenai/olmocr-2-7b",),
        )

    monkeypatch.setattr(_settings_router, "_wsl_host", lambda: "172.21.176.1")
    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/local-models?backend=lm_studio")

    assert res.status_code == 200
    assert res.json() == {
        "ok": True,
        "backend": "lm_studio",
        "url": "http://172.21.176.1:1234/v1",
        "models": ["allenai/olmocr-2-7b"],
    }
    assert "http://localhost:1234/v1" in calls
    assert "http://172.21.176.1:1234/v1" in calls


def test_local_models_normalises_ollama_address_and_rejects_wrong_provider(client, monkeypatch):
    from model_harness.discovery import ProbeResult

    calls = []

    async def fake_probe(url, **_kwargs):
        calls.append(url)
        return ProbeResult(
            url=url,
            reachable=True,
            provider="lm-studio",
            models=("some-model",),
        )

    monkeypatch.setattr(_settings_router, "_wsl_host", lambda: None)
    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/local-models?backend=ollama&url=http://localhost:11434/v1/")

    assert res.status_code == 200
    assert res.json()["ok"] is False
    assert calls == ["http://localhost:11434"]


def test_local_models_normalises_lm_studio_api_path():
    assert (
        _settings_router._canonical_local_url(
            "lm_studio", "http://localhost:1234/v1/models?source=settings#models"
        )
        == "http://localhost:1234/v1"
    )


def test_local_models_does_not_echo_rejected_address(client, monkeypatch):
    monkeypatch.setattr(
        _settings_router,
        "_local_endpoint_candidates",
        lambda *_args: ["https://models.example.com/v1"],
    )
    monkeypatch.setattr(
        _settings_router._endpoint_policy,
        "validate_url",
        lambda _url: (_ for _ in ()).throw(_settings_router.EndpointRejected("public endpoint")),
    )

    res = client.get("/api/local-models?backend=lm_studio")

    assert res.status_code == 200
    assert res.json()["url"] == ""
    assert res.json()["detail"] == "No permitted local endpoint address was provided."


def test_local_models_rejects_non_local_backend(client):
    res = client.get("/api/local-models?backend=api_key")
    assert res.status_code == 400


def test_health_check_reports_service_status(client, monkeypatch):
    # Use the config model names so the per-model health check matches
    from artifice_ocr import config as ocr_config
    from model_harness.discovery import ProbeResult

    cleanup_model = ocr_config.get("cleanup_model") or "llama3.2:3b"
    translate_model = ocr_config.get("translate_model") or "llama3.2:3b"
    ocr_model = ocr_config.get("ocr_model") or "llama3.2-vision:11b"

    ok_result = ProbeResult(
        url="http://localhost:11434",
        reachable=True,
        models=(cleanup_model, translate_model, ocr_model),
    )

    async def fake_probe(*a, **k):
        return ok_result

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    assert body["lm_studio"]["ok"] is True
    assert body["ollama"]["ok"] is True
    assert all(m["ok"] for m in body["models"])


def test_health_check_surfaces_unreachable_services(client, monkeypatch):
    from model_harness.discovery import ProbeResult

    fail_result = ProbeResult(
        url="http://localhost:11434",
        reachable=False,
        hint="Cannot reach Ollama",
    )

    async def fake_probe(*a, **k):
        return fail_result

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    assert body["lm_studio"]["ok"] is False
    assert body["ollama"]["ok"] is False
    assert all(not m["ok"] for m in body["models"])


def test_health_check_real_probe_returns_from_threadpool(client, httpx_mock: HTTPXMock):
    """GET /api/health exercises the real probe_endpoint against mocked HTTP.

    The route is `async def` and awaits `probe_endpoint` directly — LM Studio
    and Ollama concurrently via `asyncio.gather` rather than sequentially, so
    two 5-10s timeouts are never paid back-to-back when neither answers.
    Other tests mock `probe_endpoint` itself; this one mocks the HTTP layer
    underneath it so the full async path actually runs. The submit-to-a-
    thread-with-a-timeout shape predates the route's move to `async def`
    (it used to matter for the sync-wrapping-async bridge); kept as a cheap
    belt-and-suspenders regression guard against a future change reintroducing
    a blocking call that hangs the route.
    """
    # Default config probes LM Studio (port 1234) and Ollama (port 11434).
    # Align the Ollama model names with the config so the per-model checks pass.
    config.apply_overrides(
        {
            "ocr_model": "ollama-ocr",
            "cleanup_model": "ollama-cleanup",
            "translate_model": "ollama-translate",
        }
    )
    httpx_mock.add_response(
        url="http://localhost:1234/api/tags",
        json={"models": []},
    )
    httpx_mock.add_response(
        url="http://localhost:1234/v1/models",
        json={"data": [{"id": "lm-studio-model"}]},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/api/tags",
        json={
            "models": [
                {"name": "ollama-ocr"},
                {"name": "ollama-cleanup"},
                {"name": "ollama-translate"},
            ]
        },
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/models",
        json={"data": []},
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.get, "/api/health")
        res = future.result(timeout=15)

    assert res.status_code == 200
    body = res.json()
    # Key set consumed by ocr/web/static/js/settings.js
    assert set(body.keys()) == {"lm_studio", "ollama", "models"}
    assert set(body["lm_studio"].keys()) == {"ok", "detail", "url", "models"}
    assert set(body["ollama"].keys()) == {"ok", "detail", "url", "models"}
    assert body["lm_studio"]["models"] == ["lm-studio-model"]
    assert body["ollama"]["models"] == ["ollama-ocr", "ollama-cleanup", "ollama-translate"]
    assert body["lm_studio"]["ok"] is True
    assert body["ollama"]["ok"] is True
    assert all(m["ok"] for m in body["models"])


def test_health_check_real_probe_unreachable_shape(client, httpx_mock: HTTPXMock):
    """Failure path of /api/health keeps the same key set the JS reads."""
    httpx_mock.add_exception(
        httpx.ConnectError("Connection refused"),
        url="http://localhost:1234/api/tags",
    )
    httpx_mock.add_exception(
        httpx.ConnectError("Connection refused"),
        url="http://localhost:11434/api/tags",
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.get, "/api/health")
        res = future.result(timeout=15)

    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"lm_studio", "ollama", "models"}
    assert set(body["lm_studio"].keys()) == {"ok", "detail", "url", "models"}
    assert set(body["ollama"].keys()) == {"ok", "detail", "url", "models"}
    assert body["lm_studio"]["models"] == []
    assert body["ollama"]["models"] == []
    assert body["lm_studio"]["ok"] is False
    assert body["ollama"]["ok"] is False
    assert body["lm_studio"]["detail"] is not None
    assert body["ollama"]["detail"] is not None


def test_health_check_checks_model_against_its_own_configured_backend(client, monkeypatch):
    """A role's model must be graded against the model list of *its own*
    configured backend, not Ollama's -- the user's report was "it pings LM
    Studio, but it does not load the model": the OCR role was on lm_studio,
    but the old code always compared every model against the Ollama probe.
    """
    from model_harness.discovery import ProbeResult

    config.apply_overrides(
        {
            "ocr_backend": "lm_studio",
            "ocr_model": "allenai/olmocr-2-7b",
            "cleanup_backend": "ollama",
            "cleanup_model": "",
            "translate_backend": "ollama",
            "translate_model": "",
        }
    )

    async def fake_probe(url, *a, **k):
        if "1234" in url:
            return ProbeResult(url=url, reachable=True, models=("allenai/olmocr-2-7b",))
        return ProbeResult(url=url, reachable=True, models=("llama3.2:3b",))

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    models = {m["name"]: m for m in body["models"]}
    assert models["allenai/olmocr-2-7b"]["ok"] is True
    assert models["allenai/olmocr-2-7b"]["backend"] == "lm_studio"


def test_health_check_reports_model_missing_from_its_configured_backend(client, monkeypatch):
    """Mirror of the above: a model installed on neither server is reported
    not-OK, still graded against its own role's backend."""
    from model_harness.discovery import ProbeResult

    config.apply_overrides(
        {
            "ocr_backend": "lm_studio",
            "ocr_model": "not-installed-anywhere",
            "cleanup_backend": "ollama",
            "cleanup_model": "",
            "translate_backend": "ollama",
            "translate_model": "",
        }
    )

    async def fake_probe(url, *a, **k):
        return ProbeResult(url=url, reachable=True, models=("some-other-model",))

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    models = {m["name"]: m for m in body["models"]}
    assert models["not-installed-anywhere"]["ok"] is False
    assert models["not-installed-anywhere"]["backend"] == "lm_studio"


def test_health_check_all_ollama_backends_is_unchanged(client, monkeypatch):
    """Regression guard: with every role on ``ollama`` (the pre-fix common
    case), behaviour must be unchanged -- each configured model graded
    against the single Ollama probe."""
    from model_harness.discovery import ProbeResult

    config.apply_overrides(
        {
            "ocr_backend": "ollama",
            "cleanup_backend": "ollama",
            "translate_backend": "ollama",
            "ocr_model": "llama3.2-vision:11b",
            "cleanup_model": "llama3.2:3b",
            "translate_model": "llama3.2:3b",
        }
    )

    async def fake_probe(url, *a, **k):
        return ProbeResult(
            url=url,
            reachable=True,
            models=("llama3.2-vision:11b", "llama3.2:3b"),
        )

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    res = client.get("/api/health")
    body = res.json()
    assert len(body["models"]) == 3
    assert all(m["ok"] for m in body["models"])
    assert all(m["backend"] == "ollama" for m in body["models"])


def test_health_check_cloud_backend_role_not_a_false_negative(client, monkeypatch):
    """A role on a cloud backend (api_key / huggingface) has no local model
    list to check against, so it must never be reported as a plain ``ok:
    False`` -- that would misrepresent an uncheckable model as a missing one.
    """
    from model_harness.discovery import ProbeResult

    config.apply_overrides(
        {
            "ocr_backend": "ollama",
            "ocr_model": "llama3.2-vision:11b",
            "cleanup_backend": "api_key",
            "cleanup_model": "gpt-4o-mini",
            "translate_backend": "ollama",
            "translate_model": "",
        }
    )

    async def fake_probe(url, *a, **k):
        return ProbeResult(url=url, reachable=True, models=("llama3.2-vision:11b",))

    monkeypatch.setattr(_settings_router, "probe_endpoint", fake_probe)

    from artifice_ocr import _backend

    class _FakeApiKeyClient:
        def health_check(self):
            return True, None

    monkeypatch.setattr(_backend, "get_client", lambda backend: _FakeApiKeyClient())

    res = client.get("/api/health")
    body = res.json()
    models = {m["name"]: m for m in body["models"]}
    cloud_entry = models["gpt-4o-mini"]
    assert cloud_entry["ok"] is not False
    assert cloud_entry.get("checkable") is False
    assert cloud_entry["backend"] == "api_key"
