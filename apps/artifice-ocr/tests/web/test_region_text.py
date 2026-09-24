# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Region-aware PAGE correction, whole-page text routes, and segmentation capabilities."""

from artifice_ocr import config
from artifice_ocr.web import runtime

# --------------------------------------------------------------------------- #
# whole-page text routes — unchanged for segmentation-disabled items
# --------------------------------------------------------------------------- #


def test_whole_page_text_routes_unchanged_for_segmentation_disabled_item(client, tmp_path):
    """The legacy .txt/JSON editing surface keeps working exactly as before for
    a segmentation-disabled item, and never touches its authoritative PAGE
    document (the single implicit region)."""
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.output import page_path
    from artifice_ocr.pagexml import PageDocument, TextRegion, artifice_custom, write

    output_dir = tmp_path / "output"
    source = tmp_path / "page.png"
    source.write_bytes(b"x")

    item = JobItem(path=str(source))
    runtime.state.add_items([item])
    item_id = runtime.state.queue_snapshot()[-1]["id"]
    item.results = {
        "raw": {"extracted_text": "raw"},
        "cleaned": {"cleaned_text": "cleaned"},
        "translated": {"translated_text": "translated"},
    }

    # A segmentation-disabled document: one implicit full-page region.
    implicit = PageDocument(
        image_filename="page.png",
        image_width=100,
        image_height=100,
        regions=[
            TextRegion(
                id="region-0001",
                type="paragraph",
                custom=artifice_custom("unclassified"),
                polygon=[(0, 0), (100, 0), (100, 100), (0, 100)],
            )
        ],
        reading_order=["region-0001"],
    )
    implicit.regions[0].set_stage_text("raw", "raw")
    write(implicit, page_path(output_dir, item.stem))
    page_bytes_before = page_path(output_dir, item.stem).read_bytes()

    for stage_name in ("raw_ocr", "cleaned", "translated"):
        (output_dir / stage_name / "text").mkdir(parents=True)
    (output_dir / "raw_ocr" / "text" / f"{item.stem}.txt").write_text("raw", encoding="utf-8")
    (output_dir / "cleaned" / "text" / f"{item.stem}.txt").write_text("cleaned", encoding="utf-8")
    (output_dir / "translated" / "text" / f"{item.stem}.txt").write_text(
        "translated", encoding="utf-8"
    )

    config.apply_overrides({"output_dir": str(output_dir)})

    assert (
        client.post(f"/api/queue/{item_id}/raw-text", json={"text": "raw edited"}).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/queue/{item_id}/cleaned-text", json={"text": "cleaned edited"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/queue/{item_id}/translated-text", json={"text": "translated edited"}
        ).status_code
        == 200
    )

    assert (output_dir / "raw_ocr" / "text" / f"{item.stem}.txt").read_text(
        encoding="utf-8"
    ) == "raw edited"
    assert (output_dir / "cleaned" / "text" / f"{item.stem}.txt").read_text(
        encoding="utf-8"
    ) == "cleaned edited"
    assert (output_dir / "translated" / "text" / f"{item.stem}.txt").read_text(
        encoding="utf-8"
    ) == "translated edited"

    # The authoritative PAGE document is untouched by the legacy surface.
    assert page_path(output_dir, item.stem).read_bytes() == page_bytes_before


# --------------------------------------------------------------------------- #
# region-aware PAGE correction (Commit 7)
# --------------------------------------------------------------------------- #


