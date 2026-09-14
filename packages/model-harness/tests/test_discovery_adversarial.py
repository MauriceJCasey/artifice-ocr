# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Adversarial tests for :mod:`model_harness.discovery`.

These tests probe the module from outside the assumptions that shaped the
implementation and the existing 36 tests.  They exercise malformed bodies,
transport edge cases, server misbehaviour, the sync wrapper, concurrency, and
hint correctness.

No test in this file makes a real network call.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from pytest_httpx import HTTPXMock

from model_harness.discovery import (
    detect_local_servers,
    probe_endpoint,
    probe_endpoint_sync,
    _CORS_HINT,
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


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


class TestMalformedResponses:
    """The server returns a 200 that does not contain the expected JSON."""

    @pytest.mark.asyncio
    async def test_200_empty_body(self, httpx_mock: HTTPXMock):
        """A 200 with an empty body is not a reachable model list."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            status_code=200,
            content=b"",
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
            is_optional=True,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.provider == "ollama"

    @pytest.mark.asyncio
    async def test_200_html_body(self, httpx_mock: HTTPXMock):
        """A 200 returning HTML cannot be parsed as JSON."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            status_code=200,
            text="<html><body>Login</body></html>",
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
            is_optional=True,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False

    @pytest.mark.asyncio
    async def test_200_json_wrong_shape(self, httpx_mock: HTTPXMock):
        """A 200 with JSON of the wrong shape should not crash or leak."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"foo": "bar"}]},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        # The server answered /api/tags successfully, so it is reachable.
        assert result.reachable is True
        assert result.models == ()

    @pytest.mark.asyncio
    async def test_200_models_is_null(self, httpx_mock: HTTPXMock):
        """A 200 with ``models: null`` must not crash the Ollama probe."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": None},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
            is_optional=True,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        # A server that answers 200 with well-formed JSON is reachable. A null
        # models list means "nothing pulled yet", not "server is down" — the
        # original assertion here pinned the TypeError-swallowed-as-unreachable
        # bug rather than the behaviour the docstring describes.
        assert result.reachable is True
        assert result.models == ()

    @pytest.mark.asyncio
    async def test_200_openai_data_is_null(self, httpx_mock: HTTPXMock):
        """A 200 with ``data: null`` must not crash the OpenAI probe."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            status_code=404,
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            json={"data": None},
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        # See test_200_models_is_null: a well-formed 200 is reachable.
        assert result.reachable is True
        assert result.models == ()

    @pytest.mark.asyncio
    async def test_truncated_json_body(self, httpx_mock: HTTPXMock):
        """A truncated JSON body should not be treated as a reachable server."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            content=b'{"models": [{"name": "llama3.2:3b"',
            status_code=200,
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
            is_optional=True,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


