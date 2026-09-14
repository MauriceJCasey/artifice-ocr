# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Scale tests for browsing and enqueuing a large Tropy project.

The counterpart to `test_tropy_send_scale.py`, covering the *import* side of
the round trip. `test_tropy_browse.py`'s fixture is three items and three
photos — enough to prove the query logic correct, never enough to notice a
per-item cost compounding. This is the risk that matters here:
`enqueue_from_tropy` calls `tropy_db.get_item()` once per selected item id in
a plain Python loop, and `get_item()` opens and closes its own SQLite
connection every time it's called — the same "one connection per unit of
work in a loop" shape that caused the Send-to-Tropy incident these scale
tests exist to catch, just against a local file instead of a network call.
SQLite connection setup is far cheaper than an HTTP round trip, so this is
expected to hold up fine at realistic scale — these tests exist to confirm
that empirically rather than assume it, and to catch it early if it stops
being true.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from artifice_ocr import config
from artifice_ocr.tropy_db import list_items
from artifice_ocr.web.models import TropyEnqueueRequest
from artifice_ocr.web.routers import tropy_browse
from artifice_ocr.web.runtime import state

# Real Tropy schema, trimmed to nothing: every table `list_items`/`get_item`
# joins against must exist even when unused, or the query itself errors.
_TROPY_SCHEMA = """
CREATE TABLE subjects (
    id INTEGER PRIMARY KEY,
    template TEXT,
    type TEXT,
    created TEXT,
    modified TEXT
);

CREATE TABLE items (
    id INTEGER PRIMARY KEY REFERENCES subjects(id) ON DELETE CASCADE,
    cover_image_id INTEGER
);

CREATE TABLE images (
    id INTEGER PRIMARY KEY REFERENCES subjects(id),
    width INTEGER,
    height INTEGER,
    angle INTEGER,
    mirror INTEGER,
    brightness INTEGER,
    contrast INTEGER,
    hue INTEGER,
    saturation INTEGER,
    negative INTEGER,
    sharpen INTEGER
);

CREATE TABLE photos (
    id INTEGER PRIMARY KEY REFERENCES images(id),
    item_id INTEGER REFERENCES items(id),
    position INTEGER,
    path TEXT NOT NULL,
    protocol TEXT DEFAULT 'file',
    mimetype TEXT,
    checksum TEXT,
    orientation INTEGER DEFAULT 1,
    metadata TEXT,
    size INTEGER,
    page INTEGER,
    color TEXT,
    density INTEGER,
    filename TEXT
);

CREATE TABLE metadata (
    id INTEGER REFERENCES subjects(id),
    property TEXT NOT NULL,
    value_id INTEGER REFERENCES metadata_values(value_id),
    language TEXT,
    created TEXT,
    PRIMARY KEY (id, property)
);

CREATE TABLE metadata_values (
    value_id INTEGER PRIMARY KEY,
    datatype TEXT,
    text TEXT,
    data BLOB,
    UNIQUE(datatype, text)
);

CREATE TABLE lists (
    list_id INTEGER PRIMARY KEY,
    name TEXT,
    parent_list_id INTEGER DEFAULT 0,
    position INTEGER,
    created TEXT,
    modified TEXT
);

CREATE TABLE list_items (
    list_id INTEGER REFERENCES lists(list_id),
    id INTEGER REFERENCES items(id),
    position INTEGER,
    added TEXT,
    deleted TEXT,
    PRIMARY KEY (list_id, id)
);

CREATE TABLE tags (
    tag_id INTEGER PRIMARY KEY,
    name TEXT UNIQUE COLLATE NOCASE,
    color TEXT,
    created TEXT,
    modified TEXT
);

CREATE TABLE taggings (
    tag_id INTEGER REFERENCES tags(tag_id),
    id INTEGER REFERENCES subjects(id),
    created TEXT,
    PRIMARY KEY (id, tag_id)
);

CREATE TABLE trash (
    id INTEGER REFERENCES subjects(id),
    deleted TEXT,
    reason TEXT
);

CREATE TABLE project (
    project_id INTEGER PRIMARY KEY,
    name TEXT,
    created TEXT,
    base TEXT,
    store TEXT
);
"""

_ITEM_COUNT = 900


