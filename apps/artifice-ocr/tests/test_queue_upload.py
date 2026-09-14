# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for ``POST /api/queue/upload`` — the file-upload endpoint.

No real model calls, mirroring the rest of the web suite. The fixture swaps in
a fresh ``RunState`` per test (the same pattern ``test_web.py`` uses) so the
queue never leaks between tests, and redirects the staging directory to a temp
path so no test writes into the real ``~/.artifice_ocr/uploads/``.
"""

import pytest
from artifice_ocr import config
from artifice_ocr.web import runtime, server
from artifice_ocr.web.routers import queue as _queue_router
from artifice_ocr.web.runtime import RunState
from fastapi.testclient import TestClient


@pytest.fixture
def upload_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
    config.reset()
    config.load_config()
    config.apply_overrides({"history_db": str(tmp_path / "history.db")})

    fresh = RunState()
    monkeypatch.setattr(_queue_router, "state", fresh)
    monkeypatch.setattr(runtime, "state", fresh)

    staging = tmp_path / "uploads"
    monkeypatch.setattr(_queue_router, "_staging_dir", lambda: staging)

    with TestClient(server.app) as c:
        yield c, staging


def _upload(client, name, content, media_type="application/octet-stream"):
    return client.post("/api/queue/upload", files=[("files", (name, content, media_type))])


def test_valid_image_uploads_stages_and_enqueues(upload_env):
    client, staging = upload_env
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    res = _upload(client, "page1.png", png, "image/png")

    assert res.status_code == 200
    body = res.json()
    assert body["uploaded"] == [{"filename": "page1.png", "status": "ok"}]
    assert body["added"] == 1
    assert len(body["items"]) == 1

    staged = staging / "page1.png"
    assert staged.exists()
    assert staged.read_bytes() == png
    # The queue item points at the staged copy, not some arbitrary path.
    assert body["items"][0]["name"] == "page1.png"
    assert body["items"][0]["path"] == str(staged)


def test_backslash_traversal_lands_inside_staging(upload_env):
    client, staging = upload_env
    res = _upload(client, "..\\..\\x.jpg", b"\xff\xd8\xff\xd9junk", "image/jpeg")

    assert res.status_code == 200
    assert res.json()["uploaded"] == [{"filename": "x.jpg", "status": "ok"}]

    staged = staging / "x.jpg"
    assert staged.exists()
    # The resolved path must stay inside the staging directory.
    assert staged.resolve().is_relative_to(staging.resolve())


def test_traversal_without_extension_is_rejected(upload_env):
    client, staging = upload_env
    res = _upload(client, "../../etc/passwd", b"root:x:0:0:root:/root:/bin/sh\n")

    assert res.status_code == 200
    body = res.json()
    assert body["uploaded"][0]["status"] == "rejected"
    assert "reason" in body["uploaded"][0]
    assert body["added"] == 0
    assert body["items"] == []
    # Nothing was written — neither into staging nor anywhere else.
    assert not (staging / "passwd").exists()
    assert list(staging.iterdir()) == []


def test_disallowed_extension_rejected_with_reason(upload_env):
    client, staging = upload_env
    res = _upload(client, "notes.txt", b"hello")

    assert res.status_code == 200
    body = res.json()
    assert body["uploaded"][0]["status"] == "rejected"
    assert "not accepted" in body["uploaded"][0]["reason"]
    assert body["added"] == 0
    assert body["items"] == []


def test_oversized_file_rejected_without_being_staged(upload_env, monkeypatch):
    client, staging = upload_env
    monkeypatch.setattr(_queue_router, "_MAX_UPLOAD_BYTES", 100)

    res = _upload(client, "big.jpg", b"\xff\xd8" + b"\x00" * 200, "image/jpeg")

    assert res.status_code == 200
    body = res.json()
    assert body["uploaded"][0]["status"] == "rejected"
    assert "exceeds" in body["uploaded"][0]["reason"].lower()
    assert body["added"] == 0
    assert not (staging / "big.jpg").exists()


def test_two_same_named_uploads_both_survive(upload_env):
    client, staging = upload_env
    first = _upload(client, "page1.jpg", b"\xff\xd8first", "image/jpeg")
    second = _upload(client, "page1.jpg", b"\xff\xd8second", "image/jpeg")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["added"] == 1
    assert second.json()["added"] == 1

    names = sorted(p.name for p in staging.iterdir())
    assert names == ["page1.jpg", "page1_1.jpg"]
    assert (staging / "page1.jpg").read_bytes() == b"\xff\xd8first"
    assert (staging / "page1_1.jpg").read_bytes() == b"\xff\xd8second"


def test_malformed_filename_raises_400(upload_env):
    client, staging = upload_env
    for bad in ("..", "."):
        res = _upload(client, bad, b"x")
        assert res.status_code == 400


def test_mixed_batch_one_bad_file_does_not_fail_good_ones(upload_env):
    client, staging = upload_env
    res = client.post(
        "/api/queue/upload",
        files=[
            ("files", ("good.png", b"\x89PNG\r\n\x1a\n", "image/png")),
            ("files", ("bad.txt", b"nope", "text/plain")),
        ],
    )

    assert res.status_code == 200
    body = res.json()
    statuses = {e["filename"]: e["status"] for e in body["uploaded"]}
    assert statuses == {"good.png": "ok", "bad.txt": "rejected"}
    assert body["added"] == 1
    assert (staging / "good.png").exists()
    assert not (staging / "bad.txt").exists()


def test_per_file_results_do_not_leak_absolute_path(upload_env):
    client, staging = upload_env
    res = _upload(client, "page1.png", b"\x89PNG\r\n\x1a\n", "image/png")

    assert res.status_code == 200
    for entry in res.json()["uploaded"]:
        assert "path" not in entry
        assert str(staging) not in repr(entry)


def test_status_reports_upload_enabled(upload_env):
    client, staging = upload_env
    res = client.get("/api/queue")
    assert res.json()["status"]["upload_enabled"] is True
