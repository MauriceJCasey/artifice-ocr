# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for :mod:`model_harness.discovery`.

All tests mock the HTTP transport — no network, no running model server.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pytest_httpx import HTTPXMock

from model_harness import EndpointRejected, Provider
from model_harness.discovery import (
    ProbeResult,
    detect_local_servers,
    normalise_base_url,
    probe_endpoint,
    probe_endpoint_sync,
    _CORS_HINT,
    _LM_STUDIO_DOWN_HINT,
    _MALFORMED_RESPONSE_HINT,
    _MODEL_NOT_PULLED_HINT,
    _OLLAMA_SERVE_HINT,
    _RUNNER_DOWN_HINT,
    _TIMEOUT_HINT,
)
from model_harness.endpoint_policy import EndpointPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _policy() -> EndpointPolicy:
    """A policy that permits localhost on any port."""
    return EndpointPolicy(always_allowed_hosts=frozenset(["localhost"]))


def _json_response(status: int, body: dict | list) -> httpx.Response:
    return httpx.Response(status_code=status, json=body)


def _ollama_tags_response(models: list[dict] | None = None) -> httpx.Response:
    if models is None:
        models = [{"name": "llama3.2:3b"}, {"name": "gemma4:12b"}]
    return _json_response(200, {"models": models})


def _openai_models_response(ids: list[str] | None = None) -> httpx.Response:
    if ids is None:
        ids = ["gpt-4", "llama3.2-3b"]
    return _json_response(200, {"data": [{"id": mid} for mid in ids]})


# ---------------------------------------------------------------------------
# probe_endpoint — reachable servers
# ---------------------------------------------------------------------------


class TestProbeEndpointReachable:
    """Happy-path: the server is up and responds to model-listing endpoints."""

    @pytest.mark.asyncio
    async def test_ollama_both_endpoints_answer(self, httpx_mock: HTTPXMock):
        """Ollama exposes both /api/tags and /v1/models."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"name": "llama3.2:3b"}]},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            json={"data": [{"id": "llama3.2:3b"}]},
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is True
        assert result.provider == "ollama"
        assert "llama3.2:3b" in result.models
        assert result.hint is None

    @pytest.mark.asyncio
    async def test_openai_only_v1_models_answers(self, httpx_mock: HTTPXMock):
        """An OpenAI-compatible server answers only /v1/models."""
        # /api/tags returns 404 (not an Ollama server)
        httpx_mock.add_response(
            url="http://localhost:8080/api/tags",
            status_code=404,
        )
        httpx_mock.add_response(
            url="http://localhost:8080/v1/models",
            json={"data": [{"id": "mistral-7b"}]},
        )
        result = await probe_endpoint(
            "http://localhost:8080/v1",
            policy=_policy(),
        )
        assert result.reachable is True
        assert result.provider == "generic-api"
        assert result.models == ("mistral-7b",)
        assert result.hint is None

    @pytest.mark.asyncio
    async def test_ollama_tags_only(self, httpx_mock: HTTPXMock):
        """Only /api/tags answers — the /v1/models endpoint is down."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"name": "llama3.2:3b"}]},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=500,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is True
        assert result.provider == "ollama"
        assert result.models == ("llama3.2:3b",)

    @pytest.mark.asyncio
    async def test_model_not_pulled_hint(self, httpx_mock: HTTPXMock):
        """Ollama is reachable but returns zero models."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": []},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is True
        assert result.provider == "ollama"
        assert result.models == ()
        assert result.hint == _MODEL_NOT_PULLED_HINT

    @pytest.mark.asyncio
    async def test_deduplicates_across_sources(self, httpx_mock: HTTPXMock):
        """Models appearing in both /api/tags and /v1/models are deduplicated."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"name": "llama3.2:3b"}]},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            json={"data": [{"id": "llama3.2:3b"}]},
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        # "llama3.2:3b" should appear only once
        assert result.models.count("llama3.2:3b") == 1

    @pytest.mark.asyncio
    async def test_lm_studio_port_identified(self, httpx_mock: HTTPXMock):
        """Port 1234 is identified as lm-studio via the registry."""
        httpx_mock.add_response(url="http://localhost:1234/api/tags", status_code=404)
        httpx_mock.add_response(
            url="http://localhost:1234/v1/models",
            json={"data": [{"id": "local-model"}]},
        )
        result = await probe_endpoint(
            "http://localhost:1234/v1",
            policy=_policy(),
        )
        assert result.reachable is True
        assert result.provider == "lm-studio"
        assert result.models == ("local-model",)

    @pytest.mark.asyncio
    async def test_unrecognised_port_stays_identified_as_generic(self, httpx_mock: HTTPXMock):
        """A port not in the registry with only /v1/models gets generic-api."""
        httpx_mock.add_response(url="http://localhost:9999/api/tags", status_code=404)
        httpx_mock.add_response(
            url="http://localhost:9999/v1/models",
            json={"data": [{"id": "custom-model"}]},
        )
        result = await probe_endpoint(
            "http://localhost:9999/v1",
            policy=_policy(),
        )
        assert result.reachable is True
        assert result.provider == "generic-api"
        assert result.models == ("custom-model",)

    @pytest.mark.asyncio
    async def test_openai_on_ollama_port_identified_as_generic(self, httpx_mock: HTTPXMock):
        """API response wins over port heuristic.

        When /v1/models answers on port 11434 but /api/tags does not,
        the result must be ``generic-api`` — the API response is more
        authoritative than the port number.
        """
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            status_code=404,
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            json={"data": [{"id": "custom-model"}]},
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is True
        assert result.provider == "generic-api"
        assert result.models == ("custom-model",)