def _seed_region_page(output_dir, stem):
    """Persist a three-region PAGE document and return (ids, polygons).

    ``region-0002`` also carries cleaned and translated stages, so the
    index-preservation assertions can prove an edit leaves other stages alone.
    """
    from artifice_ocr.output import page_path
    from artifice_ocr.pagexml import PageDocument, TextRegion, artifice_custom, write

    def _region(rid, y, raw):
        region = TextRegion(
            id=rid,
            type="other",
            custom=artifice_custom("unclassified"),
            polygon=[(0, y), (100, y), (100, y + 20), (0, y + 20)],
        )
        region.set_stage_text("raw", raw)
        return region

    top = _region("region-0001", 0, "top text")
    middle = _region("region-0002", 30, "middle text")
    middle.set_stage_text("cleaned", "middle cleaned")
    middle.set_stage_text("translated", "middle translated")
    bottom = _region("region-0003", 60, "bottom text")

    doc = PageDocument(
        image_filename=f"{stem}.png",
        image_width=100,
        image_height=100,
        regions=[top, middle, bottom],
        reading_order=["region-0001", "region-0002", "region-0003"],
    )
    write(doc, page_path(output_dir, stem))
    return [r.id for r in doc.regions], {r.id: list(r.polygon) for r in doc.regions}


def _add_item_and_set_output(tmp_path, stem="page"):
    """Add a queue item whose stem is ``page`` and point the output dir at
    ``tmp_path``. Returns the item id."""
    from artifice_ocr.jobs import JobItem

    source = tmp_path / f"{stem}.png"
    source.write_bytes(b"x")
    item = JobItem(path=str(source))
    runtime.state.add_items([item])
    item_id = runtime.state.queue_snapshot()[-1]["id"]
    config.apply_overrides({"output_dir": str(tmp_path)})
    return item_id


def test_region_text_edit_updates_only_the_selected_index(client, tmp_path):
    from artifice_ocr.output import load_page

    item_id = _add_item_and_set_output(tmp_path)
    _seed_region_page(tmp_path, "page")

    res = client.post(
        f"/api/queue/{item_id}/region/region-0002/text",
        json={"stage": "raw", "text": "middle text (edited)"},
    )
    assert res.status_code == 200
    assert res.json() == {
        "ok": True,
        "region_id": "region-0002",
        "stage": "raw",
        "text": "middle text (edited)",
    }

    doc = load_page(tmp_path, "page")
    by_id = {r.id: r for r in doc.regions}
    # The selected index changed…
    assert by_id["region-0002"].stage_text("raw") == "middle text (edited)"
    # …and every other stage on that region, plus every other region, survived.
    assert by_id["region-0002"].stage_text("cleaned") == "middle cleaned"
    assert by_id["region-0002"].stage_text("translated") == "middle translated"
    assert by_id["region-0001"].stage_text("raw") == "top text"
    assert by_id["region-0003"].stage_text("raw") == "bottom text"
    assert len(doc.regions) == 3


def test_region_text_edit_unknown_region_is_404(client, tmp_path):
    item_id = _add_item_and_set_output(tmp_path)
    _seed_region_page(tmp_path, "page")

    res = client.post(
        f"/api/queue/{item_id}/region/region-9999/text",
        json={"stage": "raw", "text": "x"},
    )
    assert res.status_code == 404
    assert "region-9999" in res.json()["detail"]


def test_region_text_edit_unknown_stage_is_400(client, tmp_path):
    item_id = _add_item_and_set_output(tmp_path)
    _seed_region_page(tmp_path, "page")

    res = client.post(
        f"/api/queue/{item_id}/region/region-0001/text",
        json={"stage": "bogus", "text": "x"},
    )
    assert res.status_code == 400
    assert "stage" in res.json()["detail"].lower()


def test_region_text_edit_without_page_is_404(client, tmp_path):
    item_id = _add_item_and_set_output(tmp_path)  # no PAGE document written

    res = client.post(
        f"/api/queue/{item_id}/region/region-0001/text",
        json={"stage": "raw", "text": "x"},
    )
    assert res.status_code == 404


def test_region_delete_removes_region_and_reading_order_reference(client, tmp_path):
    from artifice_ocr.output import load_page

    item_id = _add_item_and_set_output(tmp_path)
    _seed_region_page(tmp_path, "page")

    res = client.delete(f"/api/queue/{item_id}/region/region-0002")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "region_id": "region-0002"}

    doc = load_page(tmp_path, "page")
    assert [r.id for r in doc.regions] == ["region-0001", "region-0003"]
    assert doc.reading_order == ["region-0001", "region-0003"]


