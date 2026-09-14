# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests that pin the seam: all OpenAI client construction flows through _backend."""

import ast
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from artifice_ocr import _backend, config
from model_harness.contract import EndpointRejected
from model_harness.endpoint_policy import EndpointPolicy


# ---------------------------------------------------------------------------
# Static audit: no OpenAI( outside _backend.py
# ---------------------------------------------------------------------------


def _source_files_under(pkg_dir: Path) -> list[Path]:
    return sorted(p for p in pkg_dir.rglob("*.py") if p.name != "__init__.py")


def test_no_openai_construction_outside_backend_module():
    """Every ``OpenAI(...)`` call in the source tree must live in _backend.py."""
    src_root = Path(__file__).resolve().parent.parent / "src" / "artifice_ocr"
    backend_file = src_root / "_backend.py"

    violations: list[tuple[str, int]] = []

    for py_file in _source_files_under(src_root):
        if py_file.resolve() == backend_file.resolve():
            continue
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "OpenAI":
                    violations.append((str(py_file), node.lineno))

    assert not violations, f"OpenAI(...) found outside _backend.py:\n" + "\n".join(
        f"  {f}:{ln}" for f, ln in violations
    )


# ---------------------------------------------------------------------------
# get_client routing
# ---------------------------------------------------------------------------


def test_get_client_returns_ollama_backend_by_default():
    client = _backend.get_client("ollama")
    assert isinstance(client, _backend.OllamaBackend)


def test_get_client_returns_ollama_openai_backend():
    client = _backend.get_client("ollama_openai")
    assert isinstance(client, _backend.OllamaOpenAIBackend)


def test_get_client_returns_lm_studio_backend():
    client = _backend.get_client("lm_studio")
    assert isinstance(client, _backend.LMStudioBackend)


def test_get_client_returns_huggingface_backend():
    client = _backend.get_client("huggingface")
    assert isinstance(client, _backend.HuggingFaceBackend)


def test_get_client_returns_api_key_backend():
    client = _backend.get_client("api_key")
    assert isinstance(client, _backend.ApiKeyBackend)


def test_get_client_unknown_backend_defaults_to_ollama():
    client = _backend.get_client("nonexistent")
    assert isinstance(client, _backend.OllamaBackend)


def test_get_client_case_insensitive():
    client = _backend.get_client("LM_STUDIO")
    assert isinstance(client, _backend.LMStudioBackend)


# ---------------------------------------------------------------------------
# ApiKeyBackend.health_check (retained — does not fit discovery cleanly;
# see the brief for rationale)
# ---------------------------------------------------------------------------


def test_api_key_health_check_missing_key(monkeypatch):
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "api_key": "",
        },
    )
    config.load_config()

    ok, detail = _backend.ApiKeyBackend().health_check()
    assert ok is False
    assert "No API key configured" in detail


def test_api_key_health_check_ok(monkeypatch):
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "api_key": "sk-test",
            "api_base_url": "http://10.0.0.1/v1",
        },
    )
    config.load_config()

    mock_client = MagicMock()
    with patch.object(_backend, "OpenAI", return_value=mock_client) as mock_openai_cls:
        ok, detail = _backend.ApiKeyBackend().health_check()
    assert ok is True
    assert detail is None
    mock_openai_cls.assert_called_once_with(base_url="http://10.0.0.1/v1", api_key="sk-test")


# ---------------------------------------------------------------------------
# check_lm_studio delegates to discovery.probe_endpoint_sync
# ---------------------------------------------------------------------------


def test_check_lm_studio_ok(monkeypatch):
    from artifice_ocr.utils import check_lm_studio
    from model_harness.discovery import ProbeResult

    ok_result = ProbeResult(url="http://localhost:1234/v1", reachable=True, models=("test-model",))
    monkeypatch.setattr("artifice_ocr.utils.probe_endpoint_sync", lambda *a, **k: ok_result)

    result = check_lm_studio()
    assert result is None


def test_check_lm_studio_returns_error(monkeypatch):
    from artifice_ocr.utils import check_lm_studio
    from model_harness.discovery import ProbeResult

    fail_result = ProbeResult(
        url="http://localhost:1234/v1",
        reachable=False,
        hint="Cannot reach LM Studio at http://x. Is it running?",
    )
    monkeypatch.setattr("artifice_ocr.utils.probe_endpoint_sync", lambda *a, **k: fail_result)

    result = check_lm_studio()
    assert "Cannot reach LM Studio" in result


