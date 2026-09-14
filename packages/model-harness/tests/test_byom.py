# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for :mod:`model_harness.byom`.

These helpers used to be duplicated, byte-for-byte, in
``apps/artifice-ocr/.../web/routers/byom.py`` and
``apps/artifice-transcribe/.../web/routers/byom.py``. Both apps now import
them from here; these tests cover the shared implementation directly.
"""

from __future__ import annotations

from model_harness.byom import TestRequest as ByomTestRequest
from model_harness.byom import byom_recommendations, name_for_probe
from model_harness.discovery import ProbeResult
from model_harness.registry import KNOWN_ENDPOINTS

# ---------------------------------------------------------------------------
# name_for_probe
# ---------------------------------------------------------------------------


def test_name_for_probe_matches_known_endpoint():
    """A probe result whose provider matches a registry entry returns that
    entry's display name, not the raw provider literal."""
    ollama_info = KNOWN_ENDPOINTS["ollama"]
    result = ProbeResult(url="http://localhost:11434/v1", reachable=True, provider="ollama")
    assert name_for_probe(result) == ollama_info.display_name


def test_name_for_probe_falls_back_to_provider_literal():
    """A provider with no matching registry entry falls back to the provider
    string itself — still display-safe, just less friendly."""
    result = ProbeResult(url="http://example.com/v1", reachable=True, provider="generic-api")
    # generic-api is a valid Provider but is not necessarily every
    # KNOWN_ENDPOINTS entry's provider; assert against the actual fallback
    # behaviour rather than assuming no registry entry uses it.
    expected = next(
        (info.display_name for info in KNOWN_ENDPOINTS.values() if info.provider == "generic-api"),
        "generic-api",
    )
    assert name_for_probe(result) == expected


def test_name_for_probe_falls_back_to_url_when_no_provider():
    """When a probe result carries no provider at all, fall back to the URL
    rather than stringifying ``None``."""
    result = ProbeResult(url="http://localhost:9999/v1", reachable=False, provider=None)
    assert name_for_probe(result) == "http://localhost:9999/v1"


# ---------------------------------------------------------------------------
# byom_recommendations
# ---------------------------------------------------------------------------


def test_byom_recommendations_returns_all_three_tiers():
    """The serialised dict always carries exactly the three hardware-tier
    keys the BYOM UI expects, regardless of app."""
    result = byom_recommendations("artifice-ocr")
    assert set(result.keys()) == {"laptop", "desktop", "mac_unified"}


def test_byom_recommendations_serialises_real_field_names():
    """Each entry uses the real ``ModelRecommendation`` field names
    (``model_name``, ``provider``, ``vision``, ``min_vram_gb``, ...), not the
    older ``{name, why, size_bytes}`` shape the byom.js contract-mismatch
    comment warns about."""
    result = byom_recommendations("artifice-ocr")
    laptop = result["laptop"]
    assert laptop, "artifice-ocr LAPTOP tier should have at least one recommendation"
    entry = laptop[0]
    assert set(entry.keys()) == {
        "model_name",
        "provider",
        "vision",
        "min_vram_gb",
        "ethos_badges",
        "role",
        "notes",
    }
    assert isinstance(entry["model_name"], str) and entry["model_name"]
    assert isinstance(entry["ethos_badges"], list)


def test_byom_recommendations_unknown_app_returns_empty_tiers_not_raises():
    """An app key absent from the registry must degrade to empty lists per
    tier rather than propagating KeyError — this is the guard the module's
    docstring documents as general, not app-specific."""
    result = byom_recommendations("nonexistent-app")
    assert result == {"laptop": [], "desktop": [], "mac_unified": []}


# ---------------------------------------------------------------------------
# TestRequest
# ---------------------------------------------------------------------------


def test_test_request_defaults_api_key_to_empty_string():
    req = ByomTestRequest(url="http://localhost:11434/v1")
    assert req.api_key == ""


def test_test_request_accepts_explicit_api_key():
    req = ByomTestRequest(url="https://api.openai.com/v1", api_key="sk-real")
    assert req.url == "https://api.openai.com/v1"
    assert req.api_key == "sk-real"
