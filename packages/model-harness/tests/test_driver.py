# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for :func:`run_structured` — the degradation-ladder driver.

All tests use stub :class:`ModelProvider` adapters that return canned responses.
No HTTP, no running model.
"""

from __future__ import annotations

import json
from typing import Optional, cast

import pytest
from pydantic import BaseModel, ValidationError

from model_harness import (
    EndpointPolicy,
    EndpointRejected,
    HarnessResult,
    ModelConnectorConfig,
    ModelProvider,
    Provider,
    ProviderCapabilities,
    RawCompletion,
    SchemaValidationFailed,
    StructuredOutputMode,
    StructuredOutputUnsupported,
    StructuredRequest,
    run_structured,
)

M = StructuredOutputMode


# ── Test schema ───────────────────────────────────────────────────────────────


class Person(BaseModel):
    name: str
    age: int


class StrictPerson(BaseModel):
    name: str
    age: int


PERSON_SCHEMA = Person.model_json_schema()


def _config(**kw) -> ModelConnectorConfig:
    defaults = dict(
        provider=cast(Provider, "ollama"),
        endpoint="http://localhost:11434/v1",
        model="test-model",
    )
    defaults.update(kw)
    return ModelConnectorConfig(**defaults)


def _request(mode: M = M.JSON_OBJECT, **kw) -> StructuredRequest:
    defaults = dict(
        instructions="Extract a person.",
        input="Alice is 30 years old.",
        schema_json=PERSON_SCHEMA,
        mode=mode,
        config=_config(),
    )
    defaults.update(kw)
    return StructuredRequest(**defaults)


# ── Stub providers ────────────────────────────────────────────────────────────


class _StaticProvider:
    """A provider that returns the same text for every call."""

    def __init__(
        self,
        text: str,
        mode: StructuredOutputMode = M.JSON_OBJECT,
        *,
        supported_modes: frozenset[StructuredOutputMode] | None = None,
    ):
        self._text = text
        self._mode = mode
        self._supported_modes = supported_modes
        self.calls: list[StructuredRequest] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=self._mode,
            supported_modes=self._supported_modes,
        )

    async def complete(self, request: StructuredRequest) -> RawCompletion:
        self.calls.append(request)
        return RawCompletion(text=self._text, model=request.config.model)


class _SequenceProvider:
    """A provider that returns canned responses in sequence."""

    def __init__(
        self,
        texts: list[str],
        mode: StructuredOutputMode = M.NATIVE_SCHEMA,
        *,
        supported_modes: frozenset[StructuredOutputMode] | None = None,
    ):
        self._texts = texts
        self._mode = mode
        self._supported_modes = supported_modes
        self.calls: list[StructuredRequest] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=self._mode,
            supported_modes=self._supported_modes,
        )

    async def complete(self, request: StructuredRequest) -> RawCompletion:
        self.calls.append(request)
        if not self._texts:
            raise RuntimeError("No more canned responses")
        text = self._texts.pop(0)
        return RawCompletion(text=text, model=request.config.model)


# ── Happy path ────────────────────────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_success_first_try_no_repair(self):
        provider = _StaticProvider(
            json.dumps({"name": "Alice", "age": 30}),
            mode=M.NATIVE_SCHEMA,
        )
        result = await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert result.data == Person(name="Alice", age=30)
        assert result.mode_used is M.NATIVE_SCHEMA
        assert result.repaired is False
        assert result.model == "test-model"

    @pytest.mark.asyncio
    async def test_mode_used_is_the_providers_best_not_the_callers_floor(self):
        """mode_used must be what actually ran, not what the caller asked for."""
        provider = _StaticProvider(
            json.dumps({"name": "Alice", "age": 30}),
            mode=M.NATIVE_SCHEMA,
        )
        result = await run_structured(
            _request(mode=M.PROMPTED),  # caller's floor
            provider,
            Person,
        )
        # The provider supports NATIVE_SCHEMA, so that is what was used.
        assert result.mode_used is M.NATIVE_SCHEMA

    @pytest.mark.asyncio
    async def test_raw_preserved_in_result(self):
        raw = json.dumps({"name": "Alice", "age": 30})
        provider = _StaticProvider(raw, mode=M.JSON_OBJECT)
        result = await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert result.raw == raw


# ── Degradation ───────────────────────────────────────────────────────────────


class TestDegradation:
    @pytest.mark.asyncio
    async def test_one_degradation_repaired_true(self):
        """First response is invalid JSON; second works.  repaired=True."""
        provider = _SequenceProvider(
            [
                "not json at all",  # fails on first rung
                json.dumps({"name": "Bob", "age": 25}),  # works on next rung
            ],
            mode=M.JSON_OBJECT,
        )
        result = await run_structured(
            _request(mode=M.JSON_OBJECT),
            provider,
            Person,
        )
        assert result.data == Person(name="Bob", age=25)
        assert result.repaired is True
        assert result.mode_used is M.PROMPTED  # degraded from JSON_OBJECT

    @pytest.mark.asyncio
    async def test_schema_mismatch_degradation(self):
        """Valid JSON but wrong schema → degrade, retry, succeed."""
        provider = _SequenceProvider(
            [
                json.dumps({"name": "Carol"}),  # missing "age"
                json.dumps({"name": "Carol", "age": 40}),
            ],
            mode=M.JSON_OBJECT,
        )
        result = await run_structured(
            _request(mode=M.JSON_OBJECT),
            provider,
            Person,
        )
        assert result.data == Person(name="Carol", age=40)
        assert result.repaired is True
        assert result.mode_used is M.PROMPTED

    @pytest.mark.asyncio
    async def test_two_degradations(self):
        """First rung fails JSON parse, second rung fails schema, third rung
        succeeds — two degradations, repaired=True, mode_used is the third rung."""
        provider = _SequenceProvider(
            [
                "garbage",  # rung 1: NATIVE_SCHEMA → unparseable
                json.dumps({"name": "Dan"}),  # rung 2: JSON_OBJECT → missing age
                json.dumps({"name": "Dan", "age": 50}),  # rung 3: PROMPTED → works
            ],
            mode=M.NATIVE_SCHEMA,
        )
        result = await run_structured(
            _request(mode=M.JSON_OBJECT),  # floor is lower, so native is chosen
            provider,
            Person,
        )
        assert result.data == Person(name="Dan", age=50)
        assert result.repaired is True
        assert result.mode_used is M.PROMPTED

    @pytest.mark.asyncio
    async def test_degradation_preserves_mode_used_as_last_successful(self):
        """mode_used is the rung that actually produced valid data."""
        provider = _SequenceProvider(
            [
                "not json",
                json.dumps({"name": "Eve", "age": 35}),
            ],
            mode=M.JSON_OBJECT,
        )
        result = await run_structured(
            _request(mode=M.JSON_OBJECT),
            provider,
            Person,
        )
        assert result.mode_used is M.PROMPTED


# ── Ladder bottoms out ────────────────────────────────────────────────────────


class TestLadderBottomOut:
    @pytest.mark.asyncio
    async def test_raises_structured_output_unsupported(self):
        """When every rung fails, the driver raises, never returns prose."""
        provider = _SequenceProvider(
            [
                "garbage 1",  # JSON_OBJECT
                "garbage 2",  # PROMPTED
            ],
            mode=M.JSON_OBJECT,
        )
        with pytest.raises(StructuredOutputUnsupported):
            await run_structured(
                _request(mode=M.JSON_OBJECT),
                provider,
                Person,
            )

    @pytest.mark.asyncio
    async def test_provider_with_none_capability_is_refused_early(self):
        """A provider declaring NONE should be refused by select_mode."""
        provider = _StaticProvider("anything", mode=M.NONE)
        with pytest.raises(StructuredOutputUnsupported):
            await run_structured(
                _request(mode=M.PROMPTED),
                provider,
                Person,
            )


# ── Caller minimum ────────────────────────────────────────────────────────────


class TestCallerMinimum:
    @pytest.mark.asyncio
    async def test_minimum_stronger_than_provider_raises(self):
        """Caller demands NATIVE_SCHEMA but provider only has JSON_OBJECT."""
        provider = _StaticProvider("anything", mode=M.JSON_OBJECT)
        with pytest.raises(StructuredOutputUnsupported):
            await run_structured(
                _request(mode=M.NATIVE_SCHEMA),
                provider,
                Person,
            )

    @pytest.mark.asyncio
    async def test_minimum_via_override_parameter(self):
        """The ``minimum`` keyword overrides the request's mode."""
        provider = _StaticProvider("anything", mode=M.JSON_OBJECT)
        with pytest.raises(StructuredOutputUnsupported):
            await run_structured(
                _request(mode=M.PROMPTED),  # weak floor in request
                provider,
                Person,
                minimum=M.NATIVE_SCHEMA,  # stronger in parameter
            )

    @pytest.mark.asyncio
    async def test_minimum_weaker_than_provider_uses_provider_best(self):
        """Caller sets a low floor; driver uses the provider's best."""
        provider = _StaticProvider(
            json.dumps({"name": "Frank", "age": 60}),
            mode=M.NATIVE_SCHEMA,
        )
        result = await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert result.mode_used is M.NATIVE_SCHEMA


