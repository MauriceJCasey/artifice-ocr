# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Settings, document types, and health-check routes."""

import asyncio
import os
import platform
import socket
import struct
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException
from model_harness.contract import EndpointRejected
from model_harness.discovery import normalise_base_url, probe_endpoint
from model_harness.endpoint_policy import EndpointPolicy
from shared_ui.path_validation import PathValidationError, normalise_path

from ... import config
from ..._prompts import DOCUMENT_TYPES
from ..._resolution import ROLE_KEYS

router = APIRouter(tags=["settings"])

# ── Model endpoints ──────────────────────────────────────────────────────────
#
# The allowlist policy lives in ``model_harness.endpoint_policy`` — this app
# only wraps it with FastAPI's exception type.  See
# :class:`model_harness.endpoint_policy.EndpointPolicy` for the full
# rationale and constraint set.

_endpoint_policy = EndpointPolicy()

_LOCAL_BACKENDS = {
    "ollama": ("ollama_url", "http://localhost:11434", 11434),
    "lm_studio": ("lm_studio_url", "http://localhost:1234/v1", 1234),
}


def _wsl_host() -> str | None:
    """Return the Windows host address when the server is running in WSL2."""
    if not (os.environ.get("WSL_INTEROP") or "microsoft" in platform.release().lower()):
        return None
    try:
        for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "00000000":
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, ValueError, struct.error):
        return None
    return None


def _canonical_local_url(backend: str, raw: str) -> str:
    value = raw.strip().rstrip("/")
    if backend == "ollama":
        return normalise_base_url(value)
    parts = urlsplit(value)
    segments = [segment for segment in parts.path.split("/") if segment]
    if "v1" in segments:
        segments = segments[: segments.index("v1") + 1]
    else:
        segments.append("v1")
    path = "/" + "/".join(segments)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _local_endpoint_candidates(backend: str, requested_url: str) -> list[str]:
    key, default, port = _LOCAL_BACKENDS[backend]
    raw = [requested_url, config.get(key) or "", default]
    wsl_host = _wsl_host()
    if wsl_host:
        raw.append(f"http://{wsl_host}:{port}")

    candidates: list[str] = []
    for value in raw:
        if not value:
            continue
        candidate = _canonical_local_url(backend, value)
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _validate_approved_folder(entry: Any) -> str:
    """Return one approved-folder entry in canonical form, or reject it.

    An approved folder becomes an *allowed root* for every later path check
    (``validation.validate_path`` passes this list as ``extra_roots``), so a
    value accepted here widens the sandbox for the whole app. The native
    folder dialog is the consent step; this guard is what stops an
    unprivileged or forged request from writing an arbitrary — or merely
    stale, or traversal-encoded — path into that list.

    The entry is normalised with the audited shared helper rather than an
    ad-hoc ``Path(entry)``, then resolved, so ``..`` segments and symlinks
    are collapsed and the *canonical* directory is what gets persisted and
    later compared against. Storing the raw string would let two spellings of
    one directory disagree with the containment check that consumes them.

    A filesystem or drive root is refused outright: granting ``/`` or ``C:\\``
    as an OCR root would nullify path validation everywhere, and no real
    archive folder is a drive root.
    """
    if not isinstance(entry, str) or not entry.strip():
        raise HTTPException(
            status_code=400,
            detail=f"approved_folders: {entry!r} is not a valid folder path",
        )
    try:
        normalised = normalise_path(entry, "approved_folders")
        # CodeQL flags this as py/path-injection, and the taint is real: the
        # value arrives in a request body and becomes an allowed root. It is
        # suppressed rather than sanitised because the usual sanitiser — check
        # the path resolves inside a known-safe root — would defeat the entire
        # feature. An approved folder exists *to be* a new root, for archives
        # on external drives and network shares that are deliberately outside
        # home, tempdir and cwd. Containing it to those roots would leave it
        # able to grant only what is already granted.
        #
        # What stands in for containment: the native OS folder dialog is the
        # consent step, the entry must resolve to an existing directory, it is
        # persisted in canonical form, and a drive or filesystem root is
        # refused outright.
        #
        # The stronger fix is to stop accepting the path from the request at
        # all — /api/native/pick-folder already runs the dialog server-side, so
        # the server could record what it returned and require membership. That
        # removes the taint instead of suppressing it, at the cost of the
        # typed-path fallback used when the native picker is unavailable.
        #
        # The alert is dismissed as "won't fix" in GitHub code scanning, which
        # is the only mechanism that works: a `# codeql[py/path-injection]`
        # comment here is inert. That was tried on this line and the alert
        # simply moved down with it. Do not re-add one — it reads as a working
        # suppression and does nothing.
        resolved = Path(normalised).expanduser().resolve(strict=False)
    except PathValidationError as e:
        raise HTTPException(status_code=400, detail=e.public_message) from e
    except OSError:
        raise HTTPException(
            status_code=400,
            detail=f"approved_folders: {entry!r} cannot be resolved",
        ) from None

    if resolved == Path(resolved.anchor):
        raise HTTPException(
            status_code=400,
            detail=(
                "approved_folders: a drive or filesystem root cannot be "
                "approved. Choose the specific folder holding your images."
            ),
        )
    if not resolved.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"approved_folders: {entry!r} is not an existing directory",
        )
    return str(resolved)