def test_check_lm_studio_rejects_link_local(monkeypatch):
    """Policy rejection in the discovery path must propagate (security property)."""
    from artifice_ocr.utils import check_lm_studio

    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "lm_studio_url": "http://169.254.169.254/v1",
        },
    )
    monkeypatch.delenv("ARTIFICE_ALLOW_PUBLIC_MODELS", raising=False)
    config.load_config()

    with pytest.raises(EndpointRejected, match="link-local"):
        check_lm_studio()


# ---------------------------------------------------------------------------
# check_ollama error strings are preserved for CLI consumers
# ---------------------------------------------------------------------------


def test_check_ollama_unreachable_string_unchanged(monkeypatch):
    """The unreachable message is byte-identical to the pre-migration version."""
    from artifice_ocr.utils import check_ollama
    from model_harness.discovery import ProbeResult

    fail_result = ProbeResult(
        url="http://localhost:11434",
        reachable=False,
        hint="any hint",
    )
    monkeypatch.setattr("artifice_ocr.utils.probe_endpoint_sync", lambda *a, **k: fail_result)

    errors = check_ollama()
    assert errors == ["Cannot reach Ollama at http://localhost:11434. Is it running?"]


def test_check_ollama_model_missing_string_unchanged(monkeypatch):
    """The missing-model message is byte-identical to the pre-migration version."""
    from artifice_ocr.utils import check_ollama
    from model_harness.discovery import ProbeResult

    result = ProbeResult(
        url="http://localhost:11434",
        reachable=True,
        models=("other-model",),
    )
    monkeypatch.setattr("artifice_ocr.utils.probe_endpoint_sync", lambda *a, **k: result)

    errors = check_ollama(["missing-model"])
    assert errors == ['Model "missing-model" is not downloaded. Open Ollama and download it first.']


def test_check_lm_studio_unreachable_string_changed(monkeypatch):
    """LM Studio unreachable returns the URL followed by discovery's hint.

    The old LMStudioBackend.health_check returned:
    'Cannot reach LM Studio at <url>. Is it running?'
    The new path now prepends the URL to discovery's hint so the caller can
    see *which* address failed — matching check_ollama's format.
    """
    from artifice_ocr.utils import check_lm_studio
    from model_harness.discovery import ProbeResult

    fail_result = ProbeResult(
        url="http://localhost:1234/v1",
        reachable=False,
        hint="Ensure your local model runner is running. Ensure LM Studio is running.",
    )
    monkeypatch.setattr("artifice_ocr.utils.probe_endpoint_sync", lambda *a, **k: fail_result)

    result = check_lm_studio()
    assert result is not None
    assert "Cannot reach LM Studio at http://localhost:1234/v1." in result
    assert fail_result.hint in result


# ---------------------------------------------------------------------------
# OCR stage uses the native Ollama backend, not ollama_openai
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_routes_ollama_to_native_ollama(mock_get_backend, tmp_path):
    """When ocr_backend is 'ollama', the OCR stage asks for the native
    ``ollama`` client, not ``ollama_openai``.

    This inverts the routing this test asserted before: ``ollama_openai``'s
    ``extra_body`` num_ctx path is not honoured by Ollama's ``/v1`` endpoint
    (measured on live Ollama 0.33.2 — see _backend.py), so OCR now goes
    through the native backend, which converts the OpenAI-shaped vision
    message itself (see TestNativeVisionMessages in test_backend.py).
    """
    backend_name = None

    def capture_be_name(be_name):
        nonlocal backend_name
        backend_name = be_name
        mock = MagicMock()
        mock.chat.return_value = MagicMock(message=MagicMock(content="text"))
        return mock

    mock_get_backend.side_effect = capture_be_name

    config.reset()
    config.load_config()
    config.apply_overrides({"ocr_backend": "ollama", "ocr_model": "test-model"})
    try:
        from artifice_ocr.stages import ocr

        img = tmp_path / "scan.png"
        img.write_bytes(b"\x89PNG fake")
        ocr.perform(str(img), output_dir=str(tmp_path / "out"))
    finally:
        config.reset()
        config.load_config()

    assert backend_name == "ollama", f"Expected ollama, got {backend_name}"


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_passes_lm_studio_through(mock_get_backend, tmp_path):
    """When ocr_backend is 'lm_studio', it is passed through unchanged."""
    backend_name = None

    def capture_be_name(be_name):
        nonlocal backend_name
        backend_name = be_name
        mock = MagicMock()
        mock.chat.return_value = MagicMock(message=MagicMock(content="text"))
        return mock

    mock_get_backend.side_effect = capture_be_name

    config.reset()
    config.load_config()
    config.apply_overrides({"ocr_backend": "lm_studio", "ocr_model": "test-model"})
    try:
        from artifice_ocr.stages import ocr

        img = tmp_path / "scan.png"
        img.write_bytes(b"\x89PNG fake")
        ocr.perform(str(img), output_dir=str(tmp_path / "out"))
    finally:
        config.reset()
        config.load_config()

    assert backend_name == "lm_studio", f"Expected lm_studio, got {backend_name}"


