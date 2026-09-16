# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tropy JSON-LD bridge: import preview, import add, export, and writable-items tests."""

import json
from pathlib import Path

import pytest
from artifice_ocr.web import runtime

from ._helpers import _seed_history_run

# --------------------------------------------------------------------------- #
_removed_tropy_bridge = pytest.mark.skip(
    reason="Removed: JSON-LD bridge is no longer a supported Tropy path"
)


@_removed_tropy_bridge
def test_tropy_import_preview_rejects_bad_suffix(client, tmp_path):
    f = tmp_path / "export.txt"
    f.write_text("{}", encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 400


@_removed_tropy_bridge
def test_tropy_import_preview_reports_missing_file(client, tmp_path):
    f = tmp_path / "gone.jsonld"
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 400


@_removed_tropy_bridge
def test_tropy_import_preview_accepts_valid_jsonld(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"}
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    (tmp_path / "a.png").write_bytes(b"x")
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 200
    body = res.json()
    assert body["export_name"] == "export.json"
    assert len(body["items"]) == 1
    assert body["items"][0]["photo_count"] == 1


@_removed_tropy_bridge
def test_tropy_import_preview_accepts_bare_list(client, tmp_path):
    export = [
        {
            "@type": "Item",
            "title": "Test Item",
            "photo": [
                {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"}
            ],
        }
    ]
    f = tmp_path / "export.json"
    (tmp_path / "a.png").write_bytes(b"x")
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 200
    assert len(res.json()["items"]) == 1


@_removed_tropy_bridge
def test_tropy_import_preview_skips_non_item_nodes(client, tmp_path):
    export = {
        "@graph": [
            {"@type": "Template", "name": "Generic"},
            {
                "@type": "Item",
                "title": "Doc 1",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"}
                ],
            },
        ]
    }
    f = tmp_path / "export.json"
    (tmp_path / "a.png").write_bytes(b"x")
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 200
    assert len(res.json()["items"]) == 1


@_removed_tropy_bridge
def test_tropy_import_preview_reports_missing_photos(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "missing.jpg",
                        "checksum": "abc",
                        "mimetype": "image/jpeg",
                    }
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 200
    assert res.json()["items"][0]["missing_count"] == 1


@_removed_tropy_bridge
def test_tropy_import_preview_rejects_absolute_paths(client, tmp_path):
    """``/etc/passwd`` is still a 400 — now rejected by the blocklist.

    The detail string must reflect the actual blocklist rejection, not a
    generic "absolute" message.
    """
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "/etc/passwd", "checksum": "x", "mimetype": "text"},
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 400
    assert "protected" in res.json()["detail"].lower()


@_removed_tropy_bridge
def test_tropy_import_preview_rejects_dotdot_segments(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "../secret", "checksum": "x", "mimetype": "text"},
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    assert res.status_code == 400


@_removed_tropy_bridge
def test_tropy_import_preview_error_message_does_not_leak_paths(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "../secret", "checksum": "x", "mimetype": "text"},
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post("/api/tropy/import/preview", json={"path": str(f)})
    detail = res.json()["detail"]
    assert str(tmp_path) not in detail
    assert str(Path.home()) not in detail


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — import add
# --------------------------------------------------------------------------- #


@_removed_tropy_bridge
def test_tropy_import_add_adds_to_queue(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"}
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    (tmp_path / "a.png").write_bytes(b"x")
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post(
        "/api/tropy/import/add",
        json={"path": str(f), "output_dir": str(tmp_path / "out")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["added"] >= 1


@_removed_tropy_bridge
def test_tropy_import_add_reports_missing(client, tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Missing Photos",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "gone.pdf",
                        "checksum": "x",
                        "mimetype": "application/pdf",
                        "page": 0,
                    }
                ],
            }
        ]
    }
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")
    res = client.post(
        "/api/tropy/import/add",
        json={"path": str(f), "output_dir": str(tmp_path / "out")},
    )
    assert res.status_code == 200
    assert "gone.pdf  p.1" in res.json()["missing"]


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — content-based import
# --------------------------------------------------------------------------- #


