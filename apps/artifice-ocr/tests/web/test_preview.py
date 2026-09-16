# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preview, source-image, and raw/cleaned/translated text-save route tests."""

import json

import pytest
from artifice_ocr import config
from artifice_ocr.web import runtime

# --------------------------------------------------------------------------- #
# preview (in-memory queue item text)
# --------------------------------------------------------------------------- #


def test_preview_missing_item_is_404(client):
    res = client.get("/api/queue/does-not-exist/preview")
    assert res.status_code == 404


def test_preview_returns_text_confidence_and_diff(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    item = runtime.state.get(item_id)
    item.results = {
        "raw": {"extracted_text": "Der Be-\nricht war unvollstandig."},
        "cleaned": {"cleaned_text": "Der Bericht war unvollstandig."},
        "translated": {"translated_text": "The report was incomplete."},
    }
    item.confidence = 91
    item.language = "German"

    res = client.get(f"/api/queue/{item_id}/preview")
    assert res.status_code == 200
    body = res.json()
    assert body["raw"].startswith("Der Be-")
    assert body["cleaned"] == "Der Bericht war unvollstandig."
    assert body["confidence"] == 91
    assert body["confidence_tier"] == "high"
    # a word actually changed between raw and cleaned, so a range exists
    assert body["diff"]["raw_ranges"] or body["diff"]["cleaned_ranges"]


# --------------------------------------------------------------------------- #
# preview: source image (zoom/pan pane) + raw-text correction
# --------------------------------------------------------------------------- #


def test_image_route_404s_for_unknown_item(client):
    res = client.get("/api/queue/does-not-exist/image")
    assert res.status_code == 404


def test_image_route_passes_jpg_through_unchanged(client, tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"\xff\xd8\xff-fake-jpeg-bytes")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    res = client.get(f"/api/queue/{item_id}/image")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content == b"\xff\xd8\xff-fake-jpeg-bytes"


def test_image_route_converts_tiff_to_png(client, tmp_path, monkeypatch):
    # No TIFF writer is available in this environment (Pillow is deliberately
    # not a dependency), so the conversion call itself is mocked rather than
    # exercised against a real TIFF file — the same class of trade-off the
    # rest of this suite makes for real model calls.
    import fitz

    f = tmp_path / "a.tif"
    f.write_bytes(b"not-a-real-tiff")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    class FakePixmap:
        def __init__(self, path):
            assert path == str(f)

        def tobytes(self, fmt):
            assert fmt == "png"
            return b"\x89PNG-fake-bytes"

    monkeypatch.setattr(fitz, "Pixmap", FakePixmap)

    res = client.get(f"/api/queue/{item_id}/image")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content == b"\x89PNG-fake-bytes"


def test_image_route_renders_only_the_pdf_page_item_points_at(client, tmp_path):
    import fitz
    from artifice_ocr.jobs import JobItem

    pdf_path = tmp_path / "doc.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=100)  # page 0: 2:1 landscape
    doc.new_page(width=50, height=150)  # page 1: 1:3 portrait — the one requested
    doc.new_page(width=300, height=100)  # page 2: 3:1 landscape
    doc.save(str(pdf_path))
    doc.close()

    item = JobItem(path=str(pdf_path), page=1)
    runtime.state.add_items([item])
    item_id = runtime.state.queue_snapshot()[-1]["id"]

    res = client.get(f"/api/queue/{item_id}/image")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"

    rendered = fitz.Pixmap(res.content)
    # Page 1's aspect ratio (tall) is distinct from both its neighbours
    # (wide) — this would fail if page 0 or page 2 were rendered instead.
    assert rendered.height > rendered.width
    assert rendered.width not in (200 * 300 // 72, 300 * 300 // 72)


def test_image_route_caps_an_oversized_pdf_page(client, tmp_path):
    import fitz
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.web.runtime import IMAGE_MAX_LONG_EDGE

    pdf_path = tmp_path / "huge.pdf"
    doc = fitz.open()
    doc.new_page(width=2000, height=1000)  # long edge at 300dpi would be ~8333px
    doc.save(str(pdf_path))
    doc.close()

    item = JobItem(path=str(pdf_path), page=0)
    runtime.state.add_items([item])
    item_id = runtime.state.queue_snapshot()[-1]["id"]

    res = client.get(f"/api/queue/{item_id}/image")
    rendered = fitz.Pixmap(res.content)
    assert max(rendered.width, rendered.height) <= IMAGE_MAX_LONG_EDGE


def test_raw_text_route_404s_for_unknown_item(client):
    res = client.post("/api/queue/does-not-exist/raw-text", json={"text": "x"})
    assert res.status_code == 404


def test_raw_text_save_updates_in_memory_only_when_no_output_exists(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    item = runtime.state.get(item_id)
    item.results = {"raw": {"extracted_text": "origianl typo"}}

    res = client.post(f"/api/queue/{item_id}/raw-text", json={"text": "original corrected"})
    assert res.status_code == 200
    body = res.json()
    assert body["raw"] == "original corrected"
    assert item.results["raw"]["extracted_text"] == "original corrected"
    # nothing on disk to touch — no output dir was ever created for this stem
    assert not (tmp_path / "raw_ocr").exists()


def test_queue_fabricated_result_persists_to_ocr_metadata(client, tmp_path):
    output_dir = tmp_path / "output"
    json_dir = output_dir / "raw_ocr" / "json"
    json_dir.mkdir(parents=True)
    source = tmp_path / "page.png"
    source.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(source)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    item.results = {"raw": {"extracted_text": "invented prose"}}
    record_path = json_dir / f"{item.stem}.json"
    record_path.write_text('{"engine":"ollama","model":"vision"}', encoding="utf-8")
    config.apply_overrides({"output_dir": str(output_dir)})

    response = client.post(f"/api/queue/{item_id}/fabricated-result", json={"fabricated": True})
    assert response.status_code == 200
    assert response.json()["fabricated_result"] is True
    assert runtime.state.get(item_id).fabricated_result is True
    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["fabricated_result"] is True
    assert saved["fabricated_reviewed_at"]


def test_queue_fabricated_result_404s_for_unknown_item(client):
    response = client.post("/api/queue/does-not-exist/fabricated-result", json={"fabricated": True})
    assert response.status_code == 404


@pytest.mark.parametrize("invalid_json", ["{truncated", "[]"])
def test_queue_fabricated_result_survives_invalid_ocr_metadata(client, tmp_path, invalid_json):
    output_dir = tmp_path / "output"
    json_dir = output_dir / "raw_ocr" / "json"
    json_dir.mkdir(parents=True)
    source = tmp_path / "page.png"
    source.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(source)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    record_path = json_dir / f"{item.stem}.json"
    record_path.write_text(invalid_json, encoding="utf-8")
    config.apply_overrides({"output_dir": str(output_dir)})

    response = client.post(f"/api/queue/{item_id}/fabricated-result", json={"fabricated": True})
    assert response.status_code == 200
    assert response.json()["fabricated_result"] is True
    assert record_path.read_text(encoding="utf-8") == invalid_json


def test_raw_text_save_overwrites_disk_output_preserving_other_provenance(client, tmp_path):
    import json as jsonlib

    output_dir = tmp_path / "output"
    text_dir = output_dir / "raw_ocr" / "text"
    json_dir = output_dir / "raw_ocr" / "json"
    text_dir.mkdir(parents=True)
    json_dir.mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    item.results = {"raw": {"extracted_text": "garbld txt"}}

    (text_dir / f"{item.stem}.txt").write_text("garbld txt", encoding="utf-8")
    original_json = {
        "source_file": str(f),
        "stage": "raw_ocr",
        "extracted_text": "garbld txt",
        "engine": "lm-studio",
        "model": "some-vision-model",
        "ocr_prompt": "OCR: Extract all visible text...",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "page": 1,
        "total_pages": 1,
    }
    (json_dir / f"{item.stem}.json").write_text(jsonlib.dumps(original_json), encoding="utf-8")

    config.apply_overrides({"output_dir": str(output_dir)})

    res = client.post(f"/api/queue/{item_id}/raw-text", json={"text": "corrected text"})
    assert res.status_code == 200

    assert (text_dir / f"{item.stem}.txt").read_text(encoding="utf-8") == "corrected text"

    saved = jsonlib.loads((json_dir / f"{item.stem}.json").read_text(encoding="utf-8"))
    assert saved["extracted_text"] == "corrected text"
    assert saved["edited"] is True
    assert "edited_at" in saved
    # everything about the *original* OCR pass is untouched
    for key in ("engine", "model", "ocr_prompt", "timestamp", "source_file", "page", "total_pages"):
        assert saved[key] == original_json[key]


def test_raw_text_save_never_touches_cleaned_or_translated_dirs(client, tmp_path):
    output_dir = tmp_path / "output"
    (output_dir / "raw_ocr" / "text").mkdir(parents=True)
    (output_dir / "raw_ocr" / "json").mkdir(parents=True)
    (output_dir / "cleaned" / "text").mkdir(parents=True)
    (output_dir / "translated" / "text").mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)

    (output_dir / "raw_ocr" / "text" / f"{item.stem}.txt").write_text("orig", encoding="utf-8")
    (output_dir / "raw_ocr" / "json" / f"{item.stem}.json").write_text(
        '{"extracted_text": "orig"}', encoding="utf-8"
    )
    config.apply_overrides({"output_dir": str(output_dir)})

    client.post(f"/api/queue/{item_id}/raw-text", json={"text": "edited"})

    assert list((output_dir / "cleaned" / "text").iterdir()) == []
    assert list((output_dir / "translated" / "text").iterdir()) == []


# --------------------------------------------------------------------------- #
# cleaned-text + translated-text
# --------------------------------------------------------------------------- #


def test_cleaned_text_404s_for_unknown_queue_item(client):
    res = client.post("/api/queue/does-not-exist/cleaned-text", json={"text": "x"})
    assert res.status_code == 404


def test_translated_text_404s_for_unknown_queue_item(client):
    res = client.post("/api/queue/does-not-exist/translated-text", json={"text": "x"})
    assert res.status_code == 404


def test_cleaned_text_save_updates_in_memory_when_no_output_exists(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    item = runtime.state.get(item_id)
    item.results = {"cleaned": {"cleaned_text": "garbld cln"}}

    res = client.post(f"/api/queue/{item_id}/cleaned-text", json={"text": "corrected clean"})
    assert res.status_code == 200
    body = res.json()
    assert body["cleaned"] == "corrected clean"
    assert item.results["cleaned"]["cleaned_text"] == "corrected clean"


def test_translated_text_save_updates_in_memory_when_no_output_exists(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    item = runtime.state.get(item_id)
    item.results = {"translated": {"translated_text": "garbld trn"}}

    res = client.post(
        f"/api/queue/{item_id}/translated-text", json={"text": "corrected translation"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["translated"] == "corrected translation"
    assert item.results["translated"]["translated_text"] == "corrected translation"


def test_cleaned_text_save_overwrites_disk_output_preserving_provenance(client, tmp_path):
    import json as jsonlib

    output_dir = tmp_path / "output"
    text_dir = output_dir / "cleaned" / "text"
    json_dir = output_dir / "cleaned" / "json"
    text_dir.mkdir(parents=True)
    json_dir.mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    item.results = {"cleaned": {"cleaned_text": "garbld cln"}}

    (text_dir / f"{item.stem}.txt").write_text("garbld cln", encoding="utf-8")
    original_json = {
        "source_file": str(f),
        "stage": "cleaned",
        "cleaned_text": "garbld cln",
        "raw_text": "raw ocr text",
        "engine": "ollama",
        "model": "some-cleanup-model",
        "system_prompt": "Clean up the text...",
        "document_type": "default",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    (json_dir / f"{item.stem}.json").write_text(jsonlib.dumps(original_json), encoding="utf-8")

    config.apply_overrides({"output_dir": str(output_dir)})

    res = client.post(f"/api/queue/{item_id}/cleaned-text", json={"text": "corrected clean"})
    assert res.status_code == 200

    assert (text_dir / f"{item.stem}.txt").read_text(encoding="utf-8") == "corrected clean"

    saved = jsonlib.loads((json_dir / f"{item.stem}.json").read_text(encoding="utf-8"))
    assert saved["cleaned_text"] == "corrected clean"
    assert saved["edited"] is True
    assert "edited_at" in saved
    for key in ("engine", "model", "system_prompt", "timestamp", "source_file"):
        assert saved[key] == original_json[key]


def test_translated_text_save_overwrites_disk_output_preserving_provenance(client, tmp_path):
    import json as jsonlib

    output_dir = tmp_path / "output"
    text_dir = output_dir / "translated" / "text"
    json_dir = output_dir / "translated" / "json"
    text_dir.mkdir(parents=True)
    json_dir.mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)
    item.results = {"translated": {"translated_text": "garbld trn"}}

    (text_dir / f"{item.stem}.txt").write_text("garbld trn", encoding="utf-8")
    original_json = {
        "source_file": str(f),
        "stage": "translated",
        "translated_text": "garbld trn",
        "cleaned_text": "cleaned text",
        "source_language": "fr",
        "source_language_name": "French",
        "engine": "ollama",
        "model": "some-translate-model",
        "system_prompt": "Translate the text...",
        "document_type": "default",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    (json_dir / f"{item.stem}.json").write_text(jsonlib.dumps(original_json), encoding="utf-8")

    config.apply_overrides({"output_dir": str(output_dir)})

    res = client.post(
        f"/api/queue/{item_id}/translated-text", json={"text": "corrected translation"}
    )
    assert res.status_code == 200

    assert (text_dir / f"{item.stem}.txt").read_text(encoding="utf-8") == "corrected translation"

    saved = jsonlib.loads((json_dir / f"{item.stem}.json").read_text(encoding="utf-8"))
    assert saved["translated_text"] == "corrected translation"
    assert saved["edited"] is True
    assert "edited_at" in saved
    for key in ("engine", "model", "system_prompt", "timestamp", "source_file"):
        assert saved[key] == original_json[key]


def test_cleaned_text_save_never_touches_raw_or_translated_dirs(client, tmp_path):
    output_dir = tmp_path / "output"
    (output_dir / "cleaned" / "text").mkdir(parents=True)
    (output_dir / "cleaned" / "json").mkdir(parents=True)
    (output_dir / "raw_ocr" / "text").mkdir(parents=True)
    (output_dir / "translated" / "text").mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)

    (output_dir / "cleaned" / "text" / f"{item.stem}.txt").write_text("orig", encoding="utf-8")
    (output_dir / "cleaned" / "json" / f"{item.stem}.json").write_text(
        '{"cleaned_text": "orig"}', encoding="utf-8"
    )
    config.apply_overrides({"output_dir": str(output_dir)})

    client.post(f"/api/queue/{item_id}/cleaned-text", json={"text": "edited"})

    assert list((output_dir / "raw_ocr" / "text").iterdir()) == []
    assert list((output_dir / "translated" / "text").iterdir()) == []


def test_translated_text_save_never_touches_raw_or_cleaned_dirs(client, tmp_path):
    output_dir = tmp_path / "output"
    (output_dir / "translated" / "text").mkdir(parents=True)
    (output_dir / "translated" / "json").mkdir(parents=True)
    (output_dir / "raw_ocr" / "text").mkdir(parents=True)
    (output_dir / "cleaned" / "text").mkdir(parents=True)

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]
    item = runtime.state.get(item_id)

    (output_dir / "translated" / "text" / f"{item.stem}.txt").write_text("orig", encoding="utf-8")
    (output_dir / "translated" / "json" / f"{item.stem}.json").write_text(
        '{"translated_text": "orig"}', encoding="utf-8"
    )
    config.apply_overrides({"output_dir": str(output_dir)})

    client.post(f"/api/queue/{item_id}/translated-text", json={"text": "edited"})

    assert list((output_dir / "raw_ocr" / "text").iterdir()) == []
    assert list((output_dir / "cleaned" / "text").iterdir()) == []
