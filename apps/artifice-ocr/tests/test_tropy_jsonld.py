# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the Tropy JSON-LD bridge — parser, containment, round-trip.

No real Tropy project needed. Every test builds a synthetic JSON-LD file in a
temp directory, with or without backing images as needed.
"""

import json
import os
from pathlib import Path

import pytest
from artifice_ocr.tropy_jsonld import (
    MAX_DEPTH,
    MAX_FILE_BYTES,
    MAX_NODES,
    TROPY_CONTEXT,
    ExportPhoto,
    TropyImportError,
    _note_html,
    build_export,
    disambiguate_stems,
    export_json,
    load_export,
    page_stem,
    photos_to_job_items,
    safe_name,
    stem_discriminator,
    write_manifest,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _make_export(path: Path, payload: dict | list) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _simple_export(tmp_path, photos=None):
    """Create a minimal JSON-LD export with one item."""
    if photos is None:
        photos = [
            {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
        ]
    export = {"@graph": [{"@type": "Item", "title": "Test Item", "photo": photos}]}
    f = tmp_path / "export.json"
    (tmp_path / "a.png").write_bytes(b"x")
    return _make_export(f, export)


# --------------------------------------------------------------------------- #
# parser: shape acceptance
# --------------------------------------------------------------------------- #


def test_accepts_graph_envelope(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "X",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            }
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert preview.export_name == "e.json"


def test_accepts_bare_list(tmp_path):
    export = [
        {
            "@type": "Item",
            "title": "X",
            "photo": [
                {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
            ],
        }
    ]
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert len(preview.items) == 1


def test_accepts_single_object(tmp_path):
    export = {
        "@type": "Item",
        "title": "X",
        "photo": [
            {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
        ],
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert len(preview.items) == 1


def test_accepts_jsonld_suffix(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "X",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            }
        ]
    }
    f = tmp_path / "e.jsonld"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert preview.export_name == "e.jsonld"


# --------------------------------------------------------------------------- #
# parser: title extraction
# --------------------------------------------------------------------------- #


def test_extracts_compact_title(tmp_path):
    f = _simple_export(tmp_path)
    preview = load_export(f)
    assert preview.items[0].title == "Test Item"


def test_extracts_expanded_title(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "http://purl.org/dc/elements/1.1/title": "Expanded Title",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            }
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert preview.items[0].title == "Expanded Title"


def test_title_falls_back_to_index(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            }
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert preview.items[0].title == "Item 1"


def test_extracts_title_from_dc_terms(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "http://purl.org/dc/terms/title": "DC Title",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            }
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert preview.items[0].title == "DC Title"


# --------------------------------------------------------------------------- #
# parser: photo extraction
# --------------------------------------------------------------------------- #


def test_extracts_photos(tmp_path):
    f = _simple_export(tmp_path)
    preview = load_export(f)
    assert len(preview.items[0].photos) == 1
    photo = preview.items[0].photos[0]
    assert photo.path_rel == "a.png"
    assert photo.checksum == "abc"
    assert photo.mimetype == "image/png"
    assert not photo.missing


def test_normalises_single_photo_dict(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "X",
                "photo": {
                    "@type": "Photo",
                    "path": "a.png",
                    "checksum": "abc",
                    "mimetype": "image/png",
                },
            }
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert len(preview.items[0].photos) == 1


def test_skips_non_dict_photo_entries(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "X",
                "photo": [
                    "nope",
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            }
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert len(preview.items[0].photos) == 1
    assert len(preview.warnings) == 1


def test_skips_photo_with_no_path(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "X",
                "photo": [
                    {"@type": "Photo", "checksum": "x", "mimetype": "image/png"},
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            }
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert len(preview.items[0].photos) == 1
    assert len(preview.warnings) >= 1


def test_skips_template_and_list_nodes(tmp_path):
    export = {
        "@graph": [
            {"@type": "Template", "name": "Generic"},
            {"@type": "List", "name": "My List"},
            {
                "@type": "Item",
                "title": "Doc 1",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            },
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert len(preview.items) == 1


def test_reports_missing_files(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Missing",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "gone.jpg",
                        "checksum": "x",
                        "mimetype": "image/jpeg",
                    },
                ],
            }
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    preview = load_export(f)
    assert preview.items[0].photos[0].missing is True


# --------------------------------------------------------------------------- #
# parser: rejections
# --------------------------------------------------------------------------- #


def test_rejects_bad_suffix(tmp_path):
    f = tmp_path / "export.txt"
    f.write_text("{}")
    with pytest.raises(TropyImportError, match="not a JSON-LD file"):
        load_export(f)


def test_rejects_missing_file(tmp_path):
    with pytest.raises(TropyImportError, match="not a file"):
        load_export(tmp_path / "gone.json")


def test_rejects_oversized_file(tmp_path):
    f = tmp_path / "big.json"
    f.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(TropyImportError, match="too large"):
        load_export(f)


def test_rejects_invalid_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json")
    with pytest.raises(TropyImportError, match="Could not parse"):
        load_export(f)


def test_rejects_deeply_nested_json(tmp_path):
    payload = {}
    current = payload
    for _i in range(MAX_DEPTH + 5):
        current["nested"] = {}
        current = current["nested"]
    f = _make_export(tmp_path / "deep.json", payload)
    with pytest.raises(TropyImportError, match="exceeds maximum depth"):
        load_export(f)


def test_rejects_too_many_nodes(tmp_path):
    payload = [{"k": i} for i in range(MAX_NODES + 10)]
    f = tmp_path / "big.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TropyImportError, match="exceeds maximum node count"):
        load_export(f)


# --------------------------------------------------------------------------- #
# parser: path safety
# --------------------------------------------------------------------------- #


def test_rejects_absolute_posix_path(tmp_path):
    """``/etc/passwd`` is rejected by the POSIX blocked-root list.

    The specific rejection reason is a protected system directory (blocklist),
    not the generic "absolute" message from the pre-pathcheck days.
    """
    export = {
        "@graph": [
            {"@type": "Item", "title": "X", "photo": [{"@type": "Photo", "path": "/etc/passwd"}]},
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="protected"):
        load_export(f)


def test_rejects_windows_drive_path(tmp_path):
    """``C:/Windows/secret`` is rejected by the form-driven Windows blocked-root
    list — runs on any host (POSIX or Windows)."""
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "X",
                "photo": [{"@type": "Photo", "path": "C:/Windows/secret"}],
            },
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="protected"):
        load_export(f)


def test_rejects_unc_path(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "X",
                "photo": [{"@type": "Photo", "path": "//server/share/file"}],
            },
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="UNC"):
        load_export(f)


def test_rejects_dotdot_escape(tmp_path):
    export = {
        "@graph": [
            {"@type": "Item", "title": "X", "photo": [{"@type": "Photo", "path": "../secret"}]},
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="escapes"):
        load_export(f)


def test_rejects_symlink_escape(tmp_path):
    export = {
        "@graph": [
            {"@type": "Item", "title": "X", "photo": [{"@type": "Photo", "path": "link"}]},
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    # Create a symlink that resolves outside the export dir
    outside = tmp_path.parent / "escape_file"
    outside.write_text("secret")
    symlink = tmp_path / "link"
    symlink.symlink_to(outside)
    try:
        with pytest.raises(TropyImportError, match="escapes"):
            load_export(f)
    finally:
        symlink.unlink(missing_ok=True)


def test_error_message_never_contains_resolved_path(tmp_path):
    """Error messages must never leak a resolved absolute filesystem path.

    Covers both relative-escape and absolute-blocklisted cases.
    """
    # Relative escape — the original case
    export = {
        "@graph": [
            {"@type": "Item", "title": "X", "photo": [{"@type": "Photo", "path": "../secret"}]},
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    try:
        load_export(f)
    except TropyImportError as exc:
        msg = str(exc)
        assert str(tmp_path.resolve()) not in msg
        assert str(Path.home()) not in msg
    else:
        pytest.fail("Expected TropyImportError")

    # Absolute blocklisted — must also not leak the resolved path
    export2 = {
        "@graph": [
            {"@type": "Item", "title": "Y", "photo": [{"@type": "Photo", "path": "/etc/shadow"}]},
        ]
    }
    f2 = _make_export(tmp_path / "e2.json", export2)
    try:
        load_export(f2)
    except TropyImportError as exc:
        msg2 = str(exc)
        assert str(tmp_path.resolve()) not in msg2
        assert str(Path.home()) not in msg2
    else:
        pytest.fail("Expected TropyImportError for absolute blocklisted path")


# --------------------------------------------------------------------------- #
# parser: absolute photo path acceptance
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(os.name == "nt", reason="POSIX absolute test")
def test_accepts_posix_absolute_path(safe_tmp_path):
    """A POSIX absolute photo path under *safe_tmp_path* imports successfully
    with ``missing=False`` and correct ``resolved``."""
    (safe_tmp_path / "scan.tif").write_bytes(b"TIFF\x00\x00")
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": str(safe_tmp_path / "scan.tif"),
                        "checksum": "abc",
                        "mimetype": "image/tiff",
                    },
                ],
            }
        ]
    }
    f = _make_export(safe_tmp_path / "e.json", export)
    preview = load_export(f)
    assert len(preview.items) == 1
    photo = preview.items[0].photos[0]
    assert not photo.missing
    assert photo.resolved == (safe_tmp_path / "scan.tif").resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX absolute test")
def test_accepts_absolute_missing_file(safe_tmp_path):
    """An absolute path passing all security checks but pointing to a
    non-existent file yields ``missing=True`` — import succeeds, cross-machine
    exports are the primary use case."""
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Missing Remote",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": str(safe_tmp_path / "nowhere.pdf"),
                        "checksum": "x",
                        "mimetype": "application/pdf",
                    },
                ],
            }
        ]
    }
    f = _make_export(safe_tmp_path / "e.json", export)
    preview = load_export(f)
    assert len(preview.items) == 1
    assert preview.items[0].photos[0].missing is True


# --------------------------------------------------------------------------- #
# parser: absolute blocked roots — parametrised
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "blocked_root",
    [
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/var",
        "/sys",
        "/proc",
        "/dev",
        "/boot",
        "/root",
        "/run",
        "/private/etc",
        "/private/var",
    ],
)
def test_rejects_each_posix_blocked_root(tmp_path, blocked_root):
    """Every member of ``POSIX_BLOCKED_ROOTS`` with a ``.tif`` suffix is
    rejected."""
    photo_path = f"{blocked_root}/secret.tif"
    export = {
        "@graph": [
            {"@type": "Item", "title": "X", "photo": [{"@type": "Photo", "path": photo_path}]},
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="protected"):
        load_export(f)


@pytest.mark.parametrize(
    "blocked_root",
    [
        "c:/windows",
        "c:/program files",
        "c:/program files (x86)",
        "c:/programdata",
        "c:/$recycle.bin",
        "c:/system volume information",
    ],
)
def test_rejects_each_windows_blocked_root(tmp_path, blocked_root):
    """Every member of ``WINDOWS_BLOCKED_ROOTS`` with a ``.tif`` suffix is
    rejected — runs on any host (form-driven)."""
    photo_path = f"{blocked_root}/secret.tif"
    export = {
        "@graph": [
            {"@type": "Item", "title": "X", "photo": [{"@type": "Photo", "path": photo_path}]},
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="protected"):
        load_export(f)


# --------------------------------------------------------------------------- #
# parser: blocked home children
# --------------------------------------------------------------------------- #


def test_rejects_credential_store_in_home(safe_tmp_path):
    """Paths under ``~/.ssh/`` etc. are rejected via the home-children
    check, using the injectable *home* parameter."""
    from artifice_ocr._tropy_pathcheck import validate_absolute_photo

    fake_home = safe_tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / ".ssh").mkdir()
    credential = fake_home / ".ssh" / "id_rsa.png"
    credential.write_bytes(b"x")

    with pytest.raises(ValueError, match="protected directory"):
        validate_absolute_photo(str(credential), home=fake_home)


def test_rejects_blocked_home_children_parametrized(safe_tmp_path):
    """A sampling of ``BLOCKED_HOME_CHILDREN`` entries."""
    from artifice_ocr._tropy_pathcheck import validate_absolute_photo

    fake_home = safe_tmp_path / "fakehome"
    fake_home.mkdir()

    for child in (".aws", ".azure", ".kube", "AppData"):
        (fake_home / child).mkdir(exist_ok=True)
        bad = fake_home / child / "secret.png"
        bad.write_bytes(b"x")
        with pytest.raises(ValueError, match="protected directory"):
            validate_absolute_photo(str(bad), home=fake_home)


# --------------------------------------------------------------------------- #
# parser: unsupported extensions
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(os.name == "nt", reason="POSIX absolute test")
@pytest.mark.parametrize(
    "suffix",
    [".pem", ".txt", ""],
)
def test_rejects_unsupported_extension(safe_tmp_path, suffix):
    """Absolute photo paths with ``.pem``, ``.txt`` or no suffix are
    rejected by the extension allowlist."""
    filename = f"file{suffix}"
    (safe_tmp_path / filename).write_bytes(b"x")
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "X",
                "photo": [{"@type": "Photo", "path": str(safe_tmp_path / filename)}],
            },
        ]
    }
    f = _make_export(safe_tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="Unsupported file type"):
        load_export(f)


# --------------------------------------------------------------------------- #
# parser: null byte / max chars
# --------------------------------------------------------------------------- #


def test_rejects_null_byte_in_path(tmp_path):
    """A photo path containing ``\\x00`` is rejected immediately."""
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "X",
                "photo": [{"@type": "Photo", "path": "/tmp/x\x00.png"}],
            },
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="null byte"):
        load_export(f)


def test_rejects_overlong_path(tmp_path):
    """A photo path exceeding ``MAX_PATH_CHARS`` is rejected immediately."""
    from artifice_ocr._tropy_pathcheck import MAX_PATH_CHARS

    long_path = "/tmp/" + "a" * (MAX_PATH_CHARS - 4) + ".tif"
    export = {
        "@graph": [
            {"@type": "Item", "title": "X", "photo": [{"@type": "Photo", "path": long_path}]},
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="too long"):
        load_export(f)


# --------------------------------------------------------------------------- #
# parser: symlink policy
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(os.name == "nt", reason="symlink test")
def test_rejects_symlink_to_blocked_root(tmp_path):
    """A symlink pointing to ``/etc/...`` is rejected by the re-validation
    of the resolved target against the blocklist."""
    # Create a file in a legit location, then symlink it to the blocked target
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    legit = tmp_path / "legit" / "photo.tif"
    legit.parent.mkdir(parents=True, exist_ok=True)
    legit.write_bytes(b"TIFF\x00\x00")

    # Create a symlink that points into /etc (blocked)
    evil_link = tmp_path / "evil_link"
    evil_link.symlink_to(Path("/etc/shadow"))

    export = {
        "@graph": [
            {"@type": "Item", "title": "X", "photo": [{"@type": "Photo", "path": str(evil_link)}]},
        ]
    }
    f = _make_export(export_dir / "e.json", export)
    with pytest.raises(TropyImportError, match="protected"):
        load_export(f)


@pytest.mark.skipif(os.name == "nt", reason="symlink test")
def test_accepts_symlink_to_legit_file_with_warning(safe_tmp_path):
    """A symlink pointing to a legitimate file under *safe_tmp_path* is accepted
    WITH a followed-symlink warning in ``preview.warnings``."""
    export_dir = safe_tmp_path / "exports"
    export_dir.mkdir()
    real = safe_tmp_path / "real" / "image.tif"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"TIFF\x00\x00")

    link = safe_tmp_path / "link"
    link.symlink_to(real)

    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Symlink Test",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": str(link),
                        "checksum": "abc",
                        "mimetype": "image/tiff",
                    }
                ],
            },
        ]
    }
    f = _make_export(export_dir / "e.json", export)
    preview = load_export(f)
    assert len(preview.items) == 1
    photo = preview.items[0].photos[0]
    assert not photo.missing
    assert photo.resolved == real.resolve()
    assert any("symbolic link" in w for w in preview.warnings), (
        f"Expected symlink warning in: {preview.warnings}"
    )


# --------------------------------------------------------------------------- #
# closed-vocabulary tests
# --------------------------------------------------------------------------- #


def test_pathcheck_frozensets_closed():
    """Assert exact membership of all four frozensets in ``_tropy_pathcheck``.
    Mirrors the ``PERMITTED_BADGES`` discipline."""
    from artifice_ocr._tropy_pathcheck import (
        BLOCKED_HOME_CHILDREN,
        MEDIA_SUFFIXES,
        POSIX_BLOCKED_ROOTS,
        WINDOWS_BLOCKED_ROOTS,
    )

    assert frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}) == MEDIA_SUFFIXES

    assert (
        frozenset(
            {
                "/etc",
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
                "/var",
                "/sys",
                "/proc",
                "/dev",
                "/boot",
                "/root",
                "/run",
                "/private/etc",
                "/private/var",
            }
        )
        == POSIX_BLOCKED_ROOTS
    )

    assert (
        frozenset(
            {
                "c:/windows",
                "c:/program files",
                "c:/program files (x86)",
                "c:/programdata",
                "c:/$recycle.bin",
                "c:/system volume information",
            }
        )
        == WINDOWS_BLOCKED_ROOTS
    )

    assert (
        frozenset({".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", "AppData"})
        == BLOCKED_HOME_CHILDREN
    )


# --------------------------------------------------------------------------- #
# parser: content-based import (drag-and-drop)
# --------------------------------------------------------------------------- #


def test_content_import_round_trips_same_as_path_import(safe_tmp_path):
    """Preview via ``content`` yields the same items/warnings as the same
    payload imported via ``path`` — when using absolute photo paths so both
    imports can resolve them."""
    from artifice_ocr.tropy_jsonld import load_export_content

    photo = safe_tmp_path / "a.png"
    photo.write_bytes(b"x")

    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": str(photo),
                        "checksum": "abc",
                        "mimetype": "image/png",
                    },
                ],
            }
        ]
    }
    f = safe_tmp_path / "e.json"
    _make_export(f, export)

    # via path
    preview_path = load_export(f)

    # via content
    text = f.read_text(encoding="utf-8")
    preview_content = load_export_content(text, filename="e.json")

    assert preview_path.export_name == preview_content.export_name
    assert len(preview_path.items) == len(preview_content.items)
    for pi, ci in zip(preview_path.items, preview_content.items, strict=True):
        assert pi.title == ci.title
        assert len(pi.photos) == len(ci.photos)
        for pp, cp in zip(pi.photos, ci.photos, strict=True):
            assert pp.path_rel == cp.path_rel
            assert pp.mimetype == cp.mimetype
            assert pp.checksum == cp.checksum
            assert pp.resolved == cp.resolved
            assert pp.missing == cp.missing


def test_content_import_skips_relative_photos(tmp_path):
    """When ``export_dir=None`` (content import), relative photo paths are
    skipped with a warning."""
    from artifice_ocr.tropy_jsonld import load_export_content

    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Test Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "relative_photo.jpg",
                        "checksum": "abc",
                        "mimetype": "image/jpeg",
                    },
                ],
            }
        ]
    }
    text = json.dumps(export)
    preview = load_export_content(text, filename="export.json")

    assert len(preview.items) == 0
    assert any("relative path" in w and "save the export to disk" in w for w in preview.warnings), (
        f"Expected relative-path warning in: {preview.warnings}"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX absolute test")
def test_content_import_accepts_absolute_photos(safe_tmp_path):
    """Content import accepts absolute photo paths — validated by pathcheck."""
    from artifice_ocr.tropy_jsonld import load_export_content

    photo = safe_tmp_path / "scan.tif"
    photo.write_bytes(b"TIFF\x00\x00")

    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Abs Photo",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": str(photo),
                        "checksum": "abc",
                        "mimetype": "image/tiff",
                    },
                ],
            }
        ]
    }
    text = json.dumps(export)
    preview = load_export_content(text, filename="dropped-export.jsonld")

    assert len(preview.items) == 1
    assert not preview.items[0].photos[0].missing
    assert preview.items[0].photos[0].resolved == photo.resolve()


# --------------------------------------------------------------------------- #
# parser: group-id determinism
# --------------------------------------------------------------------------- #


def test_group_ids_are_stable_across_reparses(tmp_path):
    """Group IDs based on SHA-256 are stable across re-parses — the same
    export file yields the same group IDs every time."""
    f = _simple_export(tmp_path)
    p1 = load_export(f)
    p2 = load_export(f)
    assert p1.items[0].group == p2.items[0].group


# --------------------------------------------------------------------------- #
# parser: rollback feature flag
# --------------------------------------------------------------------------- #


def test_relative_only_flag_rejects_absolute(monkeypatch, tmp_path):
    """When ``ARTIFICE_OCR_TROPY_RELATIVE_ONLY=1``, absolute photo paths
    hit the old raise-on-absolute branch."""
    import artifice_ocr.tropy_jsonld as tjl

    # Patch the module-level constant directly — importlib.reload would
    # create a new TropyImportError class, breaking except clauses in
    # other modules that already imported from tropy_jsonld.
    monkeypatch.setattr(tjl, "_RELATIVE_ONLY", True)

    export = {
        "@graph": [
            {"@type": "Item", "title": "X", "photo": [{"@type": "Photo", "path": "/tmp/x.tif"}]},
        ]
    }
    f = _make_export(tmp_path / "e.json", export)
    with pytest.raises(TropyImportError, match="absolute"):
        tjl.load_export(f)


# --------------------------------------------------------------------------- #
# parser: backslash normalisation
# --------------------------------------------------------------------------- #


def test_normalises_backslash_paths(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Backslash",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": r"assets\a.png",
                        "checksum": "abc",
                        "mimetype": "image/png",
                    },
                ],
            }
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)
    assert preview.items[0].photos[0].path_rel == "assets/a.png"
    assert not preview.items[0].photos[0].missing


# --------------------------------------------------------------------------- #
# parser: pdf page handling
# --------------------------------------------------------------------------- #


def test_pdf_page_is_non_null(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "PDF Item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "doc.pdf",
                        "checksum": "x",
                        "mimetype": "application/pdf",
                        "page": 2,
                    }
                ],
            }
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "doc.pdf").write_bytes(b"%PDF")
    _make_export(f, export)
    preview = load_export(f)
    assert preview.items[0].photos[0].page == 2


def test_image_page_is_none(tmp_path):
    f = _simple_export(tmp_path)
    preview = load_export(f)
    assert preview.items[0].photos[0].page is None


# --------------------------------------------------------------------------- #
# page_stem
# --------------------------------------------------------------------------- #


def test_page_stem_pdf():
    stem = page_stem("My Item", "doc.pdf", 41, "application/pdf", Path("doc.pdf"))
    assert stem == "My Item/doc_p0042"


def test_page_stem_image():
    stem = page_stem("Photos", "img001.jpg", None, "image/jpeg", Path("img001.jpg"))
    assert stem == "Photos/img001"


def test_safe_name_filters_unsafe_chars():
    assert safe_name('KV/2: "file"?') == "KV_2_ _file__"
    assert safe_name("") == "untitled"
    assert safe_name("   ") == "untitled"


# --------------------------------------------------------------------------- #
# stem_discriminator / disambiguate_stems
# --------------------------------------------------------------------------- #


def test_stem_discriminator_prefers_checksum():
    assert stem_discriminator(checksum="abcdefghijklmnop") == "abcdefghij"  # first 10 chars


def test_stem_discriminator_falls_back_to_photo_id_without_checksum():
    assert stem_discriminator(checksum="", photo_id=42) == "id42"


def test_stem_discriminator_falls_back_to_path_hash_last():
    disc = stem_discriminator(checksum="", photo_id=None, path_rel="a/b/page1.jpg")
    assert disc  # non-empty
    assert disc == stem_discriminator(checksum="", photo_id=None, path_rel="a/b/page1.jpg")
    assert disc != stem_discriminator(checksum="", photo_id=None, path_rel="c/d/page1.jpg")


def test_disambiguate_stems_leaves_first_occurrence_untouched():
    stems = ["Item/page1", "Item/page1", "Item/page2"]
    discs = ["chk1", "chk2", "chk3"]
    result = disambiguate_stems(stems, discs)
    assert result[0] == "Item/page1"
    assert result[1] != "Item/page1"
    assert result[1].startswith("Item/page1")
    assert result[2] == "Item/page2"


def test_disambiguate_stems_is_stable_not_positional():
    """The discriminator must come from the photo's own stable identity, not
    from where it sits in the batch — the same (stem, discriminator) pairs
    in a different order must still resolve to the same final stems."""
    stems_a = ["X/p", "X/p"]
    discs_a = ["aaa", "bbb"]
    stems_b = ["X/p", "X/p"]
    discs_b = ["bbb", "aaa"]
    result_a = disambiguate_stems(stems_a, discs_a)
    result_b = disambiguate_stems(stems_b, discs_b)
    # First occurrence always wins the bare stem regardless of which
    # discriminator arrives first; the *set* of final stems is the same.
    assert result_a[0] == result_b[0] == "X/p"
    assert set(result_a) == {"X/p", "X/p__bbb"}
    assert set(result_b) == {"X/p", "X/p__aaa"}


# --------------------------------------------------------------------------- #
# photos_to_job_items
# --------------------------------------------------------------------------- #


def test_pages_to_job_items_carry_origin(tmp_path):
    f = _simple_export(tmp_path)
    preview = load_export(f)
    items = photos_to_job_items(preview)
    assert len(items) == 1
    assert items[0].source["origin"] == "tropy-jsonld"
    assert items[0].source["item_title"] == "Test Item"
    assert items[0].source["tropy_group"] is not None
    assert items[0].source["photo_path_rel"] == "a.png"


def test_colliding_stems_get_a_stable_discriminator_on_the_second_photo(tmp_path):
    """Two different non-PDF photos sharing an item title and filename
    collide on `page_stem` (it only disambiguates PDF pages). The batch
    mapper must give the SECOND photo a distinct stem while leaving the
    FIRST byte-identical to plain `page_stem` output — that's the contract
    that keeps every already-OCR'd file on disk still matching on resume.
    """
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Letters",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "a/page1.jpg",
                        "checksum": "chk1",
                        "mimetype": "image/jpeg",
                    },
                ],
            },
            {
                "@type": "Item",
                "title": "Letters",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "b/page1.jpg",
                        "checksum": "chk2",
                        "mimetype": "image/jpeg",
                    },
                ],
            },
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "page1.jpg").write_bytes(b"x")
    (tmp_path / "b" / "page1.jpg").write_bytes(b"y")
    _make_export(f, export)
    preview = load_export(f)

    items = photos_to_job_items(preview)
    assert len(items) == 2

    expected_first = page_stem("Letters", "page1.jpg", None, "image/jpeg", Path("page1.jpg"))
    assert expected_first == "Letters/page1"
    assert items[0].output_stem == expected_first  # regression guard: byte-identical
    assert items[1].output_stem != items[0].output_stem
    assert items[1].output_stem.startswith(expected_first)


def test_non_colliding_stems_are_left_untouched(tmp_path):
    """No collision in the batch -> stems pass through exactly as `page_stem`
    would produce them, for every photo, not just the first."""
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Diary",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "one.jpg",
                        "checksum": "c1",
                        "mimetype": "image/jpeg",
                    },
                    {
                        "@type": "Photo",
                        "path": "two.jpg",
                        "checksum": "c2",
                        "mimetype": "image/jpeg",
                    },
                ],
            },
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "one.jpg").write_bytes(b"x")
    (tmp_path / "two.jpg").write_bytes(b"y")
    _make_export(f, export)
    preview = load_export(f)

    items = photos_to_job_items(preview)
    assert items[0].output_stem == "Diary/one"
    assert items[1].output_stem == "Diary/two"


def test_pages_to_job_items_can_filter_by_group(tmp_path):
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Keep",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            },
            {
                "@type": "Item",
                "title": "Skip",
                "photo": [
                    {"@type": "Photo", "path": "a.png", "checksum": "abc", "mimetype": "image/png"},
                ],
            },
        ]
    }
    f = tmp_path / "e.json"
    (tmp_path / "a.png").write_bytes(b"x")
    _make_export(f, export)
    preview = load_export(f)

    # Filter to just the first item's group
    group0 = preview.items[0].group
    filtered = photos_to_job_items(preview, groups=[group0])
    assert len(filtered) == 1
    assert filtered[0].source["tropy_group"] == group0


# --------------------------------------------------------------------------- #
# export: note HTML
# --------------------------------------------------------------------------- #


def test_note_html_escapes_tags():
    html = _note_html("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_note_html_wraps_lines_in_paragraphs():
    html = _note_html("line one\nline two")
    assert "<p>line one</p>" in html
    assert "<p>line two</p>" in html


def test_note_html_handles_empty_text():
    html = _note_html("")
    assert "<p>" in html


# --------------------------------------------------------------------------- #
# export: build_export
# --------------------------------------------------------------------------- #


def test_build_export_produces_valid_structure(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    photos = [
        ExportPhoto(
            abs_path=f,
            text="Der Bericht ist fertig.",
            label="doc.pdf  p.1",
            language="de",
            item_node={
                "@type": "Item",
                "title": "Doc",
                "photo": [{"@type": "Photo", "path": "doc.pdf"}],
            },
            group="abc:0",
            photo_index=0,
            path_rel="doc.pdf",
            checksum="abc123",
            mimetype="application/pdf",
        ),
    ]
    doc = build_export(photos)
    assert "@context" in doc
    assert "@graph" in doc
    assert "generator" in doc
    assert len(doc["@graph"]) >= 1

    item = doc["@graph"][0]
    assert item["@type"] == "Item"
    assert "photo" in item
    assert len(item["photo"]) >= 1


def test_build_export_preserves_item_metadata(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    original_node = {
        "@type": "Item",
        "title": "Original Title",
        "template": "https://tropy.org/v1/templates/generic#item",
        "tag": [{"@type": "Tag", "name": "important"}],
    }
    photos = [
        ExportPhoto(
            abs_path=f,
            text="Some text",
            label="doc.pdf p.1",
            language="de",
            item_node=original_node,
            group="abc:0",
            photo_index=0,
            path_rel="doc.pdf",
            checksum="abc",
            mimetype="application/pdf",
        ),
    ]
    doc = build_export(photos)
    item = doc["@graph"][0]
    assert item["title"] == "Original Title"
    assert item["template"].endswith("generic#item")
    assert "tag" in item


def test_build_export_includes_note_with_text_and_html(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    photos = [
        ExportPhoto(
            abs_path=f,
            text="Der Bericht\nZweiter Absatz",
            label="doc.pdf p.1",
            language="de",
            item_node={"@type": "Item", "title": "Doc"},
            group="abc:0",
            photo_index=0,
            path_rel="doc.pdf",
            checksum="abc",
            mimetype="application/pdf",
        ),
    ]
    doc = build_export(photos)
    note = doc["@graph"][0]["photo"][0]["note"][0]
    assert note["text"] == {"@value": "Der Bericht\nZweiter Absatz", "@language": "de"}
    assert "<p>Der Bericht</p>" in note["html"]["@value"]
    assert "<p>Zweiter Absatz</p>" in note["html"]["@value"]
    assert note["html"]["@language"] == "de"


def test_ad_hoc_photos_produce_minimal_nodes(tmp_path):
    f = tmp_path / "scan.png"
    f.write_bytes(b"PNG\x00")
    photos = [
        ExportPhoto(
            abs_path=f,
            text="Some OCR text",
            label="scan.png",
            language="de",
            item_node=None,
            group=None,
            photo_index=None,
            path_rel=None,
            checksum="",
            mimetype="",
        ),
    ]
    doc = build_export(photos)
    assert len(doc["@graph"]) == 1
    item = doc["@graph"][0]
    assert item["@type"] == "Item"
    assert item["title"] == "scan"
    assert item["template"] == "https://tropy.org/v1/templates/generic"
    assert item["photo"][0]["template"] == "https://tropy.org/v1/templates/photo"
    assert len(item["photo"]) == 1


def test_build_export_preserves_photo_metadata_and_existing_notes(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    original_note = {"@type": "Note", "text": "Research note"}
    photo_node = {
        "@type": "Photo",
        "path": "doc.pdf",
        "page": 4,
        "protocol": "file",
        "template": "https://tropy.org/v1/templates/photo",
        "orientation": 6,
        "selection": [{"@type": "Selection", "x": 10}],
        "note": [original_note],
    }
    doc = build_export(
        [
            ExportPhoto(
                abs_path=f,
                text="OCR text",
                label="doc.pdf p.5",
                language="en",
                item_node={"@type": "Item", "title": "Doc", "photo": [photo_node]},
                group="g",
                photo_index=0,
                path_rel="doc.pdf",
                checksum="abc",
                mimetype="application/pdf",
                page=4,
            )
        ]
    )
    photo = doc["@graph"][0]["photo"][0]
    assert photo["page"] == 4
    assert photo["orientation"] == 6
    assert photo["selection"] == [{"@type": "Selection", "x": 10}]
    assert photo["note"][0] == original_note
    assert photo["note"][1]["text"]["@value"] == "OCR text"


def test_ad_hoc_pdf_retains_every_page(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    photos = [
        ExportPhoto(
            abs_path=f,
            text=f"Page {page + 1}",
            label=f"doc.pdf p.{page + 1}",
            language="en",
            item_node=None,
            group=None,
            photo_index=None,
            path_rel=None,
            checksum="abc",
            mimetype="application/pdf",
            page=page,
        )
        for page in (0, 1)
    ]
    item = build_export(photos)["@graph"][0]
    assert [photo["page"] for photo in item["photo"]] == [0, 1]
    assert [photo["note"][0]["text"]["@value"] for photo in item["photo"]] == [
        "Page 1",
        "Page 2",
    ]


def test_export_json_produces_indented_json(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    photos = [
        ExportPhoto(
            abs_path=f,
            text="text",
            label="l",
            language="de",
            item_node={"@type": "Item", "title": "Doc"},
            group="g",
            photo_index=0,
            path_rel="d",
            checksum="c",
            mimetype="m",
        ),
    ]
    s = export_json(photos)
    assert s.startswith("{")
    data = json.loads(s)
    assert "generator" in data


# --------------------------------------------------------------------------- #
# round-trip: import → export → re-import
# --------------------------------------------------------------------------- #


def test_round_trip_preserves_title(tmp_path):
    # Create an export with a real image
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    export = {
        "@graph": [
            {
                "@type": "Item",
                "title": "Round Trip Doc",
                "template": "https://tropy.org/v1/templates/generic#item",
                "photo": [
                    {
                        "@type": "Photo",
                        "path": "doc.pdf",
                        "checksum": "abc123",
                        "mimetype": "application/pdf",
                        "page": 0,
                    }
                ],
            }
        ]
    }
    ef = _make_export(tmp_path / "export.json", export)
    preview = load_export(ef)
    assert preview.items[0].title == "Round Trip Doc"

    # Build export from the imported data
    photos = []
    for item in preview.items:
        for photo in item.photos:
            photos.append(
                ExportPhoto(
                    abs_path=photo.resolved,
                    text="Some OCR result",
                    label=item.label,
                    language="de",
                    item_node=photo.item_node,
                    group=photo.group,
                    photo_index=photo.photo_index,
                    path_rel=photo.path_rel,
                    checksum=photo.checksum,
                    mimetype=photo.mimetype,
                )
            )
    doc = build_export(photos)
    item = doc["@graph"][0]
    # The title survives the round-trip via the compact "title" key
    assert item["title"] == "Round Trip Doc"
    assert item["template"].endswith("generic#item")


# --------------------------------------------------------------------------- #
# manifest writer
# --------------------------------------------------------------------------- #


def test_write_manifest_creates_file(tmp_path):
    f = _simple_export(tmp_path)
    preview = load_export(f)
    out = tmp_path / "output"
    manifest = write_manifest(out, preview)
    assert manifest is not None
    assert manifest.exists()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert "export" in data
    assert "pages" in data
    assert data["export"]["name"] == "export.json"


def test_write_manifest_handles_nonexistent_dir(tmp_path):
    f = _simple_export(tmp_path)
    preview = load_export(f)
    out = tmp_path / "deeply" / "nested" / "output"
    manifest = write_manifest(out, preview)
    assert manifest is not None
    assert manifest.exists()


def test_write_manifest_merges_across_runs(tmp_path):
    f = _simple_export(tmp_path)
    preview = load_export(f)
    out = tmp_path / "output"
    write_manifest(out, preview)

    # Second call should merge, not overwrite
    manifest2 = write_manifest(out, preview)
    assert manifest2 is not None
    data = json.loads(manifest2.read_text(encoding="utf-8"))
    assert len(data["pages"]) >= 1


# --------------------------------------------------------------------------- #
# context completeness
# --------------------------------------------------------------------------- #


def test_tropy_context_has_required_keys():
    assert "@version" in TROPY_CONTEXT
    # The JSON-LD keyword is numeric. Tropy rejects the string "1.1".
    assert TROPY_CONTEXT["@version"] == 1.1
    assert "@vocab" in TROPY_CONTEXT
    assert "photo" in TROPY_CONTEXT
    assert "note" in TROPY_CONTEXT
