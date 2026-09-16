# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Queue CRUD, mutation guardrails, and add-paths validation tests."""

import os
import types
from pathlib import Path

from artifice_ocr.web.routers import (
    queue as _queue_router,
)

# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #


def test_empty_queue_on_startup(client):
    res = client.get("/api/queue")
    assert res.status_code == 200
    assert res.json() == {
        "items": [],
        "status": {"running": False, "paused": False, "total": 0, "upload_enabled": True},
    }


def test_add_paths_resolves_supported_extensions_only(client, tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"x")  # unsupported, must be ignored

    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": [str(tmp_path / "a.png"), str(tmp_path / "b.txt")],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["added"] == 1
    assert body["items"][0]["name"] == "a.png"


def test_add_paths_expands_a_folder(client, tmp_path):
    (tmp_path / "one.png").write_bytes(b"x")
    (tmp_path / "two.pdf").write_bytes(b"x")
    (tmp_path / "readme.md").write_bytes(b"x")

    res = client.post("/api/queue/add-paths", json={"paths": [str(tmp_path)]})
    assert res.json()["added"] == 2


def test_add_paths_deduplicates(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")

    first = client.post("/api/queue/add-paths", json={"paths": [str(f)]})
    second = client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    assert first.json()["added"] == 1
    assert second.json()["added"] == 0
    assert len(second.json()["items"]) == 1


def test_remove_items(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    res = client.post("/api/queue/remove", json={"ids": [item_id]})
    assert res.json()["removed"] == 1
    assert res.json()["items"] == []


def test_clear_queue(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    res = client.post("/api/queue/clear")
    assert res.json()["items"] == []
    assert client.get("/api/queue").json()["items"] == []


def test_reorder_moves_item_when_no_run_in_progress(client, tmp_path):
    f1 = tmp_path / "a.png"
    f2 = tmp_path / "b.png"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f1), str(f2)]}).json()
    ids = [item["id"] for item in added["items"]]

    res = client.post(
        "/api/queue/reorder",
        json={"drag_id": ids[1], "drop_id": ids[0], "before": True},
    )
    assert res.status_code == 200
    assert [item["id"] for item in res.json()["items"]] == [ids[1], ids[0]]


# --------------------------------------------------------------------------- #
# queue mutation guardrails while a run is in progress
# --------------------------------------------------------------------------- #
# `remove`/`clear`/`reorder` must 409 while `state.runner.is_running` is True
# (see `RunState.remove`/`clear`/`reorder` in runtime.py). A plain stand-in
# with `is_running = True` is used in place of a real `JobRunner`: a freshly
# constructed runner that was never `.start()`-ed reports `is_running ==
# False`, so racing a real thread to catch one mid-run would be flaky for no
# benefit — this only needs the attribute the guard actually reads.


def test_remove_409s_while_run_in_progress(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    _queue_router.state.runner = types.SimpleNamespace(is_running=True, is_paused=False)
    res = client.post("/api/queue/remove", json={"ids": [item_id]})
    assert res.status_code == 409
    assert "in progress" in res.json()["detail"].lower()
    # The guard must fire before any mutation — item is still there.
    assert len(client.get("/api/queue").json()["items"]) == 1


def test_clear_409s_while_run_in_progress(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    _queue_router.state.runner = types.SimpleNamespace(is_running=True, is_paused=False)
    res = client.post("/api/queue/clear")
    assert res.status_code == 409
    assert "in progress" in res.json()["detail"].lower()
    assert len(client.get("/api/queue").json()["items"]) == 1


def test_reorder_409s_while_run_in_progress(client, tmp_path):
    f1 = tmp_path / "a.png"
    f2 = tmp_path / "b.png"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f1), str(f2)]}).json()
    ids = [item["id"] for item in added["items"]]

    _queue_router.state.runner = types.SimpleNamespace(is_running=True, is_paused=False)
    res = client.post(
        "/api/queue/reorder",
        json={"drag_id": ids[1], "drop_id": ids[0], "before": True},
    )
    assert res.status_code == 409
    assert "in progress" in res.json()["detail"].lower()
    # Order untouched — the guard fired before any mutation.
    assert [item["id"] for item in client.get("/api/queue").json()["items"]] == ids


def test_remove_clear_reorder_still_work_when_runner_not_running(client, tmp_path):
    """The 409 guard must not fire for a runner that exists but has finished
    (or never started) — only a currently-running one blocks mutation."""
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    _queue_router.state.runner = types.SimpleNamespace(is_running=False, is_paused=False)
    res = client.post("/api/queue/remove", json={"ids": [item_id]})
    assert res.status_code == 200
    assert res.json()["removed"] == 1
    assert res.json()["items"] == []


def test_add_paths_still_succeeds_while_run_in_progress(client, tmp_path):
    """Part 2's explicit scope decision: additions are never blocked, even
    while a run is active — see the comment on RunState.remove/clear/reorder
    in runtime.py explaining why."""
    f = tmp_path / "a.png"
    f.write_bytes(b"x")

    _queue_router.state.runner = types.SimpleNamespace(is_running=True, is_paused=False)
    res = client.post("/api/queue/add-paths", json={"paths": [str(f)]})
    assert res.status_code == 200
    assert res.json()["added"] == 1


# --------------------------------------------------------------------------- #
# path validation — add_paths
# --------------------------------------------------------------------------- #


def test_add_paths_refuses_path_outside_allowed_roots(client, tmp_path):
    """A path outside the permitted root directories is refused with 400."""
    # Pick a directory that is not /home, /tmp, or the working directory.
    # resolve(strict=False) does not require the path to exist, so a
    # nonexistent path under /opt is sufficient.
    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": ["/opt/rejected/scan.png"],
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "outside the directories this server is permitted" in detail.lower()
    # The rejection must not disclose the server's filesystem layout. The
    # allowed roots include Path.home(), so naming them would hand the OS
    # username to an unauthenticated caller.
    assert str(Path.home()) not in detail
    assert "allowed:" not in detail.lower()


def test_add_paths_refuses_hidden_directory(client, tmp_path):
    """A path that descends into a hidden directory is refused even when the
    root itself is permitted."""
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "scan.png").write_bytes(b"x")

    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": [str(hidden / "scan.png")],
        },
    )
    assert res.status_code == 400
    assert "hidden" in res.json()["detail"].lower()


def test_add_paths_accepts_normal_path(client, tmp_path):
    """A normal folder of scans in a normal location still queues and
    processes — the security fix does not break the actual workflow."""
    (tmp_path / "scan.png").write_bytes(b"x")
    (tmp_path / "scan.pdf").write_bytes(b"x")

    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": [str(tmp_path)],
        },
    )
    assert res.status_code == 200
    assert res.json()["added"] == 2


def test_add_paths_refuses_windows_style_path(client, tmp_path):
    """A Windows-style path with backslashes that points outside allowed
    roots is rejected.

    The *reason* differs by platform and the assertion has to follow, or this
    test passes on POSIX and fails on Windows. On POSIX a drive letter is
    refused outright by `normalise_path`, because pathlib would otherwise treat
    "C:/SystemFolder" as relative and `resolve()` would prepend the cwd,
    landing it inside an allowed root. On Windows the same string is a
    perfectly valid absolute path, so it resolves and is then refused by the
    containment check instead. Either way it must be a 400 — that is the
    property under test.
    """
    res = client.post(
        "/api/queue/add-paths",
        json={
            "paths": ["C:\\SystemFolder\\file.png"],
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"].lower()
    if os.name == "posix":
        assert "not valid on this platform" in detail
    else:
        assert "outside the directories this server is permitted" in detail
