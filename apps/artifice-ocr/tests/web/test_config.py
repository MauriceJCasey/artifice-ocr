# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Config/settings save + validation, credential redaction, and Tesseract status tests."""

import pytest
from artifice_ocr import config

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_get_config_returns_expected_keys(client):
    res = client.get("/api/config")
    body = res.json()
    assert "cleanup_model" in body
    assert "ollama_think" in body
    assert "ocr_prompt_instruction" in body


def test_set_config_only_persists_whitelisted_keys(client):
    res = client.post(
        "/api/config",
        json={
            "output_dir": "somewhere",
            "not_a_real_setting": "should be dropped",
        },
    )
    assert res.json() == {"ok": True}
    assert config.get("output_dir") == "somewhere"
    assert config.get("not_a_real_setting") is None


def test_set_config_returns_canonical_durable_values(client):
    res = client.post(
        "/api/config",
        json={"ocr_model": "  local-vision  ", "output_dir": "archive-output"},
    )
    assert res.status_code == 200
    durable = client.get("/api/config").json()
    assert durable["ocr_model"] == "local-vision"
    assert durable["output_dir"] == "archive-output"


def test_set_config_does_not_change_runtime_when_persistence_fails(client, monkeypatch):
    before = config.get("cleanup_model")
    monkeypatch.setattr(
        config,
        "save_user_settings",
        lambda _values: (_ for _ in ()).throw(PermissionError("read only")),
    )
    with pytest.raises(PermissionError, match="read only"):
        client.post("/api/config", json={"cleanup_model": "not-durable"})
    assert config.get("cleanup_model") == before


def test_config_reset_discards_overrides(client):
    client.post("/api/config", json={"cleanup_model": "a-custom-model"})
    assert client.get("/api/config").json()["cleanup_model"] == "a-custom-model"

    res = client.post("/api/config/reset")
    assert res.json()["cleanup_model"] == ""
    assert client.get("/api/config").json()["cleanup_model"] == ""


# --------------------------------------------------------------------------- #
# config — endpoint URL validation on save
# --------------------------------------------------------------------------- #


