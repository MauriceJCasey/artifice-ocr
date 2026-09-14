# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from model_harness.discovery import probe_endpoint_sync
from model_harness.endpoint_policy import EndpointPolicy

from . import config

_endpoint_policy = EndpointPolicy()


def check_lm_studio(url: str | None = None) -> str | None:
    """Return an error message if LM Studio is unreachable, else None."""
    lm_studio_url = config.get("lm_studio_url") or "http://localhost:1234/v1"
    result = probe_endpoint_sync(lm_studio_url, policy=_endpoint_policy, timeout_s=5)
    if result.reachable:
        return None
    return f"Cannot reach LM Studio at {lm_studio_url}. {result.hint or 'Is it running?'}"


def check_ollama(required_models: list[str] | None = None, url: str | None = None) -> list[str]:
    """Return list of error messages for Ollama. Empty list = all OK."""
    errors: list[str] = []
    host = url or "http://localhost:11434"
    result = probe_endpoint_sync(host, policy=_endpoint_policy, timeout_s=10)

    if not result.reachable:
        return [f"Cannot reach Ollama at {host}. Is it running?"]

    if required_models:
        available = set(result.models)
        for model in required_models:
            if model not in available:
                errors.append(f'Model "{model}" is not downloaded. Open Ollama and download it first.')

    return errors
