# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Commit 2: authoritative PAGE persistence and export.

Covers the pipeline-side PAGE layering (raw/cleaned/translated indices,
resume-from-PAGE, per-stage processing steps), the byte-for-byte legacy
output invariant, and the export surfaces reading PAGE first.
"""

import json
from pathlib import Path

from artifice_ocr import config
from lxml import etree

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OCR_PYPROJECT = _REPO_ROOT / "apps" / "artifice-ocr" / "pyproject.toml"


def _real_png(tmp_path: Path, name: str = "page.png", size=(120, 200)) -> Path:
    """A real, decodable PNG — `image_dimensions` and the PAGE layer open it."""
    from PIL import Image

    p = tmp_path / name
    Image.new("RGB", size, "white").save(p)
    return p


def _mock_model_stages(monkeypatch) -> None:
    """Replace the model-facing internals so each stage is deterministic and
    makes no real inference call."""
    from artifice_ocr.stages import cleanup, ocr, translate

    monkeypatch.setattr(
        ocr, "_ocr_single_image", lambda path, orientation=1: ("RAW TEXT.", "ollama")
    )
    monkeypatch.setattr(cleanup, "_cleanup_with_chunking", lambda text, *a, **k: text)
    monkeypatch.setattr(translate, "detect_language", lambda text, doc_type="default": "de")
    monkeypatch.setattr(translate, "_translate_with_chunking", lambda text, *a, **k: "ÜBERSETZT.")


def _normalize_sidecar(path: Path) -> dict:
    """Drop the only non-deterministic field so two runs can be compared."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("timestamp", None)
    return data


# --------------------------------------------------------------------------- #
# 1. Byte-for-byte legacy output invariant
# --------------------------------------------------------------------------- #


def test_pipeline_legacy_outputs_match_direct_perform(tmp_path, monkeypatch):
    """Running the pipeline (with PAGE layering) must not change the legacy
    .txt / JSON outputs one byte — they must equal a direct
    stages/*.perform run over the same fixture."""
    config.apply_overrides({"confidence_enabled": False})
    _mock_model_stages(monkeypatch)

    img = _real_png(tmp_path)

    from artifice_ocr.pipeline import run_pipeline_batch

    out_pipeline = tmp_path / "out-pipeline"
    run_pipeline_batch([str(img)], str(out_pipeline))

    from artifice_ocr.stages import cleanup as cleanup_stage
    from artifice_ocr.stages import ocr as ocr_stage
    from artifice_ocr.stages import translate as translate_stage

    out_direct = tmp_path / "out-direct"
    raw = ocr_stage.perform(str(img), output_dir=str(out_direct))
    cleaned = cleanup_stage.perform(
        raw["extracted_text"], source_file=str(img), output_dir=str(out_direct)
    )
    translate_stage.perform(
        cleaned["cleaned_text"], source_file=str(img), output_dir=str(out_direct)
    )

    for stage in ("raw_ocr", "cleaned", "translated"):
        p_txt = out_pipeline / stage / "text" / f"{img.stem}.txt"
        d_txt = out_direct / stage / "text" / f"{img.stem}.txt"
        assert p_txt.read_bytes() == d_txt.read_bytes(), f"{stage} .txt diverged"

        p_json = out_pipeline / stage / "json" / f"{img.stem}.json"
        d_json = out_direct / stage / "json" / f"{img.stem}.json"
        assert _normalize_sidecar(p_json) == _normalize_sidecar(d_json), f"{stage} .json diverged"


def test_pipeline_persists_page_but_does_not_alter_legacy(tmp_path, monkeypatch):
    """The PAGE file is an additive side effect: legacy outputs are untouched
    and the new PAGE file exists where the resolver says it should."""
    config.apply_overrides({"confidence_enabled": False})
    _mock_model_stages(monkeypatch)

    img = _real_png(tmp_path)

    from artifice_ocr.output import page_path
    from artifice_ocr.pipeline import run_pipeline_batch

    out = tmp_path / "out"
    run_pipeline_batch([str(img)], str(out))

    assert page_path(out, img.stem).is_file()


# --------------------------------------------------------------------------- #
# 2. PAGE validity across stages, resume, and manual correction
# --------------------------------------------------------------------------- #