# ---------------------------------------------------------------------------
# Backend clients are re-created per call (stateless, config-aware)
# ---------------------------------------------------------------------------


def test_lm_studio_client_reads_config_fresh(monkeypatch):
    """Each ._client() call honours the current config, not a cached value."""
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "lm_studio_url": "http://localhost:1111/v1",
        },
    )
    config.load_config()

    calls = []
    with patch.object(_backend, "OpenAI", side_effect=lambda **kw: calls.append(kw) or MagicMock()):
        _backend.LMStudioBackend()._client()

    assert calls[0]["base_url"] == "http://localhost:1111/v1"
    assert calls[0]["api_key"] == "lm-studio"


# ---------------------------------------------------------------------------
# Endpoint policy: link-local rejection (use-time)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_name,url_key,rejected_url",
    [
        ("ollama", "ollama_url", "http://169.254.169.254/"),
        ("ollama_openai", "ollama_url", "http://169.254.169.254/v1"),
        ("lm_studio", "lm_studio_url", "http://169.254.169.254/v1"),
        ("api_key", "api_base_url", "http://169.254.169.254/v1"),
    ],
)
def test_backend_rejects_link_local_url(monkeypatch, backend_name, url_key, rejected_url):
    """A link-local address (169.254.x.x) is refused at client construction time."""
    monkeypatch.setattr(config, "_config_cache", None)
    defaults = dict(config._DEFAULTS)
    defaults[url_key] = rejected_url
    if backend_name == "api_key":
        defaults["api_key"] = "sk-test"
    monkeypatch.setattr(config, "_DEFAULTS", defaults)
    config.load_config()

    backend = _backend.get_client(backend_name)
    with pytest.raises(EndpointRejected, match="link-local"):
        if hasattr(backend, "_client"):
            backend._client()
        else:
            backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Endpoint policy: public URL requires opt-in (use-time)
# ---------------------------------------------------------------------------


def test_api_key_backend_rejects_public_url_by_default(monkeypatch):
    """The default api_base_url (api.openai.com) is refused without the env var."""
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "api_key": "sk-test",
            "api_base_url": "http://8.8.8.8/v1",
        },
    )
    # Use a policy that explicitly denies public
    strict_policy = EndpointPolicy(allow_public=False)
    monkeypatch.setattr(_backend, "_endpoint_policy", strict_policy)
    config.load_config()

    with pytest.raises(EndpointRejected, match="public address"):
        _backend.ApiKeyBackend()._client()


def test_api_key_backend_allows_public_url_with_env_var(monkeypatch):
    """A public URL is accepted when the endpoint policy permits public."""
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "api_key": "sk-test",
            "api_base_url": "http://8.8.8.8/v1",
        },
    )
    # Use a policy that explicitly permits public
    permissive_policy = EndpointPolicy(allow_public=True)
    monkeypatch.setattr(_backend, "_endpoint_policy", permissive_policy)
    config.load_config()

    mock_client = MagicMock()
    with patch.object(_backend, "OpenAI", return_value=mock_client):
        _backend.ApiKeyBackend()._client()
    # If we reach here, the URL was accepted.


# ---------------------------------------------------------------------------
# Endpoint policy: loopback / private still works (use-time)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_name,url_key,good_url",
    [
        ("ollama", "ollama_url", "http://localhost:11434"),
        ("ollama_openai", "ollama_url", "http://localhost:11434/v1"),
        ("lm_studio", "lm_studio_url", "http://localhost:1234/v1"),
        ("api_key", "api_base_url", "http://10.0.0.1/v1"),
    ],
)
def test_backend_allows_loopback_or_private_url(monkeypatch, backend_name, url_key, good_url):
    """Loopback and private-network URLs are accepted without the public opt-in."""
    monkeypatch.setattr(config, "_config_cache", None)
    defaults = dict(config._DEFAULTS)
    defaults[url_key] = good_url
    if backend_name == "api_key":
        defaults["api_key"] = "sk-test"
    monkeypatch.setattr(config, "_DEFAULTS", defaults)
    config.load_config()

    mock_client = MagicMock()
    with patch.object(_backend, "OpenAI", return_value=mock_client):
        with patch.object(_backend.ollama, "Client", return_value=MagicMock()):
            backend = _backend.get_client(backend_name)
            if hasattr(backend, "_client"):
                backend._client()
            else:
                # Ollama native backend — verify _validate_url doesn't raise
                backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# LMStudioBackend.health_check raises on rejected URL (does not swallow)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HuggingFaceBackend — public endpoint opt-in (no URL to validate)
