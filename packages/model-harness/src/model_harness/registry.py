# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Single source of truth for model and endpoint data.

This module is pure data and accessors. It performs no I/O of any kind — no
network, no filesystem, no environment reads, no imports of ``httpx``,
``requests``, or ``os.environ``. Probing whether a server is actually reachable
belongs to :mod:`model_harness.discovery`, which is a separate step.

Data here drives consent dialogs (size figures shown to a user *before* they
approve a multi-gigabyte download) and model-selection guidance, so every
``size_bytes`` value is sourced from the Hugging Face API and annotated with
its provenance.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from model_harness.contract import Provider

# ── Enums ────────────────────────────────────────────────────────────────────


class HardwareTier(Enum):
    """Hardware capability tiers for model recommendations.

    Ordered roughly by VRAM headroom:
    - ``LAPTOP``: integrated GPU or entry-level dGPU (≈ 4–8 GB VRAM).
    - ``DESKTOP``: discrete GPU with 12–24 GB VRAM.
    - ``MAC_UNIFIED``: Apple Silicon with unified memory (Metal acceleration).
    """

    LAPTOP = "laptop"
    DESKTOP = "desktop"
    MAC_UNIFIED = "mac_unified"


# ── Endpoint metadata ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EndpointInfo:
    """Metadata for a known local model server."""

    display_name: str
    """Human-readable name, e.g. ``"Ollama"``."""

    default_url: str
    """Default base URL including the ``/v1`` path, e.g. ``"http://localhost:11434/v1"``."""

    provider: Provider
    """The :data:`Provider` literal this endpoint maps to."""

    default_port: int
    """Default TCP port the server listens on."""


KNOWN_ENDPOINTS: Mapping[str, EndpointInfo] = {
    "ollama": EndpointInfo(
        display_name="Ollama",
        default_url="http://localhost:11434/v1",
        provider="ollama",
        default_port=11434,
    ),
    "lm-studio": EndpointInfo(
        display_name="LM Studio",
        default_url="http://localhost:1234/v1",
        provider="lm-studio",
        default_port=1234,
    ),
    "vllm": EndpointInfo(
        display_name="vLLM / LocalAI",
        default_url="http://localhost:8080/v1",
        provider="generic-api",
        default_port=8080,
    ),
}
"""Known local model servers.

vLLM and LocalAI share port 8080 and map to ``"generic-api"`` — there is no
dedicated :data:`Provider` literal for either, and the call contract must not
gain new members as a side effect of this registry.
"""


# ── ASR model metadata ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AsrModelInfo:
    """Download metadata for an ASR model on Hugging Face.

    These figures are shown in consent dialogs *before* a download begins, so
    ``size_bytes`` must be a real sourced value, not an estimate.
    """

    hf_repo: str
    """Hugging Face repository, e.g. ``"openai/whisper-large-v3"``."""

    size_bytes: int
    """Download size in bytes.  Sourced from the Hugging Face API — see comments
    on each :data:`ASR_MODELS` entry."""

    requires_hf_token: bool
    """``True`` if the model is gated and requires a Hugging Face access token."""

    description: str
    """One-line description for use in UI labels."""

    depends_on: tuple[str, ...] = ()
    """Keys of other :data:`ASR_MODELS` entries that must also be downloaded.

    Example: ``"pyannote-speaker-diarization"`` depends on
    ``"pyannote-embedding"`` — the pipeline pulls both the segmentation weights
    (whose size is recorded in this entry's ``size_bytes``) and the speaker
    embedding model (whose size lives on the ``"pyannote-embedding"`` entry).

    The download service MUST sum a model's own ``size_bytes`` with the
    ``size_bytes`` of every entry reachable through ``depends_on`` before
    presenting a total to the user.  Reporting only one entry's size understates
    the real download.
    """


# Sizes sourced 2026-08-04 from the Hugging Face API
# (``https://huggingface.co/api/models/<repo>?blobs=true&expand[]=siblings``):
#
#   whisper-large-v3        model.safetensors          sibling.size = 3 087 130 976
#   parakeet-tdt-1.1b       parakeet-tdt-1.1b.nemo     sibling.size = 4 283 136 000
#   segmentation-3.0        pytorch_model.bin           sibling.size =     5 905 440
#   embedding               pytorch_model.bin           sibling.size =    96 383 626
#
# The speaker-diarization-3.0 pipeline repo is a config-only wrapper; its
# actual weights live in ``pyannote/segmentation-3.0``.  The size listed here
# reflects the segmentation model the pipeline downloads at first use.