def test_region_delete_unknown_region_is_404(client, tmp_path):
    item_id = _add_item_and_set_output(tmp_path)
    _seed_region_page(tmp_path, "page")

    res = client.delete(f"/api/queue/{item_id}/region/region-9999")
    assert res.status_code == 404


def test_region_delete_without_page_is_404(client, tmp_path):
    item_id = _add_item_and_set_output(tmp_path)

    res = client.delete(f"/api/queue/{item_id}/region/region-0001")
    assert res.status_code == 404


def test_region_reorder_changes_reading_order_but_never_geometry(client, tmp_path):
    from artifice_ocr.output import load_page

    item_id = _add_item_and_set_output(tmp_path)
    _, original_polygons = _seed_region_page(tmp_path, "page")
    new_order = ["region-0003", "region-0001", "region-0002"]

    res = client.post(f"/api/queue/{item_id}/regions/reorder", json={"region_ids": new_order})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "reading_order": new_order}

    doc = load_page(tmp_path, "page")
    assert doc.reading_order == new_order
    # Region count and every polygon are unchanged — only reading order moved.
    assert {r.id for r in doc.regions} == set(original_polygons)
    for region in doc.regions:
        assert list(region.polygon) == original_polygons[region.id]


def test_region_reorder_rejects_ids_that_do_not_match_the_page(client, tmp_path):
    item_id = _add_item_and_set_output(tmp_path)
    _seed_region_page(tmp_path, "page")

    # Missing a region id
    res = client.post(
        f"/api/queue/{item_id}/regions/reorder",
        json={"region_ids": ["region-0001", "region-0002"]},
    )
    assert res.status_code == 400

    # Duplicated id, also missing one — a mismatch against the set alone,
    # not a real test of duplicate-detection in isolation (see the case below).
    res = client.post(
        f"/api/queue/{item_id}/regions/reorder",
        json={"region_ids": ["region-0001", "region-0001", "region-0002"]},
    )
    assert res.status_code == 400

    # Unknown id
    res = client.post(
        f"/api/queue/{item_id}/regions/reorder",
        json={"region_ids": ["region-0001", "region-0002", "region-9999"]},
    )
    assert res.status_code == 400

    # Duplicated id that still covers every real id (4 entries for 3 regions):
    # set(region_ids) == the actual id set, so a length-only-blind check would
    # wrongly accept this and persist a corrupted, duplicate-containing
    # reading_order.
    res = client.post(
        f"/api/queue/{item_id}/regions/reorder",
        json={
            "region_ids": [
                "region-0001",
                "region-0001",
                "region-0002",
                "region-0003",
            ]
        },
    )
    assert res.status_code == 400


def test_region_routes_404_for_unknown_item(client):
    assert (
        client.post(
            "/api/queue/does-not-exist/region/region-0001/text",
            json={"stage": "raw", "text": "x"},
        ).status_code
        == 404
    )
    assert client.delete("/api/queue/does-not-exist/region/region-0001").status_code == 404
    assert (
        client.post(
            "/api/queue/does-not-exist/regions/reorder",
            json={"region_ids": ["region-0001"]},
        ).status_code
        == 404
    )


def test_get_regions_returns_regions_in_reading_order(client, tmp_path):
    item_id = _add_item_and_set_output(tmp_path)
    _, polygons = _seed_region_page(tmp_path, "page")
    # The fixture returns the in-memory model form (list of tuples); the API
    # serialises each point as a JSON array, so normalise before comparing.
    polygons = {rid: [list(p) for p in pts] for rid, pts in polygons.items()}

    res = client.get(f"/api/queue/{item_id}/regions")
    assert res.status_code == 200
    body = res.json()

    assert body["reading_order"] == ["region-0001", "region-0002", "region-0003"]
    assert [r["id"] for r in body["regions"]] == [
        "region-0001",
        "region-0002",
        "region-0003",
    ]

    top, middle, bottom = body["regions"]
    # region-0001 carries only a raw stage — the other two must be null, not "".
    assert top == {
        "id": "region-0001",
        "type": "other",
        "polygon": polygons["region-0001"],
        "confidence": None,
        "raw": "top text",
        "cleaned": None,
        "translated": None,
        "error": None,
    }
    assert middle["polygon"] == polygons["region-0002"]
    assert middle["raw"] == "middle text"
    assert middle["cleaned"] == "middle cleaned"
    assert middle["translated"] == "middle translated"
    assert middle["error"] is None
    assert bottom["polygon"] == polygons["region-0003"]
    assert bottom["raw"] == "bottom text"
    assert bottom["cleaned"] is None
    assert bottom["translated"] is None
    assert bottom["error"] is None


