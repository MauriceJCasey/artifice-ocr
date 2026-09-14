# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "check-ruff-baseline.py"
SPEC = spec_from_file_location("check_ruff_baseline", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_counts_normalises_repo_absolute_paths() -> None:
    repo_file = MODULE.REPO_ROOT / "apps" / "artifice-draft" / "src" / "artifice_draft" / "cli.py"

    counts = MODULE._counts(
        [
            {"filename": str(repo_file), "code": "E501"},
            {"filename": str(repo_file), "code": "E501"},
        ]
    )

    assert counts == {"apps/artifice-draft/src/artifice_draft/cli.py|E501": 2}


def test_counts_preserves_relative_paths() -> None:
    counts = MODULE._counts(
        [{"filename": "packages/shared-ui/tests/test_tokens.py", "code": "F401"}]
    )

    assert counts == {"packages/shared-ui/tests/test_tokens.py|F401": 1}
