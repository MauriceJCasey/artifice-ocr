# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for :class:`AnthropicProvider`.

All tests mock the HTTP transport — no network, no running model.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from model_harness import (
    AnthropicProvider,
    EndpointRejected,
    ModelConnectorConfig,
    ProviderCapabilities,
    StructuredOutputMode,
    StructuredRequest,
)

M = StructuredOutputMode


def _config(**kw) -> ModelConnectorConfig:
    defaults = dict(
        provider="anthropic",
        endpoint="https://api.anthropic.com",
        model="claude-sonnet-4-20250514",
    )
    defaults.update(kw)
    return ModelConnectorConfig(**defaults)


def _make_request(
    mode: M,
    schema: dict | None = None,
    **kw,
) -> StructuredRequest:
    return StructuredRequest(
        instructions="Extract entities.",
        input="Some text.",
        schema_json=schema or {"type": "object", "properties": {"x": {"type": "string"}}},
        mode=mode,
        config=_config(**kw),
    )


def _text_json(text: str) -> dict:
    """Anthropic text-block response body (PROMPTED / JSON_OBJECT)."""
    return {"content": [{"type": "text", "text": text}]}


def _tool_use_json(data: dict) -> dict:
    """Anthropic tool-use response body (NATIVE_SCHEMA)."""
    return {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01ABC123",
                "name": "respond",
                "input": data,
            }
        ]
    }


# ── capabilities() ────────────────────────────────────────────────────────────


class TestCapabilities:
    """``capabilities()`` must not do I/O — no HTTP mock is set up."""

    def test_default_is_native_schema(self):
        provider = AnthropicProvider()
        caps = provider.capabilities("any-model")
        assert caps.structured_output is M.NATIVE_SCHEMA

    def test_supported_modes_skip_json_object(self):
        """The gap: Anthropic has no json_object mode."""
        provider = AnthropicProvider()
        caps = provider.capabilities("any-model")
        modes = caps.modes()
        assert M.NATIVE_SCHEMA in modes
        assert M.JSON_OBJECT not in modes
        assert M.PROMPTED in modes
        assert M.NONE not in modes

    def test_never_streaming(self):
        provider = AnthropicProvider()
        caps = provider.capabilities("any-model")
        assert caps.streaming is False

    def test_never_vision(self):
        provider = AnthropicProvider()
        caps = provider.capabilities("any-model")
        assert caps.vision is False

    def test_constructor_override(self):
        custom = ProviderCapabilities(
            structured_output=M.JSON_OBJECT,
            supported_modes=frozenset({M.JSON_OBJECT, M.PROMPTED}),
        )
        provider = AnthropicProvider(default_capability=custom)
        caps = provider.capabilities("any-model")
        assert caps.structured_output is M.JSON_OBJECT
        assert caps.modes() == frozenset({M.JSON_OBJECT, M.PROMPTED})

    def test_per_model_override(self):
        caps_haiku = ProviderCapabilities(
            structured_output=M.PROMPTED,
            supported_modes=frozenset({M.PROMPTED}),
        )
        provider = AnthropicProvider(
            model_capabilities={"claude-haiku": caps_haiku},
        )
        assert provider.capabilities("claude-haiku") is caps_haiku
        # Other models still get the default.
        caps = provider.capabilities("claude-opus")
        assert caps.structured_output is M.NATIVE_SCHEMA


# ── complete() — request shapes ───────────────────────────────────────────────


