# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the live read-only .tpy SQLite browse feature.

Uses the **real** Tropy ``.tpy`` schema (``subjects``, ``metadata``,
``metadata_values``, ``photos`` with base-relative paths, ``project``,
``trash``, ``taggings``).
"""

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from artifice_ocr import config
from artifice_ocr.jobs import JobItem
from artifice_ocr.tropy_db import (
    TropyDBError,
    TropyItem,
    TropyPhoto,
    _resolve_photo_path,
    get_item,
    items_to_job_items,
    list_items,
    list_lists,
    list_projects,
    list_tags,
    missing_asset_count,
    recent_projects,
    resolve_project_db_path,
)
from artifice_ocr.web import runtime
from artifice_ocr.web.routers import (
    events as _events_router,
)
from artifice_ocr.web.routers import (
    history as _history_router,
)
from artifice_ocr.web.routers import (
    queue as _queue_router,
)
from artifice_ocr.web.routers import (
    run as _run_router,
)
from artifice_ocr.web.routers import (
    tropy_browse as _tropy_browse_router,
)
from artifice_ocr.web.runtime import RunState
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# helpers — build a mock .tpy database (REAL Tropy schema)
# --------------------------------------------------------------------------- #

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


def _create_tpy(
    path: Path,
    *,
    with_tags: bool = False,
    with_trash: bool = False,
    with_title: bool = True,
    empty_photos: bool = False,
) -> Path:
    """Create a .tpy database with the real Tropy schema and test data.

    Items:
    - 1: title via dc:elements metadata, 2 photos (png + jpg), in list 1
    - 2: title via dc:terms metadata, 1 photo (pdf), in list 1
    - 3: no title metadata (falls back to photo filename), 0 photos
    - 4: in trash (soft-deleted) when ``with_trash=True``

    Photos use base-relative paths; ``project.base = 'project'``.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(_TROPY_SCHEMA)

    # Seed ROOT list (list_id=0)
    conn.execute("INSERT INTO lists (list_id, name) VALUES (0, 'ROOT')")

    # Lists
    conn.execute("INSERT INTO lists (list_id, name) VALUES (1, 'Inbox')")
    conn.execute("INSERT INTO lists (list_id, name) VALUES (2, 'Research')")

    # Project
    conn.execute(
        "INSERT INTO project (project_id, name, base) VALUES (1, 'Test Project', 'project')",
    )

    # ---- subjects for items ----
    conn.execute(
        "INSERT INTO subjects (id, template, type) "
        "VALUES (1, 'https://tropy.org/v1/templates/item', 'item')",
    )
    conn.execute(
        "INSERT INTO subjects (id, template, type) "
        "VALUES (2, 'https://tropy.org/v1/templates/item', 'item')",
    )
    conn.execute(
        "INSERT INTO subjects (id, template, type) "
        "VALUES (3, 'https://tropy.org/v1/templates/item', 'item')",
    )
    if with_trash:
        conn.execute(
            "INSERT INTO subjects (id, template, type) "
            "VALUES (4, 'https://tropy.org/v1/templates/item', 'item')",
        )

    # ---- items ----
    conn.execute("INSERT INTO items (id) VALUES (1)")
    conn.execute("INSERT INTO items (id) VALUES (2)")
    conn.execute("INSERT INTO items (id) VALUES (3)")
    if with_trash:
        conn.execute("INSERT INTO items (id) VALUES (4)")

    # ---- metadata for items 1 and 2 ----
    if with_title:
        # Item 1: dc:elements title
        conn.execute(
            "INSERT INTO metadata_values (value_id, datatype, text) "
            "VALUES (1, 'http://www.w3.org/2001/XMLSchema#string', "
            "'Letter from 1943')",
        )
        conn.execute(
            "INSERT INTO metadata (id, property, value_id) "
            "VALUES (1, 'http://purl.org/dc/elements/1.1/title', 1)",
        )
        # Item 2: dc:terms title
        conn.execute(
            "INSERT INTO metadata_values (value_id, datatype, text) "
            "VALUES (2, 'http://www.w3.org/2001/XMLSchema#string', "
            "'War Diary')",
        )
        conn.execute(
            "INSERT INTO metadata (id, property, value_id) "
            "VALUES (2, 'http://purl.org/dc/terms/title', 2)",
        )

    # ---- photos (base-relative paths, resolved via project.base='project') ----
    if not empty_photos:
        # Subject rows for images/photos
        for img_id in (11, 12, 13):
            conn.execute(
                "INSERT INTO subjects (id, template, type) "
                "VALUES (?, 'https://tropy.org/v1/templates/photo', 'photo')",
                (img_id,),
            )
            conn.execute(
                "INSERT INTO images (id) VALUES (?)",
                (img_id,),
            )

        conn.execute(
            "INSERT INTO photos "
            "(id, item_id, path, checksum, mimetype, page, orientation, "
            "filename) "
            "VALUES (11, 1, 'fake_photo_1.png', 'abc123', 'image/png', "
            "NULL, 1, 'fake_photo_1.png')",
        )
        conn.execute(
            "INSERT INTO photos "
            "(id, item_id, path, checksum, mimetype, page, orientation, "
            "filename) "
            "VALUES (12, 1, 'fake_photo_2.jpg', 'def456', 'image/jpeg', "
            "NULL, 1, 'fake_photo_2.jpg')",
        )
        conn.execute(
            "INSERT INTO photos "
            "(id, item_id, path, checksum, mimetype, page, orientation, "
            "filename) "
            "VALUES (13, 2, 'fake_diary.pdf', 'ghi789', 'application/pdf', "
            "0, 1, 'fake_diary.pdf')",
        )

    # ---- list memberships ----
    conn.execute(
        "INSERT INTO list_items (list_id, id, position) VALUES (1, 1, 0)",
    )
    conn.execute(
        "INSERT INTO list_items (list_id, id, position) VALUES (1, 2, 1)",
    )

    # ---- tags ----
    if with_tags:
        conn.execute(
            "INSERT INTO tags (tag_id, name) VALUES (1, 'personal')",
        )
        conn.execute(
            "INSERT INTO tags (tag_id, name) VALUES (2, 'military')",
        )
        # taggings reference subjects.id (not items.id directly, but same
        # value)
        conn.execute(
            "INSERT INTO taggings (tag_id, id) VALUES (1, 1)",
        )
        conn.execute(
            "INSERT INTO taggings (tag_id, id) VALUES (2, 2)",
        )

    # ---- trash ----
    if with_trash:
        conn.execute(
            "INSERT INTO trash (id, deleted, reason) VALUES (4, datetime('now'), 'user')",
        )

    conn.commit()
    conn.close()
    return path