@_removed_tropy_bridge
def test_tropy_import_preview_via_content(client, tmp_path):
    """Preview via ``content`` field round-trips correctly."""
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Content Item",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            }
        ]
    }
    (tmp_path / "a.png").write_bytes(b"x")
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    # via content (with relative photos, yields nothing)
    text = f.read_text(encoding="utf-8")
    res = client.post(
        "/api/tropy/import/preview",
        json={"content": text, "filename": "export.json"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["export_name"] == "export.json"


@_removed_tropy_bridge
def test_tropy_import_add_via_content_with_groups(client, safe_tmp_path):
    """``import/add`` via ``content`` adds items and respects ``groups``
    filtering — uses absolute paths in the export so content import can
    resolve photos."""
    photo_a = safe_tmp_path / "a.png"
    photo_a.write_bytes(b"x")
    photo_b = safe_tmp_path / "b.png"
    photo_b.write_bytes(b"x")

    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Group A Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": str(photo_a),
                        "checksum": "abc",
                        "mimetype": "image/png",
                    },
                ],
            },
            {
                "@type": "Item",
                "title": "Group B Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": str(photo_b),
                        "checksum": "xyz",
                        "mimetype": "image/png",
                    },
                ],
            },
        ]
    }
    f = safe_tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    # First, import via path to get group IDs (path import resolves everything)
    res_preview = client.post(
        "/api/tropy/import/preview",
        json={"path": str(f)},
    )
    groups = [item["group"] for item in res_preview.json()["items"]]
    assert len(groups) == 2

    # Now import/add via content with only the second group
    text = f.read_text(encoding="utf-8")
    res = client.post(
        "/api/tropy/import/add",
        json={
            "content": text,
            "filename": "export.json",
            "groups": groups[1:],
            "output_dir": str(safe_tmp_path / "out"),
        },
    )
    assert res.status_code == 200
    # Only the second item should be added
    assert res.json()["added"] == 1


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — request validation (422s)
# --------------------------------------------------------------------------- #


@_removed_tropy_bridge
def test_tropy_import_rejects_both_path_and_content(client):
    """Providing both ``path`` and ``content`` → 422."""
    res = client.post(
        "/api/tropy/import/preview",
        json={"path": "/tmp/e.json", "content": "{}"},
    )
    assert res.status_code == 422


@_removed_tropy_bridge
def test_tropy_import_rejects_neither_path_nor_content(client):
    """Providing neither ``path`` nor ``content`` → 422."""
    res = client.post(
        "/api/tropy/import/preview",
        json={},
    )
    assert res.status_code == 422


@_removed_tropy_bridge
def test_tropy_import_rejects_filename_without_content(client):
    """``filename`` is only valid with ``content`` → 422."""
    res = client.post(
        "/api/tropy/import/preview",
        json={"path": "/tmp/e.json", "filename": "e.json"},
    )
    assert res.status_code == 422


@_removed_tropy_bridge
def test_tropy_import_rejects_oversized_content(client, tmp_path):
    """Content exceeding ``MAX_FILE_BYTES`` → 422 from the Pydantic max_length
    constraint."""
    from artifice_ocr.tropy_jsonld import MAX_FILE_BYTES

    # Build a JSON string whose length exceeds MAX_FILE_BYTES
    big_str = "x" * (MAX_FILE_BYTES + 1)
    res = client.post(
        "/api/tropy/import/preview",
        json={"content": big_str},
    )
    assert res.status_code == 422


@_removed_tropy_bridge
def test_tropy_import_rejects_content_byte_limit_exceeded(client, tmp_path):
    """Content whose char count is under ``MAX_FILE_BYTES`` but UTF-8 byte
    count exceeds it → 400 from the loader.

    Uses a direct call to ``load_export_content`` rather than a massive
    HTTP body that can hit TestClient size limits.
    """
    from artifice_ocr.tropy_jsonld import MAX_FILE_BYTES, TropyImportError, load_export_content

    # Each → is a 3-byte UTF-8 character.  Build a string whose char count
    # is under MAX_FILE_BYTES but whose encoded byte count exceeds it.
    chunk_size = MAX_FILE_BYTES // 3 + 10
    big_content = "→" * chunk_size
    assert len(big_content) < MAX_FILE_BYTES, "char count should be under limit"
    assert len(big_content.encode("utf-8")) > MAX_FILE_BYTES, "byte count should exceed limit"

    with pytest.raises(TropyImportError, match="too large"):
        load_export_content(big_content, filename="test.jsonld")


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — export
# --------------------------------------------------------------------------- #


@_removed_tropy_bridge
def test_tropy_export_requires_tropy_origin(client, tmp_path):
    f = tmp_path / "plain.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    res = client.post("/api/tropy/export", json={"stage": "cleaned"})
    assert res.status_code == 409


