# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Anthropic HTTP adapter implementing the :class:`ModelProvider` contract.

This adapter speaks the Anthropic Messages API and can drive any model
accessible through it.  It does not import the Anthropic SDK — the transport
is plain ``httpx``, which the harness already depends on.

``capabilities()`` returns static knowledge, never probes the server.  The
reasoning is documented on :meth:`AnthropicProvider.capabilities`.

**Structured-output gap.**  The Anthropic API supports tool-use (which this
adapter uses for ``NATIVE_SCHEMA``) but has no ``json_object`` equivalent.
:class:`ProviderCapabilities.supported_modes` declares this gap explicitly,
and the degradation ladder skips ``JSON_OBJECT`` rather than asking this
adapter for a mode it cannot implement.  See the contract module-level docs
and the ``ProviderCapabilities`` docstring.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import httpx

from model_harness.contract import (
    EndpointPolicy,
    ModelConnectorConfig,
    ProviderCapabilities,
    RawCompletion,
    StructuredOutputMode,
    StructuredRequest,
)

logger = logging.getLogger(__name__)

# -- Static capability knowledge -----------------------------------------------

# Anthropic models support tool-use (NATIVE_SCHEMA) but have no json_object
# mode.  The ``supported_modes`` gap below is the whole reason
# ProviderCapabilities grew that field — it is not an oversight.

_ANTHROPIC_DEFAULT_MODE = StructuredOutputMode.NATIVE_SCHEMA
_ANTHROPIC_SUPPORTED_MODES = frozenset(
    {StructuredOutputMode.NATIVE_SCHEMA, StructuredOutputMode.PROMPTED}
)

# Per-model overrides.  None here means "not overridden"; add entries as
# concrete knowledge accumulates (e.g. a particular model that cannot
# reliably use tools).
_MODEL_OVERRIDES: dict[str, ProviderCapabilities] = {}

# -- Adapter -------------------------------------------------------------------