# ── Endpoint policy ───────────────────────────────────────────────────────────


class TestEndpointPolicy:
    @pytest.mark.asyncio
    async def test_policy_rejects_before_any_request(self):
        """An endpoint policy that rejects must raise before touching the provider."""

        class RejectingPolicy:
            def resolve(self, endpoint: str) -> str:
                raise EndpointRejected("no")

        provider = _StaticProvider("anything", mode=M.JSON_OBJECT)
        with pytest.raises(EndpointRejected, match="no"):
            await run_structured(
                _request(),
                provider,
                Person,
                endpoint_policy=RejectingPolicy(),
            )
        # The provider was never called.
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_policy_accepts_then_proceeds(self):
        """A permissive policy allows the call to proceed."""

        class PermissivePolicy:
            def resolve(self, endpoint: str) -> str:
                return endpoint

        provider = _StaticProvider(
            json.dumps({"name": "Grace", "age": 45}),
            mode=M.JSON_OBJECT,
        )
        result = await run_structured(
            _request(),
            provider,
            Person,
            endpoint_policy=PermissivePolicy(),
        )
        assert result.data == Person(name="Grace", age=45)
        assert len(provider.calls) == 1


# ── JSON extraction ───────────────────────────────────────────────────────────


class TestJsonExtraction:
    """The driver strips markdown code fences before parsing JSON."""

    @pytest.mark.asyncio
    async def test_markdown_fenced_json(self):
        text = '```json\n{"name": "Hank", "age": 55}\n```'
        provider = _StaticProvider(text, mode=M.PROMPTED)
        result = await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert result.data == Person(name="Hank", age=55)

    @pytest.mark.asyncio
    async def test_markdown_fenced_no_language_tag(self):
        text = '```\n{"name": "Iris", "age": 65}\n```'
        provider = _StaticProvider(text, mode=M.PROMPTED)
        result = await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert result.data == Person(name="Iris", age=65)

    @pytest.mark.asyncio
    async def test_leading_trailing_whitespace(self):
        text = '  \n  {"name": "Jack", "age": 70}\n  '
        provider = _StaticProvider(text, mode=M.PROMPTED)
        result = await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert result.data == Person(name="Jack", age=70)

    @pytest.mark.asyncio
    async def test_unparseable_text_degraded_not_raised(self):
        """Unparseable JSON triggers degradation, not SchemaValidationFailed."""
        provider = _SequenceProvider(
            [
                "unparseable garbage",
                json.dumps({"name": "Kate", "age": 75}),
            ],
            mode=M.JSON_OBJECT,
        )
        result = await run_structured(
            _request(mode=M.JSON_OBJECT),
            provider,
            Person,
        )
        # Degraded from JSON_OBJECT to PROMPTED after first rung returned
        # unparseable text.  The second rung succeeded.
        assert result.repaired is True
        assert result.mode_used is M.PROMPTED
        assert result.data == Person(name="Kate", age=75)


