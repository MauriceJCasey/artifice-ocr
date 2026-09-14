# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for :mod:`model_harness.resolution`.

The resolver's one job is to turn "what is installed" plus "what do we need"
into a single model name — and to refuse to substitute a different model for
one the user explicitly chose.  These tests pin the precedence order and the
two cases that must never silently degrade: a missing user choice and a vision
role with no certified vision model installed.
"""

from __future__ import annotations

import pytest
from model_harness.registry import HardwareTier
from model_harness.resolution import ResolutionSource, resolve_model

# ── Precedence branches ──────────────────────────────────────────────────────


def test_user_choice_wins_when_installed():
    """Step 1: the user's configured model, when installed, is used verbatim."""
    result = resolve_model(
        role="chat",
        installed=["qwen2.5:7b", "llama3.2:3b"],
        configured="llama3.2:3b",
    )
    assert result.model_name == "llama3.2:3b"
    assert result.source is ResolutionSource.USER_CHOICE
    assert result.configured_but_missing is False


def test_configured_missing_fails_without_substitution():
    """Step 2: a configured model that is not installed must not silently fall
    back to something else — that would run a different model than requested."""
    result = resolve_model(
        role="chat",
        installed=["qwen2.5:7b"],
        configured="llama3.2:3b",
    )
    assert result.model_name is None
    assert result.source is ResolutionSource.CONFIGURED_MISSING
    assert result.configured_but_missing is True


def test_empty_configured_is_treated_as_no_choice():
    """Both ``configured=""`` and ``configured=None`` must fall through to
    steps 3-4, never yield ``CONFIGURED_MISSING``.

    Every app's model default is now an empty string, so ``configured=""`` is
    the single most common input in production.  A regression that let an
    empty string slip into the ``if configured`` branch would make every app
    report a missing model forever.
    """
    installed = ["qwen2.5:7b", "llama3.2:3b"]
    for configured in ("", None):
        result = resolve_model(
            role="chat",
            installed=installed,
            configured=configured,
        )
        assert result.source is not ResolutionSource.CONFIGURED_MISSING
        assert result.configured_but_missing is False
        # Falls through to step 4: the first plausible installed model.
        assert result.source is ResolutionSource.FALLBACK
        assert result.model_name == "qwen2.5:7b"


def test_registry_recommendation_used_when_installed():
    """Step 3: an installed registry recommendation for (app, tier, role) wins.

    ``artifice-graph``/LAPTOP recommends ``llama3.2:3b`` first, but only
    ``qwen2.5:7b`` (the second recommendation) is installed here — the resolver
    must walk the ordered recommendation list and pick the first that is present.
    """
    result = resolve_model(
        role="chat",
        installed=["qwen2.5:7b"],
        app="artifice-graph",
        tier=HardwareTier.LAPTOP,
    )
    assert result.model_name == "qwen2.5:7b"
    assert result.source is ResolutionSource.RECOMMENDED


def test_fallback_uses_first_plausible_installed_model():
    """Step 4: with no configured choice and no recommendation, the first
    plausible installed model is used."""
    result = resolve_model(
        role="chat",
        installed=["mistral:7b", "gemma:7b"],
    )
    assert result.model_name == "mistral:7b"
    assert result.source is ResolutionSource.FALLBACK


def test_none_available_when_nothing_fits():
    """Step 5: an empty or entirely-inapplicable installed list yields nothing."""
    result = resolve_model(role="chat", installed=[])
    assert result.model_name is None
    assert result.source is ResolutionSource.NONE_AVAILABLE
    assert result.configured_but_missing is False


# ── Role-fitness limits ──────────────────────────────────────────────────────


def test_vision_role_with_only_text_models_returns_none_available():
    """A vision role must not fall back to a text model — only the registry can
    certify vision capability, and nothing here is certified."""
    result = resolve_model(
        role="vision",
        installed=["llama3.2:3b", "qwen2.5:7b"],
    )
    assert result.model_name is None
    assert result.source is ResolutionSource.NONE_AVAILABLE


def test_vision_role_uses_registry_vision_recommendation_when_installed():
    """Contrast: with the registry's vision recommendation installed, a vision
    role resolves to it via step 3."""
    result = resolve_model(
        role="vision",
        installed=["richardyoung/olmocr2:7b-q8"],
        app="artifice-ocr",
        tier=HardwareTier.DESKTOP,
    )
    assert result.model_name == "richardyoung/olmocr2:7b-q8"
    assert result.source is ResolutionSource.RECOMMENDED


def test_vision_role_recognises_native_lm_studio_olmocr_name():
    """The upstream Hugging Face ID is the model name LM Studio exposes."""
    result = resolve_model(
        role="vision",
        installed=["allenai/olmocr-2-7b", "text-embedding-nomic-embed-text-v1.5"],
        app="artifice-ocr",
        tier=HardwareTier.DESKTOP,
    )
    assert result.model_name == "allenai/olmocr-2-7b"
    assert result.source is ResolutionSource.RECOMMENDED


