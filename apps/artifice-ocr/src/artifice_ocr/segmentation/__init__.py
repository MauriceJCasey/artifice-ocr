# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Provider-agnostic page segmentation.

The pipeline imports this package lazily, only when ``segmentation_enabled``
is true, so the disabled path never loads a segmentation provider (or any of
its optional dependencies — the plan's compatibility invariant #3).
"""
