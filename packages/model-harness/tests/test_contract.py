# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the BYOM call contract.

``select_mode`` is the only branching logic in the contract, and it decides
whether a call proceeds with a weaker guarantee than the caller asked for.
Getting it wrong is silent by nature — the call still succeeds — so it is
tested at every rung.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from model_harness import (
    HarnessResult,
    ModelConnectorConfig,
    ProviderCapabilities,
    StructuredOutputMode,
    StructuredOutputUnsupported,
    StructuredRequest,
    select_mode,
)

M = StructuredOutputMode


def caps(mode: StructuredOutputMode) -> ProviderCapabilities:
    return ProviderCapabilities(structured_output=mode)


# ── select_mode ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode", [M.NATIVE_SCHEMA, M.JSON_OBJECT, M.PROMPTED]
)
def test_returns_the_providers_mode_when_it_meets_the_default_floor(mode):
    assert select_mode(caps(mode)) is mode


def test_a_provider_with_no_capability_is_refused_not_degraded():
    with pytest.raises(StructuredOutputUnsupported):
        select_mode(caps(M.NONE))


@pytest.mark.parametrize(
    ("available", "required"),
    [
        (M.PROMPTED, M.JSON_OBJECT),
        (M.PROMPTED, M.NATIVE_SCHEMA),
        (M.JSON_OBJECT, M.NATIVE_SCHEMA),
    ],
)
def test_a_weaker_provider_than_required_is_refused(available, required):
    with pytest.raises(StructuredOutputUnsupported):
        select_mode(caps(available), minimum=required)


@pytest.mark.parametrize(
    ("available", "required"),
    [
        (M.NATIVE_SCHEMA, M.PROMPTED),
        (M.NATIVE_SCHEMA, M.JSON_OBJECT),
        (M.JSON_OBJECT, M.PROMPTED),
    ],
)
def test_a_stronger_provider_than_required_is_accepted_at_its_own_strength(
    available, required
):
    """The caller sets a floor, not a target — it must not be downgraded."""
    assert select_mode(caps(available), minimum=required) is available


def test_exact_match_is_accepted():
    assert select_mode(caps(M.JSON_OBJECT), minimum=M.JSON_OBJECT) is M.JSON_OBJECT


def test_minimum_none_accepts_anything_the_provider_offers():
    """NONE is absent from the strength ordering on purpose; indexing it would
    raise ValueError rather than a HarnessError, so it is guarded explicitly."""
    assert select_mode(caps(M.PROMPTED), minimum=M.NONE) is M.PROMPTED


def test_minimum_none_still_refuses_a_provider_that_offers_nothing():
    with pytest.raises(StructuredOutputUnsupported):
        select_mode(caps(M.NONE), minimum=M.NONE)


# ── The types hold their shape ───────────────────────────────────────────────


class _Extracted(BaseModel):
    name: str
    confidence: float


def test_a_request_carries_the_schema_as_json_schema_not_a_class():
    """The adapter seam must not require pydantic knowledge of a provider."""
    cfg = ModelConnectorConfig(
        provider="ollama", endpoint="http://localhost:11434/v1", model="gemma4:12b"
    )
    req = StructuredRequest(
        instructions="Extract the entity.",
        input="Rosa Luxemburg wrote for Die Rote Fahne.",
        schema_json=_Extracted.model_json_schema(),
        mode=M.JSON_OBJECT,
        config=cfg,
    )
    assert req.schema_json["properties"].keys() == {"name", "confidence"}
    with pytest.raises(AttributeError):
        req.mode = M.PROMPTED  # frozen: a request cannot be edited after selection


def test_a_result_records_how_it_was_obtained():
    """mode_used and repaired are the point of the type — a lucky parse and a
    guaranteed one must not be indistinguishable to the caller."""
    result = HarnessResult(
        data=_Extracted(name="Rosa Luxemburg", confidence=0.9),
        mode_used=M.PROMPTED,
        model="gemma4:12b",
        raw='{"name": "Rosa Luxemburg", "confidence": 0.9}',
        repaired=True,
    )
    assert result.mode_used is M.PROMPTED
    assert result.repaired is True
    assert result.data.name == "Rosa Luxemburg"
