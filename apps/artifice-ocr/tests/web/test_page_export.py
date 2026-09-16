# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Page export route tests: persisted-PAGE copy and download."""

from pathlib import Path

# --------------------------------------------------------------------------- #
# PAGE export
# --------------------------------------------------------------------------- #


def _write_persisted_page(out: Path, stem: str) -> Path:
    """Write a dummy persisted PAGE file at its canonical persistence path.

    The export endpoints copy these files verbatim — they never parse the XML,
    so a stub document is sufficient to exercise the routing surface.
    """
    from artifice_ocr.output import page_path

    path = page_path(out, stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<Page stem='{stem}'/>", encoding="utf-8")
    return path


def test_page_export_start_exports_persisted_pages(client, tmp_path):
    from artifice_ocr.output import page_path

    out = tmp_path / "out"
    _write_persisted_page(out, "page")

    res = client.post(
        "/api/page-export/start",
        json={"output_dir": str(out), "stems": ["page"]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["skipped"] == []
    assert len(body["exported"]) == 1

    exported = Path(body["exported"][0])
    assert exported.is_file()
    assert exported.read_text(encoding="utf-8") == "<Page stem='page'/>"
    # The export is a copy, distinct from the untouched persistence file.
    assert exported.resolve() != page_path(out, "page").resolve()


def test_page_export_start_reports_skipped_as_partial_success(client, tmp_path):
    out = tmp_path / "out"
    _write_persisted_page(out, "page")

    res = client.post(
        "/api/page-export/start",
        json={"output_dir": str(out), "stems": ["page", "missing"]},
    )
    # A missing PAGE file is a partial result, not a failure.
    assert res.status_code == 200
    body = res.json()
    assert body["skipped"] == ["missing"]
    assert len(body["exported"]) == 1


def test_page_export_start_rejects_empty_stems(client, tmp_path):
    out = tmp_path / "out"
    res = client.post(
        "/api/page-export/start",
        json={"output_dir": str(out), "stems": []},
    )
    assert res.status_code == 400


def test_page_export_start_refuses_output_dir_outside_roots(client):
    res = client.post(
        "/api/page-export/start",
        json={"output_dir": "/opt/rejected", "stems": ["page"]},
    )
    assert res.status_code == 400
    assert "outside the directories this server is permitted" in res.json()["detail"].lower()


def test_page_export_download_serves_persisted_page(client, tmp_path):
    out = tmp_path / "out"
    _write_persisted_page(out, "page")

    res = client.get(
        "/api/page-export/download",
        params={"output_dir": str(out), "stem": "page"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/xml")
    assert res.content == b"<Page stem='page'/>"


def test_page_export_download_404_for_missing_page(client, tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)

    res = client.get(
        "/api/page-export/download",
        params={"output_dir": str(out), "stem": "missing"},
    )
    assert res.status_code == 404
    detail = res.json()["detail"]
    assert "No PAGE document found" in detail
    # The resolved absolute path must not leak into the error message.
    assert str(out) not in detail


def test_page_export_download_refuses_output_dir_outside_roots(client):
    res = client.get(
        "/api/page-export/download",
        params={"output_dir": "/opt/rejected", "stem": "page"},
    )
    assert res.status_code == 400
