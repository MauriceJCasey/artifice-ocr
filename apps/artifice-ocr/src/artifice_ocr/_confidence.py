# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Confidence scoring for OCR pipeline outputs.

Uses LLM self-assessment and heuristic markers to estimate quality.
"""

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from model_harness.contract import (
    ModelConnectorConfig,
    Provider,
    StructuredOutputMode,
    StructuredRequest,
)
from model_harness.driver import run_structured
from model_harness.endpoint_policy import EndpointPolicy
from model_harness.openai_adapter import OpenAIProvider
from pydantic import BaseModel, Field

from artifice_ocr._logging import get_logger
from artifice_ocr._resolution import backend_for, model_for
from artifice_ocr._retry import retry
from artifice_ocr.config import get as cfg

log = get_logger("confidence")


class SelfAssessmentSchema(BaseModel):
    """The schema the model must satisfy when rating its own confidence."""

    score: int = Field(..., ge=0, le=100, description="Confidence score 0-100")
    reasoning: str = Field(..., description="Brief explanation")


# Heuristic uncertainty markers commonly produced by LLMs
_UNCERTAINTY_MARKERS = [
    "i'm not sure",
    "i am not sure",
    "it appears that",
    "it seems",
    "possibly",
    "perhaps",
    "unclear",
    "illegible",
    "unreadable",
    "cannot determine",
    "could not read",
    "not legible",
    "damaged text",
    "faded",
    "torn",
    "obscured",
    "[unclear]",
    "[illegible]",
    "[?]",
    "...",
]

# Prompt framing that asks the model to self-rate its confidence.  The
# response *shape* (score + reasoning) is conveyed by the harness, which
# injects the schema into the prompt in PROMPTED mode — so the framing here
# only has to ask the question, not restate the JSON format.
_SELF_ASSESSMENT_INSTRUCTIONS = (
    "You just performed a translation/cleanup of a historical document. "
    "Rate your confidence in the accuracy of your output on a scale of 0-100.\n\n"
    "Consider:\n"
    "- Was the source text clear and legible?\n"
    "- Did you encounter ambiguous words or passages?\n"
    "- How confident are you in the translation choices you made?"
)

_SELF_ASSESSMENT_INPUT_TEMPLATE = (
    "Original source (first 1000 chars):\n{source_text}\n\n"
    "Your output (first 1000 chars):\n{output_text}"
)

# Backend → harness Provider mapping.
_BACKEND_PROVIDER: dict[str, Provider] = {
    "ollama": "ollama",
    "lm_studio": "lm-studio",
    "api_key": "generic-api",
    "huggingface": "generic-api",
}


@dataclass
class ConfidenceResult:
    score: int  # 0-100
    reasoning: str
    heuristic_score: int  # 0-100 from text markers
    uncertainty_markers_found: list[str]
    overall_score: int  # weighted average

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _heuristic_score(text: str) -> tuple[int, list[str]]:
    """Score text based on uncertainty marker frequency.

    Returns (score 0-100, list of markers found).
    """
    text_lower = text.lower()
    found = []
    for marker in _UNCERTAINTY_MARKERS:
        count = text_lower.count(marker)
        if count > 0:
            found.extend([marker] * min(count, 3))  # cap per-marker

    total_markers = len(found)
    word_count = max(len(text.split()), 1)
    marker_density = total_markers / word_count

    # 0% density -> 100 score, 5%+ density -> 0 score
    score = max(0, int(100 * (1 - marker_density / 0.05)))
    return score, found


def _resolve_provider_config() -> ModelConnectorConfig:
    """Build a :class:`ModelConnectorConfig` from the app config.

    Mirrors :func:`artifice_ocr.stages.title._resolve_provider_config`, keyed on
    the ``translation`` role (the role this confidence call already resolved
    through) rather than ``chat``.
    """
    backend = backend_for("translation").lower()
    provider: Provider = _BACKEND_PROVIDER.get(backend, "ollama")
    model = model_for("translation")

    endpoint: str
    api_key: str | None = None

    if backend == "ollama":
        endpoint = cfg("ollama_url") or "http://localhost:11434/v1"
    elif backend == "lm_studio":
        endpoint = cfg("lm_studio_url") or "http://localhost:1234/v1"
    elif backend == "huggingface":
        # HuggingFace Inference API exposes an OpenAI-compatible /v1 endpoint.
        endpoint = "https://api-inference.huggingface.co/v1"
        api_key = cfg("huggingface_token") or None
    else:
        # api_key or unknown
        endpoint = cfg("api_base_url") or "https://api.openai.com/v1"
        api_key = cfg("api_key") or None

    return ModelConnectorConfig(
        provider=provider,
        endpoint=endpoint,
        model=model,
        api_key=api_key,
    )


@retry(max_attempts=3, base_delay=1.0, label="Self-assessment")
def _call_self_assessment(source_text: str, output_text: str) -> SelfAssessmentSchema:
    """Ask the LLM to rate its own confidence.

    Returns a validated :class:`SelfAssessmentSchema`.  Any failure — network
    error, schema validation, unsupported structured output — raises rather
    than returning a fabricated score, so the caller's degraded-result path can
    label the outcome honestly instead of mistaking it for a genuine answer.
    """
    request = StructuredRequest(
        instructions=_SELF_ASSESSMENT_INSTRUCTIONS,
        input=_SELF_ASSESSMENT_INPUT_TEMPLATE.format(
            source_text=source_text[:1000],
            output_text=output_text[:1000],
        ),
        schema_json=SelfAssessmentSchema.model_json_schema(),
        mode=StructuredOutputMode.PROMPTED,
        config=_resolve_provider_config(),
    )

    provider_config = request.config
    policy = EndpointPolicy()

    # Create the httpx.AsyncClient inside the asyncio.run() boundary so its
    # connection pool is bound to this call's event loop and properly closed
    # before the loop tears down. Sharing a client across asyncio.run() calls
    # raises "Event loop is closed" on keep-alive reuse — see ora-2 review
    # findings.
    async def _run_with_client():
        import httpx

        async with httpx.AsyncClient() as client:
            provider = OpenAIProvider(
                provider_type=provider_config.provider,
                endpoint_policy=policy,
                http_client=client,
            )
            return await run_structured(
                request, provider, SelfAssessmentSchema, endpoint_policy=policy
            )

    result = asyncio.run(_run_with_client())
    return result.data


def evaluate_confidence(
    source_text: str,
    output_text: str,
    *,
    enable_self_assessment: bool = True,
) -> ConfidenceResult:
    """Evaluate confidence of a pipeline output.

    Combines heuristic marker detection with optional LLM self-assessment.
    """
    h_score, markers = _heuristic_score(source_text + " " + output_text)

    if enable_self_assessment:
        try:
            sa = _call_self_assessment(source_text, output_text)
            llm_score = sa.score
            reasoning = sa.reasoning
        except Exception as exc:
            log.warning("Self-assessment failed: %s", exc)
            llm_score = 50
            reasoning = f"Self-assessment failed: {exc.__class__.__name__}"
    else:
        llm_score = h_score
        reasoning = "Self-assessment disabled"

    # Weighted average: 60% heuristic, 40% LLM self-assessment
    overall = int(0.6 * h_score + 0.4 * llm_score)

    result = ConfidenceResult(
        score=llm_score,
        reasoning=reasoning,
        heuristic_score=h_score,
        uncertainty_markers_found=markers,
        overall_score=overall,
    )

    log.info(
        "Confidence: %d/100 (heuristic=%d, llm=%d, markers=%d)",
        overall,
        h_score,
        llm_score,
        len(markers),
    )
    return result