# ---------------------------------------------------------------------------


def test_huggingface_backend_rejects_when_public_not_allowed(monkeypatch):
    """HuggingFaceBackend.chat raises EndpointRejected without opt-in.

    The HuggingFace Inference API is a public cloud service.  The endpoint
    policy must permit public endpoints before any call is made.
    """
    import socket
    from unittest.mock import patch

    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "huggingface_token": "hf_test",
        },
    )
    monkeypatch.delenv("ARTIFICE_ALLOW_PUBLIC_MODELS", raising=False)
    config.load_config()

    # Ensure the HuggingFace hostname resolves to a public IP so the
    # policy correctly identifies it as public rather than failing DNS.
    mock_addrinfo = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ]

    backend = _backend.HuggingFaceBackend()
    with patch("socket.getaddrinfo", return_value=mock_addrinfo):
        with pytest.raises(EndpointRejected, match="public address"):
            backend.chat(
                model="test-model",
                messages=[{"role": "user", "content": "hi"}],
            )


def test_huggingface_backend_accepts_when_public_allowed(monkeypatch):
    """HuggingFaceBackend.chat passes validation when public endpoints are allowed.

    The endpoint policy check succeeds first; the SDK-level call is
    mocked so no real network request is made.
    """
    import socket
    from unittest.mock import MagicMock, patch

    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "huggingface_token": "hf_test",
        },
    )
    # Replace the module-level policy with one that permits public endpoints,
    # because the module-level instance was created at import time and does
    # not re-read the env var on each call.
    permissive_policy = EndpointPolicy(allow_public=True)
    monkeypatch.setattr(_backend, "_endpoint_policy", permissive_policy)
    config.load_config()

    # Ensure the HuggingFace hostname resolves to a public IP so the
    # policy permits it.
    mock_addrinfo = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ]

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "test response"

    mock_client = MagicMock()
    mock_client.chat_completion.return_value = mock_resp

    backend = _backend.HuggingFaceBackend()
    with patch("socket.getaddrinfo", return_value=mock_addrinfo):
        with patch.object(_backend, "InferenceClient", return_value=mock_client):
            result = backend.chat(
                model="test-model",
                messages=[{"role": "user", "content": "hi"}],
            )
            assert result.message.content == "test response"


# ---------------------------------------------------------------------------
# Ollama URL normalisation — one /v1, never two
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "configured",
    [
        "http://localhost:11434",
        "http://localhost:11434/",
        "http://localhost:11434/v1",
        "http://localhost:11434/v1/",
        "  http://localhost:11434/v1  ",
        " http://localhost:11434/ ",
    ],
)
def test_ollama_openai_client_appends_single_v1(monkeypatch, configured):
    """All four spellings of the Ollama base URL — plus surrounding whitespace —
    yield exactly one ``/v1``.

    Regression: the old code appended ``"/v1"`` unconditionally, so a stored URL
    already ending in ``/v1`` produced ``/v1/v1`` and a 404 on every chat
    completion.  Surrounding whitespace re-opened the same bug by defeating the
    ``/v1`` strip; the client must normalise before appending.  The OpenAI SDK
    posts chat completions to ``{base_url}/chat/completions``.
    """
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "ollama_url": configured,
        },
    )
    config.load_config()

    captured = {}
    with patch.object(
        _backend, "OpenAI", side_effect=lambda **kw: captured.update(kw) or MagicMock()
    ):
        _backend.OllamaOpenAIBackend()._client()

    assert captured["base_url"] == "http://localhost:11434/v1"
    assert captured["base_url"].rstrip("/") + "/chat/completions" == (
        "http://localhost:11434/v1/chat/completions"
    )


