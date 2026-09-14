# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI-compatible HTTP adapter implementing the :class:`ModelProvider` contract.

This adapter speaks the OpenAI chat-completions protocol and can drive Ollama,
LM Studio, OpenAI, and any other backend that exposes a ``/v1/chat/completions``
endpoint.  It does not import any provider SDK — the transport is plain
``httpx``, which three of the four apps already depend on.

``capabilities()`` returns static knowledge, never probes the server.  The
reasoning is documented on :meth:`OpenAIProvider.capabilities`.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import httpx

from model_harness.contract import (
    EndpointPolicy,
    EndpointRejected,
    ModelConnectorConfig,
    Provider,
    ProviderCapabilities,
    RawCompletion,
    StructuredOutputMode,
    StructuredRequest,
)

logger = logging.getLogger(__name__)

# -- Static capability knowledge -----------------------------------------------

# Because ``capabilities()`` must not do I/O, capability is derived from the
# provider type and model name alone.  The table below is deliberately
# conservative: it under-declares for generic-api rather than over-claiming for
# a backend whose exact version we cannot know.

# Ollama supports ``response_format: { type: "json_object" }`` as of 0.2.x.
# Structured outputs (``json_schema``) arrived in 0.5.x but not every
# installation is current.  Claiming JSON_OBJECT is safe — the ladder will
# discover NATIVE_SCHEMA support organically when an app overrides.
#
# LM Studio 0.3.x supports ``json_object``.  Structured outputs are documented
# as experimental on some builds, so we keep the same stance.
#
# "generic-api" is the catch-all for any OpenAI-compatible endpoint.  We default
# to PROMPTED because the adapter cannot know what the server supports, and the
# contract explicitly prefers a conservative underestimate to a confusing
# validation failure at call time.

_DEFAULT_CAPABILITY: dict[Provider, StructuredOutputMode] = {
    "ollama": StructuredOutputMode.JSON_OBJECT,
    "lm-studio": StructuredOutputMode.JSON_OBJECT,
    "generic-api": StructuredOutputMode.PROMPTED,
}

# Per-model overrides.  Keyed by (provider, model), or by model if the same
# model name appears under multiple providers.  These exist so a deployer can
# assert "this particular Ollama install has structured outputs enabled" without
# editing code.
#
# None here means "not overridden" — the entries are placeholders; add real ones
# as concrete knowledge accumulates.

_MODEL_OVERRIDES: dict[tuple[Provider, str], StructuredOutputMode] = {}


# -- Adapter -------------------------------------------------------------------