def _validate_base_url(raw: str, field_name: str) -> str:
    """Return *raw* after checking its scheme and host. Fails closed, loudly."""
    if urlsplit(raw).hostname in {"0.0.0.0", "::"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_name}: 0.0.0.0 is the address Ollama listens on, not an "
                "address clients can connect to. Use http://localhost:11434 on "
                "this computer, or http://<this-computer's-LAN-IP>:11434 from "
                "another computer."
            ),
        )
    try:
        return _endpoint_policy.validate_url(raw)
    except EndpointRejected as e:
        raise HTTPException(status_code=400, detail=f"{field_name}: {e}") from e


# URL field → the backend name that activates it.  A URL is only validated
# when a backend that actually consumes it is selected: the Settings form posts
# the app's own shipped defaults back regardless of what the user chose
# (``api_base_url`` ships as ``https://api.openai.com/v1`` even on a
# pure-Ollama install), and rejecting those is what made Save fail
# unconditionally.  The policy itself stays fail-closed — this narrows *which*
# fields are checked, never the check.
_URL_FIELD_BACKENDS = {
    "ollama_url": "ollama",
    "lm_studio_url": "lm_studio",
    "api_base_url": "api_key",
}

_BACKEND_KEYS = ("ocr_backend", "cleanup_backend", "translate_backend")

_CONFIG_KEYS = (
    "lm_studio_url",
    "ollama_url",
    "huggingface_token",
    "api_key",
    "api_base_url",
    "ocr_backend",
    "cleanup_backend",
    "translate_backend",
    "output_dir",
    "cleanup_model",
    "translate_model",
    "ocr_model",
    "document_type",
    "ocr_prompt_instruction",
    "max_ocr_workers",
    "chunk_max_tokens",
    "context_size",
    "ocr_max_image_edge",
    "ocr_temperature_ladder_enabled",
    "ocr_temperature_ladder_start",
    "ocr_temperature_ladder_step",
    "ocr_temperature_ladder_max",
    "ocr_blank_page_skip",
    "ocr_auto_rotation_detect",
    "resume",
    "confidence_enabled",
    "preprocess_enabled",
    "ocr_engine",
    "tesseract_lang",
    "tesseract_path",
    "tesseract_fallback_on_failure",
    "ollama_think",
    "tropy_last_path",
    "tropy_live_browse_enabled",
    "tropy_api_port",
    "approved_folders",
)

# Keys whose values must not be returned verbatim in API responses.
_REDACTED_KEYS = frozenset({"api_key", "huggingface_token"})

REDACTED_PLACEHOLDER = "*" * 12


def _redact_config(key: str, value: str) -> str:
    """Return a placeholder if *key* holds a secret that is configured."""
    if key in _REDACTED_KEYS and value:
        return REDACTED_PLACEHOLDER
    return value


@router.get("/api/config")
def get_config() -> dict:
    return {k: _redact_config(k, config.get(k)) for k in _CONFIG_KEYS}


