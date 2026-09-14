# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Route-level tests for the three native file-dialog routes.

These pin the shared file-dialog contract: every route returns
``{"state": ..., "paths": [...], "reason": ...}``, with ``paths`` populated
only for ``selected`` and ``reason`` only for ``unavailable``.  The
``shared_ui.filedialog`` async entry points are monkeypatched on the
``server`` module, so no test opens a real dialog.
"""

from pathlib import Path

import pytest
from artifice_ocr.web import server
from fastapi.testclient import TestClient
from shared_ui.filedialog import DialogResult, DialogState


def _native(paths):
    """Return *paths* as the wire format renders them on this platform.

    ``DialogResult.as_dict`` stringifies ``Path`` objects, so the separator is
    whatever the running OS uses: ``"/data/scan.png"`` comes back as
    ``"\\data\\scan.png"`` on Windows.  Asserting against a POSIX literal
    therefore passes on Linux and macOS and fails on Windows — which is
    exactly what it did.  The contract is "the paths the backend gave us,
    stringified", not "POSIX separators".
    """
    return [str(Path(p)) for p in paths]


@pytest.fixture
def client():
    """A bare TestClient — the dialog routes read no queue/config state."""
    with TestClient(server.app) as c:
        yield c


def _install(monkeypatch, name, state, paths=(), reason=""):
    """Replace one shared_ui.filedialog async entry point on ``server``.

    Returns a dict capturing the keyword arguments the route forwarded, so a
    test can assert the FileType filters and default_name the handler built.
    """
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return DialogResult(
            state=state,
            paths=tuple(Path(p) for p in paths),
            reason=reason,
        )

    monkeypatch.setattr(server, name, fake)
    return captured


# --------------------------------------------------------------------------- #
# pick-file — the shared three-state shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "state,paths,reason,expected",
    [
        (
            DialogState.SELECTED,
            ("/data/scan.png",),
            "",
            {"state": "selected", "paths": _native(["/data/scan.png"]), "reason": ""},
        ),
        (
            DialogState.CANCELLED,
            (),
            "",
            {"state": "cancelled", "paths": [], "reason": ""},
        ),
        (
            DialogState.UNAVAILABLE,
            (),
            "no native file dialog is available",
            {"state": "unavailable", "paths": [], "reason": "no native file dialog is available"},
        ),
    ],
)
def test_pick_file_returns_three_state_shape(client, monkeypatch, state, paths, reason, expected):
    _install(monkeypatch, "pick_files_async", state, paths, reason)
    res = client.post("/api/native/pick-file")
    assert res.status_code == 200
    assert res.json() == expected


def test_pick_file_allows_multiple_selection(client, monkeypatch):
    """C4 — several files may be selected and all are returned."""
    _install(
        monkeypatch,
        "pick_files_async",
        DialogState.SELECTED,
        paths=("/data/one.png", "/data/two.png"),
    )
    res = client.post("/api/native/pick-file")
    assert res.json()["paths"] == _native(["/data/one.png", "/data/two.png"])


def test_pick_file_json_preset_builds_valid_file_types(client, monkeypatch):
    """C2 — the ``json`` preset builds FileTypes whose descriptions pass the
    [word chars + spaces] rule, so the handler does not raise."""
    captured = _install(monkeypatch, "pick_files_async", DialogState.CANCELLED)
    client.post("/api/native/pick-file", json={"preset": "json"})
    assert [ft.description for ft in captured["file_types"]] == ["JSON export", "All Files"]


def test_pick_file_default_preset_is_images(client, monkeypatch):
    captured = _install(monkeypatch, "pick_files_async", DialogState.CANCELLED)
    client.post("/api/native/pick-file")
    assert [ft.description for ft in captured["file_types"]] == ["Images", "All Files"]


# --------------------------------------------------------------------------- #
# pick-folder
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "state,paths,reason,expected",
    [
        (
            DialogState.SELECTED,
            ("/data/scans",),
            "",
            {"state": "selected", "paths": _native(["/data/scans"]), "reason": ""},
        ),
        (
            DialogState.CANCELLED,
            (),
            "",
            {"state": "cancelled", "paths": [], "reason": ""},
        ),
        (
            DialogState.UNAVAILABLE,
            (),
            "tkinter is not installed",
            {"state": "unavailable", "paths": [], "reason": "tkinter is not installed"},
        ),
    ],
)
def test_pick_folder_returns_three_state_shape(client, monkeypatch, state, paths, reason, expected):
    _install(monkeypatch, "pick_folder_async", state, paths, reason)
    res = client.post("/api/native/pick-folder")
    assert res.status_code == 200
    assert res.json() == expected


# --------------------------------------------------------------------------- #
# save-file
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "state,paths,reason,expected",
    [
        (
            DialogState.SELECTED,
            ("/data/out.jsonld",),
            "",
            {"state": "selected", "paths": _native(["/data/out.jsonld"]), "reason": ""},
        ),
        (
            DialogState.CANCELLED,
            (),
            "",
            {"state": "cancelled", "paths": [], "reason": ""},
        ),
        (
            DialogState.UNAVAILABLE,
            (),
            "no live window",
            {"state": "unavailable", "paths": [], "reason": "no live window"},
        ),
    ],
)
def test_save_file_returns_three_state_shape(client, monkeypatch, state, paths, reason, expected):
    _install(monkeypatch, "save_file_async", state, paths, reason)
    res = client.post("/api/native/save-file")
    assert res.status_code == 200
    assert res.json() == expected


def test_save_file_forwards_default_name_with_extension(client, monkeypatch):
    """C3 — the extension is carried by ``default_name``, not a
    ``defaultextension`` policy the service deliberately does not hold."""
    captured = _install(
        monkeypatch,
        "save_file_async",
        DialogState.SELECTED,
        paths=("/data/out.jsonld",),
    )
    res = client.post(
        "/api/native/save-file",
        json={"preset": "json", "default_name": "artifice-ocr-tropy.jsonld"},
    )
    assert captured["default_name"] == "artifice-ocr-tropy.jsonld"
    assert [ft.description for ft in captured["file_types"]] == ["JSON export", "All Files"]
    assert res.json()["state"] == "selected"


def test_save_file_non_json_preset_uses_all_files_filter(client, monkeypatch):
    captured = _install(monkeypatch, "save_file_async", DialogState.CANCELLED)
    client.post("/api/native/save-file", json={"preset": "all", "default_name": "output.pdf"})
    assert [ft.description for ft in captured["file_types"]] == ["All Files"]