# ── Mode recording ────────────────────────────────────────────────────────────


class TestModeRecording:
    """The mode passed to the provider must match the rung being tried."""

    @pytest.mark.asyncio
    async def test_native_schema_called_with_native_schema(self):
        provider = _StaticProvider(
            json.dumps({"name": "Leo", "age": 80}),
            mode=M.NATIVE_SCHEMA,
        )
        await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert len(provider.calls) == 1
        assert provider.calls[0].mode is M.NATIVE_SCHEMA

    @pytest.mark.asyncio
    async def test_json_object_called_with_json_object(self):
        provider = _StaticProvider(
            json.dumps({"name": "Mia", "age": 85}),
            mode=M.JSON_OBJECT,
        )
        await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert len(provider.calls) == 1
        assert provider.calls[0].mode is M.JSON_OBJECT


# ── Mode gaps (supported_modes) ───────────────────────────────────────────────


ANTHROPIC_LIKE = frozenset({M.NATIVE_SCHEMA, M.PROMPTED})
"""A provider with a gap: supports NATIVE_SCHEMA and PROMPTED but not JSON_OBJECT."""


class TestModeGaps:
    """When a provider declares a gap in ``supported_modes``, the degradation
    ladder must skip unsupported modes rather than asking the adapter for a
    mode it cannot implement."""

    @pytest.mark.asyncio
    async def test_gap_skipped_on_degradation(self):
        """NATIVE_SCHEMA fails → skip JSON_OBJECT → PROMPTED succeeds."""
        provider = _SequenceProvider(
            [
                "garbage (native schema failed)",          # rung 1: NATIVE_SCHEMA
                json.dumps({"name": "Zara", "age": 90}),   # rung 2: PROMPTED (JSON_OBJECT skipped)
            ],
            mode=M.NATIVE_SCHEMA,
            supported_modes=ANTHROPIC_LIKE,
        )
        result = await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert result.data == Person(name="Zara", age=90)
        assert result.mode_used is M.PROMPTED  # JSON_OBJECT was never attempted
        assert result.repaired is True
        # Exactly two calls: NATIVE_SCHEMA then PROMPTED.  No JSON_OBJECT.
        assert len(provider.calls) == 2
        assert provider.calls[0].mode is M.NATIVE_SCHEMA
        assert provider.calls[1].mode is M.PROMPTED

    @pytest.mark.asyncio
    async def test_success_on_first_rung_no_gap_traversal(self):
        """NATIVE_SCHEMA succeeds → no degradation needed."""
        provider = _StaticProvider(
            json.dumps({"name": "Yara", "age": 95}),
            mode=M.NATIVE_SCHEMA,
            supported_modes=ANTHROPIC_LIKE,
        )
        result = await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert result.data == Person(name="Yara", age=95)
        assert result.mode_used is M.NATIVE_SCHEMA
        assert result.repaired is False
        assert len(provider.calls) == 1
        assert provider.calls[0].mode is M.NATIVE_SCHEMA

    @pytest.mark.asyncio
    async def test_both_rungs_fail_raises(self):
        """When both NATIVE_SCHEMA and PROMPTED fail, ladder bottoms out."""
        provider = _SequenceProvider(
            [
                "garbage 1",
                "garbage 2",
            ],
            mode=M.NATIVE_SCHEMA,
            supported_modes=ANTHROPIC_LIKE,
        )
        with pytest.raises(StructuredOutputUnsupported):
            await run_structured(
                _request(mode=M.PROMPTED),
                provider,
                Person,
            )

    @pytest.mark.asyncio
    async def test_schema_mismatch_then_prompted_succeeds(self):
        """NATIVE_SCHEMA returns valid JSON but wrong shape → skip JSON_OBJECT → PROMPTED."""
        provider = _SequenceProvider(
            [
                json.dumps({"name": "Xen"}),                # missing "age" → schema fails
                json.dumps({"name": "Xen", "age": 100}),    # PROMPTED works
            ],
            mode=M.NATIVE_SCHEMA,
            supported_modes=ANTHROPIC_LIKE,
        )
        result = await run_structured(
            _request(mode=M.PROMPTED),
            provider,
            Person,
        )
        assert result.data == Person(name="Xen", age=100)
        assert result.mode_used is M.PROMPTED
        assert result.repaired is True
        assert len(provider.calls) == 2
        assert provider.calls[0].mode is M.NATIVE_SCHEMA
        assert provider.calls[1].mode is M.PROMPTED

    @pytest.mark.asyncio
    async def test_select_mode_returns_native_schema_even_with_gap(self):
        """select_mode returns the strongest supported mode, regardless of gaps
        below it.  The gap only affects degradation."""
        caps_with_gap = ProviderCapabilities(
            structured_output=M.NATIVE_SCHEMA,
            supported_modes=ANTHROPIC_LIKE,
        )
        from model_harness.contract import select_mode

        chosen = select_mode(caps_with_gap, minimum=M.PROMPTED)
        assert chosen is M.NATIVE_SCHEMA
