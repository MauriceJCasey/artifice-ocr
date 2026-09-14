# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the PDF export feature.

Covers the structure guard, structure stage, folder collection, and
end-to-end PDF rendering.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from artifice_ocr import _guard, config


# --------------------------------------------------------------------------- #
# structure guard
# --------------------------------------------------------------------------- #


def test_check_structure_only_rejects_word_change():
    """The structure guard must reject any change to a word."""
    original = "Der Bericht war unvollstaendig und teilweise unklar."
    structured = "Der Bericht war unvollstaendig und teilweise klar."

    result = _guard.check_structure_only(original, structured)

    assert result.ok is False
    assert "differs" in result.reasons[0] or "fewer" in result.reasons[0]


def test_check_structure_only_allows_paragraph_breaks():
    """The structure guard must accept paragraph breaks (whitespace only)."""
    original = "Der Bericht war unvollstaendig.\nEr war auch teilweise unklar."
    structured = "Der Bericht war unvollstaendig.\n\nEr war auch teilweise unklar."

    result = _guard.check_structure_only(original, structured)

    assert result.ok is True, result.reasons


def test_check_structure_only_rejects_empty_output():
    """Empty structured output must be rejected."""
    original = "Der Bericht war unvollstaendig."
    structured = "   "

    result = _guard.check_structure_only(original, structured)

    assert result.ok is False
    assert "output empty" in result.reasons


def test_check_structure_only_rejects_word_addition():
    """Adding words must be rejected."""
    original = "Der Bericht war kurz."
    structured = "Der Bericht war sehr kurz."

    result = _guard.check_structure_only(original, structured)

    assert result.ok is False
    assert result.words_deleted > 0


def test_check_structure_only_accepts_identical_text():
    """Identical text must pass."""
    text = "Der Bericht war unvollstaendig und teilweise unklar."

    result = _guard.check_structure_only(text, text)

    assert result.ok is True, result.reasons


