# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Once-per-run model/backend resolution for artifice-ocr.

The config now ships empty model names and ``"auto"`` backends (see
:mod:`artifice_ocr.config`).  Those two defaults are *inputs to a decision*,
not a usable endpoint: an empty model name is "no explicit choice" and
``"auto"`` is "whichever local server is reachable and can serve a suitable
model".  This module is the one place that turns those into a concrete
``(model, backend)`` pair.

It performs the I/O — probing the configured Ollama / LM Studio URLs with
:func:`model_harness.discovery.probe_endpoint_sync` and consulting
:func:`model_harness.resolution.resolve_model` — exactly once per run, then
caches the answer.  The stages read the cache through :func:`model_for` /
:func:`backend_for`; they never probe, and :func:`artifice_ocr.config.get`
stays a pure settings accessor.

Resolution precedence is delegated to ``resolve_model``; the only policy this
module adds is *which endpoint to ask* and *how to fail*:

* an explicit backend (``ollama`` / ``lm_studio``) probes only that endpoint;
* ``auto`` probes both configured local endpoints and picks the one that
  serves the resolved model;
* a missing *user choice* (``configured_but_missing``) and an empty shelf
  (``NONE_AVAILABLE``) each raise a :class:`RuntimeError` naming the role, the
  endpoint probed, and what to do — never a raw provider 404.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from model_harness.contract import EndpointRejected
from model_harness.discovery import ProbeResult, probe_endpoint_sync
from model_harness.endpoint_policy import EndpointPolicy
from model_harness.registry import HardwareTier
from model_harness.resolution import ModelResolution, ResolutionSource, resolve_model

from . import config

__all__ = [
    "backend_for",
    "model_for",
    "preflight_run",
    "reset",
    "resolve_models_for_run",
]

logger = logging.getLogger(__name__)

_policy = EndpointPolicy()

_PROBE_TIMEOUT_S = 5.0

# Role → (model config key, backend config key).
ROLE_KEYS: dict[str, tuple[str, str]] = {
    "vision": ("ocr_model", "ocr_backend"),
    "chat": ("cleanup_model", "cleanup_backend"),
    "translation": ("translate_model", "translate_backend"),
}

# Pipeline stage name → role.
_STAGE_ROLES: dict[str, str] = {
    "ocr": "vision",
    "cleanup": "chat",
    "title": "chat",
    "translate": "translation",
}

# Human-readable label for failure messages.
_ROLE_LABELS: dict[str, str] = {
    "vision": "OCR",
    "chat": "cleanup",
    "translation": "translation",
}

# Backend name → (url config key, default url).  These are the two *local*
# servers ``auto`` may choose between; cloud backends (huggingface, api_key)
# are only ever honoured explicitly and are never auto-selected.
_BACKEND_URL_KEYS: dict[str, tuple[str, str]] = {
    "ollama": ("ollama_url", "http://localhost:11434"),
    "lm_studio": ("lm_studio_url", "http://localhost:1234/v1"),
}

# There is no server-side hardware detection yet (the BYOM screen stores a
# tier in the browser).  The OCR vision recommendation is identical across
# tiers, so iterating them only affects which translation model is *preferred*
# when several are installed — and the resolver's fallback still covers
# anything the recommendations miss.  Order is LAPTOP-first so a small model
# wins when the user has several.
_TIERS = (HardwareTier.LAPTOP, HardwareTier.DESKTOP, HardwareTier.MAC_UNIFIED)


@dataclass(frozen=True, slots=True)
class _RoleResolution:
    """A concrete (model, backend) pair plus how it was chosen."""

    model: str
    backend: str
    source: ResolutionSource


# Per-run cache: role → resolved pair.  Populated by resolve_models_for_run,
# read by model_for/backend_for, cleared by reset().
_cache: dict[str, _RoleResolution] = {}

