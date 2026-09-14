# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The BYOM call contract for the Artifice Suite.

This module defines *what a model call is* in this project. It contains no
transport: no HTTP client, no provider SDK, no ``openai`` import. A provider
adapter implements :class:`ModelProvider` and lives beside the code that owns
the connection; the rules about what may be asked, what may be returned, and
what happens when a provider cannot comply all live here, once.

The design answers three questions, in order of how much trouble each has
caused in this codebase:

1. **What does a structured call look like?**  Every call declares a Pydantic
   schema for its response. ``schema`` is a required argument, not an optional
   one — that is the whole mechanism by which "no freeform chat" is structural
   rather than aspirational. Code that wants prose declares a schema with one
   string field, which forces it to name what it is asking for.

2. **How does a provider say it cannot do that?**  It returns a
   :class:`ProviderCapabilities` naming the best
   :class:`StructuredOutputMode` it supports. A local Ollama build and a
   hosted endpoint have genuinely different abilities here and the suite is
   BYOM, so the contract cannot assume the strongest one.

3. **What happens when it cannot?**  The harness degrades down a fixed ladder
   and **records which rung it used** on every result. If it reaches the bottom
   it raises :class:`StructuredOutputUnsupported`. It never silently returns
   prose to a caller that asked for a schema.

That third point is the one worth defending. The suite carried **five** separate
"recover JSON from whatever the model said" helpers when this was written — not
the three first recorded — and none of them could tell a caller whether it
received a guaranteed-schema response or a lucky parse. Those two things have
very different reliability and the code treated them identically.
:attr:`HarnessResult.mode_used` and :attr:`HarnessResult.repaired` exist so that
distinction survives the call.

Three are now gone: both of ``artifice-graph``'s when its extraction path was
ported, and ``parse_json_robust`` in ``artifice-transcribe``, which turned out to
have no callers at all. **Two remain, both in ``artifice-draft``** —
``parse_llm_json_response`` and ``_parse_llm_response`` — and retiring them is
the acceptance criterion for this phase.

``driver._extract_json`` is not a sixth. It runs only on the ``PROMPTED`` rung and
its result carries ``mode_used=PROMPTED``, so a caller can tell a scrape from a
guarantee. Do not delete it to satisfy the criterion above.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

Provider = Literal["ollama", "lm-studio", "generic-api", "whisper", "parakeet", "anthropic"]

SchemaT = TypeVar("SchemaT", bound=BaseModel)


# ── Configuration ────────────────────────────────────────────────────────────


class ModelConnectorConfig(BaseModel):
    """Endpoint and credentials for a single BYOM connection."""

    provider: Provider
    endpoint: str
    model: str
    api_key: str | None = None
    timeout_s: float = 120.0


# ── Capability declaration ───────────────────────────────────────────────────


class StructuredOutputMode(str, Enum):
    """How a provider can be made to return machine-readable output.

    Ordered strongest to weakest. :func:`select_mode` walks this order, so the
    declaration order here is load-bearing — do not reorder without reading it.
    """

    NATIVE_SCHEMA = "native_schema"
    """The provider accepts the schema itself and guarantees conformance."""

    JSON_OBJECT = "json_object"
    """The provider guarantees syntactically valid JSON, but not *this* shape."""

    PROMPTED = "prompted"
    """No server-side guarantee. The schema goes in the prompt and we validate."""

    NONE = "none"
    """The provider cannot be relied on for machine-readable output at all."""


_MODE_STRENGTH: tuple[StructuredOutputMode, ...] = (
    StructuredOutputMode.NATIVE_SCHEMA,
    StructuredOutputMode.JSON_OBJECT,
    StructuredOutputMode.PROMPTED,
)


class ProviderCapabilities(BaseModel):
    """What a given provider and model can actually do.

    Declared by the adapter, not guessed by the harness. A capability the
    adapter is unsure about should be declared *low* — an over-claim produces a
    confusing validation failure at call time, an under-claim only costs a
    weaker mode.

    ``structured_output`` names the **strongest** mode this provider supports.
    The ladder degrades downward from that mode, but only through modes the
    provider actually implements — a gap in ``supported_modes`` is skipped
    rather than attempted.

    A provider that supports ``NATIVE_SCHEMA`` via tool-use but has no
    ``json_object`` API declares ``supported_modes={NATIVE_SCHEMA, PROMPTED}``.
    When ``supported_modes`` is ``None`` (the default), every mode from
    ``structured_output`` downward is assumed supported — correct for
    OpenAI-shaped providers but not for Anthropic.
    """

    structured_output: StructuredOutputMode
    supported_modes: frozenset[StructuredOutputMode] | None = None
    streaming: bool = False
    vision: bool = False

    def modes(self) -> frozenset[StructuredOutputMode]:
        """Return every mode this provider actually supports.

        ``structured_output`` is guaranteed to be in the returned set. When
        ``supported_modes`` is ``None`` the set is every mode from the best
        downward through ``_MODE_STRENGTH``.
        """
        if self.supported_modes is not None:
            return self.supported_modes
        idx = _MODE_STRENGTH.index(self.structured_output)
        return frozenset(_MODE_STRENGTH[idx:])