# --------------------------------------------------------------------------- #
# structure stage
# --------------------------------------------------------------------------- #


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_structure_perform_respects_resume(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """Second call with existing output should be a no-op."""
    from artifice_ocr.stages import structure

    raw = "Der Bericht war unvollstaendig.\nEr war unklar."
    structured = "Der Bericht war unvollstaendig.\n\nEr war unklar."
    mock_chat.return_value = MagicMock(message=MagicMock(content=structured))

    config.apply_overrides({"resume": True})

    # First run
    result1 = structure.perform(raw, source_file="test.txt", output_dir=str(tmp_path))
    assert result1["stage"] == "structured"

    # Second run should be skipped
    result2 = structure.perform(raw, source_file="test.txt", output_dir=str(tmp_path))
    assert result2.get("_skipped") is True
    assert mock_chat.call_count == 1  # only called once


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_structure_perform_fallback_on_guard_reject(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """When guard rejects, original text should be kept."""
    from artifice_ocr.stages import structure

    raw = "Der Bericht war unvollstaendig."
    corrupted = "Der Bericht war kurz."  # model changed words
    mock_chat.return_value = MagicMock(message=MagicMock(content=corrupted))

    result = structure.perform(raw, source_file="test.txt", output_dir=str(tmp_path))

    assert result["guard"]["ok"] is False
    assert result["structured_text"] == raw  # original kept
    assert "rejected_structured_text" in result


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_structure_perform_accepts_safe_restructure(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """When guard accepts, structured text should be used."""
    from artifice_ocr.stages import structure

    raw = "Der Bericht war unvollstaendig.\nEr war unklar."
    structured = "Der Bericht war unvollstaendig.\n\nEr war unklar."
    mock_chat.return_value = MagicMock(message=MagicMock(content=structured))

    result = structure.perform(raw, source_file="test.txt", output_dir=str(tmp_path))

    assert result["guard"]["ok"] is True
    assert result["structured_text"] == structured
    assert "rejected_structured_text" not in result


# --------------------------------------------------------------------------- #
# collect_folder
# --------------------------------------------------------------------------- #


def test_collect_folder_natural_sort(tmp_path):
    """Files should be natural-sorted when no manifest is present."""
    from artifice_ocr import pdf_export

    # Create test files in the expected directory structure
    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)

    # Create files that would sort incorrectly with pure lexicographic sort
    (text_dir / "page2.txt").write_text("Page 2 text")
    (text_dir / "page10.txt").write_text("Page 10 text")
    (text_dir / "page1.txt").write_text("Page 1 text")

    pages = pdf_export.collect_folder(str(tmp_path), stage="cleaned")

    assert len(pages) == 3
    # Natural sort: page1, page2, page10 (not page1, page10, page2)
    assert pages[0].label == "page1"
    assert pages[1].label == "page2"
    assert pages[2].label == "page10"


def test_collect_folder_with_manifest(tmp_path):
    """When manifest exists, pages should be ordered by page_number."""
    from artifice_ocr import pdf_export

    # Create test files
    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)

    (text_dir / "item_p0002.txt").write_text("Page 2 text")
    (text_dir / "item_p0001.txt").write_text("Page 1 text")

    # Create manifest
    manifest = {
        "item/item_p0001": {
            "photo_id": 1,
            "item_id": 10,
            "item_title": "Test Item",
            "page_number": 1,
        },
        "item/item_p0002": {
            "photo_id": 2,
            "item_id": 10,
            "item_title": "Test Item",
            "page_number": 2,
        },
    }
    (tmp_path / "tropy_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    pages = pdf_export.collect_folder(str(tmp_path), stage="cleaned")

    assert len(pages) == 2
    # Ordered by page_number from manifest
    assert pages[0].page_number == 1
    assert pages[1].page_number == 2


def test_collect_folder_stage_fallback(tmp_path):
    """Should fall back through translated > cleaned > raw_ocr."""
    from artifice_ocr import pdf_export

    # Only raw_ocr exists
    text_dir = tmp_path / "raw_ocr" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "test.txt").write_text("Raw text")

    pages = pdf_export.collect_folder(str(tmp_path), stage="cleaned")

    assert len(pages) == 1
    assert pages[0].text == "Raw text"


# --------------------------------------------------------------------------- #
# end-to-end with mocked model
# --------------------------------------------------------------------------- #


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_compile_pdf_end_to_end(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """Full pipeline: collect -> structure -> render, then verify PDF content."""
    import fitz  # PyMuPDF
    from artifice_ocr import pdf_export

    # Create test files
    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)

    (text_dir / "page1.txt").write_text("Erster Absatz.\nZweiter Satz.")
    (text_dir / "page2.txt").write_text("Zweiter Absatz.\nDritter Satz.")

    # Mock the structure call
    def side_effect(*args, **kwargs):
        messages = kwargs.get("messages", [])
        for msg in messages:
            if msg["role"] == "user":
                text = msg["content"]
                # Simulate adding paragraph breaks
                return MagicMock(message=MagicMock(content=text.replace("\n", "\n\n")))
        return MagicMock(message=MagicMock(content="structured text"))

    mock_chat.side_effect = side_effect

    # Collect and structure
    pages = pdf_export.collect_folder(str(tmp_path), stage="cleaned")
    assert len(pages) == 2

    pages = pdf_export.structure_pages(pages)

    # Render PDF
    pdf_path = tmp_path / "output.pdf"
    result = pdf_export.render_pdf(pages, pdf_path, title="Test Document")

    assert result.exists()
    assert result == pdf_path

    # Verify PDF contains original words
    doc = fitz.open(str(pdf_path))
    full_text = ""
    for page in doc:
        full_text += page.get_text()
    doc.close()

    assert "Erster" in full_text
    assert "Absatz" in full_text
    assert "Zweiter" in full_text
    assert "Satz" in full_text


# --------------------------------------------------------------------------- #
# smoke test with real data (no model call)
# --------------------------------------------------------------------------- #


def test_compile_pdf_smoke_no_structure():
    """Smoke test against real data in the repo, no model call."""
    from artifice_ocr import pdf_export

    real_folder = Path("E:/Claude Sandbox/OCR Pipeline Tool/output/cleaned/text/Fritz Eberhard KV")
    if not real_folder.exists():
        pytest.skip("Real test data not available")

    pages = pdf_export.collect_folder(str(real_folder), stage="cleaned")
    assert len(pages) >= 1

    # Render without structuring
    pdf_path = Path("E:/Claude Sandbox/OCR Pipeline Tool/output/smoke_test.pdf")
    result = pdf_export.render_pdf(pages, pdf_path, title="Fritz Eberhard KV")

    assert result.exists()
    assert result.stat().st_size > 0

    # Clean up
    result.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# compile() function (refactored entry point)
# --------------------------------------------------------------------------- #


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_structure_pages_calls_on_progress_in_order(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """on_progress should be called once per page, in order, with messages."""
    from artifice_ocr import pdf_export
    from artifice_ocr.pdf_export import PageText

    def side_effect(*args, **kwargs):
        messages = kwargs.get("messages", [])
        for msg in messages:
            if msg["role"] == "user":
                text = msg["content"]
                return MagicMock(message=MagicMock(content=text.replace("\n", "\n\n")))
        return MagicMock(message=MagicMock(content="structured"))

    mock_chat.side_effect = side_effect

    pages = [
        PageText(label="page_a", text="Page A text.\nSecond line.", source_path=tmp_path / "a.txt"),
        PageText(label="page_b", text="Page B text.\nSecond line.", source_path=tmp_path / "b.txt"),
    ]

    calls = []
    result = pdf_export.structure_pages(pages, on_progress=lambda msg: calls.append(msg))

    assert len(calls) == 2
    assert "Structuring 1/2: page_a" in calls[0]
    assert "Structuring 2/2: page_b" in calls[1]
    assert len(result) == 2


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_structure_pages_calls_on_rejected(mock_chat, tmp_path, monkeypatch):
    mock_chat = mock_chat.return_value.chat
    """on_rejected should be called when the guard rejects a page."""
    # Disable resume so structure.perform always runs the model call
    from artifice_ocr import config as _cfg

    _cfg.apply_overrides({"resume": False})
    from artifice_ocr import pdf_export
    from artifice_ocr.pdf_export import PageText

    # Model returns a word-change that the guard will reject
    mock_chat.return_value = MagicMock(message=MagicMock(content="Changed text."))

    pages = [
        PageText(
            label="page_one",
            text="Original text that must be kept.",
            source_path=tmp_path / "unique_test_page.txt",
        ),
    ]

    rejected = []
    result = pdf_export.structure_pages(pages, on_rejected=lambda l: rejected.append(l))

    assert len(rejected) == 1
    assert rejected[0] == "page_one"
    # Original text should be kept
    assert result[0].text == "Original text that must be kept."


@patch("artifice_ocr._resolution.resolve_models_for_run")
@patch("artifice_ocr.stages.structure.ollama.Client")
def test_compile_function_end_to_end(mock_chat, mock_resolve, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """pdf_export.compile() should collect, structure and render."""
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "page1.txt").write_text("Erster Absatz.\nZweiter Satz.")
    (text_dir / "page2.txt").write_text("Zweiter Absatz.\nDritter Satz.")

    def side_effect(*args, **kwargs):
        messages = kwargs.get("messages", [])
        for msg in messages:
            if msg["role"] == "user":
                text = msg["content"]
                return MagicMock(message=MagicMock(content=text.replace("\n", "\n\n")))
        return MagicMock(message=MagicMock(content="structured text"))

    mock_chat.side_effect = side_effect

    output_path = tmp_path / "result.pdf"
    progress = []
    result = pdf_export.compile(
        str(tmp_path),
        stage="cleaned",
        structure=True,
        output=str(output_path),
        on_progress=lambda msg: progress.append(msg),
    )

    assert result == output_path
    assert output_path.exists()
    assert output_path.stat().st_size > 0
    assert any("Collecting" in m for m in progress)
    assert any("Found" in m for m in progress)
    assert any("Rendering" in m for m in progress)
    assert any("Done" in m for m in progress)


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_compile_function_no_structure(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """compile() with structure=False should skip the model call."""
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "page1.txt").write_text("Page 1 text.")

    output_path = tmp_path / "result.pdf"
    result = pdf_export.compile(
        str(tmp_path),
        stage="cleaned",
        structure=False,
        output=str(output_path),
    )

    assert result.exists()
    mock_chat.assert_not_called()


# --------------------------------------------------------------------------- #
# Markdown export
# --------------------------------------------------------------------------- #


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_render_markdown_creates_file(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """compile() with format='md' should produce a Markdown file."""
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "page1.txt").write_text("Erster Absatz.\nZweiter Satz.")
    (text_dir / "page2.txt").write_text("Zweiter Absatz.\nDritter Satz.")

    mock_chat.side_effect = lambda **kw: MagicMock(
        message=MagicMock(content=kw["messages"][-1]["content"].replace("\n", "\n\n"))
    )

    output_path = pdf_export.compile(
        str(tmp_path),
        stage="cleaned",
        structure=False,
        output=str(tmp_path / "out.md"),
        format="md",
    )

    assert output_path.exists()
    assert output_path.suffix == ".md"
    content = output_path.read_text(encoding="utf-8")
    assert "## Page 1" in content
    assert "## Page 2" in content
    assert "[page1]" in content
    assert "[page2]" in content
    assert "Erster Absatz." in content


# --------------------------------------------------------------------------- #
# PDF style presets
# --------------------------------------------------------------------------- #


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_compile_with_style_preset(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """compile() with style='compact' should produce a valid PDF."""
    import fitz
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "page1.txt").write_text("Erster Absatz.\nZweiter Satz.")

    mock_chat.side_effect = lambda **kw: MagicMock(
        message=MagicMock(content=kw["messages"][-1]["content"].replace("\n", "\n\n"))
    )

    output_path = pdf_export.compile(
        str(tmp_path),
        stage="cleaned",
        structure=False,
        output=str(tmp_path / "out.pdf"),
        format="pdf",
        style="compact",
    )

    assert output_path.exists()
    assert output_path.stat().st_size > 0
    doc = fitz.open(str(output_path))
    full_text = ""
    for page in doc:
        full_text += page.get_text()
    doc.close()
    assert "Erster Absatz." in full_text


def test_compile_function_raises_on_empty_folder(tmp_path):
    """compile() should raise ValueError when no pages are found."""
    from artifice_ocr import pdf_export

    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ValueError, match="No 'cleaned' text found"):
        pdf_export.compile(str(empty), stage="cleaned", structure=False)


# --------------------------------------------------------------------------- #
# Bilingual export
# --------------------------------------------------------------------------- #


def test_collect_bilingual_folder_pairs_by_stem(tmp_path):
    """collect_bilingual_folder() should pair cleaned + translated by stem."""
    from artifice_ocr import pdf_export

    cleaned_dir = tmp_path / "cleaned" / "text"
    cleaned_dir.mkdir(parents=True)
    (cleaned_dir / "page1.txt").write_text("Original text A")
    (cleaned_dir / "page2.txt").write_text("Original text B")

    translated_dir = tmp_path / "translated" / "text"
    translated_dir.mkdir(parents=True)
    (translated_dir / "page1.txt").write_text("Translated A")
    (translated_dir / "page2.txt").write_text("Translated B")

    pages = pdf_export.collect_bilingual_folder(str(tmp_path))

    assert len(pages) == 2
    assert pages[0].original_text == "Original text A"
    assert pages[0].translated_text == "Translated A"
    assert pages[1].original_text == "Original text B"
    assert pages[1].translated_text == "Translated B"


def test_collect_bilingual_folder_missing_translation(tmp_path):
    """Missing translated files should produce blank right column."""
    from artifice_ocr import pdf_export

    cleaned_dir = tmp_path / "cleaned" / "text"
    cleaned_dir.mkdir(parents=True)
    (cleaned_dir / "page1.txt").write_text("Original text")
    (cleaned_dir / "page2.txt").write_text("Original text 2")

    translated_dir = tmp_path / "translated" / "text"
    translated_dir.mkdir(parents=True)
    (translated_dir / "page1.txt").write_text("Translated text")

    pages = pdf_export.collect_bilingual_folder(str(tmp_path))

    assert len(pages) == 2
    assert pages[0].translated_text == "Translated text"
    assert pages[1].translated_text == ""


def test_collect_bilingual_folder_no_translated_dir(tmp_path):
    """When no translated dir exists, all translated_text should be blank."""
    from artifice_ocr import pdf_export

    cleaned_dir = tmp_path / "cleaned" / "text"
    cleaned_dir.mkdir(parents=True)
    (cleaned_dir / "page1.txt").write_text("Original text")

    pages = pdf_export.collect_bilingual_folder(str(tmp_path))

    assert len(pages) == 1
    assert pages[0].original_text == "Original text"
    assert pages[0].translated_text == ""


def test_collect_bilingual_folder_with_manifest(tmp_path):
    """Manifest ordering should be respected for bilingual collection."""
    from artifice_ocr import pdf_export

    cleaned_dir = tmp_path / "cleaned" / "text"
    cleaned_dir.mkdir(parents=True)
    (cleaned_dir / "item_p0002.txt").write_text("Page 2")
    (cleaned_dir / "item_p0001.txt").write_text("Page 1")

    translated_dir = tmp_path / "translated" / "text"
    translated_dir.mkdir(parents=True)
    (translated_dir / "item_p0002.txt").write_text("Trans Page 2")
    (translated_dir / "item_p0001.txt").write_text("Trans Page 1")

    manifest = {
        "item/item_p0001": {"item_title": "Test", "page_number": 1},
        "item/item_p0002": {"item_title": "Test", "page_number": 2},
    }
    (tmp_path / "tropy_manifest.json").write_text(json.dumps(manifest))

    pages = pdf_export.collect_bilingual_folder(str(tmp_path))

    assert len(pages) == 2
    assert pages[0].page_number == 1
    assert pages[0].original_text == "Page 1"
    assert pages[0].translated_text == "Trans Page 1"
    assert pages[1].page_number == 2


def test_render_bilingual_pdf_produces_valid_pdf(tmp_path):
    """render_bilingual_pdf() should produce a valid PDF with two-column table."""
    import fitz
    from artifice_ocr import pdf_export

    pages = [
        pdf_export.BilingualPageText(
            label="page1",
            text="Original text",
            source_path=tmp_path / "p1.txt",
            original_text="Der erste Absatz.\n\nDer zweite Absatz.",
            translated_text="The first paragraph.\n\nThe second paragraph.",
        ),
    ]

    pdf_path = tmp_path / "bilingual.pdf"
    result = pdf_export.render_bilingual_pdf(pages, pdf_path, title="Test")

    assert result.exists()
    assert result.stat().st_size > 0

    doc = fitz.open(str(pdf_path))
    full_text = ""
    for p in doc:
        full_text += p.get_text()
    doc.close()

    assert "Der erste Absatz." in full_text
    assert "The first paragraph." in full_text
    assert "Der zweite Absatz." in full_text
    assert "The second paragraph." in full_text


def test_render_bilingual_pdf_missing_translation(tmp_path):
    """Bilingual PDF with missing translations should still produce valid PDF."""
    from artifice_ocr import pdf_export

    pages = [
        pdf_export.BilingualPageText(
            label="page1",
            text="Original text",
            source_path=tmp_path / "p1.txt",
            original_text="Original paragraph.",
            translated_text="",
        ),
    ]

    pdf_path = tmp_path / "bilingual_partial.pdf"
    result = pdf_export.render_bilingual_pdf(pages, pdf_path)

    assert result.exists()
    assert result.stat().st_size > 0


def test_render_bilingual_markdown_creates_file(tmp_path):
    """render_bilingual_markdown() should produce a pipe-table Markdown file."""
    from artifice_ocr import pdf_export

    pages = [
        pdf_export.BilingualPageText(
            label="page1",
            text="Original text",
            source_path=tmp_path / "p1.txt",
            original_text="Der erste Absatz.\n\nDer zweite Absatz.",
            translated_text="The first paragraph.\n\nThe second paragraph.",
        ),
    ]

    md_path = tmp_path / "bilingual.md"
    result = pdf_export.render_bilingual_markdown(pages, md_path, title="Test")

    assert result.exists()
    content = result.read_text(encoding="utf-8")

    assert "# Test" in content
    assert "| Original | Translation |" in content
    assert "Der erste Absatz." in content
    assert "The first paragraph." in content
    assert "Der zweite Absatz." in content
    assert "The second paragraph." in content


def test_compile_bilingual_pdf_end_to_end(tmp_path):
    """compile() with bilingual=True should produce a two-column PDF."""
    import fitz
    from artifice_ocr import pdf_export

    cleaned_dir = tmp_path / "cleaned" / "text"
    cleaned_dir.mkdir(parents=True)
    (cleaned_dir / "page1.txt").write_text("Erster Absatz.\n\nZweiter Absatz.")
    (cleaned_dir / "page2.txt").write_text("Dritter Absatz.")

    translated_dir = tmp_path / "translated" / "text"
    translated_dir.mkdir(parents=True)
    (translated_dir / "page1.txt").write_text("First paragraph.\n\nSecond paragraph.")
    (translated_dir / "page2.txt").write_text("Third paragraph.")

    output_path = tmp_path / "bilingual_output.pdf"
    result = pdf_export.compile(
        str(tmp_path),
        bilingual=True,
        structure=False,
        output=str(output_path),
    )

    assert result == output_path
    assert output_path.exists()

    doc = fitz.open(str(output_path))
    full_text = ""
    for p in doc:
        full_text += p.get_text()
    doc.close()

    assert "Erster Absatz." in full_text
    assert "First paragraph." in full_text
    assert "Dritter Absatz." in full_text
    assert "Third paragraph." in full_text


def test_compile_bilingual_markdown(tmp_path):
    """compile() with bilingual=True and format='md' should produce bilingual Markdown."""
    from artifice_ocr import pdf_export

    cleaned_dir = tmp_path / "cleaned" / "text"
    cleaned_dir.mkdir(parents=True)
    (cleaned_dir / "page1.txt").write_text("Original text.")

    translated_dir = tmp_path / "translated" / "text"
    translated_dir.mkdir(parents=True)
    (translated_dir / "page1.txt").write_text("Translated text.")

    output_path = tmp_path / "bilingual_output.md"
    result = pdf_export.compile(
        str(tmp_path),
        bilingual=True,
        structure=False,
        output=str(output_path),
        format="md",
    )

    assert result.exists()
    assert result.suffix == ".md"
    content = result.read_text(encoding="utf-8")
    assert "| Original | Translation |" in content
    assert "Original text." in content
    assert "Translated text." in content


def test_compile_bilingual_skips_structure_by_default(tmp_path):
    """compile(bilingual=True, structure=False) should not call the model."""
    from artifice_ocr import pdf_export

    cleaned_dir = tmp_path / "cleaned" / "text"
    cleaned_dir.mkdir(parents=True)
    (cleaned_dir / "page1.txt").write_text("Original text.")

    translated_dir = tmp_path / "translated" / "text"
    translated_dir.mkdir(parents=True)
    (translated_dir / "page1.txt").write_text("Translated text.")

    output_path = tmp_path / "bilingual_no_struct.pdf"
    with patch("artifice_ocr.stages.structure.ollama.Client") as mock_chat:
        result = pdf_export.compile(
            str(tmp_path),
            bilingual=True,
            structure=False,
            output=str(output_path),
        )
        mock_chat.assert_not_called()
    assert result.exists()


# --------------------------------------------------------------------------- #
# _find_manifest normalisation
# --------------------------------------------------------------------------- #


def test_find_manifest_normalises_nested_pages(tmp_path):
    """Real manifests nest entries under a top-level "pages" key."""
    from artifice_ocr import pdf_export

    manifest = {
        "project": "Archive.tropy",
        "output_layout": "by-item",
        "pages": {
            "Item A/page_p0001": {"item_title": "Item A", "page_number": 1},
        },
    }
    (tmp_path / "tropy_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    found = pdf_export._find_manifest(tmp_path)

    assert found == {
        "Item A/page_p0001": {"item_title": "Item A", "page_number": 1},
    }


def test_find_manifest_accepts_flat_schema(tmp_path):
    """A flat stem->entry manifest (older/synthetic) stays as-is."""
    from artifice_ocr import pdf_export

    manifest = {
        "Item A/page_p0001": {"item_title": "Item A", "page_number": 1},
    }
    (tmp_path / "tropy_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    found = pdf_export._find_manifest(tmp_path)

    assert found == manifest


# --------------------------------------------------------------------------- #
# collect_stems (batch export)
# --------------------------------------------------------------------------- #


def test_collect_stems_flat_no_manifest(tmp_path):
    """Flat stems collect from <output_dir>/<stage>/text/ directly."""
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "page1.txt").write_text("First page text")
    (text_dir / "page2.txt").write_text("Second page text")

    pages, skipped = pdf_export.collect_stems(["page1", "page2"], output_dir=str(tmp_path))

    assert skipped == []
    assert [p.stem for p in pages] == ["page1", "page2"]
    assert pages[0].text == "First page text"
    assert pages[0].label == "page1"
    assert pages[0].section is None


def test_collect_stems_with_nested_manifest(tmp_path):
    """Manifest supplies item_title/page_number/section; caller order kept."""
    from artifice_ocr import pdf_export

    item_dir = tmp_path / "cleaned" / "text" / "Item A"
    item_dir.mkdir(parents=True)
    (item_dir / "page_p0001.txt").write_text("First page")
    (item_dir / "page_p0002.txt").write_text("Second page")

    manifest = {
        "project": "x",
        "pages": {
            "Item A/page_p0001": {"item_title": "Item A", "page_number": 1},
            "Item A/page_p0002": {"item_title": "Item A", "page_number": 2},
        },
    }
    (tmp_path / "tropy_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    pages, skipped = pdf_export.collect_stems(
        ["Item A/page_p0002", "Item A/page_p0001"], output_dir=str(tmp_path)
    )

    assert skipped == []
    # Caller (queue) order preserved, not manifest page order
    assert [p.page_number for p in pages] == [2, 1]
    assert pages[0].item_title == "Item A"
    assert pages[0].section == "Item A"
    assert pages[0].label == "page_p0002"
    assert pages[0].stem == "Item A/page_p0002"


def test_collect_stems_skips_missing_and_dedupes(tmp_path):
    """Missing outputs are reported as skipped; duplicate stems collapse."""
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "one.txt").write_text("First")

    pages, skipped = pdf_export.collect_stems(["one", "missing", "one"], output_dir=str(tmp_path))

    assert [p.stem for p in pages] == ["one"]
    assert skipped == ["missing"]


def test_collect_stems_stage_fallback(tmp_path):
    """A stem with only raw OCR output still collects, via fallback."""
    from artifice_ocr import pdf_export

    raw_dir = tmp_path / "raw_ocr" / "text"
    raw_dir.mkdir(parents=True)
    (raw_dir / "page1.txt").write_text("Raw text")

    pages, skipped = pdf_export.collect_stems(["page1"], output_dir=str(tmp_path), stage="cleaned")

    assert skipped == []
    assert pages[0].text == "Raw text"


# --------------------------------------------------------------------------- #
# default_batch_output
# --------------------------------------------------------------------------- #


def test_default_batch_output_single_item_name():
    """A batch entirely inside one item is named after that item."""
    from artifice_ocr import pdf_export

    p = pdf_export.default_batch_output(["Item A/p1", "Item A/p2"], output_dir="out")

    assert p.parent == Path("out")
    assert re.fullmatch(r"Item A-\d{8}-\d{4}\.pdf", p.name)


def test_default_batch_output_mixed_or_flat_is_batch():
    """Mixed items and flat stems fall back to the generic 'batch' name."""
    from artifice_ocr import pdf_export

    mixed = pdf_export.default_batch_output(["A/p1", "B/p1"], output_dir="out")
    flat = pdf_export.default_batch_output(["page1", "page2"], output_dir="out")

    assert re.fullmatch(r"batch-\d{8}-\d{4}\.pdf", mixed.name)
    assert re.fullmatch(r"batch-\d{8}-\d{4}\.pdf", flat.name)


def test_default_batch_output_md_extension_and_sanitising():
    """Markdown gets .md; Windows-hostile characters are stripped."""
    from artifice_ocr import pdf_export

    md = pdf_export.default_batch_output(["Item A/p1"], output_dir="out", format="md")
    hostile = pdf_export.default_batch_output(["Item: A?/p1"], output_dir="out")

    assert md.name.endswith(".md")
    assert ":" not in hostile.name and "?" not in hostile.name


# --------------------------------------------------------------------------- #
# compile_batch
# --------------------------------------------------------------------------- #


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_compile_batch_end_to_end(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """Two items combine into one PDF with per-item section headings."""
    import fitz
    from artifice_ocr import pdf_export

    for item, words in (("Item A", "Alpha Absatz"), ("Item B", "Beta Absatz")):
        d = tmp_path / "cleaned" / "text" / item
        d.mkdir(parents=True)
        (d / "scan.txt").write_text(f"{words} steht hier.")

    output_path = tmp_path / "combined.pdf"
    progress = []
    result = pdf_export.compile_batch(
        ["Item A/scan", "Item B/scan"],
        output_dir=str(tmp_path),
        output=str(output_path),
        on_progress=lambda msg: progress.append(msg),
    )

    assert result == output_path
    assert result.exists()
    mock_chat.assert_not_called()  # structure defaults to off

    doc = fitz.open(str(output_path))
    full_text = "".join(p.get_text() for p in doc)
    doc.close()

    assert "Alpha Absatz" in full_text
    assert "Beta Absatz" in full_text
    assert "Item A" in full_text  # section headings
    assert "Item B" in full_text
    assert any("Skipped" not in m for m in progress)  # nothing to skip here
    assert any("Done" in m for m in progress)


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_compile_batch_reports_skipped(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """Items without processed text are skipped and reported, not fatal."""
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "done.txt").write_text("Finished text.")

    progress = []
    result = pdf_export.compile_batch(
        ["done", "not_processed"],
        output_dir=str(tmp_path),
        output=str(tmp_path / "out.pdf"),
        on_progress=lambda msg: progress.append(msg),
    )

    assert result.exists()
    assert any("Skipped 1 item(s)" in m and "not_processed" in m for m in progress)
    mock_chat.assert_not_called()


def test_compile_batch_raises_when_nothing_processed(tmp_path):
    """compile_batch raises ValueError when no stem has any output."""
    from artifice_ocr import pdf_export

    with pytest.raises(ValueError, match="No pages found"):
        pdf_export.compile_batch(["ghost"], output_dir=str(tmp_path))


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_compile_batch_default_output_is_timestamped(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """With output=None the PDF lands in output_dir with a timestamped name."""
    from artifice_ocr import pdf_export

    item_dir = tmp_path / "cleaned" / "text" / "Item A"
    item_dir.mkdir(parents=True)
    (item_dir / "scan.txt").write_text("Some text.")

    result = pdf_export.compile_batch(["Item A/scan"], output_dir=str(tmp_path))

    assert result.parent == tmp_path
    assert re.fullmatch(r"Item A-\d{8}-\d{4}\.pdf", result.name)
    assert result.exists()
    mock_chat.assert_not_called()


@patch("artifice_ocr.stages.structure.ollama.Client")
def test_compile_batch_structure_cache_isolated_per_item(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    """Regression: items sharing a page filename must not collide in the
    structured-text resume cache (previously keyed by bare filename stem)."""
    import fitz
    from artifice_ocr import pdf_export

    for item, word in (("Item A", "Alphawort"), ("Item B", "Betawort")):
        d = tmp_path / "cleaned" / "text" / item
        d.mkdir(parents=True)
        (d / "scan.txt").write_text(f"{word} steht hier.")

    def side_effect(*args, **kwargs):
        for msg in kwargs.get("messages", []):
            if msg["role"] == "user":
                # Whitespace-only change so the guard accepts it
                return MagicMock(
                    message=MagicMock(content=msg["content"].replace(" steht", "\n\nsteht"))
                )
        return MagicMock(message=MagicMock(content=""))

    mock_chat.side_effect = side_effect
    config.apply_overrides({"resume": True})
    try:
        result = pdf_export.compile_batch(
            ["Item A/scan", "Item B/scan"],
            output_dir=str(tmp_path),
            structure=True,
            output=str(tmp_path / "out.pdf"),
        )
    finally:
        config.reset()

    # Each item gets its own cache entry under its own subfolder
    cache_a = tmp_path / "structured" / "text" / "Item A" / "scan.txt"
    cache_b = tmp_path / "structured" / "text" / "Item B" / "scan.txt"
    assert cache_a.exists() and cache_b.exists()
    assert "Alphawort" in cache_a.read_text(encoding="utf-8")
    assert "Betawort" in cache_b.read_text(encoding="utf-8")

    # And the PDF carries each item's own words — no cross-contamination
    doc = fitz.open(str(result))
    full_text = "".join(p.get_text() for p in doc)
    doc.close()
    assert "Alphawort" in full_text
    assert "Betawort" in full_text


def test_collect_folder_stems_use_manifest_keys(tmp_path):
    """collect_folder populates PageText.stem from the full manifest key, so
    the structure cache is collision-free for folder-mode exports too."""
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text" / "Item A"
    text_dir.mkdir(parents=True)
    (text_dir / "scan.txt").write_text("Page text")

    manifest = {
        "pages": {
            "Item A/scan": {"item_title": "Item A", "page_number": 1},
        },
    }
    (tmp_path / "tropy_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    pages = pdf_export.collect_folder(str(text_dir), stage="cleaned")

    assert len(pages) == 1
    assert pages[0].stem == "Item A/scan"
    assert pages[0].page_number == 1


# --------------------------------------------------------------------------- #
# compile / compile_batch — path validation
# --------------------------------------------------------------------------- #


def test_compile_refuses_folder_outside_allowed_roots():
    from artifice_ocr import pdf_export

    with pytest.raises(ValueError, match="outside the directories"):
        pdf_export.compile("/opt/rejected/scans", stage="cleaned", structure=False)


def test_compile_refuses_output_outside_allowed_roots(tmp_path):
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "page1.txt").write_text("Page 1 text.", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the directories"):
        pdf_export.compile(
            str(tmp_path),
            stage="cleaned",
            structure=False,
            output="/opt/rejected/out.pdf",
        )


def test_compile_refuses_manifest_outside_allowed_roots(tmp_path):
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "page1.txt").write_text("Page 1 text.", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the directories"):
        pdf_export.compile(
            str(tmp_path),
            stage="cleaned",
            structure=False,
            manifest_path="/opt/rejected/manifest.json",
        )


def test_compile_accepts_valid_paths(tmp_path):
    from artifice_ocr import pdf_export

    text_dir = tmp_path / "cleaned" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "page1.txt").write_text("Page 1 text.", encoding="utf-8")

    result = pdf_export.compile(
        str(tmp_path),
        stage="cleaned",
        structure=False,
        output=str(tmp_path / "out.pdf"),
    )
    assert result.exists()
    assert result.suffix == ".pdf"


def test_compile_batch_refuses_output_dir_outside_allowed_roots():
    from artifice_ocr import pdf_export

    with pytest.raises(ValueError, match="outside the directories"):
        pdf_export.compile_batch(["stem"], output_dir="/opt/rejected/out")


def test_compile_batch_refuses_output_outside_allowed_roots(tmp_path):
    from artifice_ocr import pdf_export

    item_dir = tmp_path / "cleaned" / "text" / "Item A"
    item_dir.mkdir(parents=True)
    (item_dir / "scan.txt").write_text("Some text.", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the directories"):
        pdf_export.compile_batch(
            ["Item A/scan"],
            output_dir=str(tmp_path),
            output="/opt/rejected/out.pdf",
        )


def test_compile_batch_refuses_manifest_outside_allowed_roots(tmp_path):
    from artifice_ocr import pdf_export

    item_dir = tmp_path / "cleaned" / "text" / "Item A"
    item_dir.mkdir(parents=True)
    (item_dir / "scan.txt").write_text("Some text.", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the directories"):
        pdf_export.compile_batch(
            ["Item A/scan"],
            output_dir=str(tmp_path),
            manifest_path="/opt/rejected/manifest.json",
        )