@_removed_tropy_bridge
def test_tropy_export_produces_jsonld(client, tmp_path):
    # Add an item with tropy-jsonld origin
    from artifice_ocr.jobs import JobItem

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")
    item = JobItem(
        path=str(f),
        label="doc.pdf  p.1",
        source={
            "origin": "tropy-jsonld",
            "tropy_group": "abc:0",
            "item_node": {"@type": "Item", "title": "Doc"},
            "photo_index": 0,
            "photo_path_rel": "doc.pdf",
            "checksum": "abc",
            "mimetype": "application/pdf",
            "item_title": "Doc",
            "orientation": 1,
        },
    )
    item.results = {"cleaned": {"cleaned_text": "Some cleaned text"}}
    runtime.state.add_items([item])

    res = client.post("/api/tropy/export", json={"stage": "cleaned"})
    assert res.status_code == 200
    assert "application/ld+json" in res.headers["content-type"]


@_removed_tropy_bridge
def test_tropy_export_writes_to_disk_when_path_given(client, tmp_path):
    """When a `path` is provided the export writes to disk and returns JSON."""
    from artifice_ocr.jobs import JobItem

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")
    item = JobItem(
        path=str(f),
        label="doc.pdf  p.1",
        source={
            "origin": "tropy-jsonld",
            "tropy_group": "abc:0",
            "item_node": {"@type": "Item", "title": "Doc"},
            "photo_index": 0,
            "photo_path_rel": "doc.pdf",
            "checksum": "abc",
            "mimetype": "application/pdf",
            "item_title": "Doc",
            "orientation": 1,
        },
    )
    item.results = {"cleaned": {"cleaned_text": "Some cleaned text"}}
    runtime.state.add_items([item])

    out = tmp_path / "my-export.jsonld"
    res = client.post(
        "/api/tropy/export",
        json={"stage": "cleaned", "path": str(out)},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["filename"] == "my-export.jsonld"
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    data = json.loads(content)
    assert "generator" in data
    assert "@graph" in data


@_removed_tropy_bridge
def test_tropy_export_history_requires_item_node(client, tmp_path):
    run_id = _seed_history_run(runtime.state)
    rows = runtime.state.history.list_items(run_id)

    # History items seeded by _seed_history_run won't have tropy_item_node,
    # so they should be non-exportable
    if rows:
        item_id = rows[0]["item_id"]
        res = client.post(
            "/api/tropy/export/history", json={"item_ids": [item_id], "stage": "cleaned"}
        )
        assert res.status_code == 409


def test_tropy_writable_items_selects_items_with_photo_id(client):
    """An item carrying a numeric ``photo_id`` is writable back to Tropy."""
    from artifice_ocr.jobs import JobItem

    writable = JobItem(path="with-id.png", source={"photo_id": 11})
    runtime.state.add_items([writable])

    assert runtime.state.tropy_writable_items(None) == [writable]


def test_tropy_writable_items_skips_items_without_photo_id(client):
    """An item with no ``photo_id`` at all is not writable."""
    from artifice_ocr.jobs import JobItem

    plain = JobItem(path="plain.png")
    runtime.state.add_items([plain])

    assert runtime.state.tropy_writable_items(None) == []


def test_tropy_writable_items_skips_explicit_none_photo_id(client):
    """An item whose ``photo_id`` is explicitly ``None`` is not writable."""
    from artifice_ocr.jobs import JobItem

    nulled = JobItem(path="nulled.png", source={"photo_id": None})
    runtime.state.add_items([nulled])

    assert runtime.state.tropy_writable_items(None) == []


def test_tropy_writable_items_is_independent_of_origin(client):
    """Selection keys on ``photo_id``, not ``origin``.

    An item with ``origin == "tropy-jsonld"`` and a ``photo_id`` is still
    selected — the filter is on the id, not the origin.
    """
    from artifice_ocr.jobs import JobItem

    jsonld = JobItem(path="jsonld.png", source={"origin": "tropy-jsonld", "photo_id": 5})
    runtime.state.add_items([jsonld])

    assert runtime.state.tropy_writable_items(None) == [jsonld]


def test_tropy_writable_items_none_item_ids_considers_whole_queue(client):
    """``item_ids=None`` considers the whole queue, not a subset."""
    from artifice_ocr.jobs import JobItem

    a = JobItem(path="a.png", source={"photo_id": 1})
    b = JobItem(path="b.png")
    c = JobItem(path="c.png", source={"photo_id": 2})
    runtime.state.add_items([a, b, c])

    assert runtime.state.tropy_writable_items(None) == [a, c]