def _mock_pathcheck(raw_path: str, **kwargs):
    """Accept every path as present — isolates DB-query cost from real I/O.

    A real archive's asset files exist on disk; this test cares about
    `get_item()`/`list_items()`'s SQL-and-connection cost at scale, not
    filesystem latency, so it stands in for `validate_absolute_photo` the
    same way `test_tropy_browse.py` does.
    """
    from artifice_ocr._tropy_pathcheck import PhotoPathResult

    return PhotoPathResult(resolved=Path(raw_path), missing=False, is_symlink=False)


def _build_large_project(tmp_path: Path, item_count: int) -> Path:
    """One photo per item — mirrors a folder of individually scanned images,
    the shape of the run that originally triggered the Send-to-Tropy
    incident, rather than one item with hundreds of paginated photos."""
    root = tmp_path / "Archive.tropy"
    root.mkdir()
    db_path = root / "project.tpy"
    conn = sqlite3.connect(db_path)
    conn.executescript(_TROPY_SCHEMA)
    conn.execute("INSERT INTO lists (list_id, name) VALUES (0, 'ROOT')")
    conn.execute("INSERT INTO project (project_id, name, base) VALUES (1, 'Archive', 'project')")

    subjects = [
        (i, "https://tropy.org/v1/templates/item", "item") for i in range(1, item_count + 1)
    ]
    subjects += [
        (10_000 + i, "https://tropy.org/v1/templates/photo", "photo")
        for i in range(1, item_count + 1)
    ]
    conn.executemany("INSERT INTO subjects (id, template, type) VALUES (?, ?, ?)", subjects)
    conn.executemany("INSERT INTO items (id) VALUES (?)", [(i,) for i in range(1, item_count + 1)])
    conn.executemany(
        "INSERT INTO images (id) VALUES (?)", [(10_000 + i,) for i in range(1, item_count + 1)]
    )
    photos = [
        (
            10_000 + i,  # id
            i,  # item_id
            f"assets/page-{i}.jpg",  # path
            "image/jpeg",  # mimetype
            f"checksum-{i}",  # checksum
            1,  # orientation
            f"page-{i}.jpg",  # filename
        )
        for i in range(1, item_count + 1)
    ]
    conn.executemany(
        "INSERT INTO photos (id, item_id, path, mimetype, checksum, orientation, filename) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        photos,
    )
    conn.commit()
    conn.close()
    return root


def test_browsing_a_large_project_lists_every_item_quickly(tmp_path, monkeypatch):
    monkeypatch.setattr("artifice_ocr.tropy_db.validate_absolute_photo", _mock_pathcheck)
    project = _build_large_project(tmp_path, _ITEM_COUNT)

    started = time.monotonic()
    items = list_items(project / "project.tpy")
    elapsed = time.monotonic() - started

    assert len(items) == _ITEM_COUNT
    assert all(len(item.photos) == 1 and not item.photos[0].missing for item in items)
    # Generous regression tripwire, not a tight benchmark — see
    # test_tropy_send_scale.py's identical rationale.
    assert elapsed < 10.0, f"listing {_ITEM_COUNT} items took {elapsed:.1f}s"


def test_enqueueing_every_item_in_a_large_project_completes_quickly(tmp_path, monkeypatch):
    monkeypatch.setattr("artifice_ocr.tropy_db.validate_absolute_photo", _mock_pathcheck)
    config.apply_overrides({"tropy_live_browse_enabled": True})
    state.clear()

    project = _build_large_project(tmp_path, _ITEM_COUNT)

    request = TropyEnqueueRequest(
        path=str(project),
        item_ids=list(range(1, _ITEM_COUNT + 1)),
        output_dir=str(tmp_path / "output"),
    )
    started = time.monotonic()
    result = tropy_browse.enqueue_from_tropy(request)
    elapsed = time.monotonic() - started

    assert result["added"] == _ITEM_COUNT
    assert result["missing"] == 0
    assert len(state.queue_snapshot()) == _ITEM_COUNT
    # enqueue_from_tropy calls get_item() once per item id in a plain loop,
    # each call opening and closing its own SQLite connection — the same
    # shape as the Tropy-send bug, against local file I/O instead of a
    # network call. Confirms that difference actually matters at this scale
    # rather than assuming it.
    assert elapsed < 10.0, f"enqueueing {_ITEM_COUNT} items took {elapsed:.1f}s"

    state.clear()
