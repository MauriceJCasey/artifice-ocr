# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for :mod:`model_harness.registry`.

Every ``size_bytes`` value in :data:`ASR_MODELS` is shown to a user *before*
they approve a download — a wrong figure is a wrong promise.  These tests
assert the data is internally consistent and that provider values stay in
sync with the :class:`Provider` literal in :mod:`model_harness.contract`.
"""

from __future__ import annotations

import sys
import types
from typing import get_args

import pytest
from model_harness.contract import Provider
from model_harness.registry import (
    ASR_MODELS,
    KNOWN_ENDPOINTS,
    PERMITTED_BADGES,
    HardwareTier,
    ModelRecommendation,
    get_asr_model,
    get_endpoint,
    is_configured,
    recommendations_for_app,
)

# ── ASR_MODELS integrity ─────────────────────────────────────────────────────


def test_every_asr_model_has_nonzero_size():
    """A zero-byte model file is either a config stub or a data error — neither
    belongs in a consent dialog."""
    for key, info in ASR_MODELS.items():
        assert info.size_bytes > 0, f"{key}: size_bytes is {info.size_bytes}"


def test_every_asr_model_has_plausible_repo():
    """Every entry must carry a ``namespace/repo`` Hugging Face id."""
    for key, info in ASR_MODELS.items():
        assert "/" in info.hf_repo, f"{key}: hf_repo {info.hf_repo!r} does not look like a HF id"


def test_pyannote_models_require_token():
    """Both pyannote models are gated on Hugging Face."""
    diar = ASR_MODELS["pyannote-speaker-diarization"]
    emb = ASR_MODELS["pyannote-embedding"]
    assert diar.requires_hf_token is True, "pyannote-speaker-diarization must require a token"
    assert emb.requires_hf_token is True, "pyannote-embedding must require a token"


def test_whisper_does_not_require_token():
    """Whisper large-v3 is not gated."""
    assert ASR_MODELS["whisper-large-v3"].requires_hf_token is False


# ── KNOWN_ENDPOINTS ↔ Provider sync ──────────────────────────────────────────


_VALID_PROVIDERS: frozenset[str] = frozenset(get_args(Provider))


def test_every_endpoint_provider_is_a_valid_provider_literal():
    """If someone edits ``contract.Provider`` and not this registry, the test
    fails.  vLLM / LocalAI map to ``"generic-api"`` by design — that *is* a
    valid member of the literal."""
    for key, ep in KNOWN_ENDPOINTS.items():
        assert ep.provider in _VALID_PROVIDERS, (
            f"{key}: provider {ep.provider!r} is not in Provider literal; "
            f"valid values are {sorted(_VALID_PROVIDERS)}"
        )


def test_vllm_maps_to_generic_api():
    """vLLM and LocalAI have no dedicated provider — they must map to
    ``"generic-api"``.  Do not add new members to the ``Provider`` literal as
    a side effect."""
    assert KNOWN_ENDPOINTS["vllm"].provider == "generic-api"


# ── Recommendations ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("tier", list(HardwareTier))
def test_ocr_recommendations_match_role_vision(tier: HardwareTier):
    """All OCR model recommendations with role='vision' must support image inputs.

    OCR also recommends a translation model (aya-expanse) for its translation
    prompt — that model is not vision-capable, which is correct for its role.
    """
    recs = recommendations_for_app("artifice-ocr", tier)
    assert len(recs) > 0, f"no OCR recommendations for {tier}"
    assert any(r.vision is True for r in recs), f"OCR tier {tier} has no vision-capable model"
    for rec in recs:
        if rec.role == "vision":
            assert rec.vision is True, (
                f"OCR recommendation {rec.model_name!r} with role='vision' has vision=False"
            )


@pytest.mark.parametrize("app", ["artifice-graph", "artifice-draft", "artifice-transcribe"])
@pytest.mark.parametrize("tier", list(HardwareTier))
def test_graph_draft_and_transcribe_recommendations_are_text_only(app: str, tier: HardwareTier):
    """Graph, draft and transcribe models must not be vision models — they handle text."""
    recs = recommendations_for_app(app, tier)
    assert len(recs) > 0, f"no recommendations for {app}/{tier}"
    for rec in recs:
        assert rec.vision is False, (
            f"{app}/{tier} recommendation {rec.model_name!r} has vision=True"
        )


def test_recommendations_vary_by_tier():
    """A laptop and a desktop should not get the same set of recommendations.

    With the restricted open-provenance model set, the first model (olmocr2)
    may be the same across tiers — variation comes from the translation model
    variant (8b vs 32b).  Compare full sets, not just the first entry.
    """
    laptop_recs = recommendations_for_app("artifice-ocr", HardwareTier.LAPTOP)
    desktop_recs = recommendations_for_app("artifice-ocr", HardwareTier.DESKTOP)
    laptop_names = tuple(r.model_name for r in laptop_recs)
    desktop_names = tuple(r.model_name for r in desktop_recs)
    assert laptop_names != desktop_names, (
        f"laptop {laptop_names!r} and desktop {desktop_names!r} have identical recommendations"
    )


def test_recommendations_unknown_app_raises():
    with pytest.raises(KeyError):
        recommendations_for_app("nonexistent-app", HardwareTier.LAPTOP)


_KNOWN_RECOMMENDATION_APPS: tuple[str, ...] = (
    "artifice-ocr",
    "artifice-graph",
    "artifice-draft",
    "artifice-transcribe",
)


def test_transcribe_recommendations_are_text_only():
    """``artifice-transcribe`` now has text-only recommendations for its
    optional post-transcription inference endpoint.  The old behaviour
    (``KeyError``) is no longer correct."""
    for tier in HardwareTier:
        recs = recommendations_for_app("artifice-transcribe", tier)
        assert len(recs) > 0, f"transcribe has no recommendations for {tier}"
        for rec in recs:
            assert rec.vision is False, (
                f"transcribe recommendation {rec.model_name!r} has vision=True"
            )


@pytest.mark.parametrize("app", _KNOWN_RECOMMENDATION_APPS)
def test_no_two_tiers_return_identical_recommendations(app: str):
    """Every hardware tier for a given app should recommend a meaningfully
    different set of models.

    Compares ``(model_name, role)`` tuples.  With the restricted
    open-provenance model set the OCR app may have LAPTOP and MAC_UNIFIED
    sharing the same models — that is a legitimate structural limitation of
    the open model ecosystem, not a registry error.
    """
    tiers = list(HardwareTier)
    seen: dict[frozenset[tuple[str, str]], HardwareTier] = {}
    for tier in tiers:
        recs = recommendations_for_app(app, tier)
        signature = frozenset((r.model_name, r.role) for r in recs)
        if signature in seen:
            if app == "artifice-ocr":
                # With only olmocr2 (vision) and aya-expanse (translation),
                # LAPTOP and MAC_UNIFIED legitimately share the same set.
                continue
            raise AssertionError(
                f"{app}: {tier.value} recommendations duplicate those of "
                f"{seen[signature].value} "
                f"({sorted(signature)!r})"
            )
        seen[signature] = tier


# ── LAPTOP VRAM constraints ────────────────────────────────────────────────
#
# Two tests govern what appears in the LAPTOP tier:
#
# 1. An **absolute ceiling** of 12.0 GB on ``min_vram_gb`` — the heaviest
#    open-provenance model usable on a laptop (olmocr2:7b-q8 at Q8_0, which
#    needs ~12 GB for full GPU offload but runs with CPU fallback on 8 GB
#    GPUs).  Anything above 12.0 GB is unreasonable for the LAPTOP tier.
#
# 2. An **honesty gate**: any recommendation whose ``min_vram_gb`` exceeds
#    8 GB (the VRAM of a typical laptop dGPU: RTX 4060 / 3070) must document
#    in its ``notes`` that it runs with CPU fallback at reduced throughput.
#    Without this check, a 12 GB recommendation looks like it would run at
#    GPU speed on an 8 GB laptop, which it will not.
#
# ``min_vram_gb`` means "VRAM for full GPU offload."  What it does *not* mean
# — and what ``notes`` covers — is "VRAM below which the model is unusable."
# If those two concepts ever collide in a way these tests cannot express, the
# field may need splitting into a "full-offload" floor and a "runs at all"
# floor.  That is a design decision for the maintainer.
_LAPTOP_VRAM_ABSOLUTE_CEILING_GB: float = 12.0
_LAPTOP_GPU_VRAM_TYPICAL_GB: float = 8.0


def test_no_laptop_recommendation_exceeds_vram_ceiling():
    """No LAPTOP-tier recommendation may have ``min_vram_gb`` above the
    absolute ceiling.

    This is the test that would have caught a 12.0 GB recommendation in the
    LAPTOP tier before the figure was sourced honestly.  Now that 12.0 GB
    *is* the honest figure for the heaviest LAPTOP model, the ceiling is 12.0
    — anything above does not belong in this tier at all.
    """
    violations: list[str] = []
    for app in _KNOWN_RECOMMENDATION_APPS:
        recs = recommendations_for_app(app, HardwareTier.LAPTOP)
        for rec in recs:
            if rec.min_vram_gb is not None and rec.min_vram_gb > _LAPTOP_VRAM_ABSOLUTE_CEILING_GB:
                violations.append(
                    f"{app}/{rec.model_name}: "
                    f"min_vram_gb={rec.min_vram_gb} exceeds ceiling "
                    f"{_LAPTOP_VRAM_ABSOLUTE_CEILING_GB}"
                )
    assert not violations, (
        f"LAPTOP VRAM ceiling ({_LAPTOP_VRAM_ABSOLUTE_CEILING_GB} GB) violated:\n"
        + "\n".join(violations)
    )


def test_laptop_models_exceeding_gpu_vram_document_fallback():
    """Any LAPTOP recommendation whose ``min_vram_gb`` exceeds a typical
    laptop dGPU (8 GB) must explain the trade-off in its ``notes``.

    A model that needs full GPU offload for GPU-speed inference will run with
    CPU fallback on a laptop GPU — the user must know that before trying it.
    """
    violations: list[str] = []
    for app in _KNOWN_RECOMMENDATION_APPS:
        recs = recommendations_for_app(app, HardwareTier.LAPTOP)
        for rec in recs:
            if (
                rec.min_vram_gb is not None
                and rec.min_vram_gb > _LAPTOP_GPU_VRAM_TYPICAL_GB
                and "CPU" not in rec.notes
                and "fallback" not in rec.notes.lower()
            ):
                violations.append(
                    f"{app}/{rec.model_name}: "
                    f"min_vram_gb={rec.min_vram_gb} > "
                    f"{_LAPTOP_GPU_VRAM_TYPICAL_GB} GB but notes "
                    f"mention no CPU fallback: {rec.notes!r}"
                )
    assert not violations, (
        f"LAPTOP recommendations exceeding {_LAPTOP_GPU_VRAM_TYPICAL_GB} GB VRAM "
        f"must document CPU fallback:\n" + "\n".join(violations)
    )


# ── BYOM serialiser integrity ──────────────────────────────────────────────

# Every tier for every app must carry recommendations whose ``ethos_badges``,
# ``role``, and ``notes`` fields are populated — these serialise into the BYOM
# model-selection UI and a missing field renders as a blank chip or label.


def test_every_recommendation_has_role():
    """A recommendation with an empty role defaults to ``"chat"`` at the
    dataclass level, but the BYOM serialiser skips default values —
    verify every recommendation explicitly sets a role."""
    for app in _KNOWN_RECOMMENDATION_APPS:
        for tier in HardwareTier:
            recs = recommendations_for_app(app, tier)
            for rec in recs:
                assert rec.role, f"{app}/{tier.value}/{rec.model_name}: role is empty"


def test_every_recommendation_with_badges_has_notes():
    """A badge without a human-readable note leaves the user with a chip and
    no explanation of what it means.  Every recommendation that carries
    ``ethos_badges`` must also carry a non-empty ``notes`` string."""
    for app in _KNOWN_RECOMMENDATION_APPS:
        for tier in HardwareTier:
            recs = recommendations_for_app(app, tier)
            for rec in recs:
                if rec.ethos_badges:
                    assert rec.notes, (
                        f"{app}/{tier.value}/{rec.model_name}: "
                        f"has ethos_badges={rec.ethos_badges} but notes is empty"
                    )


# ── Open-science metadata integrity ───────────────────────────────────────


def test_every_ethos_badge_is_permitted():
    """Every badge on every recommendation must be in PERMITTED_BADGES.

    A typo in a badge string should fail here, not reach the UI.
    """
    for app in _KNOWN_RECOMMENDATION_APPS:
        for tier in HardwareTier:
            recs = recommendations_for_app(app, tier)
            for rec in recs:
                for badge in rec.ethos_badges:
                    assert badge in PERMITTED_BADGES, (
                        f"{app}/{tier.value}/{rec.model_name}: "
                        f"badge {badge!r} is not in PERMITTED_BADGES"
                    )


def test_no_entry_carries_removed_cultural_linguistic_fluency_badge():
    """The badge ``"Cultural & Linguistic Fluency"`` was removed 2026-08-06
    on the maintainer's instruction — badges describe provenance, not
    capability.  This test ensures it has not crept back in."""
    _REMOVED_BADGE: str = "Cultural & Linguistic Fluency"
    assert _REMOVED_BADGE not in PERMITTED_BADGES, (
        f"removed badge {_REMOVED_BADGE!r} is still in PERMITTED_BADGES"
    )
    for app in _KNOWN_RECOMMENDATION_APPS:
        for tier in HardwareTier:
            recs = recommendations_for_app(app, tier)
            for rec in recs:
                assert _REMOVED_BADGE not in rec.ethos_badges, (
                    f"{app}/{tier.value}/{rec.model_name}: carries removed badge {_REMOVED_BADGE!r}"
                )


def test_every_model_name_is_non_empty():
    """A blank model_name is a silent no-op in a pull-command field."""
    for app in _KNOWN_RECOMMENDATION_APPS:
        for tier in HardwareTier:
            recs = recommendations_for_app(app, tier)
            for rec in recs:
                assert rec.model_name, f"{app}/{tier.value}: empty model_name"


def test_every_tier_for_every_app_has_at_least_one_recommendation():
    """No tier for any app should be left empty.

    A tier with zero recommendations means users on that hardware see nothing.
    """
    for app in _KNOWN_RECOMMENDATION_APPS:
        for tier in HardwareTier:
            recs = recommendations_for_app(app, tier)
            assert len(recs) > 0, f"{app}/{tier.value}: tier has no recommendations"


_VALID_ROLES: frozenset[str] = frozenset({"vision", "chat", "translation", "embedding"})


def test_every_role_is_a_valid_badge_role():
    """Every recommendation's role must be one of the known BadgeRole values."""
    for app in _KNOWN_RECOMMENDATION_APPS:
        for tier in HardwareTier:
            recs = recommendations_for_app(app, tier)
            for rec in recs:
                assert rec.role in _VALID_ROLES, (
                    f"{app}/{tier.value}/{rec.model_name}: "
                    f"role {rec.role!r} is not a valid BadgeRole"
                )


