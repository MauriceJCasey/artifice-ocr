# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Segmentation capability routes.

A thin web wrapper over ``artifice_ocr.segmentation.registry``. The registry is
the single place that knows what providers exist and which are available on
this machine; this router only serialises that listing so the browser can
populate its provider selector without ever hard-coding a provider name (the
plan's compatibility invariant #4).
"""

from fastapi import APIRouter

from artifice_ocr.segmentation.registry import list_available

router = APIRouter(tags=["segmentation"])


@router.get("/api/segmentation/capabilities")
def segmentation_capabilities() -> dict:
    """List the segmentation providers available on this machine.

    Mirrors ``list_available()``'s own contract: never raises and never imports
    an optional dependency, so a clean install (no segmentation extras) returns
    only the always-available ``passthrough`` provider rather than an error.
    """
    return {
        "providers": [
            {"name": provider.name, "requirements": provider.requirements}
            for provider in list_available()
        ]
    }
