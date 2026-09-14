# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared BYOM connector contract for the Artifice Suite.

`CLAUDE.md` requires that every model interaction pass through structured
schemas in this package. Until 2026-07-29 this module was 29 lines defining a
config object and a ``SchemaT`` TypeVar that nothing used — the "schema-validated
call shape" its docstring promised did not exist, and all four apps carried
their own client.

:mod:`model_harness.contract` now defines that call shape. Transport is still
per-app; adapters implement :class:`~model_harness.contract.ModelProvider`.
Porting is tracked as Phase 3 in ``IMPLEMENTATION_PLAN.md``.
"""

from __future__ import annotations

from model_harness.byom import (
    TestRequest,
    byom_recommendations,
    name_for_probe,
)
from model_harness.contract import (
    EndpointPolicy,
    EndpointRejected,
    HarnessError,
    HarnessResult,
    ModelConnectorConfig,
    ModelProvider,
    Provider,
    ProviderCapabilities,
    RawCompletion,
    SchemaT,
    SchemaValidationFailed,
    StructuredOutputMode,
    StructuredOutputUnsupported,
    StructuredRequest,
    select_mode,
)
from model_harness.driver import run_structured
from model_harness.openai_adapter import OpenAIProvider
from model_harness.anthropic_adapter import AnthropicProvider
from model_harness.discovery import (
    ProbeResult,
    detect_local_servers,
    normalise_base_url,
    probe_endpoint,
    probe_endpoint_sync,
)
from model_harness.registry import (
    ASR_MODELS,
    AsrModelInfo,
    EndpointInfo,
    HardwareTier,
    KNOWN_ENDPOINTS,
    ModelRecommendation,
    get_asr_model,
    get_endpoint,
    is_configured,
    recommendations_for_app,
)
from model_harness.resolution import (
    ModelResolution,
    ResolutionSource,
    resolve_model,
)

__all__ = [
    "ASR_MODELS",
    "AnthropicProvider",
    "AsrModelInfo",
    "EndpointInfo",
    "EndpointPolicy",
    "EndpointRejected",
    "HardwareTier",
    "HarnessError",
    "HarnessResult",
    "KNOWN_ENDPOINTS",
    "ModelConnectorConfig",
    "ModelProvider",
    "ModelRecommendation",
    "ModelResolution",
    "OpenAIProvider",
    "ProbeResult",
    "Provider",
    "ProviderCapabilities",
    "RawCompletion",
    "ResolutionSource",
    "SchemaT",
    "SchemaValidationFailed",
    "StructuredOutputMode",
    "StructuredOutputUnsupported",
    "StructuredRequest",
    "TestRequest",
    "byom_recommendations",
    "detect_local_servers",
    "get_asr_model",
    "get_endpoint",
    "is_configured",
    "name_for_probe",
    "normalise_base_url",
    "probe_endpoint",
    "probe_endpoint_sync",
    "recommendations_for_app",
    "resolve_model",
    "run_structured",
    "select_mode",
]