def test_get_regions_returns_reading_order_not_insertion_order(client, tmp_path):
    from artifice_ocr.output import page_path
    from artifice_ocr.pagexml import PageDocument, TextRegion, artifice_custom, write

    def _region(rid, y):
        return TextRegion(
            id=rid,
            type="other",
            custom=artifice_custom("unclassified"),
            polygon=[(0, y), (100, y), (100, y + 20), (0, y + 20)],
        )

    # Insertion order deliberately differs from the declared reading order.
    doc = PageDocument(
        image_filename="page.png",
        image_width=100,
        image_height=100,
        regions=[_region("region-0001", 0), _region("region-0002", 30)],
        reading_order=["region-0002", "region-0001"],
    )
    write(doc, page_path(tmp_path, "page"))

    item_id = _add_item_and_set_output(tmp_path)
    body = client.get(f"/api/queue/{item_id}/regions").json()
    assert [r["id"] for r in body["regions"]] == ["region-0002", "region-0001"]
    assert body["reading_order"] == ["region-0002", "region-0001"]


def test_get_regions_surfaces_persisted_region_errors(client, tmp_path):
    import json

    from artifice_ocr.output import load_page, page_path
    from artifice_ocr.pagexml import ProcessingStep, write

    item_id = _add_item_and_set_output(tmp_path)
    _seed_region_page(tmp_path, "page")

    # Simulate a segmentation provider's per-region failure, which the pipeline
    # records as PAGE processing-step metadata (not on TextRegion itself).
    doc = load_page(tmp_path, "page")
    doc.metadata.processing_steps.append(
        ProcessingStep(
            name="region-error",
            value=json.dumps({"region_id": "region-0001", "error": "OCR failed"}),
        )
    )
    write(doc, page_path(tmp_path, "page"))

    body = client.get(f"/api/queue/{item_id}/regions").json()
    by_id = {r["id"]: r for r in body["regions"]}
    assert by_id["region-0001"]["error"] == "OCR failed"
    assert by_id["region-0002"]["error"] is None
    assert by_id["region-0003"]["error"] is None


def test_get_regions_404_for_unknown_item(client):
    assert client.get("/api/queue/does-not-exist/regions").status_code == 404


def test_get_regions_is_empty_without_page(client, tmp_path):
    # No PAGE document is the normal no-segmentation case, not an error: a 404
    # here put a red console error on every Review open.
    item_id = _add_item_and_set_output(tmp_path)  # no PAGE document written
    res = client.get(f"/api/queue/{item_id}/regions")
    assert res.status_code == 200
    assert res.json() == {"reading_order": [], "regions": []}


# --------------------------------------------------------------------------- #
# segmentation capability endpoint (Commit 7)
# --------------------------------------------------------------------------- #


def test_segmentation_capabilities_lists_only_available_providers(client, monkeypatch):
    """With every optional segmentation package absent, only ``passthrough`` is
    listed — the route never raises in a clean install (mirrors
    ``list_available``'s own contract)."""
    import sys

    for pkg in ("cv2", "kraken", "doclayout_yolo"):
        monkeypatch.setitem(sys.modules, pkg, None)

    res = client.get("/api/segmentation/capabilities")
    assert res.status_code == 200
    body = res.json()
    assert [p["name"] for p in body["providers"]] == ["passthrough"]
    assert all("requirements" in p for p in body["providers"])