@pytest.mark.parametrize(
    "configured",
    [
        "http://localhost:11434",
        "http://localhost:11434/",
        "http://localhost:11434/v1",
        "http://localhost:11434/v1/",
    ],
)
def test_ollama_backend_constructs_client_with_configured_host(monkeypatch, configured):
    """OllamaBackend must build ``ollama.Client(host=...)`` from the configured URL.

    The old code set ``ollama.host = host``, an unused module attribute, so the
    configured host was silently ignored for cleanup, translate and language
    detection (the native client kept its import-time ``$OLLAMA_HOST`` default).
    The native Ollama API is not the OpenAI-compatible one, so the ``/v1``
    suffix must be stripped before constructing the client.
    """
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "ollama_url": configured,
        },
    )
    config.load_config()

    mock_client = MagicMock()
    with patch.object(_backend.ollama, "Client", return_value=mock_client) as mock_cls:
        _backend.OllamaBackend().chat(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
        )

    mock_cls.assert_called_once_with(host="http://localhost:11434")
    mock_client.chat.assert_called_once()


def test_ollama_backend_validates_normalised_host(monkeypatch):
    """OllamaBackend runs the endpoint policy on the *normalised* host.

    The raw config value may carry a trailing ``/v1``; the client is built from
    ``normalise_base_url(host)``.  Validation must inspect that same normalised
    value, not the raw spelling, so a future policy that inspects the path
    cannot silently miss this call site.
    """
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "ollama_url": "http://localhost:11434/v1",
        },
    )
    config.load_config()

    validated: list[str] = []
    with (
        patch.object(
            _backend,
            "_validate_url",
            side_effect=lambda url, field: validated.append(url) or url,
        ),
        patch.object(_backend.ollama, "Client", return_value=MagicMock()),
    ):
        _backend.OllamaBackend().chat(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert validated == ["http://localhost:11434"]


# ---------------------------------------------------------------------------
# LM Studio URL normalisation — one /v1, never two
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "configured",
    [
        "http://localhost:1234",
        "http://localhost:1234/",
        "http://localhost:1234/v1",
        "http://localhost:1234/v1/",
    ],
)
def test_lm_studio_client_appends_single_v1(monkeypatch, configured):
    """All four spellings of the LM Studio base URL yield exactly one ``/v1``.

    Regression: if a stored URL is missing ``/v1`` or has a trailing slash,
    the probe path (which defensively adds ``/v1``) and the chat path
    (which uses _base_url() directly) diverge. A health check passes but
    chat requests 404. The client must normalise the URL before appending
    ``/v1`` to prevent doubling.
    """
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "lm_studio_url": configured,
        },
    )
    config.load_config()

    captured = {}
    with patch.object(
        _backend, "OpenAI", side_effect=lambda **kw: captured.update(kw) or MagicMock()
    ):
        _backend.LMStudioBackend()._client()

    assert captured["base_url"] == "http://localhost:1234/v1"


def test_lm_studio_client_default_url_unchanged(monkeypatch):
    """When lm_studio_url is unset, the default ``http://localhost:1234/v1`` is used."""
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "lm_studio_url": "",
        },
    )
    config.load_config()

    captured = {}
    with patch.object(
        _backend, "OpenAI", side_effect=lambda **kw: captured.update(kw) or MagicMock()
    ):
        _backend.LMStudioBackend()._client()

    assert captured["base_url"] == "http://localhost:1234/v1"


# ---------------------------------------------------------------------------
# Provider 404 wrapping — the message must name what was called
# ---------------------------------------------------------------------------


def _openai_404():
    import httpx
    from openai import NotFoundError

    request = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")
    response = httpx.Response(404, request=request, json={"error": {"message": "not found"}})
    return NotFoundError("not found", response=response, body=None)


def test_ollama_openai_404_names_base_url_and_model(monkeypatch):
    """A provider 404 on the OCR path surfaces the attempted URL and model."""
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "ollama_url": "http://localhost:11434/v1",  # the doubled-/v1 trap
        },
    )
    config.load_config()
    _backend._logged_base_urls.clear()

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = _openai_404()
    with (
        patch.object(_backend, "OpenAI", return_value=mock_client),
        pytest.raises(RuntimeError) as excinfo,
    ):
        _backend.OllamaOpenAIBackend().chat(
            model="llava:7b",
            messages=[{"role": "user", "content": "hi"}],
        )

    msg = str(excinfo.value)
    assert "404" in msg
    assert "http://localhost:11434/v1" in msg  # exactly one /v1
    assert "llava:7b" in msg


