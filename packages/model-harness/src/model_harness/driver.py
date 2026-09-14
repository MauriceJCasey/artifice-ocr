# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The driver that apps call to execute a structured model request.

This module owns the degradation ladder: it takes a :class:`StructuredRequest`,
selects the strongest mode the provider supports, calls :meth:`ModelProvider.complete`,
validates the response, and degrades one rung on validation failure.  No app
should replicate this logic — it is the single point where the contract's
guarantees are enforced.
"""

from __future__ import annotations

import json
import logging
from typing import Union

from pydantic import BaseModel, ValidationError

from model_harness.contract import (
    EndpointPolicy,
    HarnessResult,
    ModelProvider,
    SchemaT,
    SchemaValidationFailed,
    StructuredOutputMode,
    StructuredOutputUnsupported,
    StructuredRequest,
    _MODE_STRENGTH,
    select_mode,
)

logger = logging.getLogger(__name__)


async def run_structured(
    request: StructuredRequest,
    provider: ModelProvider,
    schema_model: type[SchemaT],
    *,
    endpoint_policy: EndpointPolicy | None = None,
    minimum: StructuredOutputMode | None = None,
) -> HarnessResult[SchemaT]:
    """Execute a structured model request with degradation.

    Parameters
    ----------
    request:
        The fully-specified request.  Its ``mode`` field is treated as the
        caller's minimum guarantee — the driver will use a stronger mode if
        the provider supports one, but never a weaker one.
    provider:
        Any :class:`ModelProvider` adapter.
    schema_model:
        The Pydantic model to validate the response against.
    endpoint_policy:
        If provided, the endpoint in ``request.config`` is validated through
        this policy before any network call.  Rejection raises
        :class:`~model_harness.contract.EndpointRejected`.
    minimum:
        Override the caller's minimum mode.  When ``None`` (the default),
        ``request.mode`` is used.  Allows a caller to set a stricter floor
        without reconstructing the request.

    Returns
    -------
    HarnessResult
        The validated response, with ``mode_used`` set to the rung of the
        ladder that actually produced a valid response, and ``repaired``
        indicating whether degradation occurred.

    Raises
    ------
    StructuredOutputUnsupported
        If no rung of the ladder can produce a valid response — including
        when the provider's best capability is weaker than the caller's
        minimum, or when validation fails at the bottom rung.
    SchemaValidationFailed
        If the response was unparseable as JSON.  Schema-mismatch failures
        are handled by degradation, not by raising.
    """
    floor: StructuredOutputMode = minimum if minimum is not None else request.mode

    # ── Endpoint validation ───────────────────────────────────────────────
    if endpoint_policy is not None:
        endpoint_policy.resolve(request.config.endpoint)

    # ── Mode selection ─────────────────────────────────────────────────────
    caps = provider.capabilities(request.config.model)
    current_mode = select_mode(caps, minimum=floor)
    supported = caps.modes()

    # ── Degradation ladder ─────────────────────────────────────────────────
    # Walk from strongest to weakest.  Each rung: call complete, parse JSON,
    # validate against schema.  If validation fails, drop one rung and retry.
    # Modes the provider does not implement (gaps in the ``supported_modes``
    # set) are skipped — the driver never asks an adapter for a mode it
    # cannot handle.
    repaired = False
    last_raw: str | None = None
    last_mode: StructuredOutputMode | None = None

    for mode in _rungs_from(current_mode, supported=supported):
        last_mode = mode

        # Build a request whose mode field reflects the rung we are actually
        # about to try — this is what the adapter reads.
        rung_request = StructuredRequest(
            instructions=request.instructions,
            input=request.input,
            schema_json=request.schema_json,
            mode=mode,
            config=request.config,
        )

        completion = await provider.complete(rung_request)
        last_raw = completion.text

        # Parse JSON.
        parsed = _extract_json(completion.text)
        if parsed is None:
            # The response is not valid JSON at all.  If we are on a rung
            # with a server-side guarantee (NATIVE_SCHEMA or JSON_OBJECT),
            # this is a provider bug — but we still try the next rung rather
            # than raise, because the caller would rather get a result than
            # an error.
            logger.warning(
                "mode=%s returned unparseable text; degrading",
                mode.value,
            )
            repaired = True
            continue

        # Validate against the schema.
        try:
            instance: SchemaT = schema_model.model_validate(parsed)
        except ValidationError:
            logger.info(
                "mode=%s returned valid JSON that did not match schema; "
                "degrading",
                mode.value,
            )
            repaired = True
            continue

        # Success.
        return HarnessResult(
            data=instance,
            mode_used=mode,
            model=request.config.model,
            raw=completion.text,
            repaired=repaired,
        )

    # The ladder bottomed out.
    assert last_raw is not None and last_mode is not None  # for mypy
    raise StructuredOutputUnsupported(
        f"All structured-output modes exhausted for model "
        f"{request.config.model!r}.  Last mode attempted: "
        f"{last_mode.value}."
    )


# -- Helpers -------------------------------------------------------------------


def _rungs_from(
    start: StructuredOutputMode,
    *,
    supported: frozenset[StructuredOutputMode] | None = None,
):
    """Yield *start* and every weaker rung in the ladder, skipping any mode
    not in *supported*.

    When *supported* is ``None`` every rung is yielded — the default for
    providers that implement every mode in the strength ordering.
    """
    try:
        idx = _MODE_STRENGTH.index(start)
    except ValueError:
        return
    for mode in _MODE_STRENGTH[idx:]:
        if supported is None or mode in supported:
            yield mode


def _extract_json(text: str) -> object | None:
    """Return parsed JSON from *text*, or ``None`` if unparseable.

    Tolerates leading/trailing whitespace and markdown code fences — the
    kind of wrapping a prompted model often adds.
    """
    text = text.strip()
    # Strip markdown code fences: ```json ... ```
    if text.startswith("```") and text.endswith("```"):
        # Find the first newline after the opening fence.
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 : -3].strip()
        else:
            # Single-line fence: ```content``` — unusual but handle it.
            text = text[3:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


__all__ = ["run_structured"]
