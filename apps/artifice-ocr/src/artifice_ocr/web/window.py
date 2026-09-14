# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native window re-exports from shared-ui for ArtificeOCR."""

from __future__ import annotations

from shared_ui.window import (
    WindowApi,
    WindowError,
    WindowResult,
    _unblock_frozen_bundle,
    open_native_window,
)

__all__ = [
    "WindowApi",
    "WindowError",
    "WindowResult",
    "_unblock_frozen_bundle",
    "open_native_window",
]