ASR_MODELS: Mapping[str, AsrModelInfo] = {
    "whisper-large-v3": AsrModelInfo(
        hf_repo="openai/whisper-large-v3",
        size_bytes=3_087_130_976,
        requires_hf_token=False,
        description="OpenAI Whisper large-v3 — multilingual ASR and speech translation",
    ),
    "parakeet-tdt-1.1b": AsrModelInfo(
        hf_repo="nvidia/parakeet-tdt-1.1b",
        size_bytes=4_283_136_000,
        requires_hf_token=False,
        description="NVIDIA Parakeet TDT 1.1B — English ASR (NeMo)",
    ),
    "pyannote-speaker-diarization": AsrModelInfo(
        hf_repo="pyannote/speaker-diarization-3.0",
        # The pipeline repo is a config file; the size reflects the
        # ``pyannote/segmentation-3.0`` weights it depends on.
        # The pipeline also pulls ``pyannote/embedding`` for speaker
        # embeddings — see ``depends_on``.
        size_bytes=5_905_440,
        requires_hf_token=True,
        depends_on=("pyannote-embedding",),
        description="pyannote speaker diarization 3.0 — identifies who spoke when",
    ),
    "pyannote-embedding": AsrModelInfo(
        hf_repo="pyannote/embedding",
        size_bytes=96_383_626,
        requires_hf_token=True,
        description="pyannote speaker embedding — speaker verification and identification",
    ),
}
"""ASR and diarization models available for :mod:`artifice_transcribe`.

Parakeet is data only — nothing in this repository implements it.  Its presence
here is forward-looking.  Do not imply a working code path.
"""


# ── Open-science provenance badges ────────────────────────────────────────────


PERMITTED_BADGES: frozenset[str] = frozenset(
    {
        "Strict Open Data",
        "Transparent Training",
        "Allen AI Open Science",
        "Open Science Lab",
        "Auditable Dataset",
        "Open Source Training Code",
    }
)
"""Permitted provenance labels for :attr:`ModelRecommendation.ethos_badges`.

The vocabulary is closed — badges are free strings in the dataclass but
*validation* (in tests) must reject any string not in this set.  That keeps
the UI renderable as a small set of chips and prevents badge drift across
entries.

**Badges describe provenance, not capability.**  ``"Strict Open Data"``,
``"Auditable Dataset"`` and ``"Open Source Training Code"`` are claims a user
can verify by reading the model's publication.  A capability claim
(e.g. translation fluency, benchmark scores, language coverage) belongs in
:attr:`ModelRecommendation.notes` — prose makes no verifiable claim.  Apply
this test to every badge proposed in future: could a user verify it from the
model paper alone?
"""

# ── Model role ────────────────────────────────────────────────────────────────

BadgeRole = Literal["vision", "chat", "translation", "embedding"]
"""Permitted values for :attr:`ModelRecommendation.role`.

- ``"vision"``  — multimodal vision-language (e.g. OCR, image-analysis)
- ``"chat"``    — general-purpose text chat / instruction-following
- ``"translation"``  — multilingual translation
- ``"embedding"``    — text vector embeddings for search, similarity, retrieval
"""


# ── Model recommendations ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ModelRecommendation:
    """A recommended model for a given app and hardware tier."""

    model_name: str
    """The model identifier as the provider expects it, e.g. ``"llava:7b"``."""

    provider: Provider
    """Which :data:`Provider` this model is served by."""

    vision: bool
    """``True`` if the model supports image inputs (vision-capable)."""

    min_vram_gb: float | None = None
    """Approximate minimum VRAM in GB. ``None`` when unknown.

    On unified-memory Macs (:data:`HardwareTier.MAC_UNIFIED`) this field
    means minimum *shared* memory rather than dedicated VRAM — the GPU and
    CPU draw from the same pool.
    """

    ethos_badges: list[str] = field(default_factory=list)
    """Open-science provenance labels, e.g. ``["Strict Open Data"]``.

    Every string must be a member of :data:`PERMITTED_BADGES`.
    """

    role: BadgeRole = "chat"
    """What the model does — one of ``"vision"``, ``"chat"``, ``"translation"``,
    or ``"embedding"``.
    """

    notes: str = ""
    """A one-sentence plain-language description a historian would understand."""


