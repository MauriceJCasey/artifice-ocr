# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared BYOM onboarding helpers used by every app's ``web/routers/byom.py``.

``artifice-ocr`` and ``artifice-transcribe`` each carried byte-identical (or
near-identical) copies of these helpers. They belong here rather than in
either app because every symbol they touch — :class:`~model_harness.discovery.ProbeResult`,
:data:`~model_harness.registry.KNOWN_ENDPOINTS`, :class:`~model_harness.registry.HardwareTier`,
and :func:`~model_harness.registry.recommendations_for_app` — is already owned
by this package, and both apps already depend on it.

The ``try/except KeyError`` guard in :func:`byom_recommendations` is not
transcribe-specific: it was written when the registry deliberately omitted
some apps' recommendations, and it remains correct defensive code for any
app key that might be absent from :data:`~model_harness.registry` in the
future — not a special case for any one app.
"""

from __future__ import annotations

from pydantic import BaseModel

from model_harness.discovery import ProbeResult
from model_harness.registry import KNOWN_ENDPOINTS, HardwareTier, recommendations_for_app

__all__ = [
    "TestRequest",
    "byom_recommendations",
    "name_for_probe",
]


# ── Display-name mapping ─────────────────────────────────────────────────────


def name_for_probe(r: ProbeResult) -> str:
    """Return a human-readable name for a probe result, derived from the
    registry when the provider matches a known endpoint."""
    for info in KNOWN_ENDPOINTS.values():
        if info.provider == r.provider:
            return info.display_name
    # Fall back to the provider literal — it is a display-safe string.
    return str(r.provider) if r.provider else r.url


# ── Recommendations helper ──────────────────────────────────────────────────


def byom_recommendations(app_key: str) -> dict:
    """Serialise :func:`~model_harness.registry.recommendations_for_app` for
    all three hardware tiers.

    Uses the real ``ModelRecommendation`` field names (``model_name``,
    ``provider``, ``vision``, ``min_vram_gb``) — NOT ``{name, why, size_bytes}``.
    See the KNOWN CONTRACT MISMATCH comment atop ``byom.js``.
    """
    tier_keys = {
        "laptop": HardwareTier.LAPTOP,
        "desktop": HardwareTier.DESKTOP,
        "mac_unified": HardwareTier.MAC_UNIFIED,
    }
    result: dict[str, list[dict]] = {}
    for key, tier in tier_keys.items():
        try:
            recs = recommendations_for_app(app_key, tier)
        except KeyError:
            recs = []
        result[key] = [
            {
                "model_name": r.model_name,
                "provider": r.provider,
                "vision": r.vision,
                "min_vram_gb": r.min_vram_gb,
                "ethos_badges": list(r.ethos_badges),
                "role": r.role,
                "notes": r.notes,
            }
            for r in recs
        ]
    return result


# ── Request model ────────────────────────────────────────────────────────────


class TestRequest(BaseModel):
    url: str
    api_key: str = ""
