# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for :mod:`artifice_ocr._resolution`.

The resolver's job is to turn "empty/auto defaults plus what the local server
actually serves" into a concrete (model, backend) pair — and to fail legibly
(not with a raw provider 404) when the user's explicit choice is missing or
nothing suitable is installed.
"""

from __future__ import annotations

import logging

import pytest
from artifice_ocr import _resolution, config
from artifice_ocr._resolution import _RoleResolution
from model_harness.contract import EndpointRejected
from model_harness.discovery import ProbeResult
from model_harness.resolution import ResolutionSource


@pytest.fixture(autouse=True)
def _reset_state():
    config.reset()
    _resolution.reset()
    yield
    config.reset()
    _resolution.reset()


def _ok(url: str, models: list[str] | None = None, provider: str | None = "ollama"):
    return ProbeResult(
        url=url,
        reachable=True,
        models=tuple(models or ()),
        provider=provider,
        hint=None,
    )


def _down(url: str):
    return ProbeResult(url=url, reachable=False, models=(), provider=None, hint="down")


def _patch_probes(monkeypatch, results: dict[str, ProbeResult]):
    """Route probe_endpoint_sync to a per-URL fixture table."""

    def fake_probe(url, *, policy, timeout_s):
        return results[url]

    monkeypatch.setattr(_resolution, "probe_endpoint_sync", fake_probe)


OLLAMA = "http://localhost:11434"
LM_STUDIO = "http://localhost:1234/v1"


def test_explicit_configured_model_wins(monkeypatch):
    """A user's explicit model, when installed, is used verbatim (USER_CHOICE)."""
    config.apply_overrides({"ocr_model": "llava:7b", "ocr_backend": "auto"})
    _patch_probes(
        monkeypatch,
        {
            OLLAMA: _ok(OLLAMA, ["llava:7b", "llama3.2:3b"]),
            LM_STUDIO: _down(LM_STUDIO),
        },
    )

    _resolution.resolve_models_for_run(stages={"ocr"})

    assert _resolution.model_for("vision") == "llava:7b"
    assert _resolution.backend_for("vision") == "ollama"


def test_empty_default_resolves_from_installed(monkeypatch):
    """An empty OCR default resolves to the registry vision recommendation."""
    config.apply_overrides({"ocr_model": "", "ocr_backend": "auto"})
    _patch_probes(
        monkeypatch,
        {
            OLLAMA: _ok(OLLAMA, ["richardyoung/olmocr2:7b-q8", "llama3.2:3b"]),
            LM_STUDIO: _down(LM_STUDIO),
        },
    )

    _resolution.resolve_models_for_run(stages={"ocr"})

    assert _resolution.model_for("vision") == "richardyoung/olmocr2:7b-q8"
    assert _resolution.backend_for("vision") == "ollama"


def test_empty_chat_default_falls_back_to_installed(monkeypatch):
    """An empty cleanup default falls back to any installed non-embedding model."""
    config.apply_overrides({"cleanup_model": "", "cleanup_backend": "auto"})
    _patch_probes(
        monkeypatch,
        {
            OLLAMA: _ok(OLLAMA, ["mistral:7b", "gemma:7b"]),
            LM_STUDIO: _down(LM_STUDIO),
        },
    )

    _resolution.resolve_models_for_run(stages={"cleanup"})

    assert _resolution.model_for("chat") == "mistral:7b"
    assert _resolution.backend_for("chat") == "ollama"


def test_none_available_raises_legible_error(monkeypatch):
    """With nothing suitable installed, resolution raises — it does not reach
    the provider and return a raw 404."""
    config.apply_overrides({"cleanup_model": "", "cleanup_backend": "ollama"})
    _patch_probes(monkeypatch, {OLLAMA: _ok(OLLAMA, ["nomic-embed-text"])})

    with pytest.raises(RuntimeError, match="No suitable model for cleanup"):
        _resolution.resolve_models_for_run(stages={"cleanup"})


def test_configured_but_missing_raises_legible_error(monkeypatch):
    """A user's explicit model that is not installed fails loudly."""
    config.apply_overrides({"ocr_model": "llava:7b", "ocr_backend": "ollama"})
    _patch_probes(monkeypatch, {OLLAMA: _ok(OLLAMA, ["some-other-model"])})

    with pytest.raises(RuntimeError, match="not installed on"):
        _resolution.resolve_models_for_run(stages={"ocr"})


def test_vision_none_available_mentions_vision_capable(monkeypatch):
    """The vision role must say a vision-capable model is required."""
    config.apply_overrides({"ocr_model": "", "ocr_backend": "ollama"})
    _patch_probes(monkeypatch, {OLLAMA: _ok(OLLAMA, ["llama3.2:3b"])})

    with pytest.raises(RuntimeError, match="vision-capable"):
        _resolution.resolve_models_for_run(stages={"ocr"})