# Per-run probe cache: base URL → result.  Populated lazily by :func:`_probe`
# so that a run probing several roles that share a backend (and the preflight
# that re-checks each resolved role) makes exactly one HTTP probe per distinct
# endpoint.  Cleared by reset() so config changes between runs are re-probed.
_probe_cache: dict[str, ProbeResult] = {}


def reset() -> None:
    """Clear the per-run resolution and probe caches."""
    _cache.clear()
    _probe_cache.clear()


def _redact_url(url: str) -> str:
    """Return *url* safe for a log line: no userinfo credentials, no query.

    A configured base URL should never carry credentials, but a pasted one can
    (``http://user:pass@host``), and the endpoint policy does not reject the
    userinfo component.  Logs get pasted into issue reports, so the password
    must not survive into one.  The query/fragment is dropped too, as defence
    in depth for a base URL that should not carry one in the first place.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _base_url_for(backend: str) -> str:
    """The base URL a resolved *backend* will call, from config or default.

    Only used for logging (and the preflight's failure messages) — the probe
    and the backends themselves resolve their own URLs, so this must not be
    treated as authoritative for I/O.
    """
    if backend in _BACKEND_URL_KEYS:
        url_key, default = _BACKEND_URL_KEYS[backend]
        return config.get(url_key) or default
    if backend == "api_key":
        return config.get("api_base_url") or "https://api.openai.com/v1"
    if backend == "huggingface":
        return "https://api-inference.huggingface.co/models"
    return "<unknown>"


def model_for(role: str) -> str:
    """Return the model to use for *role*.

    When :func:`resolve_models_for_run` has populated the cache this is the
    resolved name; otherwise it is the raw configured value (which may be the
    empty default), so an explicit choice set in config still wins even when
    a stage is invoked directly without a resolution pass.
    """
    resolved = _cache.get(role)
    if resolved is not None:
        return resolved.model
    return config.get(ROLE_KEYS[role][0]) or ""


def backend_for(role: str) -> str:
    """Return the backend name to use for *role*.

    Mirrors :func:`model_for`: the resolved backend when available, otherwise
    the raw configured value (possibly ``"auto"``).
    """
    resolved = _cache.get(role)
    if resolved is not None:
        return resolved.backend
    return config.get(ROLE_KEYS[role][1]) or ""


# ---------------------------------------------------------------------------
# Resolution step (the only code in this module that performs I/O)
# ---------------------------------------------------------------------------


def _roles_for_stages(stages: set[str] | None) -> tuple[str, ...]:
    """Return the roles to resolve, in a stable order, for *stages*.

    ``None`` means "resolve every role" — the safe default for callers that do
    not know their stage set up front.
    """
    if stages is None:
        return ("vision", "chat", "translation")
    roles: list[str] = []
    for stage, role in _STAGE_ROLES.items():
        if stage in stages and role not in roles:
            roles.append(role)
    return tuple(roles)


def resolve_models_for_run(*, stages: set[str] | None = None) -> None:
    """Resolve every needed role once, caching the result for the run.

    Probes the configured local endpoints, calls
    :func:`model_harness.resolution.resolve_model` once per role, and stores
    the outcome so :func:`model_for` / :func:`backend_for` can serve it.

    Raises:
        RuntimeError: with a legible message when a role cannot be resolved —
            the user's explicit model is not installed, no suitable model
            is installed on any reachable server, or the endpoint is rejected
            by the local-first policy.  The message names the role, the
            endpoint(s) probed, and what to do.

    One INFO log line is emitted per resolved role recording the backend,
    resolved base URL, model and :class:`ResolutionSource`, so a later
    provider failure can be traced to the endpoint that was actually chosen.
    """
    reset()
    for role in _roles_for_stages(stages):
        try:
            resolved = _resolve_role(role)
        except EndpointRejected as exc:
            raise RuntimeError(
                f"{_ROLE_LABELS[role]} endpoint is rejected by the "
                f"local-first endpoint policy: {exc}"
            ) from exc
        _cache[role] = resolved
        logger.info(
            "Model resolution: role=%s backend=%s base_url=%s model=%s source=%s",
            role,
            resolved.backend,
            _redact_url(_base_url_for(resolved.backend)),
            resolved.model,
            resolved.source.value,
        )


def _probe(backend: str) -> ProbeResult:
    url_key, default = _BACKEND_URL_KEYS[backend]
    url = config.get(url_key) or default
    cached = _probe_cache.get(url)
    if cached is not None:
        return cached
    result = probe_endpoint_sync(url, policy=_policy, timeout_s=_PROBE_TIMEOUT_S)
    _probe_cache[url] = result
    return result


def _resolve_role(role: str) -> _RoleResolution:
    model_key, backend_key = ROLE_KEYS[role]
    configured_model = (config.get(model_key) or "").strip() or None
    backend = (config.get(backend_key) or "").strip().lower() or "auto"

    if backend == "auto":
        return _resolve_auto(role, configured_model)
    if backend in _BACKEND_URL_KEYS:
        return _resolve_local(role, backend, configured_model)
    # Any other explicit backend (huggingface, api_key, ollama_openai, …):
    # no local probe, honour the configured model verbatim exactly as before.
    if not configured_model:
        raise RuntimeError(
            f"No model configured for {_ROLE_LABELS[role]} and the backend "
            f"'{backend}' cannot be auto-resolved. Set "
            f"{model_key!r} in Settings."
        )
    return _RoleResolution(
        model=configured_model,
        backend=backend,
        source=ResolutionSource.USER_CHOICE,
    )


def _resolve_auto(role: str, configured: str | None) -> _RoleResolution:
    backends = ("ollama", "lm_studio")
    probes = {b: _probe(b) for b in backends}
    reachable = [(b, r) for b, r in probes.items() if r.reachable]

    # Combined installed list in a stable order (Ollama first), so the
    # resolver's fallback tie-break is deterministic across endpoints.
    installed: list[str] = []
    for _backend_name, result in reachable:
        for name in result.models:
            if name not in installed:
                installed.append(name)

    resolution = _resolve_with_tiers(role, installed, configured)
    if resolution.model_name is None:
        raise _failure(role, resolution, configured, reachable)

    backend = _backend_serving(resolution.model_name, reachable)
    if backend is None:
        # The resolved name came from `installed`, which came from a reachable
        # endpoint, so this should not happen — but never crash silently.
        backend = reachable[0][0]
    return _RoleResolution(resolution.model_name, backend, resolution.source)


def _resolve_local(role: str, backend: str, configured: str | None) -> _RoleResolution:
    result = _probe(backend)
    installed = list(result.models) if result.reachable else []
    resolution = _resolve_with_tiers(role, installed, configured)
    if resolution.model_name is None:
        raise _failure(role, resolution, configured, [(backend, result)])
    return _RoleResolution(resolution.model_name, backend, resolution.source)


def _resolve_with_tiers(role: str, installed: list[str], configured: str | None) -> ModelResolution:
    """Call ``resolve_model`` across the hardware tiers, returning the first
    answer that is not ``NONE_AVAILABLE``.

    ``configured_but_missing`` and ``USER_CHOICE`` are tier-independent and
    return on the first iteration; only a recommendation is tier-specific.
    """
    result = None
    for tier in _TIERS:
        result = resolve_model(
            role=role,
            installed=installed,
            app="artifice-ocr",
            tier=tier,
            configured=configured,
        )
        if result.model_name is not None or result.source is ResolutionSource.CONFIGURED_MISSING:
            return result
    return result  # the last NONE_AVAILABLE


def _backend_serving(model: str, reachable: list[tuple[str, ProbeResult]]) -> str | None:
    for backend, result in reachable:
        for name in result.models:
            if name == model:
                return backend
    return None


# ---------------------------------------------------------------------------
# Failure messages
# ---------------------------------------------------------------------------


def _failure(
    role: str,
    resolution,
    configured: str | None,
    probed: list[tuple[str, ProbeResult]],
) -> RuntimeError:
    label = _ROLE_LABELS[role]
    reachable_urls = [r.url for _, r in probed if r.reachable]
    all_urls = [r.url for _, r in probed]

    if not reachable_urls:
        return RuntimeError(
            f"Cannot reach any local model server for {label}. "
            f"Tried: {_join(all_urls)}. "
            "Ensure Ollama or LM Studio is running, then retry."
        )

    if resolution.configured_but_missing:
        return RuntimeError(
            f"{label} model '{configured}' is not installed on "
            f"{_join(reachable_urls)}. Install it (e.g. 'ollama pull "
            f"{configured}') or choose a different model in Settings."
        )

    vision_note = (
        f"{label} requires a vision-capable model; a text-only model cannot be substituted. "
        if role == "vision"
        else ""
    )
    return RuntimeError(
        f"No suitable model for {label} is installed on "
        f"{_join(reachable_urls)}. {vision_note}Install one and retry."
    )


def _join(urls: list[str]) -> str:
    if not urls:
        return "<no endpoint>"
    if len(urls) == 1:
        return urls[0]
    return " and ".join(urls)


# ---------------------------------------------------------------------------
# Preflight (cheap per-backend check before the runner starts)
# ---------------------------------------------------------------------------


def preflight_run(*, stages: set[str] | None = None) -> None:
    """Probe each resolved role's backend once more, failing fast on any problem.

    Runs after :func:`resolve_models_for_run` has populated the cache, so it
    reads the resolved ``(model, backend)`` pairs and reuses the probe results
    the resolution pass already fetched — a healthy run pays no extra network
    round-trip.  Each failure is raised as a :class:`RuntimeError` naming the
    cause, provider, base URL and model, so the web layer maps it to a 409
    instead of starting a run that 404s on every queued page.

    Cloud backends (``api_key`` / ``huggingface``) are not probed here: they
    carry no local model list to check, and their endpoints are validated by
    the endpoint policy at client-construction time.
    """
    for role in _roles_for_stages(stages):
        resolved = _cache.get(role)
        if resolved is None or resolved.backend not in _BACKEND_URL_KEYS:
            continue
        _check_resolved_role(role, resolved)


def _check_resolved_role(role: str, resolved: _RoleResolution) -> None:
    label = _ROLE_LABELS[role]
    url = _base_url_for(resolved.backend)
    redacted = _redact_url(url)

    try:
        result = _probe(resolved.backend)
    except EndpointRejected as exc:
        raise RuntimeError(
            f"{label} endpoint {redacted} (backend '{resolved.backend}', "
            f"model '{resolved.model}') is rejected by the local-first "
            f"endpoint policy: {exc}"
        ) from exc

    if not result.reachable:
        raise RuntimeError(_unreachable_cause(label, resolved, redacted, result))

    if resolved.model not in result.models:
        raise RuntimeError(
            f"{label} model '{resolved.model}' is not installed on backend "
            f"'{resolved.backend}' at {redacted}."
        )


def _unreachable_cause(
    label: str,
    resolved: _RoleResolution,
    redacted_url: str,
    result: ProbeResult,
) -> str:
    """Build the unreachable-vs-wrong-shape distinction from the probe hint.

    ``probe_endpoint_sync`` collapses several failures into ``reachable=False``
    and leaves the *kind* of failure in ``hint``.  A "model list" hint means the
    server answered but did not expose a model list — wrong base URL or wrong
    API shape (e.g. a doubled ``/v1``); anything else is a transport failure
    (connection refused, DNS, timeout).
    """
    hint = result.hint or ""
    if "model list" in hint:
        return (
            f"{label} endpoint {redacted_url} (backend '{resolved.backend}', "
            f"model '{resolved.model}') responded but does not look like a "
            f"model server — wrong base URL or API shape. {hint}"
        )
    return (
        f"Cannot reach {label} endpoint {redacted_url} (backend "
        f"'{resolved.backend}', model '{resolved.model}'). {hint}"
    )