def _client_with_state(tmp_path, monkeypatch) -> TestClient:
    """Build a TestClient with a fresh RunState wired to all routers."""
    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
    config.reset()
    config.load_config()
    config.apply_overrides({"history_db": str(tmp_path / "history.db")})

    fresh = RunState()
    monkeypatch.setattr(_queue_router, "state", fresh)
    monkeypatch.setattr(_run_router, "state", fresh)
    monkeypatch.setattr(_events_router, "state", fresh)
    monkeypatch.setattr(_history_router, "state", fresh)
    monkeypatch.setattr(_tropy_browse_router, "state", fresh)
    monkeypatch.setattr("artifice_ocr.web.runtime.state", fresh)

    # Reset pdf_export_state
    import queue as _queue_mod

    pstate = runtime.pdf_export_state
    if pstate.thread is not None and pstate.thread.is_alive():
        pstate.thread.join(timeout=5)
    pstate.status = "idle"
    pstate.error = None
    pstate.output_path = None
    while True:
        try:
            pstate.events.get_nowait()
        except _queue_mod.Empty:
            break

    from artifice_ocr.web import server

    return TestClient(server.app)


# --------------------------------------------------------------------------- #
# pathcheck mock — returns a successful PhotoPathResult
# --------------------------------------------------------------------------- #


def _mock_pathcheck(raw_path: str, **kwargs):
    """Mock validate_absolute_photo that accepts any path."""
    from artifice_ocr._tropy_pathcheck import PhotoPathResult

    p = Path(raw_path)
    exists = p.exists()
    return PhotoPathResult(resolved=p, missing=not exists, is_symlink=False)


# --------------------------------------------------------------------------- #
# tests: photo path resolution (unit)
# --------------------------------------------------------------------------- #


class TestResolvePhotoPath:
    """Unit tests for _resolve_photo_path()."""

    def test_project_base(self, tmp_path):
        db = tmp_path / "test.tpy"
        db.write_text("")
        result = _resolve_photo_path("photo.jpg", db, "project")
        assert result == (tmp_path / "photo.jpg").resolve()

    def test_home_base(self, tmp_path, monkeypatch):
        db = tmp_path / "test.tpy"
        db.write_text("")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.Path.home",
            lambda: tmp_path / "fakehome",
        )
        result = _resolve_photo_path("photo.jpg", db, "home")
        assert result == (tmp_path / "fakehome" / "photo.jpg").resolve()

    def test_absolute_base(self, tmp_path):
        db = tmp_path / "test.tpy"
        db.write_text("")
        result = _resolve_photo_path(
            "photo.jpg",
            db,
            "/absolute/base",
        )
        assert result == Path("/absolute/base/photo.jpg").resolve()

    def test_none_base(self, tmp_path):
        db = tmp_path / "sub" / "test.tpy"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("")
        result = _resolve_photo_path("photo.jpg", db, None)
        assert result == (db.parent / "photo.jpg").resolve()

    def test_relative_base_string(self, tmp_path):
        db = tmp_path / "data" / "test.tpy"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("")
        result = _resolve_photo_path("photo.jpg", db, "images")
        assert result == (db.parent / "images" / "photo.jpg").resolve()


# --------------------------------------------------------------------------- #
# tests: tropy_db library functions
# --------------------------------------------------------------------------- #


class TestListProjects:
    """Tests for list_projects()."""

    def test_returns_projects(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        projects = list_projects(tpy)
        assert len(projects) == 1
        assert projects[0]["name"] == "Test Project"
        assert projects[0]["base"] == "project"

    def test_file_not_found(self):
        with pytest.raises(TropyDBError, match="not found"):
            list_projects(Path("/nonexistent/file.tpy"))

    def test_locked_database(self, tmp_path, monkeypatch):
        """Verify TropyDBError with 'close Tropy' message on locked DB."""
        tpy = _create_tpy(tmp_path / "locked.tpy")
        orig_connect = sqlite3.connect

        def _mock_connect(*args, **kwargs):
            if args and isinstance(args[0], str) and "?mode=ro" in args[0]:
                raise sqlite3.OperationalError("database is locked")
            return orig_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _mock_connect)
        with pytest.raises(TropyDBError, match="close Tropy"):
            list_projects(tpy)