def test_native_ollama_404_names_base_url_and_model(monkeypatch):
    """The native Ollama path wraps a 404 the same way."""
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "ollama_url": "http://localhost:11434",
        },
    )
    config.load_config()
    _backend._logged_base_urls.clear()

    mock_client = MagicMock()
    mock_client.chat.side_effect = _backend.ollama.ResponseError("model not found", status_code=404)
    with (
        patch.object(_backend.ollama, "Client", return_value=mock_client),
        pytest.raises(RuntimeError) as excinfo,
    ):
        _backend.OllamaBackend().chat(
            model="llama3.2:3b",
            messages=[{"role": "user", "content": "hi"}],
        )

    msg = str(excinfo.value)
    assert "404" in msg
    assert "http://localhost:11434" in msg
    assert "llama3.2:3b" in msg


# ---------------------------------------------------------------------------
# Base URL logging — once at INFO, never an API key
# ---------------------------------------------------------------------------


def test_backend_logs_base_url_once_at_info(monkeypatch, caplog):
    """First client construction logs INFO; a repeat for the same URL is DEBUG."""
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "ollama_url": "http://localhost:11434/v1",
        },
    )
    config.load_config()
    _backend._logged_base_urls.clear()

    with patch.object(_backend, "OpenAI", return_value=MagicMock()), caplog.at_level(logging.INFO):
        _backend.OllamaOpenAIBackend()._client()
        _backend.OllamaOpenAIBackend()._client()

    info_lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "artifice_ocr._backend" and r.levelno == logging.INFO
    ]
    assert len(info_lines) == 1
    assert "http://localhost:11434/v1" in info_lines[0]


def test_backend_log_never_contains_api_key(monkeypatch, caplog):
    """Client construction logs the base URL, never the configured key."""
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(
        config,
        "_DEFAULTS",
        {
            **config._DEFAULTS,
            "api_key": "sk-secret-backend",
            "api_base_url": "http://10.0.0.1/v1",
        },
    )
    config.load_config()
    _backend._logged_base_urls.clear()

    with patch.object(_backend, "OpenAI", return_value=MagicMock()), caplog.at_level(logging.INFO):
        _backend.ApiKeyBackend()._client()

    assert "sk-secret-backend" not in caplog.text
    assert "http://10.0.0.1/v1" in caplog.text


# ---------------------------------------------------------------------------
# Context size — num_ctx, and the actionable overflow message
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_config():
    """Isolate config mutations; these tests set context_size deliberately."""
    config.reset()
    config.load_config()
    yield
    config.reset()
    config.load_config()