def test_page_document_valid_after_each_stage_and_resume(tmp_path, monkeypatch):
    config.apply_overrides({"confidence_enabled": False})
    _mock_model_stages(monkeypatch)

    img = _real_png(tmp_path)

    from artifice_ocr.output import load_page, page_path
    from artifice_ocr.pagexml import schema_path, serialize, write
    from artifice_ocr.pipeline import run_cleanup_step, run_ocr_step, run_translate_step

    schema = etree.XMLSchema(etree.parse(str(schema_path())))
    out = tmp_path / "out"

    raw = run_ocr_step(str(img), str(out))
    doc = raw["_page_document"]
    assert doc is not None
    assert doc.image_width == 120 and doc.image_height == 200
    assert doc.regions[0].stage_text("raw") == "RAW TEXT."
    schema.assertValid(etree.fromstring(serialize(doc)))
    assert doc.metadata.processing_steps[-1].name == "ocr"

    cleaned = run_cleanup_step(raw, img.stem, str(out))
    doc = cleaned["_page_document"]
    assert doc.regions[0].stage_text("cleaned") == "RAW TEXT."  # identity mock
    schema.assertValid(etree.fromstring(serialize(doc)))
    assert doc.metadata.processing_steps[-1].name == "cleanup"

    translated = run_translate_step(cleaned, img.stem, str(out))
    doc = translated["_page_document"]
    assert doc.regions[0].stage_text("translated") == "ÜBERSETZT."
    schema.assertValid(etree.fromstring(serialize(doc)))
    assert doc.metadata.processing_steps[-1].name == "translate"

    # Resume: re-running skips, and re-serves text from the persisted PAGE.
    raw2 = run_ocr_step(str(img), str(out))
    assert raw2["_skipped"] is True
    assert raw2["extracted_text"] == "RAW TEXT."

    # Manual correction replaces only the selected index and stays valid.
    doc = load_page(out, img.stem)
    assert doc is not None
    doc.regions[0].set_stage_text("cleaned", "CORRECTED.")
    write(doc, page_path(out, img.stem))
    schema.assertValid(etree.fromstring(serialize(doc)))
    assert doc.regions[0].stage_text("cleaned") == "CORRECTED."
    assert doc.regions[0].stage_text("raw") == "RAW TEXT."
    assert doc.regions[0].stage_text("translated") == "ÜBERSETZT."


def test_pre_page_folder_resume_does_not_write_page(tmp_path, monkeypatch):
    """A pre-PAGE folder (legacy .txt only) resumes without silently writing a
    PAGE file — PAGE is only ever written on an actual stage execution."""
    img = _real_png(tmp_path)

    # Legacy OCR output, no PAGE.
    (tmp_path / "out" / "raw_ocr" / "text").mkdir(parents=True)
    (tmp_path / "out" / "raw_ocr" / "text" / f"{img.stem}.txt").write_text(
        "legacy text", encoding="utf-8"
    )

    from artifice_ocr.output import page_path
    from artifice_ocr.pipeline import run_ocr_step

    result = run_ocr_step(str(img), str(tmp_path / "out"))
    assert result["_skipped"] is True
    assert result["extracted_text"] == "legacy text"
    assert not page_path(tmp_path / "out", img.stem).exists()


# --------------------------------------------------------------------------- #
# 3. Export surfaces read PAGE first
# --------------------------------------------------------------------------- #


def _write_page_with_stages(out: Path, stem: str) -> None:
    from artifice_ocr.output import page_path
    from artifice_ocr.pagexml import PageDocument, TextRegion, artifice_custom, write

    doc = PageDocument(
        image_filename="p.png",
        image_width=10,
        image_height=10,
        regions=[
            TextRegion(
                id="region-0001",
                type="paragraph",
                custom=artifice_custom("unclassified"),
                polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            )
        ],
        reading_order=["region-0001"],
    )
    doc.regions[0].set_stage_text("raw", "PAGE RAW")
    doc.regions[0].set_stage_text("cleaned", "PAGE CLEANED")
    doc.regions[0].set_stage_text("translated", "PAGE TRANSLATED")
    write(doc, page_path(out, stem))


def _write_legacy_text(out: Path, stem: str, stage: str, text: str) -> None:
    from artifice_ocr.output import stage_dir

    d = stage_dir(out, stage) / "text"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.txt").write_text(text, encoding="utf-8")