class TestCompleteRequestShapes:
    """Each mode must produce the correct HTTP payload for the Anthropic API."""

    @pytest.mark.asyncio
    async def test_native_schema_uses_tool_use(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_tool_use_json({"x": "hello"}))
        provider = AnthropicProvider()
        req = _make_request(M.NATIVE_SCHEMA)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        # Tools and tool_choice must be present.
        assert "tools" in body
        assert len(body["tools"]) == 1
        assert body["tools"][0]["name"] == "respond"
        assert body["tools"][0]["input_schema"] == req.schema_json
        assert body["tool_choice"] == {"type": "tool", "name": "respond"}
        # Must NOT have response_format (OpenAI-ism).
        assert "response_format" not in body

    @pytest.mark.asyncio
    async def test_prompted_has_no_tools(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert "tools" not in body
        assert "tool_choice" not in body

    @pytest.mark.asyncio
    async def test_system_is_top_level_field(self, httpx_mock: HTTPXMock):
        """Anthropic puts instructions in a top-level ``system`` field,
        not as a message."""
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.PROMPTED, instructions="Extract entities.")
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert "system" in body
        # PROMPTED mode embeds the schema into the system prompt, so the
        # system field is a superset of the original instructions.
        assert req.instructions in body["system"]
        # Messages must NOT contain a system role.
        for msg in body.get("messages", []):
            assert msg.get("role") != "system"

    @pytest.mark.asyncio
    async def test_system_includes_schema_for_prompted(self, httpx_mock: HTTPXMock):
        """For PROMPTED, the schema is embedded in the system prompt."""
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        req = _make_request(M.PROMPTED, schema=schema)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert '"name"' in body["system"]
        assert "JSON Schema" in body["system"]

    @pytest.mark.asyncio
    async def test_max_tokens_present(self, httpx_mock: HTTPXMock):
        """max_tokens is required by the Anthropic API."""
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider(max_tokens=2048)
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert body["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_max_tokens_default(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert body["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_temperature_is_zero(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert body["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_model_name_from_config(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert body["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_json_object_treated_like_prompted(self, httpx_mock: HTTPXMock):
        """JSON_OBJECT should not reach this adapter through the normal
        degradation ladder (the gap in supported_modes skips it), but when
        it does the adapter handles it defensively — same payload as
        PROMPTED."""
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.JSON_OBJECT)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert "tools" not in body
        assert "tool_choice" not in body
        assert "JSON Schema" in body["system"]


# ── complete() — headers ──────────────────────────────────────────────────────


class TestCompleteHeaders:
    """Anthropic uses ``x-api-key`` and ``anthropic-version`` headers."""

    @pytest.mark.asyncio
    async def test_api_key_header(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.PROMPTED, api_key="dummy-key-for-tests")
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        assert call.headers["x-api-key"] == "dummy-key-for-tests"
        assert "Authorization" not in call.headers

    @pytest.mark.asyncio
    async def test_no_api_key_no_auth_header(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        assert "x-api-key" not in call.headers

    @pytest.mark.asyncio
    async def test_anthropic_version_header(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        assert call.headers["anthropic-version"] == "2023-06-01"

    @pytest.mark.asyncio
    async def test_content_type_header(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        assert call.headers["Content-Type"] == "application/json"


# ── complete() — response handling ────────────────────────────────────────────


class TestCompleteResponse:
    """RawCompletion carries the right fields from the response."""

    @pytest.mark.asyncio
    async def test_text_response_joined(self, httpx_mock: HTTPXMock):
        """Multiple text blocks are joined into a single string."""
        httpx_mock.add_response(
            json={
                "content": [
                    {"type": "text", "text": '{"name": "'},
                    {"type": "text", "text": 'Alice"}'},
                ]
            }
        )
        provider = AnthropicProvider()
        result = await provider.complete(_make_request(M.PROMPTED))
        assert result.text == '{"name": "Alice"}'

    @pytest.mark.asyncio
    async def test_tool_use_response_extracts_input(self, httpx_mock: HTTPXMock):
        """For NATIVE_SCHEMA, the tool_use input field is returned as JSON text."""
        data = {"name": "Alice", "age": 30}
        httpx_mock.add_response(json=_tool_use_json(data))
        provider = AnthropicProvider()
        result = await provider.complete(_make_request(M.NATIVE_SCHEMA))
        assert json.loads(result.text) == data

    @pytest.mark.asyncio
    async def test_result_model_is_from_config(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        result = await provider.complete(_make_request(M.PROMPTED))
        assert result.model == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_empty_content(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json={"content": []})
        provider = AnthropicProvider()
        result = await provider.complete(_make_request(M.PROMPTED))
        assert result.text == ""

    @pytest.mark.asyncio
    async def test_complex_nested_tool_use(self, httpx_mock: HTTPXMock):
        nested = {"data": [1, 2, 3], "meta": {"count": 3}}
        httpx_mock.add_response(json=_tool_use_json(nested))
        provider = AnthropicProvider()
        result = await provider.complete(_make_request(M.NATIVE_SCHEMA))
        assert json.loads(result.text) == nested


# ── Endpoint resolution ──────────────────────────────────────────────────────


class TestEndpointResolution:
    """The adapter resolves through the EndpointPolicy Protocol."""

    @pytest.mark.asyncio
    async def test_stub_policy_is_called(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))

        class StubPolicy:
            def resolve(self, endpoint: str) -> str:
                return endpoint

        provider = AnthropicProvider(endpoint_policy=StubPolicy())
        await provider.complete(_make_request(M.PROMPTED))
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_stub_policy_can_rewrite_endpoint(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))

        class RewritingPolicy:
            def resolve(self, endpoint: str) -> str:
                return "http://rewritten.example.com"

        provider = AnthropicProvider(endpoint_policy=RewritingPolicy())
        await provider.complete(_make_request(M.PROMPTED))

        [call] = httpx_mock.get_requests()
        assert "rewritten.example.com" in call.url.host

    @pytest.mark.asyncio
    async def test_endpoint_appends_messages_path(self, httpx_mock: HTTPXMock):
        """The URL is constructed by appending /v1/messages to the base."""
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        await provider.complete(_make_request(M.PROMPTED))

        [call] = httpx_mock.get_requests()
        assert call.url.path.endswith("/v1/messages")

    @pytest.mark.asyncio
    async def test_rejecting_policy_raises_before_request(self, httpx_mock: HTTPXMock):
        """An endpoint policy that rejects must raise before touching the network."""

        class RejectingPolicy:
            def resolve(self, endpoint: str) -> str:
                raise EndpointRejected("no")

        provider = AnthropicProvider(endpoint_policy=RejectingPolicy())
        with pytest.raises(EndpointRejected, match="no"):
            await provider.complete(_make_request(M.PROMPTED))
        # No HTTP request was made.
        assert len(httpx_mock.get_requests()) == 0


# ── complete() — no endpoint policy ───────────────────────────────────────────


class TestCompleteWithoutPolicy:
    """When no policy is provided, the raw endpoint is used."""

    @pytest.mark.asyncio
    async def test_no_policy_uses_raw_endpoint(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider()
        req = _make_request(
            M.PROMPTED,
            endpoint="https://custom.anthropic.example.com",
        )
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        assert "custom.anthropic.example.com" in call.url.host


# ── Robustness ────────────────────────────────────────────────────────────────


class TestRobustness:
    """Edge cases that the adapter must handle."""

    @pytest.mark.asyncio
    async def test_response_with_mixed_content_blocks(self, httpx_mock: HTTPXMock):
        """Content can contain both text and tool_use blocks.  The adapter
        extracts the right one for the mode."""
        httpx_mock.add_response(
            json={
                "content": [
                    {"type": "text", "text": "Here is the data: "},
                    {
                        "type": "tool_use",
                        "id": "toolu_01X",
                        "name": "respond",
                        "input": {"name": "Bob", "age": 25},
                    },
                ]
            }
        )
        provider = AnthropicProvider()
        result = await provider.complete(_make_request(M.NATIVE_SCHEMA))
        # Tool_use input is what matters for NATIVE_SCHEMA.
        assert json.loads(result.text) == {"name": "Bob", "age": 25}

    @pytest.mark.asyncio
    async def test_native_schema_falls_back_to_text_when_no_tool_use(
        self, httpx_mock: HTTPXMock
    ):
        """If a NATIVE_SCHEMA response lacks a tool_use block (should not
        happen, but be defensive), text blocks are joined."""
        httpx_mock.add_response(
            json={"content": [{"type": "text", "text": '{"x":"fallback"}'}]}
        )
        provider = AnthropicProvider()
        result = await provider.complete(_make_request(M.NATIVE_SCHEMA))
        assert result.text == '{"x":"fallback"}'

    @pytest.mark.asyncio
    async def test_http_error_raised(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            status_code=401, json={"error": "unauthorized"}
        )
        provider = AnthropicProvider()
        with pytest.raises(Exception):
            await provider.complete(_make_request(M.PROMPTED))

    @pytest.mark.asyncio
    async def test_custom_max_tokens(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_text_json('{"x":"hello"}'))
        provider = AnthropicProvider(max_tokens=8192)
        req = _make_request(M.PROMPTED)
        await provider.complete(req)

        [call] = httpx_mock.get_requests()
        body = json.loads(call.content)
        assert body["max_tokens"] == 8192
