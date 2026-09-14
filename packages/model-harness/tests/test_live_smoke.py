# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Live smoke test: a real call against a running Ollama server.

This test is **not** run by default.  It is guarded by ``--live-smoke`` so that
CI never hits a real model.  When run, it:

1. Reads the endpoint from ``ARTIFICE_SMOKE_BASE_URL`` (fallback: the WSL
   gateway ``http://172.21.176.1:11434/v1`` — this address can change when WSL
   restarts, so prefer the env var).
2. Reads the model from ``ARTIFICE_SMOKE_MODEL`` (fallback: ``gemma4:12b``).
3. Skips cleanly when the server is unreachable.
4. Makes a real call through the adapter + driver and validates the response.

Usage::

    uv run pytest tests/test_live_smoke.py --live-smoke -v

The ``--live-smoke`` flag exists so ``pytest`` without it (which is what CI
does) never discovers these tests.  No API key is stored in this file or any
other tracked file — the adapter reads it from the config, which this test
leaves ``None``.
"""

from __future__ import annotations

import os
from typing import cast

import httpx
import pytest
from pydantic import BaseModel

from model_harness import (
    ModelConnectorConfig,
    OpenAIProvider,
    Provider,
    StructuredOutputMode,
    StructuredRequest,
    run_structured,
)


# -- Configuration --------------------------------------------------------------

# Read from the environment; fall back to the WSL gateway documented in
# opencode.json.  The gateway address can change when WSL restarts, so the
# env var is the preferred way to run this test.
_DEFAULT_BASE_URL = "http://172.21.176.1:11434/v1"
SMOKE_BASE_URL = os.environ.get("ARTIFICE_SMOKE_BASE_URL", _DEFAULT_BASE_URL)
SMOKE_MODEL = os.environ.get("ARTIFICE_SMOKE_MODEL", "gemma4:12b")


# -- Response schema ------------------------------------------------------------


class _SmokePerson(BaseModel):
    name: str
    age: int


# -- Reachability check ---------------------------------------------------------


async def _is_reachable(url: str, timeout: float = 3.0) -> bool:
    """Return ``True`` if *url* responds within *timeout* seconds."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url.rstrip("/") + "/models")
            return resp.status_code == 200
    except Exception:
        return False


# -- The test -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.live_smoke
async def test_real_ollama_call_returns_valid_person():
    """Send a real request through the adapter and driver.

    Skips when Ollama is unreachable so CI never fails for that reason.
    """
    if not await _is_reachable(SMOKE_BASE_URL):
        pytest.skip(f"Ollama not reachable at {SMOKE_BASE_URL}")

    config = ModelConnectorConfig(
        provider=cast(Provider, "ollama"),
        endpoint=SMOKE_BASE_URL,
        model=SMOKE_MODEL,
        timeout_s=30.0,
    )

    request = StructuredRequest(
        instructions=(
            "You are a data extraction tool.  Return exactly one JSON object "
            "with the keys 'name' and 'age'."
        ),
        input="Alice is 30 years old.",
        schema_json=_SmokePerson.model_json_schema(),
        mode=StructuredOutputMode.JSON_OBJECT,
        config=config,
    )

    provider = OpenAIProvider(cast(Provider, "ollama"))

    result = await run_structured(request, provider, _SmokePerson)

    # The response must match the schema.
    assert isinstance(result.data, _SmokePerson)
    assert isinstance(result.data.name, str)
    assert isinstance(result.data.age, int)

    # mode_used must be truthfully reported — never inferred.
    assert result.mode_used in (
        StructuredOutputMode.NATIVE_SCHEMA,
        StructuredOutputMode.JSON_OBJECT,
        StructuredOutputMode.PROMPTED,
    )

    # raw text must be present.
    assert len(result.raw) > 0
    assert result.model == SMOKE_MODEL