@router.post("/api/config")
def set_config(overrides: dict[str, Any]) -> dict:
    allowed = {k: v for k, v in overrides.items() if k in config.PERSISTED_KEYS}

    # Never persist the redaction placeholder over a real secret.  The form
    # round-trips the GET /api/config response straight back to this route,
    # and GET redacts secrets to a placeholder — persisting that placeholder
    # would overwrite a genuine key with twelve asterisks.
    for key in _REDACTED_KEYS:
        if allowed.get(key) == REDACTED_PLACEHOLDER:
            del allowed[key]

    # Canonicalise what is stored before validation and persistence.  Model
    # names and URLs accumulate surrounding whitespace (a pasted value, a
    # trailing space in a hand-edited settings file) — strip it so the stored
    # config is canonical and a later reader cannot be caught by a spelling
    # variant.  ``ollama_url`` is a host root to which the caller appends
    # ``/v1``, so it goes through ``normalise_base_url``; ``lm_studio_url`` and
    # ``api_base_url`` already carry ``/v1`` as part of their value and are
    # only whitespace-stripped.
    for key in ("ocr_model", "cleanup_model", "translate_model"):
        value = allowed.get(key)
        if isinstance(value, str):
            allowed[key] = value.strip()

    if isinstance(allowed.get("ollama_url"), str):
        allowed["ollama_url"] = normalise_base_url(allowed["ollama_url"])
    for key in ("lm_studio_url", "api_base_url"):
        value = allowed.get(key)
        if isinstance(value, str):
            allowed[key] = value.strip()

    # Validate endpoint URLs before persisting — a bad value should be
    # refused when entered rather than only when used.  But only validate a
    # URL when a backend that actually uses it is active, so the shipped
    # default for an *unused* field (e.g. api_base_url on a pure-Ollama
    # install) is not rejected.  The effective backends are read from the
    # incoming overrides *merged over current config* — the form may post a
    # partial payload.
    active_backends = {b for b in (allowed.get(k, config.get(k)) for k in _BACKEND_KEYS) if b}
    for field, backend in _URL_FIELD_BACKENDS.items():
        if field in allowed and allowed[field] and backend in active_backends:
            _validate_base_url(allowed[field], field)

    # Approved folders are an explicit user grant, but they must still be
    # validated on write: each entry has to be an existing directory, or the
    # whole save is refused and the offending entry named. The native folder
    # dialog is the consent step; this guard stops an unprivileged request
    # from writing an arbitrary (or stale) path into the approved list.
    if "approved_folders" in allowed:
        folders = allowed["approved_folders"]
        if not isinstance(folders, list):
            raise HTTPException(
                status_code=400,
                detail="approved_folders must be a list of folder paths",
            )
        allowed["approved_folders"] = [_validate_approved_folder(entry) for entry in folders]

    config.save_user_settings(allowed)
    config.apply_overrides(allowed)
    return {"ok": True}


@router.post("/api/config/reset")
def reset_config() -> dict:
    config.reset()
    config.load_config(include_user_settings=False)
    return {k: _redact_config(k, config.get(k)) for k in _CONFIG_KEYS}


@router.get("/api/document-types")
def document_types() -> dict:
    return {"types": DOCUMENT_TYPES}


@router.get("/api/local-models")
async def local_models(backend: str, url: str = "") -> dict:
    """Discover one selected local backend and return its installed models.

    The configured address is tried first, followed by the normal localhost
    address and the Windows host when Artifice runs inside WSL2. This keeps
    endpoint details out of the normal setup path while still allowing an
    advanced address override.
    """
    if backend not in _LOCAL_BACKENDS:
        raise HTTPException(status_code=400, detail="backend must be 'ollama' or 'lm_studio'")

    candidates = _local_endpoint_candidates(backend, url)
    allowed: list[str] = []
    rejected = 0
    for candidate in candidates:
        try:
            _endpoint_policy.validate_url(candidate)
        except EndpointRejected:
            rejected += 1
            continue
        allowed.append(candidate)

    probes = await asyncio.gather(
        *(probe_endpoint(candidate, policy=_endpoint_policy, timeout_s=5) for candidate in allowed),
        return_exceptions=True,
    )
    for candidate, probe in zip(allowed, probes, strict=True):
        if isinstance(probe, Exception) or not probe.reachable:
            continue
        # Ollama exposes both its native and OpenAI-compatible APIs. Do not
        # mistake a different OpenAI server for Ollama merely because it was
        # entered in the Ollama row.
        if backend == "ollama" and probe.provider != "ollama":
            continue
        if backend == "lm_studio" and probe.provider == "ollama":
            continue
        return {
            "ok": True,
            "backend": backend,
            "url": candidate,
            "models": list(probe.models),
        }

    return {
        "ok": False,
        "backend": backend,
        "url": allowed[0] if allowed else "",
        "models": [],
        "detail": (
            "No permitted local endpoint address was provided."
            if not allowed and rejected
            else f"Could not find a running {backend.replace('_', ' ').title()} server."
        ),
    }


# Local backends that carry a probeable model list (mirrors
# artifice_ocr._resolution._BACKEND_URL_KEYS). Anything else — api_key,
# huggingface — is a cloud backend with no local shelf to check.
_BACKEND_URL_KEYS_HEALTH = frozenset({"lm_studio", "ollama"})