def test_unreachable_server_raises(monkeypatch):
    """No reachable server yields a distinct, actionable message."""
    config.apply_overrides({"cleanup_model": "", "cleanup_backend": "auto"})
    _patch_probes(
        monkeypatch,
        {OLLAMA: _down(OLLAMA), LM_STUDIO: _down(LM_STUDIO)},
    )

    with pytest.raises(RuntimeError, match="Cannot reach any local model server"):
        _resolution.resolve_models_for_run(stages={"cleanup"})


def test_explicit_lm_studio_backend_honoured(monkeypatch):
    """An explicit ``lm_studio`` backend resolves against LM Studio only."""
    config.apply_overrides({"ocr_model": "", "ocr_backend": "lm_studio"})
    _patch_probes(monkeypatch, {LM_STUDIO: _ok(LM_STUDIO, ["richardyoung/olmocr2:7b-q8"])})

    _resolution.resolve_models_for_run(stages={"ocr"})

    assert _resolution.model_for("vision") == "richardyoung/olmocr2:7b-q8"
    assert _resolution.backend_for("vision") == "lm_studio"


def test_lm_studio_auto_detects_native_olmocr_name(monkeypatch):
    """The empty-model default recognises LM Studio's upstream olmOCR ID."""
    config.apply_overrides({"ocr_model": "", "ocr_backend": "lm_studio"})
    _patch_probes(
        monkeypatch,
        {LM_STUDIO: _ok(LM_STUDIO, ["allenai/olmocr-2-7b", "nomic-embed-text"])},
    )

    _resolution.resolve_models_for_run(stages={"ocr"})

    assert _resolution.model_for("vision") == "allenai/olmocr-2-7b"
    assert _resolution.backend_for("vision") == "lm_studio"


def test_accessor_falls_back_to_config_when_unresolved():
    """Before any resolution pass, the accessors return the raw configured
    values (empty/auto) — so an explicit choice set in config still wins when
    a stage is invoked directly."""
    config.apply_overrides({"cleanup_model": "my-model", "cleanup_backend": "ollama"})

    assert _resolution.model_for("chat") == "my-model"
    assert _resolution.backend_for("chat") == "ollama"


# ---------------------------------------------------------------------------
# Resolution logging (one line per role, redacted)
# ---------------------------------------------------------------------------


def _resolution_info_records(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "artifice_ocr._resolution" and r.levelno == logging.INFO
    ]


def test_resolution_logs_provider_url_model_source(monkeypatch, caplog):
    """One INFO line per role records provider, base URL, model and source."""
    config.apply_overrides({"ocr_model": "llava:7b", "ocr_backend": "ollama"})
    _patch_probes(monkeypatch, {OLLAMA: _ok(OLLAMA, ["llava:7b"])})

    with caplog.at_level(logging.INFO):
        _resolution.resolve_models_for_run(stages={"ocr"})

    lines = _resolution_info_records(caplog)
    assert len(lines) == 1
    assert "backend=ollama" in lines[0]
    assert "base_url=http://localhost:11434" in lines[0]
    assert "model=llava:7b" in lines[0]
    assert "source=user_choice" in lines[0]


def test_resolution_logs_exactly_once_per_role(monkeypatch, caplog):
    """Four stages (title shares the chat role) yield exactly three lines."""
    config.apply_overrides(
        {
            "ocr_model": "",
            "ocr_backend": "auto",
            "cleanup_model": "",
            "cleanup_backend": "auto",
            "translate_model": "",
            "translate_backend": "auto",
        }
    )
    _patch_probes(
        monkeypatch,
        {
            OLLAMA: _ok(OLLAMA, ["richardyoung/olmocr2:7b-q8", "llama3.2:3b"]),
            LM_STUDIO: _down(LM_STUDIO),
        },
    )

    with caplog.at_level(logging.INFO):
        _resolution.resolve_models_for_run(stages={"ocr", "cleanup", "title", "translate"})

    lines = _resolution_info_records(caplog)
    assert len(lines) == 3  # vision, chat (cleanup+title), translation
    for line in lines:
        assert "backend=" in line
        assert "base_url=" in line
        assert "model=" in line
        assert "source=" in line


def test_resolution_log_never_contains_api_key(monkeypatch, caplog):
    """A configured api_key must never reach a log record."""
    config.apply_overrides(
        {
            "ocr_model": "gpt-4o-mini",
            "ocr_backend": "api_key",
            "api_key": "sk-super-secret-value",
            "api_base_url": "http://localhost:8080/v1",
        }
    )

    with caplog.at_level(logging.INFO):
        _resolution.resolve_models_for_run(stages={"ocr"})

    assert "sk-super-secret-value" not in caplog.text
    # The base URL itself is still logged (redacted, key-free).
    assert "http://localhost:8080/v1" in caplog.text