class TestContextSize:
    """The model's context window, and what happens when a page overflows it.

    Not to be confused with ``chunk_max_tokens``, which splits *text* before the
    cleanup/translate stages. This is the window the model is loaded with, and
    it is what a page image overflows.
    """

    def test_zero_sends_no_num_ctx(self, clean_config):
        """0 means "leave it to the model" — the behaviour before this existed.

        A config written before this setting must not suddenly acquire a
        ceiling nobody asked for.
        """
        config.apply_overrides({"context_size": 0})
        assert _backend._configured_context_size() is None

    def test_a_positive_value_is_used(self, clean_config):
        config.apply_overrides({"context_size": 8192})
        assert _backend._configured_context_size() == 8192

    def test_a_malformed_value_falls_back_to_zero(self, clean_config):
        """A bad setting degrades to the old path rather than stopping a run."""
        for bad in ("", "not-a-number", None, -1):
            config.apply_overrides({"context_size": bad})
            assert _backend._configured_context_size() is None, bad

    def test_ollama_sends_num_ctx_only_when_set(self, clean_config):
        """``num_ctx`` rides in the same options dict as ``num_predict``."""
        config.apply_overrides({"context_size": 4096, "ollama_url": "http://localhost:11434"})
        with patch("artifice_ocr._backend.ollama.Client") as mock_client:
            mock_client.return_value.chat.return_value = MagicMock()
            _backend.OllamaBackend().chat(model="m", messages=[{"role": "user", "content": "x"}])
        assert mock_client.return_value.chat.call_args.kwargs["options"]["num_ctx"] == 4096

        config.apply_overrides({"context_size": 0})
        with patch("artifice_ocr._backend.ollama.Client") as mock_client:
            mock_client.return_value.chat.return_value = MagicMock()
            _backend.OllamaBackend().chat(model="m", messages=[{"role": "user", "content": "x"}])
        assert "num_ctx" not in mock_client.return_value.chat.call_args.kwargs["options"]

    def test_overflow_error_names_the_numbers_and_where_to_fix_it(self):
        """The raw provider error is a wall of escaped JSON. Ours is not.

        This is the string LM Studio actually returns, nested inside an OpenAI
        SDK error — reproduced verbatim so a change in our parsing is caught.
        """
        raw = (
            "Error code: 400 - {'error': {'message': '{\"error\":{\"code\":400,"
            '"message":"request (4107 tokens) exceeds the available context size '
            '(4096 tokens), try increasing it","type":"exceed_context_size_error",'
            "\"n_prompt_tokens\":4107,\"n_ctx\":4096}}', 'type': 'invalid_request_error'}}"
        )
        assert _backend._is_context_overflow(Exception(raw))

        text = str(_backend._context_overflow(Exception(raw), backend_name="lm_studio"))
        assert "4107" in text and "4096" in text
        # LM Studio cannot be changed from our Settings, so the message must not
        # send the user to a control that cannot help them.
        assert "lms load" in text
        assert "Settings" not in text

    def test_overflow_hint_is_per_backend(self):
        assert "Settings" in _backend._context_overflow_hint("ollama")
        lm_hint = _backend._context_overflow_hint("lm_studio")
        assert "LM Studio" in lm_hint and "Settings" not in lm_hint
        assert "server-side" in _backend._context_overflow_hint("api_key")

    def test_a_non_overflow_error_is_untouched(self):
        """Only overflow errors are rewritten; everything else propagates."""
        assert not _backend._is_context_overflow(Exception("connection refused"))

    def test_overflow_detection_does_not_fire_on_unrelated_errors(self):
        """Rewriting an error replaces what the user sees, so a false positive
        hides a real failure behind advice about a limit they have not hit.

        The last entry is the one that matters: an earlier draft matched the
        bare phrase "maximum context length", which fires on a *capability*
        error and would have reported it as an overflow. Detection is on
        provider error identifiers now — LM Studio's ``exceed_context_size_error``
        and OpenAI's ``context_length_exceeded`` — because providers change
        prose and not codes.
        """
        for message in (
            "maximum retries exceeded",
            "429 Too Many Requests: rate limit exceeded",
            "Read timed out after 60s",
            "model 'x' not found, try pulling it first",
            "maximum context length not supported for this model",
        ):
            assert not _backend._is_context_overflow(Exception(message)), message

    def test_overflow_detection_fires_on_both_providers(self):
        """LM Studio reports a ``type``; OpenAI reports a ``code``."""
        assert _backend._is_context_overflow(
            Exception('{"type":"exceed_context_size_error","n_ctx":4096}')
        )
        assert _backend._is_context_overflow(Exception('{"code":"context_length_exceeded"}'))

    def test_overflow_detection_fires_on_openai_prose_without_the_code(self):
        """A provider sending the sentence but not the code still matches.

        The trailing clause is what separates it from the capability error in
        the test above — "maximum context length" alone is not enough, and
        treating it as enough is what would rewrite a real failure into advice
        about a limit the user has not hit.
        """
        assert _backend._is_context_overflow(
            Exception(
                "This model's maximum context length is 4096 tokens, "
                "however you requested 5000 tokens"
            )
        )


# ---------------------------------------------------------------------------
# Native-message conversion — OCR's OpenAI-shaped vision content on the
# native Ollama backend, which is the only one that honours num_ctx.
#
# Measured on live Ollama 0.33.2: /v1/chat/completions silently ignores
# num_ctx (both nested under extra_body.options and as a bare top-level
# field) — a requested 2048 loaded as Ollama's 4096 default per /api/ps.
# The native /api/chat with options.num_ctx honoured the request exactly
# (8192 -> 8192, 2048 -> 2048). That is why OCR's vision call must go
# through OllamaBackend, and why OllamaBackend must learn to read the
# OpenAI-shaped ``image_url`` content blocks OCR sends.
# ---------------------------------------------------------------------------


