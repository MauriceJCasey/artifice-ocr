# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Library-level path validation.

Thin wrapper around ``shared_ui.path_validation`` that hardcodes the OCR-
specific allowed-roots environment variable so the public API stays unchanged
for existing callers.
"""

from __future__ import annotations

from shared_ui.path_validation import validate_path as _shared_validate_path

from . import config


def validate_path(raw: str, field_name: str) -> str:
    """Return *raw* as a normalised path string after checking it resides
    within an allowed root directory.  Raises ``ValueError`` on rejection.

    Delegates to ``shared_ui.path_validation.validate_path`` with
    ``ARTIFICE_OCR_ALLOWED_ROOTS`` as the env-var name and the user-approved
    folder list from config (``approved_folders``) as the extra roots. The
    env var keeps working unchanged; approved folders are an additional,
    user-granted set of roots.
    """
    return _shared_validate_path(
        raw,
        field_name,
        allowed_roots_env_var="ARTIFICE_OCR_ALLOWED_ROOTS",
        extra_roots=config.get("approved_folders", []),
    )
