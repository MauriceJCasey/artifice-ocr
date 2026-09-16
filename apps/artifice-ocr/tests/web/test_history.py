# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""History run/item routes, removed workspace surfaces, and Tropy export fields."""

from artifice_ocr.web import runtime

from ._helpers import _seed_history_run

# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #


def test_history_runs_lists_finished_runs(client):
    _seed_history_run(runtime.state)

    res = client.get("/api/history/runs")
    runs = res.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["total"] == 1
    assert runs[0]["succeeded"] == 1


def test_history_run_items_lists_documents(client):
    run_id = _seed_history_run(runtime.state)

    res = client.get(f"/api/history/runs/{run_id}/items")
    items = res.json()["items"]
    assert items[0]["name"] == "letter.png"
    assert items[0]["language"] == "German"


def test_history_item_detail_includes_text_and_diff(client):
    run_id = _seed_history_run(runtime.state)
    item_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]

    res = client.get(f"/api/history/items/{item_id}")
    body = res.json()
    assert body["raw"] == "raw text"
    assert body["cleaned"] == "cleaned text"
    assert body["confidence_tier"] == "high"
    assert "diff" in body


def test_history_item_detail_404_for_unknown_id(client):
    res = client.get("/api/history/items/999999")
    assert res.status_code == 404


def test_history_item_detail_includes_page(client):
    run_id = _seed_history_run(runtime.state)
    item_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]
    body = client.get(f"/api/history/items/{item_id}").json()
    assert "page" in body


def test_history_fabricated_result_can_be_flagged_and_exported(client):
    run_id = _seed_history_run(runtime.state)
    item_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]

    flagged = client.post(
        f"/api/history/items/{item_id}/fabricated-result", json={"fabricated": True}
    )
    assert flagged.status_code == 200
    assert flagged.json()["fabricated_result"] is True
    listed = client.get(f"/api/history/runs/{run_id}/items").json()["items"]
    assert listed[0]["fabricated_result"] is True

    export = client.get("/api/history/fabricated-results")
    assert export.status_code == 200
    body = export.json()
    assert body["schema_version"] == 1
    assert body["items"][0]["item_id"] == item_id
    assert body["items"][0]["raw_text"] == "raw text"
    assert body["items"][0]["source_file"] == "letter.png"
    assert "tropy_item_node" not in body["items"][0]


def test_history_fabricated_result_404s_for_unknown_item(client):
    response = client.post("/api/history/items/999999/fabricated-result", json={"fabricated": True})
    assert response.status_code == 404


def test_history_image_route_404_for_unknown_item(client):
    res = client.get("/api/history/items/999999/image")
    assert res.status_code == 404


def test_history_image_route_404_when_source_file_gone(client, tmp_path):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path=str(tmp_path / "nope.png"))
    item.state = JobState.DONE
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)
    hist_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]
    res = client.get(f"/api/history/items/{hist_id}/image")
    assert res.status_code == 404


def test_history_image_route_passes_jpg_through_unchanged(client, tmp_path):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    f = tmp_path / "scan.jpg"
    f.write_bytes(b"\xff\xd8\xff-fake-jpeg")
    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path=str(f))
    item.state = JobState.DONE
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)
    hist_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]
    res = client.get(f"/api/history/items/{hist_id}/image")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content == b"\xff\xd8\xff-fake-jpeg"


def test_history_image_route_renders_page_parsed_from_name_when_page_col_null(client, tmp_path):
    import fitz
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    pdf_path = tmp_path / "doc.pdf"
    doc = fitz.open()
    doc.new_page(width=50, height=200)  # page 0 — tall
    doc.new_page(width=200, height=50)  # page 1 — wide
    doc.save(str(pdf_path))
    doc.close()

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=2)
    for idx in (0, 1):
        item = JobItem(path=str(pdf_path))
        item.state = JobState.DONE
        item.label = f"Eberhard KV 3.pdf  p.{idx + 1}"
        runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=2, failed=0, elapsed=1.0)

    items = client.get(f"/api/history/runs/{run_id}/items").json()["items"]
    img0 = client.get(f"/api/history/items/{items[0]['item_id']}/image").content
    img1 = client.get(f"/api/history/items/{items[1]['item_id']}/image").content
    pix0 = fitz.Pixmap(img0)
    pix1 = fitz.Pixmap(img1)
    assert pix0.height > pix0.width  # tall (page 0)
    assert pix1.width > pix1.height  # wide (page 1)
    assert img0 != img1  # different pages rendered