class OpenAIProvider:
    """An :class:`ModelProvider` backed by an OpenAI-compatible HTTP endpoint.

    Parameters
    ----------
    provider_type:
        Which provider class this adapter represents.  Determines the default
        capability returned by :meth:`capabilities`.
    endpoint_policy:
        An object satisfying the :class:`EndpointPolicy` Protocol.  The adapter
        resolves every endpoint through this before making a request.  Pass a
        stub for testing.
    http_client:
        An ``httpx.AsyncClient`` instance.  The adapter does not own it — the
        caller manages its lifetime and can share it across calls.
    default_capability:
        Override the default capability for this adapter.  Takes precedence
        over the static lookup table for *this* adapter only.  Useful when the
        deployer knows the exact server version.
    model_capabilities:
        Per-model capability overrides, keyed by model name.  Merged on top of
        any static overrides.
    """

    def __init__(
        self,
        provider_type: Provider,
        *,
        endpoint_policy: EndpointPolicy | None = None,
        http_client: httpx.AsyncClient | None = None,
        default_capability: StructuredOutputMode | None = None,
        model_capabilities: dict[str, StructuredOutputMode] | None = None,
    ) -> None:
        self._provider_type: Provider = provider_type
        self._endpoint_policy: EndpointPolicy | None = endpoint_policy
        self._http: httpx.AsyncClient = http_client or httpx.AsyncClient()

        if default_capability is not None:
            self._default_capability = default_capability
        else:
            self._default_capability = _DEFAULT_CAPABILITY.get(
                provider_type, StructuredOutputMode.PROMPTED
            )

        self._model_capabilities: dict[str, StructuredOutputMode] = dict(
            model_capabilities or {}
        )

    # -- ModelProvider ----------------------------------------------------------

    def capabilities(self, model: str) -> ProviderCapabilities:
        """Declare what this provider and model can do.  **No I/O.**

        Capability is derived from static knowledge: the adapter's provider
        type, the model name, and any constructor overrides.  The reasoning is
        documented at the module level.

        The returned ``streaming`` field is always ``False`` — the harness
        contract deliberately excludes streaming from :class:`ModelProvider`,
        and this adapter's :meth:`complete` never streams.

        The returned ``vision`` field is always ``False`` — the adapter does
        not yet support image inputs, and declaring vision support without
        implementing it would produce a confusing failure at call time rather
        than a clear "not supported" at configuration time.
        """
        # Per-model override (constructor) wins over static knowledge.
        mode = self._model_capabilities.get(model)
        if mode is None:
            mode = _MODEL_OVERRIDES.get((self._provider_type, model))
        if mode is None:
            mode = self._default_capability

        return ProviderCapabilities(
            structured_output=mode,
            streaming=False,
            vision=False,
        )

    async def complete(self, request: StructuredRequest) -> RawCompletion:
        """Perform one call in exactly the mode the request specifies.

        The adapter does **not** choose a mode and does **not** retry into a
        different one — that is the driver's job.  This method takes the mode
        from `request.mode` and builds the correct HTTP payload for it.

        Endpoint resolution happens here, through the policy the adapter was
        constructed with.  If no policy was provided, the raw endpoint from the
        config is used — but in the intended deployment path the driver passes
        a policy and every endpoint is validated before the first byte leaves
        the machine.

        Returns
        -------
        RawCompletion
            The model's text response, plus the model identifier from the
            config (not echoed from the server, because some local backends
            return a different name from what was requested).

        Raises
        ------
        EndpointRejected
            If the endpoint policy rejects the URL.
        httpx.HTTPError
            On transport failure — the driver wraps this as appropriate.
        """
        # Resolve the endpoint before touching the network.
        endpoint = request.config.endpoint
        if self._endpoint_policy is not None:
            endpoint = self._endpoint_policy.resolve(endpoint)

        url = endpoint.rstrip("/") + "/chat/completions"
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

        text = data["choices"][0]["message"]["content"]
        return RawCompletion(text=text, model=request.config.model)

    # -- internal ---------------------------------------------------------------

    @staticmethod
    def _headers(config: ModelConnectorConfig) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        return headers

    @staticmethod
    def _build_payload(request: StructuredRequest) -> dict:
        """Build the HTTP request body for the given mode.

        Each mode produces a different payload shape.  This method does not
        validate the schema JSON — the schema was already validated when the
        request was constructed from a pydantic model.
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": request.instructions},
            {"role": "user", "content": request.input},
        ]

        base: dict = {
            "model": request.config.model,
            "messages": messages,
            "temperature": 0.0,
        }

        mode = request.mode
        if mode is StructuredOutputMode.NATIVE_SCHEMA:
            base["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": request.schema_json,
                    "strict": True,
                },
            }
        elif mode is StructuredOutputMode.JSON_OBJECT:
            # Inject the schema into the prompt so the model has context for
            # what shape to produce.  The server only guarantees valid JSON,
            # not schema conformance, so we help.
            schema_text = json.dumps(request.schema_json, indent=2)
            messages[0]["content"] = (
                f"{request.instructions}\n\n"
                f"You must respond with a single JSON object that matches "
                f"this JSON Schema:\n\n```json\n{schema_text}\n```"
            )
            base["response_format"] = {"type": "json_object"}
        elif mode is StructuredOutputMode.PROMPTED:
            schema_text = json.dumps(request.schema_json, indent=2)
            messages[0]["content"] = (
                f"{request.instructions}\n\n"
                f"You must respond with a single JSON object that matches "
                f"this JSON Schema:\n\n```json\n{schema_text}\n```"
            )
            # No response_format — the server provides no guarantee.
        # NONE is not handled here — the driver should never submit a request
        # in NONE mode.

        return base