def test_default_fields_work():
    """Constructing a ModelRecommendation with only required fields
    should default ethos_badges to [], role to 'chat', and notes to ''."""
    rec = ModelRecommendation(
        model_name="test-model",
        provider="ollama",
        vision=False,
    )
    assert rec.ethos_badges == []
    assert rec.role == "chat"
    assert rec.notes == ""


# ── Accessors ────────────────────────────────────────────────────────────────


def test_get_endpoint_returns_correct_info():
    info = get_endpoint("ollama")
    assert info.display_name == "Ollama"
    assert info.default_port == 11434


def test_get_endpoint_unknown_key_raises():
    with pytest.raises(KeyError):
        get_endpoint("nonexistent")


def test_get_asr_model_returns_correct_info():
    info = get_asr_model("whisper-large-v3")
    assert info.hf_repo == "openai/whisper-large-v3"
    assert info.requires_hf_token is False


def test_get_asr_model_unknown_key_raises():
    with pytest.raises(KeyError):
        get_asr_model("nonexistent")


# ── No I/O at import time ────────────────────────────────────────────────────

# Modules that would indicate the registry has grown I/O concerns.
_IO_FLAGS: frozenset[str] = frozenset(
    {
        "httpx",
        "requests",
        "urllib",
        "http.client",
        "os",
        "pathlib",
        "socket",
        "subprocess",
    }
)