# App → HardwareTier → recommendations.
# These are guidance, not requirements — the suite is BYOM, and any model the
# provider serves is fair game.  Every name listed here is a model known to
# exist on Ollama as of 2026-08-04.
_RECOMMENDATIONS: Mapping[str, Mapping[HardwareTier, Sequence[ModelRecommendation]]] = {
    "artifice-ocr": {
        HardwareTier.LAPTOP: [
            ModelRecommendation(
                model_name="richardyoung/olmocr2:7b-q8",
                provider="ollama",
                vision=True,
                min_vram_gb=12.0,
                role="vision",
                ethos_badges=["Strict Open Data", "Transparent Training", "Allen AI Open Science"],
                notes=(
                    "Allen AI's olmOCR-2: open-research document OCR. Needs ≈12 GB VRAM for full "
                    "GPU offload; runs on 8 GB GPUs with CPU fallback (reduced throughput)."
                ),
            ),
            ModelRecommendation(
                model_name="allenai/olmocr-2-7b",
                provider="lm-studio",
                vision=True,
                min_vram_gb=12.0,
                role="vision",
                ethos_badges=["Strict Open Data", "Transparent Training", "Allen AI Open Science"],
                notes=(
                    "Allen AI's native olmOCR-2 model served through LM Studio; on an 8 GB "
                    "GPU, LM Studio may need CPU fallback or a lower offload setting."
                ),
            ),
            ModelRecommendation(
                model_name="aya-expanse:8b",
                provider="ollama",
                vision=False,
                min_vram_gb=6.0,
                role="translation",
                ethos_badges=["Open Science Lab"],
                notes=(
                    "Cohere For AI multilingual model with strong performance across 23 languages."
                ),
            ),
        ],
        HardwareTier.DESKTOP: [
            ModelRecommendation(
                model_name="richardyoung/olmocr2:7b-q8",
                provider="ollama",
                vision=True,
                min_vram_gb=12.0,
                role="vision",
                ethos_badges=["Strict Open Data", "Transparent Training", "Allen AI Open Science"],
                notes=(
                    "Allen AI's olmOCR-2: open-research document OCR, runs locally on a single GPU."
                ),
            ),
            ModelRecommendation(
                model_name="allenai/olmocr-2-7b",
                provider="lm-studio",
                vision=True,
                min_vram_gb=12.0,
                role="vision",
                ethos_badges=["Strict Open Data", "Transparent Training", "Allen AI Open Science"],
                notes="Allen AI's native olmOCR-2 model served through LM Studio.",
            ),
            ModelRecommendation(
                model_name="aya-expanse:32b",
                provider="ollama",
                vision=False,
                min_vram_gb=20.0,
                role="translation",
                ethos_badges=["Open Science Lab"],
                notes="Cohere For AI's 32B-parameter multilingual translation model.",
            ),
        ],
        HardwareTier.MAC_UNIFIED: [
            ModelRecommendation(
                model_name="richardyoung/olmocr2:7b-q8",
                provider="ollama",
                vision=True,
                min_vram_gb=12.0,
                role="vision",
                ethos_badges=["Strict Open Data", "Transparent Training", "Allen AI Open Science"],
                notes=(
                    "Allen AI's olmOCR-2: open-research document OCR, runs locally on a single GPU."
                ),
            ),
            ModelRecommendation(
                model_name="allenai/olmocr-2-7b",
                provider="lm-studio",
                vision=True,
                min_vram_gb=12.0,
                role="vision",
                ethos_badges=["Strict Open Data", "Transparent Training", "Allen AI Open Science"],
                notes="Allen AI's native olmOCR-2 model served through LM Studio.",
            ),
            ModelRecommendation(
                model_name="aya-expanse:8b",
                provider="ollama",
                vision=False,
                min_vram_gb=6.0,
                role="translation",
                ethos_badges=["Open Science Lab"],
                notes=(
                    "Cohere For AI multilingual model with strong performance across 23 languages."
                ),
            ),
        ],
    },
    "artifice-graph": {
        HardwareTier.LAPTOP: [
            ModelRecommendation(
                model_name="llama3.2:3b",
                provider="ollama",
                vision=False,
                min_vram_gb=4.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="qwen2.5:7b",
                provider="ollama",
                vision=False,
                min_vram_gb=8.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="nomic-embed-text",
                provider="ollama",
                vision=False,
                min_vram_gb=1.0,
                role="embedding",
                ethos_badges=["Strict Open Data", "Auditable Dataset", "Open Source Training Code"],
                notes=(
                    "Fully transparent, open-data vector model for semantic search, document "
                    "matching, and knowledge graph indexing."
                ),
            ),
        ],
        HardwareTier.DESKTOP: [
            ModelRecommendation(
                model_name="qwen2.5:32b",
                provider="ollama",
                vision=False,
                min_vram_gb=24.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="llama3.1:8b",
                provider="ollama",
                vision=False,
                min_vram_gb=8.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="nomic-embed-text",
                provider="ollama",
                vision=False,
                min_vram_gb=1.0,
                role="embedding",
                ethos_badges=["Strict Open Data", "Auditable Dataset", "Open Source Training Code"],
                notes=(
                    "Fully transparent, open-data vector model for semantic search, document "
                    "matching, and knowledge graph indexing."
                ),
            ),
        ],
        HardwareTier.MAC_UNIFIED: [
            ModelRecommendation(
                model_name="qwen2.5:14b",
                provider="ollama",
                vision=False,
                min_vram_gb=12.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="llama3.1:8b",
                provider="ollama",
                vision=False,
                min_vram_gb=8.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="nomic-embed-text",
                provider="ollama",
                vision=False,
                min_vram_gb=1.0,
                role="embedding",
                ethos_badges=["Strict Open Data", "Auditable Dataset", "Open Source Training Code"],
                notes=(
                    "Fully transparent, open-data vector model for semantic search, document "
                    "matching, and knowledge graph indexing."
                ),
            ),
        ],
    },
    "artifice-draft": {
        HardwareTier.LAPTOP: [
            ModelRecommendation(
                model_name="llama3.2:3b",
                provider="ollama",
                vision=False,
                min_vram_gb=4.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="qwen2.5:7b",
                provider="ollama",
                vision=False,
                min_vram_gb=8.0,
                role="chat",
            ),
        ],
        HardwareTier.DESKTOP: [
            ModelRecommendation(
                model_name="qwen2.5:32b",
                provider="ollama",
                vision=False,
                min_vram_gb=24.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="llama3.1:8b",
                provider="ollama",
                vision=False,
                min_vram_gb=8.0,
                role="chat",
            ),
        ],
        HardwareTier.MAC_UNIFIED: [
            ModelRecommendation(
                model_name="qwen2.5:14b",
                provider="ollama",
                vision=False,
                min_vram_gb=12.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="llama3.1:8b",
                provider="ollama",
                vision=False,
                min_vram_gb=8.0,
                role="chat",
            ),
        ],
    },
    "artifice-transcribe": {
        HardwareTier.LAPTOP: [
            ModelRecommendation(
                model_name="llama3.2:3b",
                provider="ollama",
                vision=False,
                min_vram_gb=4.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="phi4-mini:3.8b",
                provider="ollama",
                vision=False,
                min_vram_gb=4.0,
                role="chat",
            ),
        ],
        HardwareTier.DESKTOP: [
            ModelRecommendation(
                model_name="qwen2.5:32b",
                provider="ollama",
                vision=False,
                min_vram_gb=24.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="llama3.1:8b",
                provider="ollama",
                vision=False,
                min_vram_gb=8.0,
                role="chat",
            ),
        ],
        HardwareTier.MAC_UNIFIED: [
            ModelRecommendation(
                model_name="qwen2.5:14b",
                provider="ollama",
                vision=False,
                min_vram_gb=12.0,
                role="chat",
            ),
            ModelRecommendation(
                model_name="llama3.1:8b",
                provider="ollama",
                vision=False,
                min_vram_gb=8.0,
                role="chat",
            ),
        ],
    },
}
"""
Model recommendations by app and hardware tier.

``artifice-ocr`` receives vision models (for OCR) and translation models
(for its optional translation prompt).  ``artifice-graph`` receives
text-chat models (for entity extraction) and an embedding model (for vector
search).  ``artifice-draft`` and ``artifice-transcribe`` receive text-only
chat models.  ``artifice-transcribe`` additionally uses :data:`ASR_MODELS`
for its transcription engines themselves — these recommendations cover only
its optional post-transcription inference endpoint (summarize / cleanup).
"""