def test_vision_role_does_not_use_translation_recommendation():
    """A translation recommendation (role='translation', vision=False) must not
    satisfy a vision role even though it is installed and in the same app."""
    result = resolve_model(
        role="vision",
        installed=["aya-expanse:8b"],
        app="artifice-ocr",
        tier=HardwareTier.LAPTOP,
    )
    assert result.model_name is None
    assert result.source is ResolutionSource.NONE_AVAILABLE


def test_embedding_role_picks_embed_named_model_even_when_not_first():
    """An embedding role selects the ``embed``-named model, skipping earlier
    non-embedding models, even when the embed model is not first."""
    result = resolve_model(
        role="embedding",
        installed=["qwen2.5:7b", "llama3.2:3b", "nomic-embed-text"],
    )
    assert result.model_name == "nomic-embed-text"
    assert result.source is ResolutionSource.FALLBACK


def test_chat_role_skips_embedding_models():
    """A chat role will not silently pick an embedding model as its fallback."""
    result = resolve_model(
        role="chat",
        installed=["nomic-embed-text", "qwen2.5:7b"],
    )
    assert result.model_name == "qwen2.5:7b"
    assert result.source is ResolutionSource.FALLBACK


# ── Name-form equivalence (Ollama ``:`` vs OpenAI-compatible ``-``) ──────────


def test_ollama_colon_form_matches_openai_hyphen_form():
    """Configured in Ollama form, served in OpenAI form: still a USER_CHOICE,
    and the installed string is what gets returned (the endpoint accepts it)."""
    result = resolve_model(
        role="chat",
        installed=["llama3.2-3b"],
        configured="llama3.2:3b",
    )
    assert result.source is ResolutionSource.USER_CHOICE
    assert result.model_name == "llama3.2-3b"
    assert result.configured_but_missing is False


def test_openai_hyphen_form_matches_ollama_colon_form():
    """The reverse direction is equivalent."""
    result = resolve_model(
        role="chat",
        installed=["llama3.2:3b"],
        configured="llama3.2-3b",
    )
    assert result.source is ResolutionSource.USER_CHOICE
    assert result.model_name == "llama3.2:3b"


def test_recommendation_matches_across_name_forms():
    """Step 3 also tolerates the name-form difference: a registry recommendation
    in Ollama form matches an OpenAI-form installed entry."""
    result = resolve_model(
        role="chat",
        installed=["llama3.2-3b"],
        app="artifice-graph",
        tier=HardwareTier.LAPTOP,
    )
    assert result.source is ResolutionSource.RECOMMENDED
    assert result.model_name == "llama3.2-3b"


def test_exact_match_wins_over_normalised_match():
    """When both name forms are installed, the exact string is preferred."""
    result = resolve_model(
        role="chat",
        installed=["llama3.2-3b", "llama3.2:3b"],
        configured="llama3.2:3b",
    )
    assert result.source is ResolutionSource.USER_CHOICE
    assert result.model_name == "llama3.2:3b"


# ── Determinism ──────────────────────────────────────────────────────────────


def test_determinism_same_inputs_same_output():
    """Repeated calls with identical inputs yield an identical, equal result."""
    installed = ["qwen2.5:7b", "llama3.2:3b", "nomic-embed-text"]
    expected = resolve_model(
        role="embedding",
        installed=installed,
        app="artifice-graph",
        tier=HardwareTier.DESKTOP,
    )
    for _ in range(1000):
        got = resolve_model(
            role="embedding",
            installed=installed,
            app="artifice-graph",
            tier=HardwareTier.DESKTOP,
        )
        assert got == expected
        assert got.model_name == "nomic-embed-text"


def test_installed_order_is_the_fallback_tie_break():
    """Given the same set of names in a different order, the fallback follows
    the input order — first plausible model wins."""
    result = resolve_model(role="chat", installed=["b-model", "a-model"])
    assert result.model_name == "b-model"
    assert result.source is ResolutionSource.FALLBACK


# ── Result type shape ────────────────────────────────────────────────────────


def test_model_resolution_is_frozen():
    """The result is immutable so callers cannot mutate a shared answer."""
    result = resolve_model(role="chat", installed=["llama3.2:3b"])
    with pytest.raises((AttributeError, TypeError)):
        result.model_name = "changed"  # type: ignore[misc]


def test_resolution_source_is_a_closed_enum():
    """The source is an enum, not a free string, with exactly the five states."""
    assert {s.value for s in ResolutionSource} == {
        "user_choice",
        "configured_missing",
        "recommended",
        "fallback",
        "none_available",
    }
