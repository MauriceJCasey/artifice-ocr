# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared helpers for talking to LLMs across backends (Ollama, LM Studio, Hugging Face)."""

from typing import Any

from . import _backend
from ._logging import get_logger

log = get_logger("llm")


def chat(
    *,
    backend: str = "ollama",
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    think: bool | None = None,
    num_predict: int | None = None,
) -> Any:
    """Send a chat completion request via the selected backend."""
    client = _backend.get_client(backend)
    return client.chat(
        model=model,
        messages=messages,
        temperature=temperature,
        think=think,
        num_predict=num_predict,
    )