# ── Accessors ────────────────────────────────────────────────────────────────


def get_endpoint(key: str) -> EndpointInfo:
    """Return the :class:`EndpointInfo` for a known endpoint key.

    Raises:
        KeyError: if *key* is not in :data:`KNOWN_ENDPOINTS`.
    """
    return KNOWN_ENDPOINTS[key]


def get_asr_model(key: str) -> AsrModelInfo:
    """Return the :class:`AsrModelInfo` for an ASR model key.

    Raises:
        KeyError: if *key* is not in :data:`ASR_MODELS`.
    """
    return ASR_MODELS[key]


def recommendations_for_app(app: str, tier: HardwareTier) -> Sequence[ModelRecommendation]:
    """Return recommended models for *app* on the given *tier*.

    Args:
        app: One of ``"artifice-ocr"``, ``"artifice-graph"``,
            ``"artifice-draft"``, ``"artifice-transcribe"``.
            ``"artifice-transcribe"`` receives **text** recommendations
            for its optional post-transcription inference endpoint
            (summarize / cleanup); it uses :data:`ASR_MODELS` separately
            for the transcription engines themselves.
        tier: The hardware capability tier.

    Returns:
        A (possibly empty) sequence of :class:`ModelRecommendation` instances
        ordered from most- to least-preferred.

    Raises:
        KeyError: if *app* has no recommendations registered, or if *tier*
            has no entries for the given app.
    """
    return _RECOMMENDATIONS[app][tier]


