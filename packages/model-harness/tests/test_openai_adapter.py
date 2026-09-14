# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for :class:`OpenAIProvider`.

All tests mock the HTTP transport — no network, no running model.
"""

from __future__ import annotations

import json
from typing import cast

import httpx
import pytest
from pytest_httpx import HTTPXMock

from model_harness import (
    ModelConnectorConfig,
    OpenAIProvider,
    Provider,
    ProviderCapabilities,
    RawCompletion,
    StructuredOutputMode,
    StructuredRequest,
)

M = StructuredOutputMode


def _config(**kw) -> ModelConnectorConfig:
    defaults = dict(
        provider=cast(Provider, "ollama"),
        endpoint="http://localhost:11434/v1",
        model="test-model",
    )
    defaults.update(kw)
    return ModelConnectorConfig(**defaults)


def _make_request(mode: M, schema: dict | None = None) -> StructuredRequest:
    return StructuredRequest(
        instructions="Extract entities.",
        input="Some text.",
        schema_json=schema or {"type": "object", "properties": {"x": {"type": "string"}}},
        mode=mode,
        config=_config(),
    )


def _json_response(data: dict) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(data)}}],
    })


# ── capabilities() ────────────────────────────────────────────────────────────


class TestCapabilities:
    """``capabilities()`` must not do I/O — no HTTP mock is set up."""

    @pytest.mark.parametrize(
        ("provider_type", "expected"),
        [
            ("ollama", M.JSON_OBJECT),
            ("lm-studio", M.JSON_OBJECT),
            ("generic-api", M.PROMPTED),
        ],
    )
    def test_defaults_per_provider(self, provider_type, expected):
        provider = OpenAIProvider(cast(Provider, provider_type))
        caps = provider.capabilities("any-model")
        assert caps.structured_output is expected

    def test_constructor_override(self):
        provider = OpenAIProvider(
            cast(Provider, "ollama"),
            default_capability=M.NATIVE_SCHEMA,
        )
        caps = provider.capabilities("any-model")
        assert caps.structured_output is M.NATIVE_SCHEMA

    def test_per_model_override(self):
        provider = OpenAIProvider(
            cast(Provider, "generic-api"),
            model_capabilities={"gpt-4": M.NATIVE_SCHEMA},
        )
        # Default for generic-api is PROMPTED
        assert provider.capabilities("unknown-model").structured_output is M.PROMPTED
        # But gpt-4 overrides
        assert provider.capabilities("gpt-4").structured_output is M.NATIVE_SCHEMA

    def test_never_streaming(self):
        provider = OpenAIProvider(cast(Provider, "ollama"))
        caps = provider.capabilities("any-model")
        assert caps.streaming is False

    def test_never_vision(self):
        provider = OpenAIProvider(cast(Provider, "ollama"))
        caps = provider.capabilities("any-model")
        assert caps.vision is False

    def test_unknown_provider_defaults_to_prompted(self):
        """A provider type not in _DEFAULT_CAPABILITY must degrade safely."""
        provider = OpenAIProvider(cast(Provider, "whisper"))
        caps = provider.capabilities("any-model")
        assert caps.structured_output is M.PROMPTED


# ── complete() — request shapes ───────────────────────────────────────────────


class TestCompleteRequestShapes:
    """Each mode must produce the correct HTTP payload."""

    @pytest.mark.asyncio
    async def test_native_schema_mode(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        req = _make_request(M.NATIVE_SCHEMA)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["name"] == "response"
        assert body["response_format"]["json_schema"]["schema"] == req.schema_json
        assert body["response_format"]["json_schema"]["strict"] is True

    @pytest.mark.asyncio
    async def test_json_object_mode(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        req = _make_request(M.JSON_OBJECT)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert body["response_format"]["type"] == "json_object"
        # Schema is embedded in the system prompt
        system = body["messages"][0]["content"]
        assert '"x"' in system

    @pytest.mark.asyncio
    async def test_prompted_mode_no_response_format(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert "response_format" not in body
        system = body["messages"][0]["content"]
        assert '"x"' in system

    @pytest.mark.asyncio
    async def test_api_key_header(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "generic-api"))
        req = StructuredRequest(
            instructions="Hi",
            input="Test",
            schema_json={"type": "object"},
            mode=M.JSON_OBJECT,
            config=_config(provider="generic-api", api_key="sk-12345"),
        )
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        assert call.headers["Authorization"] == "Bearer sk-12345"

    @pytest.mark.asyncio
    async def test_no_api_key_no_auth_header(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        req = _make_request(M.JSON_OBJECT)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        assert "Authorization" not in call.headers

    @pytest.mark.asyncio
    async def test_temperature_is_zero(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        req = _make_request(M.JSON_OBJECT)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert body["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_model_name_from_config(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        req = _make_request(M.JSON_OBJECT)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert body["model"] == "test-model"


# ── complete() — response handling ────────────────────────────────────────────


class TestCompleteResponse:
    """RawCompletion carries the right fields from the response."""

    @pytest.mark.asyncio
    async def test_returns_text_and_model(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        result = await provider.complete(_make_request(M.JSON_OBJECT))
        assert result.text == '{"x":"hello"}'
        assert result.model == "test-model"

    @pytest.mark.asyncio
    async def test_complex_nested_response(self, httpx_mock: HTTPXMock):
        nested = json.dumps({"data": [1, 2, 3], "meta": {"count": 3}})
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": nested}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        result = await provider.complete(_make_request(M.JSON_OBJECT))
        assert json.loads(result.text) == {"data": [1, 2, 3], "meta": {"count": 3}}


# ── Endpoint resolution ──────────────────────────────────────────────────────


class TestEndpointResolution:
    """The adapter resolves through the EndpointPolicy Protocol."""

    @pytest.mark.asyncio
    async def test_stub_policy_is_called(self, httpx_mock: HTTPXMock):
        """A stub satisfying the Protocol is accepted and used."""
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })

        class StubPolicy:
            def resolve(self, endpoint: str) -> str:
                return endpoint  # passthrough

        provider = OpenAIProvider(
            cast(Provider, "ollama"),
            endpoint_policy=StubPolicy(),
        )
        await provider.complete(_make_request(M.JSON_OBJECT))
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_stub_policy_can_rewrite_endpoint(self, httpx_mock: HTTPXMock):
        """The resolved endpoint is the one that receives the request."""
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })

        class RewritingPolicy:
            def resolve(self, endpoint: str) -> str:
                return "http://rewritten.example.com/v1"

        provider = OpenAIProvider(
            cast(Provider, "ollama"),
            endpoint_policy=RewritingPolicy(),
        )
        await provider.complete(_make_request(M.JSON_OBJECT))

        [call] = httpx_mock.get_requests()
        assert call.url.scheme == "http"
        assert "rewritten.example.com" in call.url.host

    @pytest.mark.asyncio
    async def test_endpoint_appends_chat_completions_path(self, httpx_mock: HTTPXMock):
        """The URL is constructed by appending /chat/completions."""
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        await provider.complete(
            _make_request(M.JSON_OBJECT, schema={"type": "object"})
        )

        [call] = httpx_mock.get_requests()
        assert call.url.path.endswith("/chat/completions")


# ── complete() — no endpoint policy ───────────────────────────────────────────


class TestCompleteWithoutPolicy:
    """When no policy is provided, the raw endpoint is used."""

    @pytest.mark.asyncio
    async def test_no_policy_uses_raw_endpoint(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '{"x":"hello"}'}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        # Custom endpoint with a port
        req = StructuredRequest(
            instructions="Hi",
            input="Test",
            schema_json={"type": "object"},
            mode=M.JSON_OBJECT,
            config=_config(endpoint="http://192.168.1.50:1234/v1"),
        )
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        assert "192.168.1.50" in call.url.host
        assert call.url.port == 1234


# ── Robustness ────────────────────────────────────────────────────────────────


class TestRobustness:
    """Edge cases that the adapter must handle."""

    @pytest.mark.asyncio
    async def test_empty_response_content(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": ""}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        result = await provider.complete(_make_request(M.JSON_OBJECT))
        assert result.text == ""

    @pytest.mark.asyncio
    async def test_response_with_whitespace(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={
            "choices": [{"message": {"content": '  \n{"x":"hello"}\n  '}}],
        })
        provider = OpenAIProvider(cast(Provider, "ollama"))
        result = await provider.complete(_make_request(M.JSON_OBJECT))
        assert '"x":"hello"' in result.text
