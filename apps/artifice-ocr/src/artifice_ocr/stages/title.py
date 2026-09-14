# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Optional title-generation stage: produce a short archival title for each
OCR'd page using the configured cleanup model, routed through the
:mod:`model_harness` contract.

This is the first OCR-side inference call to use the harness contract.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from artifice_ocr._guard import _UMLAUT, _WORD
from artifice_ocr._logging import get_logger
from artifice_ocr._resolution import backend_for, model_for
from artifice_ocr.config import get as cfg
from artifice_ocr.output import write_stage_output
from model_harness.contract import (
    ModelConnectorConfig,
    Provider,
    SchemaValidationFailed,
    StructuredOutputMode,
    StructuredOutputUnsupported,
    StructuredRequest,
)
from model_harness.driver import run_structured
from model_harness.endpoint_policy import EndpointPolicy
from model_harness.openai_adapter import OpenAIProvider
from pydantic import BaseModel, Field

log = get_logger("title")

# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------


class PageTitleSchema(BaseModel):
    title: str = Field(
        ...,
        max_length=120,
        description="Short archival title reflecting page content",
    )
    language: str = Field(..., description="Detected source language ISO code of the page")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an archivist. Generate a short title (max 120 characters) "
    "describing the content of this page. Write the title in the same "
    "language as the source text. Do not translate. Do not modernize "
    "spelling. Return JSON with 'title' and 'language' fields."
)

# ---------------------------------------------------------------------------
# Backend → harness Provider mapping
# ---------------------------------------------------------------------------

_BACKEND_PROVIDER: dict[str, Provider] = {
    "ollama": "ollama",
    "lm_studio": "lm-studio",
    "api_key": "generic-api",
    "huggingface": "generic-api",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_provider_config() -> ModelConnectorConfig:
    """Build a :class:`ModelConnectorConfig` from the app config."""
    backend = backend_for("chat").lower()
    provider: Provider = _BACKEND_PROVIDER.get(backend, "ollama")
    model = model_for("chat")

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


def _build_request(cleaned_text: str) -> StructuredRequest:
    """Build the harness request for title generation."""
    # Truncate input — a title only needs enough text to understand topic.
    truncated = cleaned_text[:2000]

    return StructuredRequest(
        instructions=_SYSTEM_PROMPT,
        input=truncated,
        schema_json=PageTitleSchema.model_json_schema(),
        mode=StructuredOutputMode.PROMPTED,
        config=_resolve_provider_config(),
    )


def _check_title_repetition(title: str, *, max_repeat_ratio: float = 0.5) -> bool:
    """Return True if the title appears to be a degenerate repetition loop.

    A genuinely generated title should have at least some word diversity.
    If a single word accounts for more than *max_repeat_ratio* of the word
    count, the model is likely looping.
    """
    words = _WORD.findall(title)
    if not words:
        return False  # empty title handled elsewhere
    if len(words) < 3:
        return False  # too short for a repetition check to be meaningful
    counts = Counter(w.lower() for w in words)
    most_common_count = counts.most_common(1)[0][1]
    return (most_common_count / len(words)) >= max_repeat_ratio


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def perform(
    cleaned_text: str,
    *,
    source_file: str = "",
    output_dir: str = "output",
    stem: str | None = None,
) -> dict[str, Any]:
    """Generate a short archival title for one page.

    Uses the configured ``cleanup_model`` and ``cleanup_backend``, routed
    through :func:`model_harness.driver.run_structured`.  On any failure
    the stage falls back to the source file's basename — title generation
    is an enhancement, never a pipeline blocker.
    """
    base_name = stem or (Path(source_file).stem if source_file else "unknown")
    model = model_for("chat")
    backend = backend_for("chat")

    log.info("Generating title for %s (model=%s, backend=%s)", base_name, model, backend)

    try:
        request = _build_request(cleaned_text)
        provider_config = request.config
        policy = EndpointPolicy()

        # Create the httpx.AsyncClient inside the asyncio.run() boundary so
        # its connection pool is bound to this call's event loop and properly
        # closed before the loop tears down. Sharing a client across
        # asyncio.run() calls raises "Event loop is closed" on keep-alive
        # reuse — see ora-2 review findings.
        async def _run_with_client():
            import httpx

            async with httpx.AsyncClient() as client:
                provider = OpenAIProvider(
                    provider_type=provider_config.provider,
                    endpoint_policy=policy,
                    http_client=client,
                )
                return await run_structured(
                    request, provider, PageTitleSchema, endpoint_policy=policy
                )

        result = asyncio.run(_run_with_client())

        data = result.data
        title = data.title
        language = data.language

    except (StructuredOutputUnsupported, SchemaValidationFailed) as exc:
        log.warning(
            "Title generation failed for %s: %s — falling back to basename",
            base_name,
            exc,
        )
        return _fallback_result(source_file, base_name, output_dir, error=str(exc))

    except Exception as exc:
        log.warning(
            "Title generation failed for %s: %s — falling back to basename",
            base_name,
            exc,
        )
        return _fallback_result(source_file, base_name, output_dir, error=str(exc))

    # -- Guards ---------------------------------------------------------------
    guard_results: dict[str, Any] = {}
    max_chars = int(cfg("title_max_chars") or 120)

    # 1. Length cap (truncation, not retry)
    if len(title) > max_chars:
        log.warning(
            "Title for %s exceeds %d chars — truncating",
            base_name,
            max_chars,
        )
        title = title[:max_chars]
        guard_results["truncated"] = True

    # 2. Accent check: warn but keep (generated content, not a transcription edit)
    if _UMLAUT.search(title) and cleaned_text and not _UMLAUT.search(cleaned_text[:2000]):
        log.warning(
            "Title for %s introduces diacritics not present in source — keeping title but flagging",
            base_name,
        )
        guard_results["accent_warning"] = True

    # 3. Repetition check
    if _check_title_repetition(title):
        log.warning(
            "Title for %s is a repetition loop — falling back to basename",
            base_name,
        )
        return _fallback_result(
            source_file,
            base_name,
            output_dir,
            error="repetition loop: " + title,
        )

    # -- Write output ---------------------------------------------------------
    output_data = {
        "source_file": source_file,
        "stage": "title",
        "title": title,
        "language": language,
        "model": model,
        "mode_used": result.mode_used.value,
        "repaired": result.repaired,
        "generated_by_model": True,
        "timestamp": datetime.now(UTC).isoformat(),
        "guard": guard_results,
    }

    write_stage_output(output_dir, "title", base_name, text_content=title, metadata=output_data)

    log.info(
        "Title generated for %s: %s",
        base_name,
        output_data["title"],
    )
    return output_data


def _fallback_result(
    source_file: str,
    base_name: str,
    output_dir: str,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """Produce a fallback result when the model call fails."""
    title = Path(source_file).stem if source_file else base_name

    data = {
        "source_file": source_file,
        "stage": "title",
        "title": title,
        "language": "",
        "generated_by_model": False,
        "error": error,
        "timestamp": datetime.now(UTC).isoformat(),
        "guard": {},
    }

    write_stage_output(output_dir, "title", base_name, text_content=title, metadata=data)

    return data