# ── shared configured rule ──────────────────────────────────────────────────


# API keys that do not count as user-configured — empty strings and
# ``"not-needed"``, which transcribe writes as its default placeholder.
_PLACEHOLDER_API_KEYS: frozenset[str] = frozenset({"", "not-needed"})


def is_configured(
    base_url: str,
    api_key: str = "",
    *,
    defaults: Iterable[str] = (),
    model: str = "",
) -> bool:
    """Return ``True`` when the user has departed from the built-in defaults.

    A connection is considered configured when any of:

    * *api_key* is non-empty and is not a known placeholder
      (``""``, ``"not-needed"``), **or**
    * *base_url* is non-empty and is not one of the values the app ships
      as its default, **or**
    * *model* is non-empty — the user has explicitly chosen a model.

    **This reports intent, not readiness.** It performs no I/O: it cannot say
    whether the endpoint answers, nor whether the chosen model is installed.
    Callers must not present the result as "connected" or "ready" — a
    configured endpoint that is currently down would make that a lie, and
    recovering from exactly that is what the BYOM screen exists for.

    Args:
        base_url: The URL the app will connect to (may be empty).
        api_key: The API key (may be empty or a placeholder).
        defaults: The set of base URLs the app ships as out-of-the-box
            defaults. Callers should pass the app-specific shipped base
            URL values to compare against.
        model: The chosen model name, if any. Empty means the app will
            resolve one per run from what the endpoint serves, which is the
            normal state and is *not* a defect — see
            :mod:`model_harness.resolution`.

    Returns:
        ``True`` if the user has intentionally changed at least one
        configuration value away from the shipped default.
    """
    if api_key and api_key not in _PLACEHOLDER_API_KEYS:
        return True
    if base_url and base_url not in defaults:
        return True
    if model:  # noqa: SIM103
        return True
    return False


__all__ = [
    "ASR_MODELS",
    "AsrModelInfo",
    "BadgeRole",
    "EndpointInfo",
    "HardwareTier",
    "KNOWN_ENDPOINTS",
    "ModelRecommendation",
    "PERMITTED_BADGES",
    "get_asr_model",
    "get_endpoint",
    "is_configured",
    "recommendations_for_app",
]
