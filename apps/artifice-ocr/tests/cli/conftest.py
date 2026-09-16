# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Autouse fixture resetting config and model-resolution state before each test."""

import pytest


@pytest.fixture(autouse=True)
def _reset_config_and_resolution():
    """Reset config and the per-run resolution cache before/after each test.

    Model/backend defaults are now empty/auto, so a stale override from one
    test (or a leftover resolution cache) would leak into the next.
    """
    from artifice_ocr import _resolution, config

    config.reset()
    _resolution.reset()
    yield
    config.reset()
    _resolution.reset()