# ── Requests and results ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StructuredRequest:
    """A single model call, fully specified.

    ``instructions`` and ``input`` are deliberately named for their role rather
    than as "system" and "user". Not every provider in a BYOM suite has those
    two roles, and the pair here means "the standing rules" and "the thing to
    apply them to".
    """

    instructions: str
    input: str
    schema_json: dict
    """JSON Schema for the expected response, from ``Model.model_json_schema()``."""

    mode: StructuredOutputMode
    """The mode the harness selected. The adapter must honour it or raise."""

    config: ModelConnectorConfig


@dataclass(frozen=True, slots=True)
class RawCompletion:
    """What an adapter hands back: text, plus what it took to get it."""

    text: str
    model: str


@dataclass(frozen=True, slots=True)
class HarnessResult(Generic[SchemaT]):
    """A validated response, and an honest account of how it was obtained."""

    data: SchemaT
    mode_used: StructuredOutputMode
    """Which rung of the ladder actually produced this. Never inferred."""

    model: str
    raw: str
    """The unparsed response. Kept for audit — a local-first tool should be
    able to show a user exactly what its model said."""

    repaired: bool = False
    """True if the first response failed validation and a retry was needed.
    Worth logging: a provider that is frequently repaired is mis-declaring its
    capabilities, and this is the only place that shows up."""


# ── Failures ─────────────────────────────────────────────────────────────────


class HarnessError(Exception):
    """Base for every failure originating in the harness."""


class StructuredOutputUnsupported(HarnessError):
    """The provider cannot produce machine-readable output for this call.

    Raised rather than degrading to prose. A caller asked for a schema; giving
    it something else silently is how a pipeline ends up deduplicating nothing
    and reporting success.
    """


class SchemaValidationFailed(HarnessError):
    """The response did not match the requested schema, after any retry.

    Carries the raw text, because the first question anyone asks is "what did
    it actually say".
    """

    def __init__(self, message: str, *, raw: str, mode: StructuredOutputMode) -> None:
        super().__init__(message)
        self.raw = raw
        self.mode = mode


class EndpointRejected(HarnessError):
    """The endpoint is not permitted by the local-first policy."""


# ── Mode selection ───────────────────────────────────────────────────────────


def select_mode(
    capabilities: ProviderCapabilities,
    *,
    minimum: StructuredOutputMode = StructuredOutputMode.PROMPTED,
) -> StructuredOutputMode:
    """Pick the strongest mode a provider supports, or raise.

    ``minimum`` lets a caller refuse to accept a weak guarantee. Extraction
    that feeds a knowledge graph may reasonably demand ``JSON_OBJECT`` or
    better and fail loudly on a model that cannot manage it, rather than
    quietly producing worse data than the caller believes it asked for.
    """
    if capabilities.structured_output is StructuredOutputMode.NONE:
        raise StructuredOutputUnsupported(
            "provider declares no machine-readable output capability"
        )

    if minimum is StructuredOutputMode.NONE:
        # "I will accept anything" — and the provider offers something, since
        # the NONE case above has already returned. Guarded explicitly because
        # NONE is deliberately absent from _MODE_STRENGTH, so indexing it would
        # raise ValueError rather than a HarnessError.
        return capabilities.structured_output

    available = _MODE_STRENGTH.index(capabilities.structured_output)
    floor = _MODE_STRENGTH.index(minimum)

    if available > floor:
        raise StructuredOutputUnsupported(
            f"provider supports at best {capabilities.structured_output.value}, "
            f"but this call requires at least {minimum.value}"
        )

    return capabilities.structured_output


# ── Endpoint policy ──────────────────────────────────────────────────────────


class EndpointPolicy(Protocol):
    """Decides which endpoints this local-first suite may call.

    Kept in the contract because it is the one rule that must not be
    reimplemented per app. `CLAUDE.md` requires local model calls to route via
    ``host.docker.internal`` or loopback; today each app decides that for
    itself, which is both the SSRF surface and the reason a working setup
    breaks differently in each app.
    """

    def resolve(self, endpoint: str) -> str:
        """Return the endpoint to use, or raise :class:`EndpointRejected`."""
        ...


# ── The provider seam ────────────────────────────────────────────────────────


@runtime_checkable
class ModelProvider(Protocol):
    """What an adapter must implement. Deliberately two methods.

    Anything richer — streaming, model listing, vision probing — belongs to the
    adapter's own interface, not to this contract. The suite has four apps that
    each grew their own client precisely because "one more convenience method"
    kept being the easiest next step.
    """

    def capabilities(self, model: str) -> ProviderCapabilities:
        """Declare what this provider and model can do. Must not do I/O."""
        ...

    async def complete(self, request: StructuredRequest) -> RawCompletion:
        """Perform one call in exactly the mode the request specifies."""
        ...


__all__ = [
    "EndpointPolicy",
    "EndpointRejected",
    "HarnessError",
    "HarnessResult",
    "ModelConnectorConfig",
    "ModelProvider",
    "Provider",
    "ProviderCapabilities",
    "RawCompletion",
    "SchemaT",
    "SchemaValidationFailed",
    "StructuredOutputMode",
    "StructuredOutputUnsupported",
    "StructuredRequest",
    "select_mode",
]