# ---------------------------------------------------------------------------
# probe_endpoint — connection failures
# ---------------------------------------------------------------------------


class TestProbeEndpointUnreachable:
    """Error paths: connection refused, timeout, etc."""

    @pytest.mark.asyncio
    async def test_connection_refused_generic(self, httpx_mock: HTTPXMock):
        """Connection refused with no registry match → generic runner-down hint."""
        httpx_mock.add_exception(
            httpx.ConnectError("[Errno 111] Connection refused"),
        )
        result = await probe_endpoint(
            "http://localhost:9999/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.provider is None
        assert _RUNNER_DOWN_HINT in result.hint
        # Generic — no provider-specific suffix
        assert _OLLAMA_SERVE_HINT not in result.hint
        assert _LM_STUDIO_DOWN_HINT not in result.hint

    @pytest.mark.asyncio
    async def test_connection_refused_ollama(self, httpx_mock: HTTPXMock):
        """Connection refused on port 11434 → runner-down + ollama-serve hints."""
        httpx_mock.add_exception(
            httpx.ConnectError(
                "[WinError 10061] No connection could be made because "
                "the target machine actively refused it"
            ),
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.provider == "ollama"
        assert _RUNNER_DOWN_HINT in result.hint
        assert _OLLAMA_SERVE_HINT in result.hint

    @pytest.mark.asyncio
    async def test_connection_refused_lm_studio(self, httpx_mock: HTTPXMock):
        """Connection refused on port 1234 → runner-down + lm-studio hints."""
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
        )
        result = await probe_endpoint(
            "http://localhost:1234/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.provider == "lm-studio"
        assert _RUNNER_DOWN_HINT in result.hint
        assert _LM_STUDIO_DOWN_HINT in result.hint

    @pytest.mark.asyncio
    async def test_cors_hint_on_failed_to_fetch(self, httpx_mock: HTTPXMock):
        """Error message containing 'failed to fetch' → OLLAMA_ORIGINS hint."""
        httpx_mock.add_exception(
            Exception("Failed to fetch from server"),
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert _CORS_HINT in result.hint

    @pytest.mark.asyncio
    async def test_cors_hint_on_origin(self, httpx_mock: HTTPXMock):
        """Error message containing 'origin' with a blocking keyword → CORS hint."""
        httpx_mock.add_exception(
            Exception("Blocked by origin policy"),
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert _CORS_HINT in result.hint

    @pytest.mark.asyncio
    async def test_cors_hint_on_cors_keyword(self, httpx_mock: HTTPXMock):
        """Error message containing 'cors' → OLLAMA_ORIGINS hint."""
        httpx_mock.add_exception(
            Exception("CORS error: disallowed origin"),
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert _CORS_HINT in result.hint

    @pytest.mark.asyncio
    async def test_timeout(self, httpx_mock: HTTPXMock):
        """Timeout → timeout hint."""
        httpx_mock.add_exception(httpx.TimeoutException("timed out"))
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.hint == _TIMEOUT_HINT

    @pytest.mark.asyncio
    async def test_unknown_error_ollama(self, httpx_mock: HTTPXMock):
        """An unknown exception on an Ollama endpoint → runner-down + ollama-serve."""
        httpx_mock.add_exception(
            Exception("Something unexpected happened"),
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert _RUNNER_DOWN_HINT in result.hint
        assert _OLLAMA_SERVE_HINT in result.hint

    @pytest.mark.asyncio
    async def test_neither_endpoint_answers(self, httpx_mock: HTTPXMock):
        """Both endpoints return non-200 but no transport error."""
        httpx_mock.add_response(url="http://localhost:11434/api/tags", status_code=500)
        httpx_mock.add_response(url="http://localhost:11434/v1/models", status_code=500)
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.provider == "ollama"
        assert "model list" in (result.hint or "")

    @pytest.mark.asyncio
    async def test_connect_error_after_partial_success(self, httpx_mock: HTTPXMock):
        """Ollama /api/tags works but /v1/models gets ConnectError."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"name": "llama3.2:3b"}]},
        )
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url="http://localhost:11434/v1/models",
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        # /api/tags succeeded → reachable, despite /v1/models failure
        assert result.reachable is True
        assert result.provider == "ollama"
        assert result.models == ("llama3.2:3b",)


# ---------------------------------------------------------------------------
# probe_endpoint — policy validation
# ---------------------------------------------------------------------------


class TestProbeEndpointPolicy:
    """The policy is called before any network request."""

    @pytest.mark.asyncio
    async def test_rejected_url_raises_endpoint_rejected(self, httpx_mock: HTTPXMock):
        """A public URL with opt-in disabled raises EndpointRejected."""
        strict = EndpointPolicy(allow_public=False)
        with pytest.raises(EndpointRejected, match="ARTIFICE_ALLOW_PUBLIC_MODELS"):
            await probe_endpoint(
                "http://8.8.8.8:11434/v1",
                policy=strict,
            )
        # No HTTP request should have been made
        assert len(httpx_mock.get_requests()) == 0

    @pytest.mark.asyncio
    async def test_policy_called_with_exact_url(self, httpx_mock: HTTPXMock):
        """The URL passed to validate_url is the raw input, not a constructed one."""
        # A policy subclass that records what it was called with
        called_with: list[str] = []

        class RecordingPolicy(EndpointPolicy):
            def validate_url(self, raw: str) -> str:
                called_with.append(raw)
                return raw

        httpx_mock.add_response(url="http://custom:1234/api/tags", status_code=404)
        httpx_mock.add_response(
            url="http://custom:1234/v1/models",
            json={"data": []},
        )

        await probe_endpoint(
            "http://custom:1234/v1",
            policy=RecordingPolicy(),
        )
        assert called_with == ["http://custom:1234/v1"]


# ---------------------------------------------------------------------------
# detect_local_servers
# ---------------------------------------------------------------------------


class TestDetectLocalServers:
    """Scanning all known endpoints."""

    @pytest.mark.asyncio
    async def test_all_three_known_endpoints_probed(self, httpx_mock: HTTPXMock):
        """detect_local_servers probes ollama, lm-studio, and vllm."""
        # Ollama reachable
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"name": "llama3.2:3b"}]},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            json={"data": [{"id": "llama3.2:3b"}]},
        )
        # LM Studio connection refused
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url="http://localhost:1234/api/tags",
        )
        # vLLM timeout
        httpx_mock.add_exception(
            httpx.TimeoutException("timed out"),
            url="http://localhost:8080/api/tags",
        )

        results = await detect_local_servers(policy=_policy())
        assert len(results) == 3

        ollama = [r for r in results if r.provider == "ollama"]
        assert len(ollama) == 1
        assert ollama[0].reachable is True
        assert ollama[0].models == ("llama3.2:3b",)

        lm_studio = [r for r in results if r.provider == "lm-studio"]
        assert len(lm_studio) == 1
        assert lm_studio[0].reachable is False
        assert _RUNNER_DOWN_HINT in lm_studio[0].hint
        assert _LM_STUDIO_DOWN_HINT in lm_studio[0].hint

        vllm = [r for r in results if r.provider == "generic-api"]
        assert len(vllm) == 1
        assert vllm[0].reachable is False
        assert vllm[0].hint == _TIMEOUT_HINT

    @pytest.mark.asyncio
    async def test_rejected_endpoints_are_omitted(self, httpx_mock: HTTPXMock):
        """Endpoints that fail policy validation are genuinely omitted.

        A subclass that rejects a specific URL must cause that endpoint to
        be absent from the results — this proves the policy check is honoured
        rather than being a no-op on localhost.
        """

        class RejectOllamaPolicy(EndpointPolicy):
            def validate_url(self, raw: str) -> str:
                if ":11434" in raw:
                    raise EndpointRejected("Ollama not permitted by test policy")
                return super().validate_url(raw)

        # LM Studio — connection refused
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url="http://localhost:1234/api/tags",
        )
        # vLLM — reachable with one model
        httpx_mock.add_response(
            url="http://localhost:8080/api/tags",
            status_code=404,
        )
        httpx_mock.add_response(
            url="http://localhost:8080/v1/models",
            json={"data": [{"id": "vllm-model"}]},
        )

        results = await detect_local_servers(policy=RejectOllamaPolicy())
        # Ollama is policy-rejected → only 2 results
        assert len(results) == 2

        providers = {r.provider for r in results}
        assert "ollama" not in providers
        assert "lm-studio" in providers
        assert "generic-api" in providers


# ---------------------------------------------------------------------------
# probe_endpoint_sync — sync wrapper
# ---------------------------------------------------------------------------


class TestProbeEndpointSync:
    """The synchronous wrapper behaves correctly."""

    def test_sync_wrapper_works_standalone(self, httpx_mock: HTTPXMock):
        """probe_endpoint_sync runs in a fresh event loop."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"name": "llama3.2:3b"}]},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            json={"data": []},
        )
        result = probe_endpoint_sync(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is True
        assert result.provider == "ollama"

    def test_sync_wrapper_inside_running_loop_raises(self, httpx_mock: HTTPXMock):
        """Calling probe_endpoint_sync from inside a running loop raises RuntimeError."""

        async def _call_from_loop():
            return probe_endpoint_sync(
                "http://localhost:11434/v1",
                policy=_policy(),
            )

        with pytest.raises(RuntimeError, match="cannot be called from inside"):
            asyncio.run(_call_from_loop())

    def test_sync_wrapper_endpoint_rejected(self, httpx_mock: HTTPXMock):
        """Policy rejection propagates through the sync wrapper."""
        strict = EndpointPolicy(allow_public=False)
        with pytest.raises(EndpointRejected, match="ARTIFICE_ALLOW_PUBLIC_MODELS"):
            probe_endpoint_sync(
                "http://8.8.8.8:11434/v1",
                policy=strict,
            )
        assert len(httpx_mock.get_requests()) == 0


# ---------------------------------------------------------------------------
# Probing malformed responses
# ---------------------------------------------------------------------------


class TestMalformedResponseHint:
    """A reachable server returning non-JSON gets a dedicated hint, not runner-down."""

    @pytest.mark.asyncio
    async def test_json_decode_error_gives_malformed_hint(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            content=b"not json",
            status_code=200,
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.hint == _MALFORMED_RESPONSE_HINT
        assert _RUNNER_DOWN_HINT not in result.hint


# ---------------------------------------------------------------------------
# ProbeResult immutability
# ---------------------------------------------------------------------------


class TestProbeResultImmutability:
    """ProbeResult is frozen — callers cannot accidentally mutate it."""

    def test_cannot_set_attribute(self):
        r = ProbeResult(url="http://localhost:11434/v1", reachable=False)
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            r.reachable = True  # type: ignore[misc]

    def test_defaults(self):
        r = ProbeResult(url="http://localhost:11434/v1", reachable=True)
        assert r.models == ()
        assert r.hint is None
        assert r.provider is None


# ---------------------------------------------------------------------------
# URL patterns — _strip_v1 edge cases
# ---------------------------------------------------------------------------


class TestUrlPatterns:
    """Edge cases for URL construction."""

    @pytest.mark.asyncio
    async def test_base_url_without_v1(self, httpx_mock: HTTPXMock):
        """Probing a URL that does not end with /v1."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"name": "test-model"}]},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            json={"data": []},
        )
        result = await probe_endpoint(
            "http://localhost:11434",
            policy=_policy(),
        )
        assert result.reachable is True
        assert result.provider == "ollama"

    @pytest.mark.asyncio
    async def test_base_url_with_trailing_slash(self, httpx_mock: HTTPXMock):
        """Trailing slashes are normalised correctly."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"name": "test-model"}]},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1/",
            policy=_policy(),
        )
        assert result.reachable is True

    @pytest.mark.asyncio
    async def test_openai_models_path_not_doubled(self, httpx_mock: HTTPXMock):
        """When base_url already includes /models, no double /models/models."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            status_code=404,
        )
        # The URL we expect to be called is .../v1/models, never .../v1/models/models
        # (Our function should append /models to the /v1 prefix, not to a base that
        # already contains it.)
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            json={"data": [{"id": "test"}]},
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is True
        # Verify no double-/models request was made
        urls = [r.url.path for r in httpx_mock.get_requests()]
        assert "/v1/models/models" not in urls


# ---------------------------------------------------------------------------
# normalise_base_url
# ---------------------------------------------------------------------------


class TestNormaliseBaseUrl:
    """``normalise_base_url`` canonicalises the four Ollama URL spellings.

    Regression: :func:`model_harness.discovery._strip_v1` removes a trailing
    ``/v1`` before probing ``/api/tags``, so a stored ``.../v1`` URL made every
    probe pass while inference appended a second ``/v1`` and 404'd.  This helper
    is the single canonical form both callers build from.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:11434",
            "http://localhost:11434/",
            "http://localhost:11434/v1",
            "http://localhost:11434/v1/",
        ],
    )
    def test_four_spellings_normalise_to_one_host(self, url):
        assert normalise_base_url(url) == "http://localhost:11434"

    def test_preserves_non_v1_path(self):
        assert (
            normalise_base_url("http://localhost:11434/custom/path")
            == "http://localhost:11434/custom/path"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "  http://localhost:11434/v1  ",
            "http://localhost:11434/v1",
            "http://localhost:11434",
            " http://localhost:11434/ ",
        ],
    )
    def test_surrounding_whitespace_normalises_to_one_host(self, url):
        """Leading/trailing whitespace must not defeat the ``/v1`` strip."""
        assert normalise_base_url(url) == "http://localhost:11434"

    def test_trailing_v1_only_stripped_not_nested(self):
        """Only a *trailing* ``/v1`` is stripped — a nested path is preserved.

        This is inherited behaviour, not a regression: the caller that appends
        ``/v1`` would also double it on this input.  Pinned so the scope is
        explicit and a later change cannot silently alter it.
        """
        assert (
            normalise_base_url("http://localhost:11434/v1/chat/completions")
            == "http://localhost:11434/v1/chat/completions"
        )

    def test_strip_v1_keeps_historical_behaviour(self):
        # _strip_v1 keeps its trailing-slash behaviour for the /v1 case; only
        # normalise_base_url drops it.  Existing callers are unaffected.
        from model_harness.discovery import _strip_v1

        assert _strip_v1("http://localhost:11434/v1") == "http://localhost:11434/"
        assert _strip_v1("http://localhost:11434") == "http://localhost:11434"


# ---------------------------------------------------------------------------
# Explicit follow_redirects=False
# ---------------------------------------------------------------------------


class TestNoRedirectFollowing:
    """Redirects are not followed — tested structurally (Firecrawl checkpoint)."""

    @pytest.mark.asyncio
    async def test_redirect_not_followed(self, httpx_mock: HTTPXMock):
        """A 302 redirect is treated as non-200, not followed."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            status_code=302,
            headers={"Location": "http://localhost:11434/other"},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
