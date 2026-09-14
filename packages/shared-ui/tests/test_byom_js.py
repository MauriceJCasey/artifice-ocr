# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for ``_rolesFromState`` in the shared ``byom.js``.

``byom.js`` is a browser IIFE with no test entry point of its own, so these
tests drive the real file through Node (present in the dev environment) with a
minimal ``window``/``document`` stub and assert on the returned role list.

The behaviour under test: the picker is driven by ``state.roles`` (published
server-side from each app's ``_ROLE_SETTING``), not by the tier-keyed
``recommendations`` dict, which is only a fallback for servers that have not
been updated to publish ``roles``. OCR's recommendations carry ``vision`` and
``translation`` but no ``chat``, so a picker built from them would make the
cleanup role unreachable — the exact defect this change set removes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

BYOM_JS = Path(__file__).resolve().parent.parent / "shared_ui" / "assets" / "byom.js"

# Stub just enough of the browser environment for the IIFE to load and for
# `create({appName})` to construct an instance without touching fetch/DOM.
# `_rolesFromState` itself only reads `this.state`.
_HARNESS = """
global.window = {
  matchMedia: function () { return { matches: false }; },
  localStorage: {
    getItem: function () { return null; },
    setItem: function () {}
  },
  navigator: {},
  fetch: function () {
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve({}); }
    });
  }
};
global.document = { title: "test" };
require(require("path").resolve(process.argv[1]));
var byom = window.ArtificeByom.create({ appName: "Test App" });
byom.state = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(byom._rolesFromState()));
"""

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise byom.js"
)


def _roles_for(fixture: dict) -> list[str]:
    """Load byom.js in Node, set ``state`` to *fixture*, return the roles."""
    proc = subprocess.run(
        ["node", "-e", _HARNESS, str(BYOM_JS), json.dumps(fixture)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_roles_field_is_authoritative_for_ocr():
    """A realistic tier-keyed payload with a `roles` field returns all three
    OCR roles — including `chat`, which the recommendations dict omits."""
    fixture = {
        "app": "artifice-ocr",
        "roles": ["vision", "chat", "translation"],
        "recommendations": {
            "laptop": [{"role": "vision"}],
            "desktop": [{"role": "vision"}],
            "mac_unified": [{"role": "translation"}],
        },
    }
    assert _roles_for(fixture) == ["vision", "chat", "translation"]


def test_no_roles_field_defaults_to_chat():
    """A payload with no `roles` field (and no recommendations) returns the
    single chat role — correct for draft and transcribe."""
    assert _roles_for({"app": "artifice-draft"}) == ["chat"]


def test_tier_keyed_recommendations_fallback():
    """Without `roles`, the tier-keyed recommendations dict is walked correctly
    (the old code read `recs.models`, which no server sends)."""
    fixture = {
        "app": "artifice-graph",
        "recommendations": {
            "laptop": [{"role": "chat"}],
            "desktop": [{"role": "chat"}, {"role": "embedding"}],
            "mac_unified": [{"role": "embedding"}],
        },
    }
    assert _roles_for(fixture) == ["chat", "embedding"]


def test_roles_deduped_and_order_preserved():
    """Duplicates collapse and order is stable."""
    fixture = {
        "app": "artifice-ocr",
        "roles": ["vision", "chat", "vision", "translation", "chat"],
    }
    assert _roles_for(fixture) == ["vision", "chat", "translation"]