def test_redact_url_strips_userinfo_and_query():
    """The log helper strips credentials and query/fragment."""
    assert _resolution._redact_url("http://user:pass@localhost:11434") == ("http://localhost:11434")
    assert _resolution._redact_url("http://localhost:11434/v1?api_key=secret") == (
        "http://localhost:11434/v1"
    )
    assert _resolution._redact_url("http://localhost:11434") == "http://localhost:11434"


def test_resolution_log_redacts_userinfo(monkeypatch, caplog):
    """A URL carrying credentials is logged without its password."""
    secret_url = "http://user:secret-pass@localhost:11434"
    config.apply_overrides(
        {"ocr_model": "llava:7b", "ocr_backend": "ollama", "ollama_url": secret_url}
    )
    _patch_probes(monkeypatch, {secret_url: _ok(secret_url, ["llava:7b"])})

    with caplog.at_level(logging.INFO):
        _resolution.resolve_models_for_run(stages={"ocr"})

    assert "secret-pass" not in caplog.text
    assert "http://localhost:11434" in caplog.text


# ---------------------------------------------------------------------------
# Preflight — each failure class names the cause and the base URL
# ---------------------------------------------------------------------------


def _seed_resolved(role="vision", backend="ollama", model="llava:7b"):
    _resolution._cache[role] = _RoleResolution(
        model=model, backend=backend, source=ResolutionSource.USER_CHOICE
    )


def test_preflight_unreachable_raises_with_url(monkeypatch):
    config.apply_overrides({"ocr_backend": "ollama", "ollama_url": OLLAMA})
    _seed_resolved()
    monkeypatch.setattr(
        _resolution,
        "probe_endpoint_sync",
        lambda *a, **k: ProbeResult(url=OLLAMA, reachable=False, hint="down"),
    )

    with pytest.raises(RuntimeError) as excinfo:
        _resolution.preflight_run(stages={"ocr"})
    msg = str(excinfo.value)
    assert "Cannot reach OCR endpoint" in msg
    assert OLLAMA in msg
    assert "backend 'ollama'" in msg
    assert "llava:7b" in msg


def test_preflight_wrong_api_shape_raises_with_url(monkeypatch):
    config.apply_overrides({"ocr_backend": "ollama", "ollama_url": OLLAMA})
    _seed_resolved()
    monkeypatch.setattr(
        _resolution,
        "probe_endpoint_sync",
        lambda *a, **k: ProbeResult(
            url=OLLAMA,
            reachable=False,
            hint="Server responded but did not return a model list.",
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        _resolution.preflight_run(stages={"ocr"})
    msg = str(excinfo.value)
    assert "does not look like a model server" in msg
    assert OLLAMA in msg


def test_preflight_model_not_installed_raises_with_url(monkeypatch):
    config.apply_overrides({"ocr_backend": "ollama", "ollama_url": OLLAMA})
    _seed_resolved(model="llava:7b")
    monkeypatch.setattr(
        _resolution,
        "probe_endpoint_sync",
        lambda *a, **k: ProbeResult(url=OLLAMA, reachable=True, models=("other-model",)),
    )

    with pytest.raises(RuntimeError) as excinfo:
        _resolution.preflight_run(stages={"ocr"})
    msg = str(excinfo.value)
    assert "not installed" in msg
    assert "llava:7b" in msg
    assert OLLAMA in msg


def test_preflight_policy_rejection_raises_with_url(monkeypatch):
    config.apply_overrides({"ocr_backend": "ollama", "ollama_url": OLLAMA})
    _seed_resolved()

    def rejected(*a, **k):
        raise EndpointRejected("host resolves to the link-local address")

    monkeypatch.setattr(_resolution, "probe_endpoint_sync", rejected)

    with pytest.raises(RuntimeError) as excinfo:
        _resolution.preflight_run(stages={"ocr"})
    msg = str(excinfo.value)
    assert "policy" in msg
    assert OLLAMA in msg
    assert "llava:7b" in msg


def test_preflight_passes_when_reachable_and_model_present(monkeypatch):
    config.apply_overrides({"ocr_backend": "ollama", "ollama_url": OLLAMA})
    _seed_resolved(model="llava:7b")
    monkeypatch.setattr(
        _resolution,
        "probe_endpoint_sync",
        lambda *a, **k: ProbeResult(url=OLLAMA, reachable=True, models=("llava:7b",)),
    )

    # Must not raise.
    _resolution.preflight_run(stages={"ocr"})