def test_collect_stems_reads_page_index_for_each_stage(tmp_path):
    from artifice_ocr.pdf_export import collect_stems

    out = tmp_path / "out"
    _write_page_with_stages(out, "page")
    # Legacy .txt deliberately differs, to prove PAGE is preferred.
    _write_legacy_text(out, "page", "raw_ocr", "LEGACY RAW")
    _write_legacy_text(out, "page", "cleaned", "LEGACY CLEANED")
    _write_legacy_text(out, "page", "translated", "LEGACY TRANSLATED")

    for stage, expected in (
        ("raw_ocr", "PAGE RAW"),
        ("cleaned", "PAGE CLEANED"),
        ("translated", "PAGE TRANSLATED"),
    ):
        pages, skipped = collect_stems(["page"], output_dir=str(out), stage=stage)
        assert skipped == []
        assert pages[0].text == expected


def test_collect_stems_falls_back_to_legacy_without_page(tmp_path):
    from artifice_ocr.pdf_export import collect_stems

    out = tmp_path / "out"
    _write_legacy_text(out, "page", "cleaned", "LEGACY CLEANED")

    pages, skipped = collect_stems(["page"], output_dir=str(out), stage="cleaned")
    assert skipped == []
    assert pages[0].text == "LEGACY CLEANED"


def test_collect_folder_reads_page_index(tmp_path):
    from artifice_ocr.pdf_export import collect_folder

    out = tmp_path / "out"
    _write_page_with_stages(out, "page")
    _write_legacy_text(out, "page", "cleaned", "LEGACY CLEANED")

    pages = collect_folder(str(out), stage="cleaned")
    assert len(pages) == 1
    assert pages[0].text == "PAGE CLEANED"


def test_tropy_note_queue_reads_page_index(tmp_path, monkeypatch):
    from artifice_ocr.jobs import JobItem
    from artifice_ocr.web.routers import tropy_notes

    _write_page_with_stages(tmp_path, "page")
    from artifice_ocr.output import load_page

    doc = load_page(tmp_path, "page")

    item = JobItem(path="x.png")
    item.source = {
        "origin": "tropy-jsonld",
        "photo_id": 5,
        "tropy_project": str(tmp_path / "proj.tpy"),
        "tropy_item_id": 7,
    }
    item.results["raw"] = {"extracted_text": "LEGACY RAW", "_page_document": doc}
    item.results["cleaned"] = {"cleaned_text": "LEGACY CLEANED", "_page_document": doc}
    item.results["translated"] = {"translated_text": "LEGACY TRANS", "_page_document": doc}

    class _FakeState:
        def get(self, _id):
            return item

    monkeypatch.setattr(tropy_notes, "state", _FakeState())

    for stage, expected in (
        ("raw_ocr", "PAGE RAW"),
        ("cleaned", "PAGE CLEANED"),
        ("translated", "PAGE TRANSLATED"),
    ):
        req = tropy_notes.TropyNotesRequest(source="queue", item_ids=["x"], stage=stage)
        entries, _n = tropy_notes._queue_entries(req)
        assert entries[0].text == expected


# --------------------------------------------------------------------------- #
# 4. PAGE export path (distinct from persistence)
# --------------------------------------------------------------------------- #


def test_export_page_xml_writes_copies_distinct_from_persistence(tmp_path):
    from artifice_ocr.output import page_path
    from artifice_ocr.pdf_export import export_page_xml

    out = tmp_path / "out"
    _write_page_with_stages(out, "page")

    dest, exported, skipped = export_page_xml(["page"], output_dir=str(out))
    assert skipped == []
    assert len(exported) == 1
    assert Path(exported[0]).is_file()
    # Persistence file is untouched; export is a distinct location.
    assert page_path(out, "page").is_file()
    assert Path(exported[0]).resolve() != page_path(out, "page").resolve()
    # The export dir is the legacy fallback (no canonical project.json here).
    assert dest == (out / "page-export")


# --------------------------------------------------------------------------- #
# 5. Wheel inspection (source-level: XSD declared + resolvable)
# --------------------------------------------------------------------------- #


def test_pagexml_schema_is_packaged_and_resolvable():
    from artifice_ocr.pagexml import schema_path

    assert schema_path().is_file()
    assert schema_path().name == "pagecontent.xsd"

    text = _OCR_PYPROJECT.read_text(encoding="utf-8")
    assert '"artifice_ocr.pagexml"' in text
    assert "schema/*.xsd" in text