class TestNativeVisionMessages:
    """``_to_native_messages`` and its use inside ``OllamaBackend.chat``."""

    def test_plain_string_content_passes_through_unchanged(self):
        """The cleanup/translate path sends plain-string content; it must not
        regress — no ``images`` key, and the string is untouched."""
        messages = [{"role": "user", "content": "translate this"}]
        native = _backend._to_native_messages(messages)
        assert native == [{"role": "user", "content": "translate this"}]
        assert "images" not in native[0]

    def test_image_url_block_becomes_native_images_field(self):
        """An OpenAI-shaped vision message becomes Ollama's native shape:
        text blocks joined into ``content``, images collected into
        ``images`` as bare base64 with the ``data:...;base64,`` prefix
        stripped (Ollama's ``images`` field takes raw base64, not a URI)."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Transcribe this page."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAABBBB"},
                    },
                ],
            }
        ]
        native = _backend._to_native_messages(messages)
        assert native == [
            {
                "role": "user",
                "content": "Transcribe this page.",
                "images": ["AAAABBBB"],
            }
        ]

    def test_multiple_text_blocks_are_joined(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,ZZZZ"},
                    },
                ],
            }
        ]
        native = _backend._to_native_messages(messages)
        assert native[0]["content"] == "first\nsecond"
        assert native[0]["images"] == ["ZZZZ"]

    def test_non_data_uri_image_url_raises_clear_error(self):
        """An http(s) image URL is a real limitation, not something to send
        to Ollama silently mangled — Ollama's ``images`` field only accepts
        base64, not a URL it would fetch itself."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                ],
            }
        ]
        with pytest.raises(ValueError, match="data:"):
            _backend._to_native_messages(messages)

    def test_text_only_list_content_omits_images_key(self):
        """No image blocks at all -> no ``images`` key, so a text-only list
        payload stays byte-identical to what a dict-comparison would expect
        of the old behaviour."""
        messages = [{"role": "user", "content": [{"type": "text", "text": "just text"}]}]
        native = _backend._to_native_messages(messages)
        assert native == [{"role": "user", "content": "just text"}]
        assert "images" not in native[0]

    def test_ollama_backend_chat_converts_vision_message_and_sends_num_ctx(self, clean_config):
        """End to end through ``OllamaBackend.chat``: the vision message is
        converted to native shape *and* num_ctx still rides in options —
        the two things the OpenAI-compatible path could not do together."""
        config.apply_overrides({"context_size": 2048, "ollama_url": "http://localhost:11434"})
        vision_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "OCR this."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,QUJD"},
                    },
                ],
            }
        ]
        with patch("artifice_ocr._backend.ollama.Client") as mock_client:
            mock_client.return_value.chat.return_value = MagicMock()
            _backend.OllamaBackend().chat(model="m", messages=vision_messages)

        call_kwargs = mock_client.return_value.chat.call_args.kwargs
        assert call_kwargs["messages"] == [
            {"role": "user", "content": "OCR this.", "images": ["QUJD"]}
        ]
        assert call_kwargs["options"]["num_ctx"] == 2048

        config.apply_overrides({"context_size": 0})
        with patch("artifice_ocr._backend.ollama.Client") as mock_client:
            mock_client.return_value.chat.return_value = MagicMock()
            _backend.OllamaBackend().chat(model="m", messages=vision_messages)

        assert "num_ctx" not in mock_client.return_value.chat.call_args.kwargs["options"]


# ---------------------------------------------------------------------------
# Provider calls must only pass arguments the provider SDK accepts
# ---------------------------------------------------------------------------


def test_provider_calls_pass_no_unknown_kwargs():
    """No provider call may pass a keyword the real SDK would reject.

    This exists because a keyword meant for our own ``_guarded_chat`` wrapper
    was inserted into the ``client.chat.completions.create(...)`` call inside
    it, in four backends at once. Every test passed and every page failed:

        Completions.create() got an unexpected keyword argument 'backend_name'

    Tests could not see it because they mock the client, and a ``MagicMock``
    accepts any keyword silently — the defect only exists where a real SDK
    object is on the other end. So this checks the *source*, not a call.

    ``_guarded_chat`` is ours and takes ``backend_name``; the provider calls it
    wraps are not and do not.
    """
    import ast

    path = Path(__file__).resolve().parents[1] / "src" / "artifice_ocr" / "_backend.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Keywords our wrapper owns. Anything here appearing in a provider call is
    # the bug this test is named for.
    OURS = {"backend_name", "base_url"}

    # Attribute chains that are a real third-party SDK call.
    PROVIDER_CALLS = {"create", "chat_completion"}

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in PROVIDER_CALLS:
            continue
        for kw in node.keywords:
            if kw.arg in OURS:
                offenders.append(f"{path.name}:{node.lineno} passes {kw.arg!r} to .{func.attr}()")

    assert not offenders, (
        "provider SDK calls must not receive our wrapper's keywords:\n  " + "\n  ".join(offenders)
    )
