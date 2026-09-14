# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Model-server discovery — probing endpoints for capability and liveness.

This module is where the I/O lives.  :mod:`model_harness.registry` holds the
known-endpoint data; this module consumes it to ask "is there a model server at
this URL, and what does it offer?"

Every probe passes through :meth:`EndpointPolicy.validate_url` before any
network call.  The policy raises :class:`~model_harness.contract.EndpointRejected`
on failure; nothing in this module catches it — callers that sit behind an HTTP
layer wrap it in their own exception type.

The diagnostic hints preserved here exist because real users hit them.  Each
fires on its own error condition, never by substring-matching a port number in
a URL.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

import httpx

from model_harness.contract import EndpointRejected, Provider
from model_harness.endpoint_policy import EndpointPolicy
from model_harness.registry import KNOWN_ENDPOINTS

__all__ = [
    "ProbeResult",
    "detect_local_servers",
    "normalise_base_url",
    "probe_endpoint",
    "probe_endpoint_sync",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT_S: float = 10.0

_OLLAMA_TAGS_PATH: str = "/api/tags"
_OPENAI_MODELS_PATH: str = "/models"

_RUNNER_DOWN_HINT: str = "Ensure your local model runner (Ollama, LM Studio, vLLM) is running"
_CORS_HINT: str = "If running Ollama, ensure OLLAMA_ORIGINS=* is set in your environment"
_OLLAMA_SERVE_HINT: str = "Run 'ollama serve' to start the Ollama server"
_LM_STUDIO_DOWN_HINT: str = "Ensure the LM Studio server is running and accessible"
_MODEL_NOT_PULLED_HINT: str = "Use 'ollama pull <model>' to download models to this provider"
_TIMEOUT_HINT: str = (
    "The server did not respond in time. Check that it is running and the URL is correct."
)
_MALFORMED_RESPONSE_HINT: str = (
    "The server responded but the body was not a valid model list. "
    "Check that the URL points to a supported model server "
    "(Ollama, LM Studio, vLLM)."
)


# ---------------------------------------------------------------------------
# ProbeResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The outcome of probing one endpoint.

    Frozen so the dataclass itself cannot be reassigned.  The *models* tuple
    is also immutable — callers receive a snapshot they cannot accidentally
    append to.
    """

    url: str
    """The URL that was probed (the same string that was validated)."""

    reachable: bool
    """``True`` when at least one model-listing endpoint responded successfully."""

    provider: Provider | None = None
    """The :data:`~model_harness.contract.Provider` this endpoint looks like.

    Heuristic based on which API endpoints answered and the registry match.
    ``None`` when the endpoint could not be identified (e.g. an unrecognised
    port or a server that did not respond).
    """

    models: tuple[str, ...] = field(default_factory=tuple)
    """Model names / IDs discovered.  Ollama models from ``/api/tags`` carry
    their tag-style names (``"llama3.2:3b"``); OpenAI-compatible models from
    ``/v1/models`` carry the ``id`` field.

    The same model may appear from both sources; the list attempts to deduplicate
    but the two APIs use different naming conventions, so a model known as
    ``"llama3.2:3b"`` on Ollama and ``"llama3.2-3b"`` on the OpenAI-compatible
    endpoint will appear twice.
    """

    hint: str | None = None
    """Human-readable diagnostic when the probe did not fully succeed.

    Attached on its own condition rather than by port-substring matching.
    ``None`` when no advice is warranted.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identify_provider(url: str) -> Provider | None:
    """Match a URL against :data:`KNOWN_ENDPOINTS` by port.

    Returns the :data:`~model_harness.contract.Provider` literal of a known
    endpoint whose default port matches the URL.  ``None`` when no registry
    entry matches — the URL may still host a valid model server.

    This is cheap identification, not authoritative.  The actual API responses
    (:meth:`_probe_ollama_tags` / :meth:`_probe_openai_models`) are what
    determine reachability.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    port = parsed.port
    if port is None:
        # Default ports: try matching by scheme → port 80 / 443 are generic
        return None
    for info in KNOWN_ENDPOINTS.values():
        if port == info.default_port:
            return info.provider
    return None


def normalise_base_url(url: str) -> str:
    """Return *url* with surrounding whitespace, any trailing ``/v1`` path and
    trailing slashes removed.

    Canonicalises the four common spellings of an Ollama base URL to a single
    host root, so a caller can append exactly one API prefix — ``/v1`` for the
    OpenAI-compatible API, ``/api/...`` for the native one — without doubling a
    path segment::

        http://localhost:11434
        http://localhost:11434/
        http://localhost:11434/v1
        http://localhost:11434/v1/

    all normalise to ``http://localhost:11434``.

    Only a *trailing* ``/v1`` is stripped — a URL whose path continues past
    ``/v1`` (e.g. ``http://localhost:11434/v1/chat/completions``) is returned
    unchanged.  Surrounding whitespace is removed first, because a pasted URL
    often carries a trailing space that would otherwise survive the path
    comparison and defeat the ``/v1`` strip.
    """
    return _strip_v1(url.strip()).rstrip("/")


def _strip_v1(url: str) -> str:
    """Return *url* with the trailing ``/v1`` path removed.

    Used to build the Ollama-native ``/api/tags`` URL from an OpenAI-compatible
    base URL like ``http://localhost:11434/v1``.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        new_path = path[:-3].rstrip("/") or "/"
        parsed = parsed._replace(path=new_path)
    return urlunparse(parsed)


async def _probe_ollama_tags(client: httpx.AsyncClient, base_url: str) -> list[str] | None:
    """Hit ``/api/tags`` and return the model names found.

    Returns *None* when the endpoint does not answer (non-200, wrong
    provider, or unreachable).  Returns a (possibly empty) list when
    the server is reachable and the ``/api/tags`` endpoint responded —
    an empty list means the server is running but no models are pulled.
    """
    url = f"{_strip_v1(base_url).rstrip('/')}{_OLLAMA_TAGS_PATH}"
    resp = await client.get(url)
    if resp.status_code != 200:
        return None
    data = resp.json()
    models: list[str] = []
    models_list = data.get("models", [])
    if models_list is None:
        models_list = []
    for m in models_list:
        name = m.get("name", "") or m.get("model", "")
        if name:
            models.append(name)
    return models


async def _probe_openai_models(client: httpx.AsyncClient, base_url: str) -> list[str] | None:
    """Hit ``/v1/models`` and return the model IDs found.

    The *base_url* is expected to include the ``/v1`` prefix (it is stripped
    here so we can append ``/models`` without a double slash).

    Returns *None* when the endpoint does not answer (non-200).  Returns a
    (possibly empty) list when the ``/v1/models`` endpoint responded.
    """
    base = base_url.rstrip("/")
    # base may or may not include /v1 — construct the canonical path
    if base.endswith("/v1"):
        url = f"{base}/models"
    else:
        url = f"{base}/v1/models"
    resp = await client.get(url)
    if resp.status_code != 200:
        return None
    data = resp.json()
    ids: list[str] = []
    data_list = data.get("data", [])
    if data_list is None:
        data_list = []
    for m in data_list:
        mid = m.get("id", "")
        if mid:
            ids.append(mid)
    return ids


def _hints_for_error(
    exc: Exception,
    provider: Provider | None,
) -> str:
    """Build diagnostic hints from an exception and the best-guess provider.

    Each hint fires on its own condition; multiple conditions produce multiple
    sentences joined by ``". "``.
    """
    err_str = str(exc)
    err_lower = err_str.lower()
    hints: list[str] = []

    # -- Connection refused (WinError 10061 on Windows, errno 111 on Linux) --
    if "10061" in err_str or "connection refused" in err_lower:
        hints.append(_RUNNER_DOWN_HINT)
        if provider == "ollama":
            hints.append(_OLLAMA_SERVE_HINT)
        elif provider == "lm-studio":
            hints.append(_LM_STUDIO_DOWN_HINT)

    # -- CORS / origin — tightened so a bare "origin" substring does not
    #    fire on an unrelated error (e.g. a DNS failure mentioning origin.local).
    #
    #    "cors" and "failed to fetch" are unambiguous CORS signals on their own.
    #    "origin" only fires when accompanied by a blocking/denial keyword.
    if "cors" in err_lower or "failed to fetch" in err_lower:
        hints.append(_CORS_HINT)
    elif "origin" in err_lower and any(
        kw in err_lower for kw in ("blocked", "disallow", "access-control")
    ):
        hints.append(_CORS_HINT)

    # -- Fallback: if nothing matched but we know the provider, add a
    #    provider-specific hint on any error --
    if not hints:
        if provider == "ollama":
            hints.append(_RUNNER_DOWN_HINT)
            hints.append(_OLLAMA_SERVE_HINT)
        elif provider == "lm-studio":
            hints.append(_RUNNER_DOWN_HINT)
            hints.append(_LM_STUDIO_DOWN_HINT)
        else:
            hints.append(_RUNNER_DOWN_HINT)

    return ". ".join(hints)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def probe_endpoint(
    url: str,
    *,
    policy: EndpointPolicy,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ProbeResult:
    """Probe a single endpoint for liveness, provider identity, and models.

    Args:
        url: The base URL of the model server (e.g. ``"http://localhost:11434/v1"``).
        policy: The :class:`EndpointPolicy` that validates *url* before any
            network call.  Raises :class:`EndpointRejected` on failure.
        timeout_s: Per-request timeout in seconds.  Applies to each HTTP call
            made during the probe (typically two).

    Returns:
        A :class:`ProbeResult` describing what was found.

    Raises:
        EndpointRejected: if *url* is not permitted by the policy.
    """
    policy.validate_url(url)

    provider = _identify_provider(url)
    models: list[str] = []
    ollama_ok = False
    openai_ok = False
    malformed = False

    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
            # Probe Ollama-native /api/tags
            try:
                tags = await _probe_ollama_tags(client, url)
                if tags is not None:
                    models.extend(tags)
                    ollama_ok = True
            except json.JSONDecodeError:
                # Server responded 200 but the body was not valid JSON.
                malformed = True

            # Probe OpenAI-compatible /v1/models
            try:
                ids = await _probe_openai_models(client, url)
                if ids is not None:
                    for mid in ids:
                        if mid not in models:
                            models.append(mid)
                    openai_ok = True
            except json.JSONDecodeError:
                malformed = True

    except httpx.ConnectError as exc:
        if ollama_ok or openai_ok:
            # At least one endpoint answered before the connection dropped —
            # treat this as reachable and fall through to result-building.
            pass
        else:
            hint = _hints_for_error(exc, provider)
            return ProbeResult(
                url=url,
                reachable=False,
                provider=provider,
                hint=hint,
            )

    except httpx.TimeoutException:
        if ollama_ok or openai_ok:
            pass
        else:
            return ProbeResult(
                url=url,
                reachable=False,
                provider=provider,
                hint=_TIMEOUT_HINT,
            )

    except (httpx.ReadError, httpx.RemoteProtocolError, httpx.PoolTimeout):
        if ollama_ok or openai_ok:
            pass
        else:
            return ProbeResult(
                url=url,
                reachable=False,
                provider=provider,
                hint=(
                    "A network error occurred while communicating with the "
                    "server. Check that the server is running and the URL "
                    "is correct."
                ),
            )

    except Exception as exc:
        # Programming errors (TypeError, NameError, AttributeError,
        # and their subclasses) must not masquerade as infrastructure
        # problems — let them propagate so they are caught by the test
        # suite and never reach a user.
        if isinstance(exc, (TypeError, NameError, AttributeError)):
            raise
        if ollama_ok or openai_ok:
            pass
        else:
            hint = _hints_for_error(exc, provider)
            return ProbeResult(
                url=url,
                reachable=False,
                provider=provider,
                hint=hint,
            )

    # -- Malformed response (200 but unusable body) --
    if not ollama_ok and not openai_ok and malformed:
        return ProbeResult(
            url=url,
            reachable=False,
            provider=provider,
            hint=_MALFORMED_RESPONSE_HINT,
        )

    # -- Build the result from what we learned --
    if ollama_ok or openai_ok:
        # Identify the provider from what answered.
        # The API response is more authoritative than the port heuristic.
        detected: Provider | None
        if ollama_ok and not openai_ok:
            detected = "ollama" if provider is None else provider
        elif openai_ok and not ollama_ok:
            # Only the OpenAI-compatible endpoint answered.  If the port
            # heuristic says "ollama" but /api/tags did not answer, the
            # port is wrong — trust the API response.
            if provider == "ollama":
                detected = "generic-api"
            else:
                detected = provider or "generic-api"
        else:
            # Both answered — almost certainly Ollama (it exposes both APIs)
            detected = provider or "ollama"

        hint: str | None = None
        if detected == "ollama" and not models:
            hint = _MODEL_NOT_PULLED_HINT

        return ProbeResult(
            url=url,
            reachable=True,
            provider=detected,
            models=tuple(models),
            hint=hint,
        )

    # Neither endpoint answered but no transport-level exception either
    # (e.g. server returned 4xx/5xx on both paths).
    return ProbeResult(
        url=url,
        reachable=False,
        provider=provider,
        hint=(
            "Server responded but did not return a model list. "
            "Check that the URL points to a supported model server "
            "(Ollama, LM Studio, vLLM)."
        ),
    )


async def detect_local_servers(
    *,
    policy: EndpointPolicy,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> list[ProbeResult]:
    """Probe every endpoint in :data:`KNOWN_ENDPOINTS` and return results.

    Probes run concurrently so the total wall time is bounded by roughly one
    timeout rather than the sum.

    Args:
        policy: The :class:`EndpointPolicy` used to validate every URL before
            probing.  A URL that is rejected by the policy is **skipped** (not
            probed) and omitted from the results — this is a local-server scan,
            and a URL that cannot pass the local-first policy is by definition
            not a local server.
        timeout_s: Per-endpoint timeout.

    Returns:
        One :class:`ProbeResult` per known endpoint that passed policy
        validation, regardless of whether the server was reachable.

        Endpoints that fail policy validation are silently omitted so a caller
        that has not opted into public endpoints sees only local results.
    """

    async def _probe_one(info) -> ProbeResult | None:
        try:
            policy.validate_url(info.default_url)
        except EndpointRejected:
            return None
        return await probe_endpoint(
            info.default_url,
            policy=policy,
            timeout_s=timeout_s,
        )

    tasks = [_probe_one(info) for info in KNOWN_ENDPOINTS.values()]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[ProbeResult] = []
    for item in gathered:
        if isinstance(item, ProbeResult):
            results.append(item)
    return results


def probe_endpoint_sync(
    url: str,
    *,
    policy: EndpointPolicy,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ProbeResult:
    """Synchronous wrapper around :func:`probe_endpoint`.

    Safe to call from CLI code that does not own an event loop.  Guards against
    being called from inside a running event loop — that path raises
    :class:`RuntimeError` with a clear message rather than deadlocking.

    Raises:
        RuntimeError: if called from inside a running asyncio event loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — we are free to create one
        return asyncio.run(probe_endpoint(url, policy=policy, timeout_s=timeout_s))
    raise RuntimeError(
        "probe_endpoint_sync cannot be called from inside a running event loop. "
        "Use the async probe_endpoint() instead to avoid deadlocking."
    )