def test_set_config_rejects_link_local_ollama_url(client):
    """A link-local ollama_url is refused at save time when a backend uses it."""
    res = client.post(
        "/api/config", json={"ocr_backend": "ollama", "ollama_url": "http://169.254.169.254/"}
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "link-local" in detail.lower()


def test_set_config_explains_ollama_wildcard_bind_address(client):
    res = client.post(
        "/api/config",
        json={"ocr_backend": "ollama", "ollama_url": "http://0.0.0.0:11434"},
    )
    assert res.status_code == 400
    assert "address Ollama listens on" in res.json()["detail"]
    assert "localhost:11434" in res.json()["detail"]


def test_set_config_rejects_link_local_lm_studio_url(client):
    """A link-local lm_studio_url is refused at save time when a backend uses it."""
    res = client.post(
        "/api/config",
        json={"ocr_backend": "lm_studio", "lm_studio_url": "http://169.254.169.254/v1"},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "link-local" in detail.lower()


def test_set_config_rejects_link_local_api_base_url(client):
    """A link-local api_base_url is refused at save time when a backend uses it."""
    res = client.post(
        "/api/config",
        json={"ocr_backend": "api_key", "api_base_url": "http://169.254.169.254/v1"},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "link-local" in detail.lower()


def test_set_config_allows_loopback_urls(client):
    """Loopback URLs are accepted at save time (no rejection)."""
    res = client.post(
        "/api/config",
        json={
            "ocr_backend": "ollama",
            "cleanup_backend": "lm_studio",
            "translate_backend": "api_key",
            "ollama_url": "http://localhost:11434",
            "lm_studio_url": "http://localhost:1234/v1",
            "api_base_url": "http://localhost:8080/v1",
        },
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_set_config_rejects_public_api_base_url_without_env_var(client, monkeypatch):
    """A public api_base_url is refused at save time without the env var."""
    from artifice_ocr.web.routers import settings as settings_mod
    from model_harness.endpoint_policy import EndpointPolicy

    strict_policy = EndpointPolicy(allow_public=False)
    monkeypatch.setattr(settings_mod, "_endpoint_policy", strict_policy)
    res = client.post(
        "/api/config", json={"ocr_backend": "api_key", "api_base_url": "http://8.8.8.8/v1"}
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "public address" in detail.lower()


def test_set_config_allows_public_api_base_url_with_env_var(client, monkeypatch):
    """A public api_base_url is accepted at save time when the env var is set."""
    from artifice_ocr.web.routers import settings as settings_mod
    from model_harness.endpoint_policy import EndpointPolicy

    permissive_policy = EndpointPolicy(allow_public=True)
    monkeypatch.setattr(settings_mod, "_endpoint_policy", permissive_policy)
    res = client.post(
        "/api/config", json={"ocr_backend": "api_key", "api_base_url": "http://8.8.8.8/v1"}
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_set_config_passes_non_url_fields_through(client):
    """Non-URL config fields are not affected by endpoint validation."""
    res = client.post("/api/config", json={"output_dir": "somewhere"})
    assert res.status_code == 200
    assert config.get("output_dir") == "somewhere"


def test_set_config_trims_model_name_whitespace(client):
    """A model name posted with trailing whitespace is persisted trimmed."""
    res = client.post(
        "/api/config",
        json={"cleanup_model": "aya-expanse:8b-q8_0  "},
    )
    assert res.status_code == 200
    assert config.get("cleanup_model") == "aya-expanse:8b-q8_0"
    assert config.load_user_settings().get("cleanup_model") == "aya-expanse:8b-q8_0"


def test_set_config_normalises_ollama_url(client):
    """``ollama_url`` is stored canonical — whitespace and a trailing ``/v1``
    removed — so a later reader appends exactly one ``/v1``."""
    res = client.post(
        "/api/config",
        json={"ocr_backend": "ollama", "ollama_url": "  http://localhost:11434/v1  "},
    )
    assert res.status_code == 200
    assert config.get("ollama_url") == "http://localhost:11434"
    assert config.load_user_settings().get("ollama_url") == "http://localhost:11434"


# --------------------------------------------------------------------------- #
# config — URL validation only for active backends (pure-Ollama save regression)
# --------------------------------------------------------------------------- #


def test_set_config_pure_ollama_with_default_api_base_url_saves(client, monkeypatch):
    """The exact failure the maintainer hit: all three backends ``ollama``,
    an empty api_key, and api_base_url at its shipped public default must save
    with 200 — not be rejected by the endpoint policy for a field no backend
    is using."""
    from artifice_ocr.web.routers import settings as settings_mod
    from model_harness.endpoint_policy import EndpointPolicy

    # Fail closed deliberately: a public api_base_url must STILL be rejected
    # when api_key is active, so this test proves the save succeeds because the
    # inactive field is skipped, not because the policy was relaxed.
    monkeypatch.setattr(settings_mod, "_endpoint_policy", EndpointPolicy(allow_public=False))

    res = client.post(
        "/api/config",
        json={
            "ocr_backend": "ollama",
            "cleanup_backend": "ollama",
            "translate_backend": "ollama",
            "api_key": "",
            "api_base_url": "https://api.openai.com/v1",
            "ollama_url": "http://localhost:11434",
        },
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_set_config_skips_validation_for_inactive_url_fields(client, monkeypatch):
    """A link-local value in an *inactive* URL field is not validated at save
    time — it is deferred to use-time by the endpoint policy."""
    # Default backends are "auto"; no field maps to "auto", so none is checked.
    res = client.post("/api/config", json={"api_base_url": "http://169.254.169.254/v1"})
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_set_config_redaction_round_trip_preserves_secret(client):
    """GET → form → POST must not overwrite a real key with the placeholder."""
    from artifice_ocr.web.routers.settings import REDACTED_PLACEHOLDER

    client.post("/api/config", json={"api_key": "sk-real-secret"})
    body = client.get("/api/config").json()
    assert body["api_key"] == REDACTED_PLACEHOLDER

    res = client.post("/api/config", json=body)
    assert res.status_code == 200
    assert config.get("api_key") == "sk-real-secret"
    assert config.load_user_settings().get("api_key") == "sk-real-secret"


def test_set_config_redaction_round_trip_preserves_huggingface_token(client):
    """Same round-trip for the Hugging Face token."""
    from artifice_ocr.web.routers.settings import REDACTED_PLACEHOLDER

    client.post("/api/config", json={"huggingface_token": "hf-real-token"})
    body = client.get("/api/config").json()
    assert body["huggingface_token"] == REDACTED_PLACEHOLDER

    res = client.post("/api/config", json=body)
    assert res.status_code == 200
    assert config.get("huggingface_token") == "hf-real-token"
    assert config.load_user_settings().get("huggingface_token") == "hf-real-token"


# --------------------------------------------------------------------------- #
# config reset — credential redaction
# --------------------------------------------------------------------------- #


def test_config_reset_does_not_leak_credentials(client, monkeypatch):
    """POST /api/config/reset must not return api_key or huggingface_token
    verbatim.

    ``config.reset()`` clears the in-memory cache; we prevent that here by
    making it a no-op so the secrets survive into the response path.  On the
    unfixed code the response includes them verbatim; with the fix
    ``_redact_config`` replaces them with the shared placeholder.
    """
    # Populate secrets in the live config cache.
    config.apply_overrides(
        {
            "api_key": "sk-secret-test-key",
            "huggingface_token": "hf-secret-test-token",
        }
    )

    # Prevent reset from clearing the cache so the secrets survive into the
    # response dict — this reproduces the leak scenario from the review.
    monkeypatch.setattr(config, "reset", lambda: None)

    res = client.post("/api/config/reset")
    assert res.status_code == 200
    body = res.json()

    assert body.get("api_key") != "sk-secret-test-key", (
        "api_key was returned verbatim in reset_config response"
    )
    assert body.get("huggingface_token") != "hf-secret-test-token", (
        "huggingface_token was returned verbatim in reset_config response"
    )
    # Optionally confirm the placeholder appears (only true if the values
    # are truthy — they are here).
    assert body.get("api_key") == "************", (
        f"Expected placeholder for api_key, got: {body.get('api_key')}"
    )
    assert body.get("huggingface_token") == "************", (
        f"Expected placeholder for huggingface_token, got: {body.get('huggingface_token')}"
    )


def test_tesseract_status_route_returns_shape(client, monkeypatch):
    """The detection endpoint always returns a stable shape, whether or not a
    real Tesseract binary is present on the machine running the test."""
    from artifice_ocr import _tesseract

    monkeypatch.setattr(_tesseract, "resolve_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(_tesseract, "version", lambda binary=None: "tesseract 5.3.3")

    res = client.get("/api/tesseract/status")
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"available", "path", "version", "lang"}
    assert body["available"] is True
    assert body["version"] == "tesseract 5.3.3"