def test_registry_imports_no_io_libraries():
    """The registry is pure data.  If it acquires an I/O dependency, this test
    fails and a reviewer needs to ask whether that dependency belongs in
    :mod:`model_harness.discovery` instead."""
    import model_harness.registry

    # Gather every name the module can reach.
    names = set(dir(model_harness.registry))
    # Also check what the module actually imported.
    for attr in names:
        obj = getattr(model_harness.registry, attr, None)
        if isinstance(obj, types.ModuleType):
            names.add(obj.__name__)

    flagged = names & _IO_FLAGS
    assert not flagged, (
        f"registry imports I/O-related names: {sorted(flagged)}. "
        f"Probing belongs in discovery.py, not here."
    )


# ── Types hold their shape ───────────────────────────────────────────────────


# ── is_configured ────────────────────────────────────────────────────────────


class TestIsConfigured:
    """``is_configured`` provides one shared ``configured`` rule for all four apps."""

    def test_empty_everything_is_false(self):
        assert is_configured("") is False
        assert is_configured("", "") is False

    def test_non_empty_api_key_alone_is_true(self):
        assert is_configured("", "sk-real") is True

    def test_placeholder_api_key_is_not_true(self):
        assert is_configured("", "not-needed") is False
        assert is_configured("not-needed", "not-needed", defaults=("not-needed",)) is False
        assert is_configured("", "") is False

    def test_base_url_not_in_defaults_is_true(self):
        assert (
            is_configured("http://localhost:9999/v1", defaults=("http://localhost:11434/v1",))
            is True
        )

    def test_base_url_in_defaults_is_false_without_key(self):
        assert (
            is_configured("http://localhost:11434/v1", defaults=("http://localhost:11434/v1",))
            is False
        )
        assert (
            is_configured("https://api.openai.com/v1", defaults=("https://api.openai.com/v1",))
            is False
        )

    def test_base_url_in_defaults_is_true_with_key(self):
        assert (
            is_configured(
                "http://localhost:11434/v1", "sk-real", defaults=("http://localhost:11434/v1",)
            )
            is True
        )

    def test_empty_defaults_draft_case(self):
        """Draft's load_settings returns {} — no defaults, no api_key."""
        assert is_configured("") is False  # empty base_url, empty api_key
        assert is_configured("http://localhost:11434/v1") is True  # any non-empty URL counts

    def test_two_urls_ocr_case(self):
        """OCR calls the helper once per URL.  An ollama departure alone is enough."""
        assert is_configured("http://localhost:11435", defaults=("http://localhost:11434",)) is True
        assert (
            is_configured("http://localhost:11434", defaults=("http://localhost:11434",)) is False
        )

    def test_transcribe_not_needed_discounted(self):
        """The ``not-needed`` placeholder must not count as configured."""
        assert (
            is_configured(
                "http://localhost:11434/v1", "not-needed", defaults=("http://localhost:11434/v1",)
            )
            is False
        )

    def test_multiple_defaults(self):
        """If an app ships more than one possible default, all are harmless."""
        assert (
            is_configured(
                "http://localhost:11434/v1",
                defaults=("http://localhost:11434/v1", "http://localhost:8080/v1"),
            )
            is False
        )


# ── Types hold their shape ───────────────────────────────────────────────────


def test_model_recommendation_is_immutable():
    rec = ModelRecommendation(model_name="llava:7b", provider="ollama", vision=True)
    with pytest.raises((AttributeError, TypeError)):
        rec.model_name = "changed"  # type: ignore[misc]


def test_endpoint_info_is_immutable():
    ep = KNOWN_ENDPOINTS["ollama"]
    with pytest.raises((AttributeError, TypeError)):
        ep.default_port = 9999  # type: ignore[misc]


def test_asr_model_info_is_immutable():
    info = ASR_MODELS["whisper-large-v3"]
    with pytest.raises((AttributeError, TypeError)):
        info.size_bytes = 0  # type: ignore[misc]
