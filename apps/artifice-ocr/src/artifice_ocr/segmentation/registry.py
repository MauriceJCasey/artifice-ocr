# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Named segmentation-provider construction.

Mirrors ``artifice_ocr._backend.get_client``'s style — named implementations
and a flat factory, with no decorator registry and no plugin discovery. Each
provider declares its own optional dependency and imports it lazily inside
``segment``/``is_available``, so importing this module never forces one
(the plan's compatibility invariant #3).
"""

from __future__ import annotations

from .diff_residual import DiffResidualProvider
from .doclayout_yolo import DocLayoutYoloProvider
from .kraken import KrakenProvider
from .model import SegmentationProvider
from .passthrough import PassthroughProvider

_PROVIDER_BY_NAME: dict[str, type[SegmentationProvider]] = {
    PassthroughProvider.name: PassthroughProvider,
    DocLayoutYoloProvider.name: DocLayoutYoloProvider,
    KrakenProvider.name: KrakenProvider,
    DiffResidualProvider.name: DiffResidualProvider,
}


def get_provider(name: str) -> SegmentationProvider:
    """Return a provider instance by its stable ``name`` (default passthrough).

    Raises ``KeyError`` for an unknown name so a caller can distinguish a
    misconfigured setting from an unavailable provider.
    """
    key = (name or PassthroughProvider.name).lower()
    provider_cls = _PROVIDER_BY_NAME.get(key)
    if provider_cls is None:
        raise KeyError(f"Unknown segmentation provider: {name!r}")
    return provider_cls()


def list_available() -> list[SegmentationProvider]:
    """The providers whose ``is_available()`` is true right now.

    Never raises and never imports an optional dependency: an unavailable
    provider is simply omitted (the plan's compatibility invariant #4).
    """
    return [cls() for cls in _PROVIDER_BY_NAME.values() if cls().is_available()]