class TestTransportFailures:
    """Network and TLS edge cases are reported, not masked."""

    @pytest.mark.asyncio
    async def test_dns_failure(self, httpx_mock: HTTPXMock):
        """A DNS failure produces a runner-down hint."""
        httpx_mock.add_exception(
            httpx.ConnectError("Name or service not known"),
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.provider == "ollama"
        assert _RUNNER_DOWN_HINT in result.hint

    @pytest.mark.asyncio
    async def test_connect_timeout(self, httpx_mock: HTTPXMock):
        """Connect timeout is a timeout, not a refused connection."""
        httpx_mock.add_exception(httpx.ConnectTimeout("timed out"))
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.hint == _TIMEOUT_HINT

    @pytest.mark.asyncio
    async def test_read_timeout(self, httpx_mock: HTTPXMock):
        """Read timeout is a timeout, not a refused connection."""
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False
        assert result.hint == _TIMEOUT_HINT

    @pytest.mark.asyncio
    async def test_tls_error(self, httpx_mock: HTTPXMock):
        """A TLS error is reported as unreachable."""
        httpx_mock.add_exception(
            httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
            ),
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False


# ---------------------------------------------------------------------------
# Server misbehaviour
# ---------------------------------------------------------------------------


class TestServerMisbehaviour:
    """Servers that return unexpected HTTP responses."""

    @pytest.mark.asyncio
    async def test_401_and_403(self, httpx_mock: HTTPXMock):
        """Auth failures on both endpoints mean unreachable."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            status_code=401,
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=403,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.reachable is False

    @pytest.mark.asyncio
    async def test_redirect_loop(self, httpx_mock: HTTPXMock):
        """A redirect loop is treated as a non-200 response."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            status_code=301,
            headers={"Location": "http://localhost:11434/api/tags"},
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

    @pytest.mark.asyncio
    async def test_redirect_to_different_host(self, httpx_mock: HTTPXMock):
        """A redirect to a different host is not followed by default.

        Policy validation happens on the *original* URL, so a redirect that
        would have been rejected must not be silently followed.
        """
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            status_code=302,
            headers={"Location": "http://8.8.8.8:11434/api/tags"},
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
        # No request should have been made to the redirected public host.
        requested_hosts = {r.url.host for r in httpx_mock.get_requests()}
        assert "8.8.8.8" not in requested_hosts


# ---------------------------------------------------------------------------
# Sync wrapper
# ---------------------------------------------------------------------------


class TestSyncWrapperAdversarial:
    """The synchronous wrapper must fail fast, never deadlock."""

    @pytest.mark.asyncio
    async def test_sync_wrapper_inside_loop_raises_quickly(self, httpx_mock: HTTPXMock):
        """Calling the sync wrapper from inside a running loop raises fast.

        A regression here can deadlock CI; the timeout makes the failure visible.
        """

        async def _call_from_loop():
            return probe_endpoint_sync(
                "http://localhost:11434/v1",
                policy=_policy(),
            )

        with pytest.raises(RuntimeError, match="cannot be called from inside"):
            await asyncio.wait_for(
                asyncio.create_task(_call_from_loop()),
                timeout=5.0,
            )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    """detect_local_servers must not serialise slow endpoints."""

    @pytest.mark.asyncio
    async def test_slow_endpoints_do_not_serialise(self, httpx_mock: HTTPXMock):
        """One slow endpoint should not push the wall time past the slowest probe.

        Ollama and LM Studio each take ~2 seconds; vLLM is fast.  If the
        scan is concurrent the total should be ~2 seconds.  If it is sequential
        it will be ~4 seconds and this test will fail.
        """

        async def slow_tags(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(2.0)
            return httpx.Response(200, json={"models": [{"name": "llama3.2:3b"}]})

        async def slow_models(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(2.0)
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})

        # Ollama: slow /api/tags, fast /v1/models
        httpx_mock.add_callback(
            slow_tags,
            url="http://localhost:11434/api/tags",
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
        )

        # LM Studio: fast /api/tags, slow /v1/models
        httpx_mock.add_response(
            url="http://localhost:1234/api/tags",
            status_code=404,
        )
        httpx_mock.add_callback(
            slow_models,
            url="http://localhost:1234/v1/models",
        )

        # vLLM: fast on both endpoints
        httpx_mock.add_response(
            url="http://localhost:8080/api/tags",
            status_code=404,
        )
        httpx_mock.add_response(
            url="http://localhost:8080/v1/models",
            json={"data": []},
        )

        start = time.perf_counter()
        results = await detect_local_servers(policy=_policy(), timeout_s=10.0)
        elapsed = time.perf_counter() - start

        assert len(results) == 3
        # Concurrent scan should finish in under ~3.5 seconds; sequential will
        # need ~4 seconds.
        assert elapsed < 3.5, (
            f"detect_local_servers took {elapsed:.2f}s; "
            "endpoints appear to be probed sequentially"
        )


# ---------------------------------------------------------------------------
# Hint correctness
# ---------------------------------------------------------------------------


class TestHintCorrectness:
    """Each diagnostic hint fires on its own condition and does not mask another."""

    @pytest.mark.asyncio
    async def test_timeout_hint_not_overridden(self, httpx_mock: HTTPXMock):
        """A timeout must produce the timeout hint, not the runner-down hint."""
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        assert result.hint == _TIMEOUT_HINT
        assert _RUNNER_DOWN_HINT not in result.hint

    @pytest.mark.asyncio
    async def test_fresh_ollama_install_hint(self, httpx_mock: HTTPXMock):
        """A healthy Ollama with nothing pulled gives a sensible, actionable hint."""
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
        assert result.hint == _MODEL_NOT_PULLED_HINT
        # The server is up; do not claim it is down.
        assert _RUNNER_DOWN_HINT not in result.hint
        assert _OLLAMA_SERVE_HINT not in result.hint

    @pytest.mark.asyncio
    async def test_cors_hint_does_not_mask_dns_error(self, httpx_mock: HTTPXMock):
        """A DNS error that happens to contain 'origin' must not trigger the CORS hint."""
        httpx_mock.add_exception(
            httpx.ConnectError("Name or service not known: origin.local"),
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        # Current behaviour: substring matching on 'origin' fires the CORS hint.
        # This assertion documents the expected correct behaviour.
        assert _CORS_HINT not in result.hint

    @pytest.mark.asyncio
    async def test_malformed_json_hint(self, httpx_mock: HTTPXMock):
        """A reachable server that returns invalid JSON should not be reported as down."""
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            content=b"not json",
            status_code=200,
        )
        httpx_mock.add_response(
            url="http://localhost:11434/v1/models",
            status_code=404,
            is_optional=True,
        )
        result = await probe_endpoint(
            "http://localhost:11434/v1",
            policy=_policy(),
        )
        # Current behaviour: JSONDecodeError is caught by the generic Exception
        # handler and reported as a runner-down problem, which is misleading.
        # A server that returns invalid JSON is reachable but misconfigured.
        assert _RUNNER_DOWN_HINT not in result.hint