class TestListLists:
    """Tests for list_lists()."""

    def test_returns_lists_excluding_root(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        lists = list_lists(tpy)
        assert len(lists) == 2
        names = {row["name"] for row in lists}
        assert names == {"Inbox", "Research"}
        # ROOT row (list_id=0) must be excluded
        ids = {row["list_id"] for row in lists}
        assert 0 not in ids


class TestListTags:
    """Tests for list_tags()."""

    def test_returns_tags(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy", with_tags=True)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        tags = list_tags(tpy)
        assert len(tags) == 2
        names = {row["name"] for row in tags}
        assert names == {"personal", "military"}

    def test_empty_when_no_tags(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy", with_tags=False)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        tags = list_tags(tpy)
        assert tags == []


class TestListItems:
    """Tests for list_items()."""

    def test_returns_all_items(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy")
        # Create actual photo files so pathcheck doesn't mark them missing.
        (tmp_path / "fake_photo_1.png").write_bytes(b"x")
        (tmp_path / "fake_photo_2.jpg").write_bytes(b"x")

        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(tpy)
        assert len(items) == 3
        titles = {it.title for it in items}
        assert titles == {"Letter from 1943", "War Diary", "Item 3"}

    def test_filter_by_list_id(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(tpy, list_id=1)
        assert len(items) == 2
        titles = {it.title for it in items}
        assert titles == {"Letter from 1943", "War Diary"}

    def test_filter_by_tag(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy", with_tags=True)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(tpy, tag="personal")
        assert len(items) == 1
        assert items[0].title == "Letter from 1943"

    def test_photos_attached(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(tpy)
        item1 = next(it for it in items if it.item_id == 1)
        assert len(item1.photos) == 2
        assert {p.photo_id for p in item1.photos} == {11, 12}

    def test_photo_missing_when_pathcheck_rejects(self, tmp_path, monkeypatch):
        """Photos that fail pathcheck are marked missing=True but NOT excluded."""
        tpy = _create_tpy(tmp_path / "test.tpy")

        def _rejecting_pathcheck(raw_path: str, **kwargs):
            raise ValueError("blocked root")

        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _rejecting_pathcheck,
        )
        items = list_items(tpy)
        item1 = next(it for it in items if it.item_id == 1)
        assert len(item1.photos) == 2  # not excluded
        assert all(p.missing for p in item1.photos)

    def test_title_via_metadata_join(self, tmp_path, monkeypatch):
        """Item with dc:elements title metadata returns that title."""
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(tpy)
        item1 = next(it for it in items if it.item_id == 1)
        assert item1.title == "Letter from 1943"

    def test_title_via_dc_terms(self, tmp_path, monkeypatch):
        """Item with dc:terms title metadata returns that title."""
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(tpy)
        item2 = next(it for it in items if it.item_id == 2)
        assert item2.title == "War Diary"

    def test_title_falls_back_to_photo_filename(self, tmp_path, monkeypatch):
        """Item with no title metadata and no photos falls back to 'Item {id}'."""
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(tpy)
        item3 = next(it for it in items if it.item_id == 3)
        assert item3.title == "Item 3"


class TestSoftDelete:
    """Tests for trash (soft-delete) filtering."""

    def test_soft_deleted_excluded(self, tmp_path, monkeypatch):
        """Item 4 is in trash and should be excluded from list_items."""
        tpy = _create_tpy(tmp_path / "test.tpy", with_trash=True)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(tpy)
        item_ids = {it.item_id for it in items}
        assert 4 not in item_ids

    def test_soft_deleted_not_in_get_item(self, tmp_path, monkeypatch):
        """get_item returns None for soft-deleted items."""
        tpy = _create_tpy(tmp_path / "test.tpy", with_trash=True)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        item = get_item(tpy, 4)
        assert item is None


class TestGetItem:
    """Tests for get_item()."""

    def test_returns_single_item(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        item = get_item(tpy, 1)
        assert item is not None
        assert item.title == "Letter from 1943"
        assert len(item.photos) == 2

    def test_returns_none_for_missing(self, tmp_path, monkeypatch):
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        item = get_item(tpy, 999)
        assert item is None

    def test_title_falls_back_to_id(self, tmp_path, monkeypatch):
        """Item with no title metadata and no photos uses 'Item {id}' fallback."""
        tpy = _create_tpy(tmp_path / "test.tpy")
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        item = get_item(tpy, 3)
        assert item is not None
        assert item.title == "Item 3"


class TestFilenameFallback:
    """Title fallback to photo filename."""

    def test_title_from_photo_filename(self, tmp_path, monkeypatch):
        """Item without title metadata but with photos uses the first photo's
        filename as title."""
        tpy = tmp_path / "test.tpy"
        conn = sqlite3.connect(str(tpy))
        conn.executescript(_TROPY_SCHEMA)
        # ROOT list
        conn.execute("INSERT INTO lists (list_id, name) VALUES (0, 'ROOT')")
        conn.execute("INSERT INTO lists (list_id, name) VALUES (1, 'Inbox')")
        conn.execute(
            "INSERT INTO project (project_id, name, base) VALUES (1, 'Test Project', 'project')",
        )
        # Item 1 — no metadata
        conn.execute(
            "INSERT INTO subjects (id, template, type) "
            "VALUES (1, 'https://tropy.org/v1/templates/item', 'item')",
        )
        conn.execute("INSERT INTO items (id) VALUES (1)")
        # Photo with filename
        conn.execute(
            "INSERT INTO subjects (id, template, type) "
            "VALUES (11, 'https://tropy.org/v1/templates/photo', 'photo')",
        )
        conn.execute("INSERT INTO images (id) VALUES (11)")
        conn.execute(
            "INSERT INTO photos (id, item_id, path, checksum, mimetype, "
            "filename) "
            "VALUES (11, 1, 'scan_1943.tiff', 'aaa', 'image/tiff', "
            "'scan_1943.tiff')",
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        item = get_item(tpy, 1)
        assert item is not None
        assert item.title == "scan_1943.tiff"


class TestItemsToJobItems:
    """Tests for items_to_job_items()."""

    def test_produces_job_items(self):
        photos = [
            TropyPhoto(
                photo_id=1,
                path="/tmp/photo.png",
                item_id=1,
                page=None,
                mimetype="image/png",
                checksum="abc",
                orientation=1,
                missing=False,
            ),
        ]
        item = TropyItem(item_id=1, title="Test", photos=photos)
        job_items = items_to_job_items(
            [item], project_db="/tmp/project.tpy", output_dir="/tmp/output"
        )

        assert len(job_items) == 1
        ji = job_items[0]
        assert isinstance(ji, JobItem)
        assert ji.path == "/tmp/photo.png"
        assert ji.source["origin"] == "tropy-live"
        assert ji.source["tropy_item_id"] == 1
        assert ji.source["item_title"] == "Test"
        assert ji.page is None

    def test_pdf_page_stem(self):
        """PDF photos get page-stem naming and page index."""
        photos = [
            TropyPhoto(
                photo_id=3,
                path="/tmp/diary.pdf",
                item_id=2,
                page=2,
                mimetype="application/pdf",
                checksum="ghi",
                orientation=1,
                missing=False,
            ),
        ]
        item = TropyItem(item_id=2, title="War Diary", photos=photos)
        job_items = items_to_job_items(
            [item], project_db="/tmp/project.tpy", output_dir="/tmp/output"
        )

        assert len(job_items) == 1
        ji = job_items[0]
        assert ji.page == 2  # PDF, so page is carried through
        assert "_p0003" in ji.output_stem

    def test_source_carries_tropy_project(self):
        """JobItem source records which project the live-browsed item came from."""
        photos = [
            TropyPhoto(
                photo_id=1,
                path="/tmp/photo.png",
                item_id=1,
                page=None,
                mimetype="image/png",
                checksum="abc",
                orientation=1,
                missing=False,
            ),
        ]
        item = TropyItem(item_id=1, title="Test", photos=photos)
        job_items = items_to_job_items(
            [item], project_db="/tmp/project.tpy", output_dir="/tmp/output"
        )

        ji = job_items[0]
        assert ji.source["tropy_project"] == str(Path("/tmp/project.tpy").resolve())

    def test_tropy_project_is_resolved(self, tmp_path):
        """A relative/`..` spelling is stored as its canonical resolved path."""
        photos = [
            TropyPhoto(
                photo_id=1,
                path="/tmp/photo.png",
                item_id=1,
                page=None,
                mimetype="image/png",
                checksum="abc",
                orientation=1,
                missing=False,
            ),
        ]
        item = TropyItem(item_id=1, title="Test", photos=photos)
        raw = tmp_path / "sub" / ".." / "project.tpy"
        job_items = items_to_job_items([item], project_db=raw, output_dir="/tmp/output")

        ji = job_items[0]
        assert ji.source["tropy_project"] == str((tmp_path / "project.tpy").resolve())
        assert ".." not in ji.source["tropy_project"]

    def test_project_db_is_required(self):
        """Calling without ``project_db`` fails loudly, not with a ``None``."""
        photos = [
            TropyPhoto(
                photo_id=1,
                path="/tmp/photo.png",
                item_id=1,
                page=None,
                mimetype="image/png",
                checksum="abc",
                orientation=1,
                missing=False,
            ),
        ]
        item = TropyItem(item_id=1, title="Test", photos=photos)
        with pytest.raises(TypeError):
            items_to_job_items([item], output_dir="/tmp/output")

    def test_colliding_stems_get_distinct_second_suffix(self):
        """Two different items sharing a title and photo filename collide on
        `page_stem`. The first item's stem must stay byte-identical to plain
        `page_stem` output (existing on-disk outputs keep matching); the
        second gets a stable, checksum-derived suffix — never a bare
        positional counter."""
        from artifice_ocr.tropy_jsonld import page_stem

        photo_a = TropyPhoto(
            photo_id=10,
            path="/tmp/a/page1.jpg",
            item_id=1,
            page=None,
            mimetype="image/jpeg",
            checksum="chk1",
            orientation=1,
            missing=False,
        )
        photo_b = TropyPhoto(
            photo_id=20,
            path="/tmp/b/page1.jpg",
            item_id=2,
            page=None,
            mimetype="image/jpeg",
            checksum="chk2",
            orientation=1,
            missing=False,
        )
        item_a = TropyItem(item_id=1, title="Letters", photos=[photo_a])
        item_b = TropyItem(item_id=2, title="Letters", photos=[photo_b])

        job_items = items_to_job_items(
            [item_a, item_b], project_db="/tmp/project.tpy", output_dir="/tmp/output"
        )

        expected_first = page_stem("Letters", "page1.jpg", None, "image/jpeg", Path("page1.jpg"))
        assert job_items[0].output_stem == expected_first
        assert job_items[1].output_stem != job_items[0].output_stem
        assert job_items[1].output_stem.startswith(expected_first)

    def test_colliding_stems_fall_back_to_photo_id_without_checksum(self):
        """When neither colliding photo carries a checksum, the discriminator
        falls back to the (always-present, DB primary key) photo id."""
        from artifice_ocr.tropy_jsonld import page_stem

        photo_a = TropyPhoto(
            photo_id=10,
            path="/tmp/a/page1.jpg",
            item_id=1,
            page=None,
            mimetype="image/jpeg",
            checksum="",
            orientation=1,
            missing=False,
        )
        photo_b = TropyPhoto(
            photo_id=20,
            path="/tmp/b/page1.jpg",
            item_id=2,
            page=None,
            mimetype="image/jpeg",
            checksum="",
            orientation=1,
            missing=False,
        )
        item_a = TropyItem(item_id=1, title="Letters", photos=[photo_a])
        item_b = TropyItem(item_id=2, title="Letters", photos=[photo_b])

        job_items = items_to_job_items(
            [item_a, item_b], project_db="/tmp/project.tpy", output_dir="/tmp/output"
        )

        expected_first = page_stem("Letters", "page1.jpg", None, "image/jpeg", Path("page1.jpg"))
        assert job_items[0].output_stem == expected_first
        assert job_items[1].output_stem == f"{expected_first}__id20"


class TestPhotoOrientation:
    """Orientation is read from DB and passed through to JobItem source."""

    def test_orientation_in_job_item(self, tmp_path, monkeypatch):
        from artifice_ocr._tropy_pathcheck import PhotoPathResult

        def _mock(raw_path: str, **kwargs):
            return PhotoPathResult(
                resolved=Path(raw_path),
                missing=False,
                is_symlink=False,
            )

        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock,
        )

        tpy = _create_tpy(tmp_path / "test.tpy")
        items = list_items(tpy)
        ji = items_to_job_items(items, project_db=tpy, output_dir="/tmp/output")
        # All test data uses orientation=1
        for j in ji:
            assert j.source["orientation"] == 1


# --------------------------------------------------------------------------- #
# tests: feature-flagged API routes
# --------------------------------------------------------------------------- #


class TestFeatureFlag:
    """Feature flag OFF — all routes return 404.

    Live browse is enabled by default (see config.py), so these tests force it
    off explicitly rather than relying on the default.
    """

    @pytest.fixture(autouse=True)
    def _ensure_flag_off(self, monkeypatch):
        monkeypatch.delenv("ARTIFICE_OCR_TROPY_LIVE_READ", raising=False)
        monkeypatch.setattr(
            config,
            "get",
            lambda key, default=None: False if key == "tropy_live_browse_enabled" else default,
        )

    def test_projects_404_when_disabled(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)
        resp = client.post(
            "/api/tropy/browse/projects",
            json={"path": "/nonexistent.tpy"},
        )
        assert resp.status_code == 404
        assert "not enabled" in resp.json()["detail"].lower()

    def test_items_404_when_disabled(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)
        resp = client.post(
            "/api/tropy/browse/items",
            json={"path": "/nonexistent.tpy"},
        )
        assert resp.status_code == 404

    def test_enqueue_404_when_disabled(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)
        resp = client.post(
            "/api/tropy/browse/enqueue",
            json={"path": "/nonexistent.tpy", "item_ids": [1]},
        )
        assert resp.status_code == 404

    def test_recent_404_when_disabled(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)
        resp = client.get("/api/tropy/browse/recent")
        assert resp.status_code == 404


class TestBrowseRoutesEnabled:
    """Routes work when ARTIFICE_OCR_TROPY_LIVE_READ=1."""

    @pytest.fixture(autouse=True)
    def _enable_flag(self, monkeypatch):
        monkeypatch.setenv("ARTIFICE_OCR_TROPY_LIVE_READ", "1")

    def test_projects_returns_data(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)

        tpy = _create_tpy(tmp_path / "test.tpy")
        resp = client.post(
            "/api/tropy/browse/projects",
            json={"path": str(tpy)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["projects"]) == 1
        assert data["projects"][0]["name"] == "Test Project"

    def test_items_returns_data(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )

        tpy = _create_tpy(tmp_path / "test.tpy")
        resp = client.post(
            "/api/tropy/browse/items",
            json={"path": str(tpy)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 3

    def test_enqueue_adds_items(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )

        tpy = _create_tpy(tmp_path / "test.tpy")
        resp = client.post(
            "/api/tropy/browse/enqueue",
            json={
                "path": str(tpy),
                "item_ids": [1],
                "output_dir": "/tmp/output",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["added"] == 2  # item 1 has 2 photos

    def test_enqueue_adds_only_explicitly_selected_pages(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        tpy = _create_tpy(tmp_path / "test.tpy")

        resp = client.post(
            "/api/tropy/browse/enqueue",
            json={
                "path": str(tpy),
                "item_ids": [1],
                "photo_ids": [12],
                "output_dir": "/tmp/output",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["added"] == 1
        assert body["items"][0]["source"]["photo_id"] == 12

    def test_invalid_path_rejected(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)

        resp = client.post(
            "/api/tropy/browse/projects",
            json={"path": "/etc/passwd.tpy"},
        )
        assert resp.status_code == 400

    def test_recent_projects_route_returns_empty(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.tropy_config_dir",
            lambda: tmp_path / "Tropy",
        )
        resp = client.get("/api/tropy/browse/recent")
        assert resp.status_code == 200
        assert resp.json() == {"projects": []}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param([1, 2, 3], id="top-level-list"),
            pytest.param("a bare string", id="top-level-string"),
            pytest.param(42, id="top-level-int"),
            pytest.param({}, id="recent-missing"),
            pytest.param({"recent": None}, id="recent-null"),
            pytest.param({"recent": "not-a-list"}, id="recent-string"),
            pytest.param({"recent": {"a": "b"}}, id="recent-dict"),
            pytest.param({"recent": [12345]}, id="recent-entry-int"),
            pytest.param({"recent": [None]}, id="recent-entry-null"),
            pytest.param({"recent": [True]}, id="recent-entry-bool"),
            pytest.param({"recent": [["nested"]]}, id="recent-entry-list"),
            pytest.param({"recent": [{"a": 1}]}, id="recent-entry-dict"),
        ],
    )
    def test_malformed_state_route_returns_200(self, tmp_path, monkeypatch, payload):
        """A malformed state.json must 200 with an empty list, never a 500."""
        client = _client_with_state(tmp_path, monkeypatch)
        cfg = tmp_path / "Tropy"
        cfg.mkdir()
        (cfg / "state.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr("artifice_ocr.tropy_db.tropy_config_dir", lambda: cfg)
        resp = client.get("/api/tropy/browse/recent")
        assert resp.status_code == 200
        assert resp.json() == {"projects": []}


class TestBrowseRoutesEnabledViaConfig:
    """Routes work when tropy_live_browse_enabled=True via config (no env var)."""

    @pytest.fixture(autouse=True)
    def _ensure_env_unset(self, monkeypatch):
        """Ensure the env var is NOT set — we're testing the config path."""
        monkeypatch.delenv("ARTIFICE_OCR_TROPY_LIVE_READ", raising=False)

    def test_projects_enabled_via_config(self, tmp_path, monkeypatch):
        """Config-based toggle enables browse routes without touching env var."""
        client = _client_with_state(tmp_path, monkeypatch)
        config.apply_overrides({"tropy_live_browse_enabled": True})

        tpy = _create_tpy(tmp_path / "test.tpy")
        resp = client.post(
            "/api/tropy/browse/projects",
            json={"path": str(tpy)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["projects"]) == 1

    def test_enabled_by_default(self, tmp_path, monkeypatch):
        """Browse routes are enabled by default (maintainer decision, 2026-08-25)."""
        client = _client_with_state(tmp_path, monkeypatch)

        tpy = _create_tpy(tmp_path / "test.tpy")
        resp = client.post(
            "/api/tropy/browse/projects",
            json={"path": str(tpy)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["projects"]) == 1

    def test_flip_then_call_without_restart(self, tmp_path, monkeypatch):
        """Demonstrate: flip via POST /api/config, then immediately call a
        browse route — it must work with no server restart."""
        client = _client_with_state(tmp_path, monkeypatch)

        # Start from an explicit disabled state so the toggle is exercised in
        # both directions (the default is now enabled).
        config.apply_overrides({"tropy_live_browse_enabled": False})
        resp = client.post(
            "/api/tropy/browse/projects",
            json={"path": "/nonexistent.tpy"},
        )
        assert resp.status_code == 404

        # Flip the toggle via the same POST /api/config route the UI uses
        resp = client.post("/api/config", json={"tropy_live_browse_enabled": True})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Immediately (same server process, no restart) the browse route works
        tpy = _create_tpy(tmp_path / "test.tpy")
        resp = client.post(
            "/api/tropy/browse/projects",
            json={"path": str(tpy)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["projects"]) == 1

        # Flip back off — route should 404 again
        resp = client.post("/api/config", json={"tropy_live_browse_enabled": False})
        assert resp.status_code == 200

        resp = client.post(
            "/api/tropy/browse/projects",
            json={"path": str(tpy)},
        )
        assert resp.status_code == 404


class TestSchemaMissingTables:
    """Missing tables are handled gracefully — return empty lists, no crash."""

    def test_empty_db_returns_empty(self, tmp_path, monkeypatch):
        """An empty SQLite file (no tables) returns empty lists."""
        empty = tmp_path / "empty.tpy"
        conn = sqlite3.connect(str(empty))
        conn.close()

        for fn in (list_projects, list_lists, list_tags):
            result = fn(empty)
            assert result == [], f"{fn.__name__} should return empty list"

        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(empty)
        assert items == []


# --------------------------------------------------------------------------- #
# recent projects (item 1)
# --------------------------------------------------------------------------- #


class TestRecentProjects:
    """recent_projects() reads Tropy's own state.json, soft-failing to []."""

    def test_no_state_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.tropy_config_dir",
            lambda: tmp_path / "Tropy",
        )
        assert recent_projects() == []

    def test_parses_valid_state_and_drops_missing(self, tmp_path, monkeypatch):
        cfg = tmp_path / "Tropy"
        cfg.mkdir()
        existing = tmp_path / "ISK Project.tropy"
        existing.mkdir()
        (cfg / "state.json").write_text(
            json.dumps(
                {
                    "recent": [
                        str(existing),
                        str(tmp_path / "gone.tropy"),
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("artifice_ocr.tropy_db.tropy_config_dir", lambda: cfg)
        assert recent_projects() == [existing]

    def test_corrupt_json_returns_empty(self, tmp_path, monkeypatch):
        cfg = tmp_path / "Tropy"
        cfg.mkdir()
        (cfg / "state.json").write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr("artifice_ocr.tropy_db.tropy_config_dir", lambda: cfg)
        assert recent_projects() == []

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param([1, 2, 3], id="top-level-list"),
            pytest.param("a bare string", id="top-level-string"),
            pytest.param(42, id="top-level-int"),
            pytest.param({}, id="recent-missing"),
            pytest.param({"recent": None}, id="recent-null"),
            pytest.param({"recent": "not-a-list"}, id="recent-string"),
            pytest.param({"recent": {"a": "b"}}, id="recent-dict"),
            pytest.param({"recent": [12345]}, id="recent-entry-int"),
            pytest.param({"recent": [None]}, id="recent-entry-null"),
            pytest.param({"recent": [True]}, id="recent-entry-bool"),
            pytest.param({"recent": [["nested"]]}, id="recent-entry-list"),
            pytest.param({"recent": [{"a": 1}]}, id="recent-entry-dict"),
        ],
    )
    def test_malformed_state_returns_empty(self, tmp_path, monkeypatch, payload):
        """Tropy's state.json is untrusted: any malformed shape soft-fails to []."""
        cfg = tmp_path / "Tropy"
        cfg.mkdir()
        (cfg / "state.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr("artifice_ocr.tropy_db.tropy_config_dir", lambda: cfg)
        assert recent_projects() == []

    def test_skips_bad_entry_keeps_valid(self, tmp_path, monkeypatch):
        """A non-string entry is skipped, not the whole list."""
        cfg = tmp_path / "Tropy"
        cfg.mkdir()
        existing = tmp_path / "Real Project.tropy"
        existing.mkdir()
        (cfg / "state.json").write_text(
            json.dumps({"recent": [12345, str(existing), None, True]}),
            encoding="utf-8",
        )
        monkeypatch.setattr("artifice_ocr.tropy_db.tropy_config_dir", lambda: cfg)
        assert recent_projects() == [existing]

    def test_path_home_raising_returns_empty(self, monkeypatch):
        """Path.home() raising (HOME/USERPROFILE unset) must not propagate."""

        def _no_home():
            raise RuntimeError("Could not determine home directory")

        monkeypatch.setattr("artifice_ocr.tropy_db.Path.home", _no_home)
        assert recent_projects() == []


# --------------------------------------------------------------------------- #
# project path resolution (item 2)
# --------------------------------------------------------------------------- #


class TestResolveProjectDbPath:
    """resolve_project_db_path() accepts a bundle dir, a .tpy, or a folder."""

    def test_bundle_directory(self, tmp_path):
        bundle = tmp_path / "ISK Project.tropy"
        bundle.mkdir()
        db = bundle / "project.tpy"
        db.write_text("")
        assert resolve_project_db_path(bundle) == db

    def test_project_tpy_file(self, tmp_path):
        db = tmp_path / "project.tpy"
        db.write_text("")
        assert resolve_project_db_path(db) == db

    def test_containing_folder(self, tmp_path):
        folder = tmp_path / "exports"
        folder.mkdir()
        db = folder / "whatever.tpy"
        db.write_text("")
        assert resolve_project_db_path(folder) == db

    def test_prefers_project_tpy_over_backup(self, tmp_path):
        """The real hazard: backups sort ahead of project.tpy (digits < 't')."""
        bundle = tmp_path / "Rose Cohen Letters.tropy"
        bundle.mkdir()
        real = bundle / "project.tpy"
        real.write_text("")
        (bundle / "project.20260801.backup.tpy").write_text("")
        (bundle / "project.20260701.backup.tpy").write_text("")
        assert resolve_project_db_path(bundle) == real

    def test_backup_excluded_when_no_project_tpy(self, tmp_path):
        folder = tmp_path / "exports"
        folder.mkdir()
        (folder / "project.20260801.backup.tpy").write_text("")
        other = folder / "other.tpy"
        other.write_text("")
        assert resolve_project_db_path(folder) == other

    def test_only_backups_refused_by_construction(self, tmp_path):
        """Only ``*.backup.tpy`` files present — never open a stale snapshot.

        The resolver filters backups out, leaving no candidate, so it falls
        through to the constructed ``project.tpy`` path (which does not exist).
        A later "helpful" relaxation of the backup filter would silently return
        a stale backup here.
        """
        bundle = tmp_path / "Rose Cohen Letters.tropy"
        bundle.mkdir()
        (bundle / "project.20260801.backup.tpy").write_text("")
        (bundle / "project.20260701.backup.tpy").write_text("")
        resolved = resolve_project_db_path(bundle)
        # Not one of the backups, and no real project.tpy exists on disk.
        assert not resolved.name.endswith(".backup.tpy")
        assert resolved == bundle / "project.tpy"
        assert not resolved.exists()


# --------------------------------------------------------------------------- #
# nested lists (item 3)
# --------------------------------------------------------------------------- #


class TestNestedLists:
    """Selecting a parent list must include items from its child lists."""

    def test_parent_list_includes_child_items(self, tmp_path, monkeypatch):
        tpy = tmp_path / "nested.tpy"
        conn = sqlite3.connect(str(tpy))
        conn.executescript(_TROPY_SCHEMA)
        conn.execute("INSERT INTO lists (list_id, name) VALUES (0, 'ROOT')")
        conn.execute(
            "INSERT INTO lists (list_id, name, parent_list_id) VALUES (1, 'Correspondence', 0)",
        )
        conn.execute(
            "INSERT INTO lists (list_id, name, parent_list_id) VALUES (2, 'Vienna 1937-38', 1)",
        )
        conn.execute(
            "INSERT INTO project (project_id, name, base) VALUES (1, 'Test Project', 'project')",
        )
        for iid in (1, 2):
            conn.execute(
                "INSERT INTO subjects (id, template, type) "
                "VALUES (?, 'https://tropy.org/v1/templates/item', 'item')",
                (iid,),
            )
            conn.execute("INSERT INTO items (id) VALUES (?)", (iid,))
        conn.execute(
            "INSERT INTO list_items (list_id, id, position) VALUES (1, 1, 0)",
        )
        conn.execute(
            "INSERT INTO list_items (list_id, id, position) VALUES (2, 2, 0)",
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )
        items = list_items(tpy, list_id=1)
        assert {it.item_id for it in items} == {1, 2}

    def test_cyclic_parent_chain_terminates(self, tmp_path, monkeypatch):
        """A cyclic parent chain (1 -> 2 -> 1) must terminate, not hang.

        The recursive CTE uses ``UNION`` (not ``UNION ALL``) so the recursion
        dedupes and reaches a fixed point.  Run the query in a daemon thread
        with a join timeout so a regression fails loudly instead of hanging CI
        forever.
        """
        tpy = tmp_path / "cyclic.tpy"
        conn = sqlite3.connect(str(tpy))
        conn.executescript(_TROPY_SCHEMA)
        conn.execute("INSERT INTO lists (list_id, name) VALUES (0, 'ROOT')")
        conn.execute(
            "INSERT INTO lists (list_id, name, parent_list_id) VALUES (1, 'One', 2)",
        )
        conn.execute(
            "INSERT INTO lists (list_id, name, parent_list_id) VALUES (2, 'Two', 1)",
        )
        conn.execute(
            "INSERT INTO project (project_id, name, base) VALUES (1, 'Test Project', 'project')",
        )
        for iid in (1, 2):
            conn.execute(
                "INSERT INTO subjects (id, template, type) "
                "VALUES (?, 'https://tropy.org/v1/templates/item', 'item')",
                (iid,),
            )
            conn.execute("INSERT INTO items (id) VALUES (?)", (iid,))
        conn.execute(
            "INSERT INTO list_items (list_id, id, position) VALUES (1, 1, 0)",
        )
        conn.execute(
            "INSERT INTO list_items (list_id, id, position) VALUES (2, 2, 0)",
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )

        result = {}

        def _run():
            result["items"] = list_items(tpy, list_id=1)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=10.0)
        assert not thread.is_alive(), (
            "list_items() hung on a cyclic parent chain — regression to "
            "UNION ALL in the recursive CTE?"
        )
        assert {it.item_id for it in result["items"]} == {1, 2}


# --------------------------------------------------------------------------- #
# mixed path separators (item 4)
# --------------------------------------------------------------------------- #


class TestMixedPathSeparators:
    """A stored backslash path resolves correctly on POSIX."""

    def test_backslash_path_resolves_on_posix(self, tmp_path):
        db = tmp_path / "test.tpy"
        db.write_text("")
        result = _resolve_photo_path("KV Files\\KV-2-2339.pdf", db, "project")
        assert result == (tmp_path / "KV Files" / "KV-2-2339.pdf").resolve()
        assert result.parent.name == "KV Files"
        assert result.name == "KV-2-2339.pdf"


# --------------------------------------------------------------------------- #
# missing-asset preflight (item 5)
# --------------------------------------------------------------------------- #


class TestMissingAssetPreflight:
    """Missing page counts are surfaced at enqueue time, before a run."""

    def test_missing_asset_count_aggregates(self):
        photos = [
            TropyPhoto(
                photo_id=1,
                path="/tmp/a.png",
                item_id=1,
                page=None,
                mimetype="image/png",
                checksum="a",
                orientation=1,
                missing=True,
            ),
            TropyPhoto(
                photo_id=2,
                path="/tmp/b.png",
                item_id=1,
                page=None,
                mimetype="image/png",
                checksum="b",
                orientation=1,
                missing=False,
            ),
        ]
        item = TropyItem(item_id=1, title="T", photos=photos)
        assert missing_asset_count([item]) == (1, 2)

    def test_enqueue_reports_missing_count(self, tmp_path, monkeypatch):
        client = _client_with_state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "artifice_ocr.tropy_db.validate_absolute_photo",
            _mock_pathcheck,
        )

        tpy = _create_tpy(tmp_path / "test.tpy")
        # Create one of the three photos so two are missing on disk.
        (tmp_path / "fake_photo_1.png").write_bytes(b"x")

        resp = client.post(
            "/api/tropy/browse/enqueue",
            json={
                "path": str(tpy),
                "item_ids": [1, 2],
                "output_dir": "/tmp/output",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["missing"] == 2
        assert data["added"] == 3