class AnthropicProvider:
    """An :class:`ModelProvider` backed by the Anthropic Messages API.

    Parameters
    ----------
    endpoint_policy:
        An object satisfying the :class:`EndpointPolicy` Protocol.  The
        adapter resolves every endpoint through this before making a request.
        Pass a stub for testing.
    http_client:
        An ``httpx.AsyncClient`` instance.  The adapter does not own it — the
        caller manages its lifetime and can share it across calls.
    default_capability:
        Override the default capability for this adapter.  The
        ``supported_modes`` of this override replaces the default gap-aware
        set — pass ``frozenset({NATIVE_SCHEMA, JSON_OBJECT, PROMPTED})`` only
        if you are certain the backend implements ``json_object``.
    model_capabilities:
        Per-model capability overrides, keyed by model name.  Merged on top
        of any static overrides.
    max_tokens:
        Maximum tokens in the response.  **Required** by the Anthropic API;
        defaults to 4096.
    """

    def __init__(
        self,
        *,
        endpoint_policy: EndpointPolicy | None = None,
        http_client: httpx.AsyncClient | None = None,
        default_capability: ProviderCapabilities | None = None,
        model_capabilities: dict[str, ProviderCapabilities] | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self._endpoint_policy: EndpointPolicy | None = endpoint_policy
        self._http: httpx.AsyncClient = http_client or httpx.AsyncClient()
        self._max_tokens: int = max_tokens

        if default_capability is not None:
            self._default_capability = default_capability
        else:
            self._default_capability = ProviderCapabilities(
                structured_output=_ANTHROPIC_DEFAULT_MODE,
                supported_modes=_ANTHROPIC_SUPPORTED_MODES,
            )

        self._model_capabilities: dict[str, ProviderCapabilities] = dict(
            model_capabilities or {}
        )

    # -- ModelProvider ----------------------------------------------------------

    def capabilities(self, model: str) -> ProviderCapabilities:
        """Declare what this provider and model can do.  **No I/O.**

        All Anthropic models support tool-use (``NATIVE_SCHEMA``) but none
        support ``json_object``.  The ``supported_modes`` set is
        ``{NATIVE_SCHEMA, PROMPTED}`` so the degradation ladder skips
        ``JSON_OBJECT``.

        The returned ``streaming`` field is always ``False`` — the harness
        contract deliberately excludes streaming from :class:`ModelProvider`.

        The returned ``vision`` field is always ``False`` — the adapter does
        not yet support image inputs.
        """
        # Per-model override (constructor) wins over static knowledge.
        caps = self._model_capabilities.get(model)
        if caps is None:
            caps = _MODEL_OVERRIDES.get(model)
        if caps is not None:
            return caps
        return self._default_capability

    async def complete(self, request: StructuredRequest) -> RawCompletion:
        """Perform one call in exactly the mode the request specifies.

        The adapter does **not** choose a mode and does **not** retry into a
        different one — that is the driver's job.  This method takes the mode
        from ``request.mode`` and builds the correct HTTP payload for it.

        Endpoint resolution happens here, through the policy the adapter was
        constructed with.  If no policy was provided, the raw endpoint from
        the config is used.

        Returns
        -------
        RawCompletion
            The model's text response, plus the model identifier from the
            config.

        Raises
        ------
        EndpointRejected
            If the endpoint policy rejects the URL.
        httpx.HTTPError
            On transport failure.
        """
        # Resolve the endpoint before touching the network.
        endpoint = request.config.endpoint
        if self._endpoint_policy is not None:
            endpoint = self._endpoint_policy.resolve(endpoint)

        url = endpoint.rstrip("/") + "/v1/messages"
        payload = self._build_payload(request)

        logger.debug("POST %s with mode=%s", url, request.mode.value)
        response = await self._http.post(
            url,
            json=payload,
            headers=self._headers(request.config),
            timeout=request.config.timeout_s,
        )
        response.raise_for_status()
        data = response.json()

        text = self._extract_text(data, request.mode)
        return RawCompletion(text=text, model=request.config.model)

    # -- internal ---------------------------------------------------------------

    @staticmethod
    def _headers(config: ModelConnectorConfig) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if config.api_key:
            headers["x-api-key"] = config.api_key
        return headers

    def _build_payload(self, request: StructuredRequest) -> dict:
        """Build the Anthropic Messages API request body for the given mode.

        Each mode produces a different payload shape.  ``JSON_OBJECT`` is
        treated identically to ``PROMPTED`` — the Anthropic API has no
        ``json_object`` mode, so both rungs embed the schema in the system
        prompt.  In normal operation the degradation ladder skips
        ``JSON_OBJECT`` for this provider, but the adapter handles it
        defensively in case of direct use.
        """
        system_text = request.instructions

        base: dict = {
            "model": request.config.model,
            "messages": [{"role": "user", "content": request.input}],
            "max_tokens": self._max_tokens,
            "temperature": 0.0,
        }

        mode = request.mode
        if mode is StructuredOutputMode.NATIVE_SCHEMA:
            # Tool-use for structured output.  The model must call the
            # "respond" tool; its ``input`` field carries the validated
            # data.
            base["tools"] = [
                {
                    "name": "respond",
                    "description": (
                        "Return structured data that exactly matches "
                        "the requested schema."
                    ),
                    "input_schema": request.schema_json,
                }
            ]
            base["tool_choice"] = {"type": "tool", "name": "respond"}
        elif mode in (StructuredOutputMode.JSON_OBJECT, StructuredOutputMode.PROMPTED):
            # No json_object on Anthropic — embed schema in the system
            # prompt for both modes.  JSON_OBJECT should not reach this
            # adapter through the normal degradation ladder (the gap in
            # supported_modes skips it), but the adapter handles it
            # defensively.
            schema_text = json.dumps(request.schema_json, indent=2)
            system_text = (
                f"{request.instructions}\n\n"
                f"You must respond with a single JSON object that matches "
                f"this JSON Schema:\n\n```json\n{schema_text}\n```"
            )
        # NONE is not handled here — the driver should never submit a
        # request in NONE mode.

        # System prompt is a top-level field, not a message.
        base["system"] = system_text

        return base

    @staticmethod
    def _extract_text(
        data: dict, mode: StructuredOutputMode
    ) -> str:
        """Extract text from an Anthropic response.

        For ``NATIVE_SCHEMA`` the response is a ``tool_use`` content block
        whose ``input`` field is the structured data.  For every other mode
        the response is one or more ``text`` content blocks joined together.
        """
        content: list[dict] = data.get("content", [])

        if mode is StructuredOutputMode.NATIVE_SCHEMA:
            for block in content:
                if block.get("type") == "tool_use":
                    return json.dumps(block.get("input", {}))
            # Fallback: if no tool_use block is present (should not happen
            # with ``tool_choice`` set but be defensive), join text blocks.
            logger.warning(
                "NATIVE_SCHEMA response contained no tool_use block; "
                "falling back to text extraction."
            )

        # Join all text blocks — the common case for PROMPTED / JSON_OBJECT
        # and the fallback for a tool_use-less NATIVE_SCHEMA response.
        parts = [
            block["text"]
            for block in content
            if block.get("type") == "text"
        ]
        return "".join(parts)


__all__ = ["AnthropicProvider"]