@router.get("/api/health")
async def health_check() -> dict:
    backends = {
        config.get("ocr_backend") or "auto",
        config.get("cleanup_backend") or "auto",
        config.get("translate_backend") or "auto",
    }
    # ``auto`` means "whichever local server is reachable" — probe both.
    wants_auto = "auto" in backends

    results: dict[str, Any] = {}
    # Populated only for the local backends actually probed below, so each
    # role's model can be graded against *its own* backend's probe rather
    # than always against Ollama's — see the per-role loop after.
    probes: dict[str, Any] = {}

    lm_studio_url = config.get("lm_studio_url") or "http://localhost:1234/v1"
    ollama_url = config.get("ollama_url") or "http://localhost:11434"

    # Both probes are independent network calls with their own timeouts (5s,
    # 10s) — run concurrently, matching /api/local-models next to this route,
    # instead of paying both timeouts back-to-back when neither server answers.
    tasks: dict[str, Any] = {}
    if "lm_studio" in backends or wants_auto:
        tasks["lm_studio"] = probe_endpoint(lm_studio_url, policy=_endpoint_policy, timeout_s=5)
    if "ollama" in backends or wants_auto:
        tasks["ollama"] = probe_endpoint(ollama_url, policy=_endpoint_policy, timeout_s=10)
    gathered = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values()), strict=True))

    if "lm_studio" in gathered:
        probe = gathered["lm_studio"]
        probes["lm_studio"] = probe
        results["lm_studio"] = {
            "ok": probe.reachable,
            "detail": None if probe.reachable else (probe.hint or "Cannot reach LM Studio"),
            "url": lm_studio_url,
            "models": list(probe.models) if probe.reachable else [],
        }

    if "ollama" in gathered:
        probe = gathered["ollama"]
        probes["ollama"] = probe
        results["ollama"] = {
            "ok": probe.reachable,
            "detail": None if probe.reachable else (probe.hint or "Cannot reach Ollama"),
            "url": ollama_url,
            "models": list(probe.models) if probe.reachable else [],
        }

    if "huggingface" in backends:
        token = config.get("huggingface_token")
        results["huggingface"] = {
            "ok": bool(token),
            "detail": None if token else "No Hugging Face token configured",
        }

    if "api_key" in backends:
        base_url = config.get("api_base_url") or "https://api.openai.com/v1"
        from ..._backend import get_client

        ok, detail = get_client("api_key").health_check()
        results["api_key"] = {"ok": ok, "detail": detail, "url": base_url}

    # Per-role model check, each graded against the backend that role is
    # actually configured to use — never blanket-checked against Ollama.
    # This is returned whenever any local backend is active (not only
    # Ollama), so a pure-LM-Studio install gets a real model check too.
    results["models"] = _model_health(probes)

    return results


def _model_health(probes: dict[str, Any]) -> list[dict[str, Any]]:
    """Grade each role's configured model against its own backend's probe.

    ``ROLE_KEYS`` (from ``artifice_ocr._resolution``) maps each role to its
    ``(model config key, backend config key)`` pair — the same mapping the
    once-per-run resolver uses, so "which endpoint is this role's model
    checked against" cannot drift from "which endpoint the run will actually
    call".

    * An explicit local backend (``lm_studio`` / ``ollama``) is graded
      against that backend's own probe.
    * ``auto`` is graded against the union of whichever local endpoints are
      reachable, mirroring ``_resolution._resolve_auto`` (which never
      auto-selects a cloud backend).
    * A cloud backend (``api_key`` / ``huggingface``) has no local model list
      to check — it is reported as not checkable rather than a false
      negative.
    * An empty configured model name is skipped, as before.
    """
    entries: list[dict[str, Any]] = []
    for _role, (model_key, backend_key) in ROLE_KEYS.items():
        model_name = (config.get(model_key) or "").strip()
        if not model_name:
            continue

        role_backend = (config.get(backend_key) or "auto").strip().lower() or "auto"

        if role_backend == "auto":
            reachable = [p for p in probes.values() if p.reachable]
            available: set[str] = set()
            for p in reachable:
                available.update(p.models)
            entries.append(
                {
                    "name": model_name,
                    "backend": "auto",
                    "ok": bool(reachable) and model_name in available,
                }
            )
            continue

        if role_backend not in _BACKEND_URL_KEYS_HEALTH:
            # Cloud backend (api_key, huggingface, ...): no local model list
            # to check against. Do not report a false negative — mark it
            # explicitly as not checkable instead.
            entries.append(
                {
                    "name": model_name,
                    "backend": role_backend,
                    "ok": None,
                    "checkable": False,
                    "detail": f"'{role_backend}' has no local model list to check against.",
                }
            )
            continue

        probe = probes.get(role_backend)
        if probe is None:
            # A local backend that was not probed above (config drift between
            # the ``backends`` set and this loop) — treat as unreachable
            # rather than crash.
            entries.append({"name": model_name, "backend": role_backend, "ok": False})
            continue

        entries.append(
            {
                "name": model_name,
                "backend": role_backend,
                "ok": probe.reachable and model_name in set(probe.models),
            }
        )

    return entries


@router.get("/api/tesseract/status")
def tesseract_status() -> dict:
    """Whether the Tesseract binary is detected, where, its version, and the
    configured language — so the UI can tell the user honestly rather than
    offering an engine that silently does nothing."""
    from ..._tesseract import status

    return status()