def test_history_image_route_honours_page_column_when_set(client, tmp_path):
    import fitz
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    pdf_path = tmp_path / "doc.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=50)  # page 0
    doc.new_page(width=50, height=200)  # page 1
    doc.save(str(pdf_path))
    doc.close()

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path=str(pdf_path), page=1)  # explicitly page 1
    item.state = JobState.DONE
    item.label = "Eberhard KV 3.pdf  p.1"  # name says page 1, but column should win
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)

    hist_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]
    img_bytes = client.get(f"/api/history/items/{hist_id}/image").content
    pix = fitz.Pixmap(img_bytes)
    assert pix.height > pix.width  # page 1 is tall — column beats name parse


def test_history_raw_text_save_updates_text(client):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path="C:/docs/report.png")
    item.state = JobState.DONE
    item.results = {"raw": {"extracted_text": "original text"}}
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)
    hist_id = client.get(f"/api/history/runs/{run_id}/items").json()["items"][0]["item_id"]

    res = client.post(f"/api/history/items/{hist_id}/raw-text", json={"text": "corrected text"})
    assert res.status_code == 200
    body = res.json()
    assert body["raw"] == "corrected text"

    re_read = client.get(f"/api/history/items/{hist_id}").json()
    assert re_read["raw"] == "corrected text"


def test_history_raw_text_save_404_for_unknown_item(client):
    res = client.post("/api/history/items/999999/raw-text", json={"text": "x"})
    assert res.status_code == 404


def test_history_search_finds_by_filename(client):
    _seed_history_run(runtime.state)

    hit = client.get("/api/history/search", params={"q": "letter"})
    miss = client.get("/api/history/search", params={"q": "nonexistent"})

    assert len(hit.json()["items"]) == 1
    assert miss.json()["items"] == []


def test_history_fulltext_search_finds_text(client):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.jobs import State as JobState

    run_id = runtime.state.history.start_run(stages=["ocr"], output_dir="out", total=1)
    item = JobItem(path="C:/docs/report.png")
    item.state = JobState.DONE
    item.results = {
        "raw": {"extracted_text": "This is a confidential report about quantum computing."},
        "cleaned": {"cleaned_text": "This is a cleaned confidential report."},
    }
    runtime.state.history.record_item(run_id, item)
    runtime.state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)

    res = client.get("/api/history/fulltext", params={"q": "quantum"})
    results = res.json()["results"]
    assert len(results) == 1
    assert results[0]["name"] == "report.png"


def test_history_search_with_no_query_returns_nothing(client):
    _seed_history_run(runtime.state)
    res = client.get("/api/history/search")
    assert res.json()["items"] == []


def test_delete_run_removes_it_but_not_output_files(client):
    run_id = _seed_history_run(runtime.state)

    res = client.delete(f"/api/history/runs/{run_id}")
    assert res.json() == {"ok": True}
    assert client.get("/api/history/runs").json()["runs"] == []


# --------------------------------------------------------------------------- #
# removed workspace surfaces
# --------------------------------------------------------------------------- #


def test_unrelated_workspace_routes_are_removed(client):
    assert client.get("/api/analytics/stats").status_code == 404
    assert client.get("/api/templates").status_code == 404


# --------------------------------------------------------------------------- #
# tropy json-ld bridge — serialiser fields
# --------------------------------------------------------------------------- #


def test_history_detail_includes_tropy_exportable(client):
    run_id = _seed_history_run(runtime.state)
    rows = runtime.state.history.list_items(run_id)
    if rows:
        item_id = rows[0]["item_id"]
        res = client.get(f"/api/history/items/{item_id}")
        data = res.json()
        assert "tropy_exportable" in data
        assert "tropy_group" in data
        assert "tropy_photo_path" in data
