# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from artifice_ocr.cli import app
from model_harness.contract import (
    HarnessResult,
    SchemaValidationFailed,
    StructuredOutputMode,
    StructuredOutputUnsupported,
)
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_config_and_resolution():
    """Reset config and the per-run resolution cache before/after each test.

    Model/backend defaults are now empty/auto, so a stale override from one
    test (or a leftover resolution cache) would leak into the next.
    """
    from artifice_ocr import _resolution, config

    config.reset()
    _resolution.reset()
    yield
    config.reset()
    _resolution.reset()


def _mock_backend_response(text="Sample extracted text"):
    """Return a MagicMock that looks like a _backend._SimpleResponse."""
    mock_resp = MagicMock()
    mock_resp.message = MagicMock()
    mock_resp.message.content = text
    return mock_resp


# ---------------------------------------------------------------------------
# OCR stage tests
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_writes_files(mock_get_client, tmp_path):
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("Hello from OCR")
    mock_get_client.return_value = mock_client

    from artifice_ocr import config
    from artifice_ocr.stages import ocr

    config.apply_overrides({"ocr_model": "olmocr-2-7b", "ocr_backend": "lm_studio"})

    test_image = tmp_path / "doc.png"
    test_image.write_bytes(b"\x89PNG fake")

    out_dir = tmp_path / "output"
    result = ocr.perform(str(test_image), output_dir=str(out_dir))

    assert result["extracted_text"] == "Hello from OCR"
    assert result["engine"] == "lm_studio"
    assert result["model"] == "olmocr-2-7b"
    assert "timestamp" in result

    text_file = out_dir / "raw_ocr" / "text" / "doc.txt"
    json_file = out_dir / "raw_ocr" / "json" / "doc.json"
    assert text_file.exists()
    assert json_file.exists()
    assert text_file.read_text(encoding="utf-8") == "Hello from OCR"

    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["source_file"] == str(test_image.resolve())
    assert data["stage"] == "raw_ocr"

    mock_client.chat.assert_called_once()
    call_kwargs = mock_client.chat.call_args
    assert call_kwargs.kwargs["model"] == "olmocr-2-7b"
    user_msg = call_kwargs.kwargs["messages"][0]
    assert user_msg["role"] == "user"
    assert isinstance(user_msg["content"], list)
    assert len(user_msg["content"]) == 2
    assert user_msg["content"][0]["type"] == "text"
    assert user_msg["content"][1]["type"] == "image_url"
    assert "base64," in user_msg["content"][1]["image_url"]["url"]


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_records_source_identity_in_sidecar(mock_get_client, tmp_path):
    """When `source` carries a checksum and/or photo id (a Tropy-sourced
    photo), the raw_ocr sidecar records it — this is what lets a later
    resume tell two photos with a colliding stem apart instead of silently
    reusing one photo's text for another."""
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("Hello from OCR")
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    test_image = tmp_path / "doc.png"
    test_image.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    ocr.perform(
        str(test_image),
        output_dir=str(out_dir),
        source={"checksum": "abc123", "photo_id": 7, "origin": "tropy-live"},
    )

    data = json.loads((out_dir / "raw_ocr" / "json" / "doc.json").read_text(encoding="utf-8"))
    assert data["checksum"] == "abc123"
    assert data["photo_id"] == 7


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_without_source_omits_identity_fields(mock_get_client, tmp_path):
    """No `source` (a plain non-Tropy file) -> no identity fields at all,
    not even empty ones — this is the shape every existing sidecar has."""
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("Hello from OCR")
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    test_image = tmp_path / "doc.png"
    test_image.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    ocr.perform(str(test_image), output_dir=str(out_dir))

    data = json.loads((out_dir / "raw_ocr" / "json" / "doc.json").read_text(encoding="utf-8"))
    assert "checksum" not in data
    assert "photo_id" not in data


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_rejects_unsupported_type(mock_get_client, tmp_path):
    from artifice_ocr.stages import ocr

    test_file = tmp_path / "doc.bmp"
    test_file.write_bytes(b"fake")

    try:
        ocr.perform(str(test_file))
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Unsupported file type" in str(e)

    mock_get_client.assert_not_called()


@patch("artifice_ocr.cli.resolve_models_for_run")
@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_cli_wires_through(mock_get_client, mock_resolve, tmp_path):
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("CLI test text")
    mock_get_client.return_value = mock_client

    test_image = tmp_path / "scan.tiff"
    test_image.write_bytes(b"fake tiff")
    out_dir = tmp_path / "cli_output"

    result = runner.invoke(app, ["ocr", str(test_image), "--output-dir", str(out_dir)])
    assert result.exit_code == 0
    assert "Processing" in result.output
    assert (
        "CLI test text"
        in json.loads((out_dir / "raw_ocr" / "json" / "scan.json").read_text(encoding="utf-8"))[
            "extracted_text"
        ]
    )


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_raw_output_preserved_fully(mock_get_client, tmp_path):
    noisy_text = "  Dr. Smith's 1938 report — pg. 1  "
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response(noisy_text)
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    test_image = tmp_path / "archival.jpg"
    test_image.write_bytes(b"fake jpg")
    out_dir = tmp_path / "output"

    result = ocr.perform(str(test_image), output_dir=str(out_dir))
    assert result["extracted_text"] == noisy_text

    text_file = out_dir / "raw_ocr" / "text" / "archival.txt"
    assert text_file.read_text(encoding="utf-8") == noisy_text


# ---------------------------------------------------------------------------
# Cleanup stage tests
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.cleanup.ollama.Client")
def test_cleanup_stage_writes_files(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="Cleaned output text"))

    from artifice_ocr import config
    from artifice_ocr.stages import cleanup

    config.apply_overrides({"cleanup_model": "gemma4:12b", "cleanup_backend": "ollama"})

    out_dir = tmp_path / "output"
    result = cleanup.perform(
        "Dirty OCR text here",
        source_file="doc.png",
        output_dir=str(out_dir),
    )

    assert result["cleaned_text"] == "Cleaned output text"
    assert result["raw_text"] == "Dirty OCR text here"
    assert result["engine"] == "ollama"
    assert result["model"] == "gemma4:12b"
    assert result["source_file"] == "doc.png"
    assert "timestamp" in result

    text_file = out_dir / "cleaned" / "text" / "doc.txt"
    json_file = out_dir / "cleaned" / "json" / "doc.json"
    assert text_file.exists()
    assert json_file.exists()
    assert text_file.read_text(encoding="utf-8") == "Cleaned output text"

    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["stage"] == "cleaned"
    assert data["raw_text"] == "Dirty OCR text here"

    mock_chat.assert_called_once()
    call_kwargs = mock_chat.call_args
    assert call_kwargs.kwargs["model"] == "gemma4:12b"


@patch("artifice_ocr.stages.cleanup.ollama.Client")
def test_cleanup_stage_uses_prompt_file(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="ok"))

    from artifice_ocr.stages import cleanup

    cleanup.perform("test text", output_dir=str(tmp_path))

    call_kwargs = mock_chat.call_args
    messages = call_kwargs.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert "archivist" in messages[0]["content"].lower()
    assert messages[1]["role"] == "user"
    assert "test text" in messages[1]["content"]
    assert "{raw_text}" not in messages[1]["content"]


@patch("artifice_ocr.stages.cleanup.ollama.Client")
@patch("artifice_ocr.cli.resolve_models_for_run")
def test_cleanup_cli_wires_through(mock_resolve, mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="Cleaned via CLI"))

    # This test covers CLI plumbing, not guard behaviour. The stub reply is far
    # shorter than its input, which the content-preservation guard would (quite
    # correctly) reject, so the guard is switched off for the duration.
    from artifice_ocr import config

    config.apply_overrides({"cleanup_guard": False})

    try:
        raw_file = tmp_path / "raw_ocr.txt"
        raw_file.write_text("Some raw OCR output", encoding="utf-8")
        out_dir = tmp_path / "cli_output"

        result = runner.invoke(app, ["cleanup", str(raw_file), "--output-dir", str(out_dir)])
        assert result.exit_code == 0
        assert "Cleaned" in result.output

        json_file = out_dir / "cleaned" / "json" / "raw_ocr.json"
        assert json_file.exists()
        data = json.loads(json_file.read_text(encoding="utf-8"))
        assert data["cleaned_text"] == "Cleaned via CLI"
        assert data["raw_text"] == "Some raw OCR output"
    finally:
        config.apply_overrides({"cleanup_guard": True})


@patch("artifice_ocr.stages.cleanup.ollama.Client")
def test_cleanup_preserves_raw_text_in_json(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    raw = "Hon. J. Smith, Dec. 1938 — re: budget"
    mock_chat.return_value = MagicMock(message=MagicMock(content="cleaned version"))

    from artifice_ocr.stages import cleanup

    out_dir = tmp_path / "output"
    result = cleanup.perform(raw, source_file="report.tif", output_dir=str(out_dir))

    json_file = out_dir / "cleaned" / "json" / "report.json"
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["raw_text"] == raw
    assert data["source_file"] == "report.tif"


# ---------------------------------------------------------------------------
# Translate stage tests
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_translate_stage_writes_files(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="Translated output text"))

    from artifice_ocr import config
    from artifice_ocr.stages import translate

    config.apply_overrides({"translate_model": "translategemma:4b", "translate_backend": "ollama"})

    out_dir = tmp_path / "output"
    result = translate.perform(
        "German text here",
        source_file="doc.txt",
        output_dir=str(out_dir),
    )

    assert result["translated_text"] == "Translated output text"
    assert result["cleaned_text"] == "German text here"
    assert result["engine"] == "ollama"
    assert result["model"] == "translategemma:4b"
    assert result["source_file"] == "doc.txt"
    assert result["stage"] == "translated"
    assert "timestamp" in result
    assert "source_language" in result

    text_file = out_dir / "translated" / "text" / "doc.txt"
    json_file = out_dir / "translated" / "json" / "doc.json"
    assert text_file.exists()
    assert json_file.exists()
    assert text_file.read_text(encoding="utf-8") == "Translated output text"

    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["stage"] == "translated"
    assert data["cleaned_text"] == "German text here"
    assert "source_language" in data

    # 2 calls: language detection + translation. Confidence self-assessment now
    # routes through model_harness.run_structured, not the chat client.
    assert mock_chat.call_count == 2


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_translate_stage_uses_prompt_file(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="ok"))

    from artifice_ocr.stages import translate

    translate.perform("Ein Test", output_dir=str(tmp_path))

    # Find the translation call (has system + user messages)
    for call in mock_chat.call_args_list:
        messages = call.kwargs.get("messages", [])
        if len(messages) >= 2 and messages[0]["role"] == "system":
            assert "translator" in messages[0]["content"].lower()
            assert "Ein Test" in messages[1]["content"]
            assert "{text}" not in messages[1]["content"]
            return
    assert False, "Translation call with system message not found"


@patch("artifice_ocr.stages.translate.ollama.Client")
@patch("artifice_ocr.cli.resolve_models_for_run")
def test_translate_cli_wires_through(mock_resolve, mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="Translated via CLI"))

    cleaned_file = tmp_path / "cleaned.txt"
    cleaned_file.write_text("Some cleaned text", encoding="utf-8")
    out_dir = tmp_path / "cli_output"

    result = runner.invoke(app, ["translate", str(cleaned_file), "--output-dir", str(out_dir)])
    assert result.exit_code == 0
    assert "Translated" in result.output

    json_file = out_dir / "translated" / "json" / "cleaned.json"
    assert json_file.exists()
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["translated_text"] == "Translated via CLI"
    assert data["cleaned_text"] == "Some cleaned text"


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_translate_preserves_cleaned_text_in_json(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    cleaned = "Die Dokumente aus dem Jahr 1938"
    mock_chat.return_value = MagicMock(message=MagicMock(content="The documents from 1938"))

    from artifice_ocr.stages import translate

    out_dir = tmp_path / "output"
    result = translate.perform(cleaned, source_file="report.txt", output_dir=str(out_dir))

    json_file = out_dir / "translated" / "json" / "report.json"
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["cleaned_text"] == cleaned
    assert data["source_file"] == "report.txt"


# ---------------------------------------------------------------------------
# Language detection tests
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_detect_language_returns_iso_code(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="de"))

    from artifice_ocr import config
    from artifice_ocr.stages.translate import detect_language

    config.apply_overrides({"translate_model": "translategemma:4b", "translate_backend": "ollama"})

    lang = detect_language("Das ist ein Test")
    assert lang == "de"

    call_kwargs = mock_chat.call_args
    assert call_kwargs.kwargs["model"] == "translategemma:4b"
    assert "Identify the primary language" in call_kwargs.kwargs["messages"][0]["content"]


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_detect_language_handles_garbled_response(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.return_value = MagicMock(message=MagicMock(content="The language is German."))

    from artifice_ocr.stages.translate import detect_language

    lang = detect_language("Some text")
    assert lang == "unknown"


@patch("artifice_ocr.stages.translate.ollama.Client")
def test_translate_includes_detected_language_in_json(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    mock_chat.side_effect = [
        MagicMock(message=MagicMock(content="fr")),
        MagicMock(message=MagicMock(content="The documents from 1938")),
    ]

    from artifice_ocr.stages import translate

    result = translate.perform(
        "Les documents de 1938",
        source_file="archive.txt",
        output_dir=str(tmp_path / "out"),
    )

    assert result["source_language"] == "fr"
    assert result["source_language_name"] == "French"


# ---------------------------------------------------------------------------
# Config loading tests
# ---------------------------------------------------------------------------


def test_config_defaults_are_set():
    from artifice_ocr import config

    config.reset()
    cfg = config.load_config()
    # Models are resolved at run time; the default is an empty string, not a
    # concrete name (a concrete name was the bug: it presumed a server the
    # suite does not install).
    assert cfg["ocr_model"] == ""
    assert cfg["cleanup_model"] == ""
    assert cfg["translate_model"] == ""
    assert cfg["ocr_backend"] == "auto"
    assert cfg["cleanup_backend"] == "auto"
    assert cfg["translate_backend"] == "auto"
    assert cfg["lm_studio_url"] == "http://localhost:1234/v1"
    config.reset()


def test_config_file_override(tmp_path):
    from artifice_ocr import config

    cfg_file = tmp_path / "test.yaml"
    cfg_file.write_text(
        'ocr_model: "custom-model"\noutput_dir: "/tmp/out"\n',
        encoding="utf-8",
    )

    config.reset()
    cfg = config.load_config(cfg_file)
    assert cfg["ocr_model"] == "custom-model"
    assert cfg["output_dir"] == "/tmp/out"
    assert cfg["cleanup_model"] == ""  # default preserved (resolved at run time)
    config.reset()


def test_config_env_override(tmp_path, monkeypatch):
    from artifice_ocr import config

    monkeypatch.setenv("OCR_MODEL", "env-model")

    config.reset()
    cfg = config.load_config()
    assert cfg["ocr_model"] == "env-model"
    config.reset()


def test_ollama_url_env_override(tmp_path, monkeypatch):
    from artifice_ocr import config

    monkeypatch.setenv("OLLAMA_URL", "http://host.docker.internal:11434")

    config.reset()
    cfg = config.load_config()
    assert cfg["ollama_url"] == "http://host.docker.internal:11434"
    config.reset()


def test_ollama_url_default_when_env_absent(tmp_path, monkeypatch):
    from artifice_ocr import config

    # Ensure OLLAMA_URL is NOT set
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    config.reset()
    cfg = config.load_config()
    assert cfg["ollama_url"] == "http://localhost:11434"
    config.reset()


def test_config_get_shorthand():
    from artifice_ocr import config

    config.reset()
    assert config.get("ocr_model") == ""
    assert config.get("nonexistent", "fallback") == "fallback"
    config.reset()


# ---------------------------------------------------------------------------
# PDF OCR tests
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.ocr._ocr_single_image")
@patch("artifice_ocr.stages.ocr._pdf_to_page_images")
def test_pdf_ocr_concatenates_pages(mock_pages, mock_ocr, tmp_path):
    mock_pages.return_value = [tmp_path / "p1.png", tmp_path / "p2.png"]
    # _ocr_single_image now returns (text, engine).
    mock_ocr.side_effect = [("Text from page 1", "ollama"), ("Text from page 2", "ollama")]

    from artifice_ocr.stages import ocr

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"fake pdf")

    out_dir = tmp_path / "output"
    result = ocr.perform(str(pdf_path), output_dir=str(out_dir))

    assert "Page Break" in result["extracted_text"]
    assert "Text from page 1" in result["extracted_text"]
    assert "Text from page 2" in result["extracted_text"]
    assert result["total_pages"] == 2
    assert mock_ocr.call_count == 2


@patch("artifice_ocr.stages.ocr._ocr_single_image")
def test_single_image_ocr_sets_total_pages_1(mock_ocr, tmp_path):
    mock_ocr.return_value = ("Single page text", "ollama")

    from artifice_ocr.stages import ocr

    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG fake")

    result = ocr.perform(str(img), output_dir=str(tmp_path / "out"))
    assert result["total_pages"] == 1


# ---------------------------------------------------------------------------
# Tropy orientation correction
# ---------------------------------------------------------------------------
#
# Confirmed on a real archive page: Tropy's own `photos.orientation` column
# said 1 (normal — nobody had flagged the scan), and the file's own EXIF had
# no orientation tag either, yet the page was genuinely scanned upside-down.
# Once a caller (jobs.py, from `item.source["orientation"]`) does know the
# correct value, `perform()` must actually apply it before the model ever
# sees the image.


def _make_test_image(path: Path, width=60, height=90) -> None:
    """A small, real, fitz-openable PNG — not a fake byte stub — since these
    tests exercise the actual rotation matrix, not just a mocked pipeline."""
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((5, 15), "T", fontsize=12)  # asymmetric mark
    pix = page.get_pixmap()
    pix.save(str(path))
    doc.close()


def test_exif_orientation_matrix_returns_none_for_normal():
    from artifice_ocr.stages import ocr

    assert ocr._exif_orientation_matrix(1, 100, 150) is None


def test_exif_orientation_matrix_returns_none_for_unrecognised_value():
    from artifice_ocr.stages import ocr

    assert ocr._exif_orientation_matrix(99, 100, 150) is None


def test_exif_orientation_matrix_covers_every_valid_exif_value():
    from artifice_ocr.stages import ocr

    for orientation in range(2, 9):
        assert ocr._exif_orientation_matrix(orientation, 100, 150) is not None


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_applies_orientation_correction_before_encoding(mock_get_client, tmp_path):
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("text")
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    img = tmp_path / "scan.png"
    _make_test_image(img)
    out_dir = tmp_path / "out"

    ocr.perform(str(img), output_dir=str(out_dir), orientation=3, stem="rotated")
    rotated_url = mock_client.chat.call_args.kwargs["messages"][0]["content"][1]["image_url"]["url"]

    mock_client.chat.reset_mock()
    ocr.perform(str(img), output_dir=str(out_dir), orientation=1, stem="normal")
    normal_url = mock_client.chat.call_args.kwargs["messages"][0]["content"][1]["image_url"]["url"]

    assert rotated_url.startswith("data:image/png;base64,")
    assert rotated_url != normal_url  # the bytes actually changed, not just relabelled


def test_encode_image_is_untouched_when_preprocess_disabled(tmp_path, monkeypatch):
    """With pre-processing off (the default), _encode_image returns the raw
    file bytes and original mime — no behaviour change for existing users."""
    from artifice_ocr.stages import ocr, preprocess

    monkeypatch.setattr(preprocess, "is_enabled", lambda: False)
    img = tmp_path / "scan.png"
    _make_test_image(img)

    b64, mime = ocr._encode_image(img)
    assert mime == "image/png"
    import base64

    with open(img, "rb") as f:
        raw = f.read()
    assert base64.standard_b64decode(b64) == raw  # bytes passed through unchanged


def test_encode_image_applies_preprocessing_when_enabled(tmp_path, monkeypatch):
    """With pre-processing on, _encode_image funnels the image through the
    stage and the emitted bytes differ from the raw file."""
    from artifice_ocr.stages import ocr, preprocess

    monkeypatch.setattr(preprocess, "is_enabled", lambda: True)
    img = tmp_path / "scan.png"
    _make_test_image(img)

    b64, mime = ocr._encode_image(img)
    assert mime == "image/png"
    import base64

    with open(img, "rb") as f:
        raw = f.read()
    assert base64.standard_b64decode(b64) != raw  # the stage actually ran


# ---------------------------------------------------------------------------
# OCR degeneracy guard integration
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_rejects_a_repetition_loop(mock_get_client, tmp_path):
    from artifice_ocr import config

    mock_client = MagicMock()
    looped_text = "\n\n".join(["Same hallucinated sentence over and over."] * 40)
    mock_client.chat.return_value = _mock_backend_response(looped_text)
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    # The per-page temperature ladder (on by default) owns repetition-guard
    # rejections before perform() ever sees the text. This test exercises
    # perform()'s document-level guard path, so disable the ladder first:
    # the mocked client's single call at temperature=0.0 returns the looped
    # text and perform()'s own check_no_repetition_loop rejects it.
    # Restore the CAPTURED prior value, not a hardcoded True — otherwise this
    # test forces the ladder on for every test that runs after it, making the
    # suite order-dependent.
    prior = config.get("ocr_temperature_ladder_enabled")
    config.apply_overrides({"ocr_temperature_ladder_enabled": False})
    try:
        img = tmp_path / "bad_scan.png"
        img.write_bytes(b"\x89PNG fake")
        out_dir = tmp_path / "output"

        with pytest.raises(RuntimeError, match="OCR rejected"):
            ocr.perform(str(img), output_dir=str(out_dir), stem="bad_scan")

        # A rejected page has nothing safe to write as "the" text.
        assert not (out_dir / "raw_ocr" / "text" / "bad_scan.txt").exists()

        # But the JSON is kept for forensic review, with the verdict + the text
        # that was rejected, mirroring structure.py's rejected_structured_text.
        data = json.loads(
            (out_dir / "raw_ocr" / "json" / "bad_scan.json").read_text(encoding="utf-8")
        )
        assert data["guard"]["ok"] is False
        assert data["rejected_extracted_text"] == looped_text
    finally:
        config.apply_overrides({"ocr_temperature_ladder_enabled": prior})


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_repetition_guard_can_be_disabled(mock_get_client, tmp_path):
    from artifice_ocr import config

    mock_client = MagicMock()
    looped_text = "\n\n".join(["Same hallucinated sentence over and over."] * 40)
    mock_client.chat.return_value = _mock_backend_response(looped_text)
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    config.apply_overrides({"ocr_repetition_guard": False})
    try:
        img = tmp_path / "bad_scan.png"
        img.write_bytes(b"\x89PNG fake")
        result = ocr.perform(str(img), output_dir=str(tmp_path / "output"), stem="bad_scan")
        assert result["extracted_text"] == looped_text
    finally:
        config.apply_overrides({"ocr_repetition_guard": True})


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_ocr_stage_accepts_real_varied_text(mock_get_client, tmp_path):
    mock_client = MagicMock()
    real_text = "\n\n".join(f"Genuinely distinct sentence number {i}." for i in range(40))
    mock_client.chat.return_value = _mock_backend_response(real_text)
    mock_get_client.return_value = mock_client

    from artifice_ocr.stages import ocr

    img = tmp_path / "good_scan.png"
    img.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    result = ocr.perform(str(img), output_dir=str(out_dir), stem="good_scan")

    assert result["extracted_text"] == real_text
    assert result["guard"]["ok"] is True
    assert (out_dir / "raw_ocr" / "text" / "good_scan.txt").exists()


# ---------------------------------------------------------------------------
# P2: Folder input / batch pipeline tests
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.ocr._get_backend_client")
@patch("artifice_ocr.stages.cleanup.ollama.Client")
@patch("artifice_ocr.stages.translate.ollama.Client")
@patch("artifice_ocr.cli.resolve_models_for_run")
def test_pipeline_batch_folder(
    mock_resolve, mock_translate, mock_cleanup, mock_get_client, tmp_path
):
    mock_cleanup = mock_cleanup.return_value.chat
    mock_translate = mock_translate.return_value.chat
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("Batch OCR text")
    mock_get_client.return_value = mock_client
    mock_cleanup.return_value = MagicMock(message=MagicMock(content="Batch cleaned"))
    mock_translate.side_effect = [
        MagicMock(message=MagicMock(content="en")),
        MagicMock(message=MagicMock(content="Batch translated")),
    ]

    scan_dir = tmp_path / "scans"
    scan_dir.mkdir()
    for i in range(3):
        img = scan_dir / f"scan_{i}.png"
        img.write_bytes(b"\x89PNG fake")

    out_dir = tmp_path / "output"
    result = runner.invoke(app, ["pipeline", str(scan_dir), "--output-dir", str(out_dir)])
    assert result.exit_code == 0
    assert "3 file(s)" in result.output

    for i in range(3):
        assert (out_dir / "raw_ocr" / "text" / f"scan_{i}.txt").exists()


@patch("artifice_ocr.stages.ocr._get_backend_client")
@patch("artifice_ocr.stages.cleanup.ollama.Client")
@patch("artifice_ocr.cli.resolve_models_for_run")
def test_pipeline_skip_translate(mock_resolve, mock_cleanup, mock_get_client, tmp_path):
    mock_cleanup = mock_cleanup.return_value.chat
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("OCR text")
    mock_get_client.return_value = mock_client
    mock_cleanup.return_value = MagicMock(message=MagicMock(content="Cleaned text"))

    img = tmp_path / "doc.png"
    img.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    result = runner.invoke(
        app, ["pipeline", str(img), "--output-dir", str(out_dir), "--skip-translate"]
    )
    assert result.exit_code == 0
    assert (out_dir / "raw_ocr" / "text" / "doc.txt").exists()
    assert (out_dir / "cleaned" / "text" / "doc.txt").exists()
    assert not (out_dir / "translated" / "text" / "doc.txt").exists()


@patch("artifice_ocr.stages.ocr._get_backend_client")
@patch("artifice_ocr.stages.cleanup.ollama.Client")
@patch("artifice_ocr.cli.resolve_models_for_run")
def test_pipeline_force_reprocess(mock_resolve, mock_cleanup, mock_get_client, tmp_path):
    mock_cleanup = mock_cleanup.return_value.chat
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("Fresh OCR")
    mock_get_client.return_value = mock_client
    mock_cleanup.return_value = MagicMock(message=MagicMock(content="Fresh cleaned"))

    img = tmp_path / "doc.png"
    img.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    # First run — creates output
    runner.invoke(app, ["pipeline", str(img), "--output-dir", str(out_dir)])
    text_file = out_dir / "raw_ocr" / "text" / "doc.txt"
    assert text_file.read_text(encoding="utf-8") == "Fresh OCR"
    mock_client.chat.assert_called_once()

    # Second run without --force — should skip OCR
    mock_client.chat.reset_mock()
    mock_cleanup.reset_mock()
    runner.invoke(app, ["pipeline", str(img), "--output-dir", str(out_dir)])
    mock_client.chat.assert_not_called()
    mock_cleanup.assert_not_called()

    # Third run with --force — should re-run everything
    mock_client.chat.return_value = _mock_backend_response("Re-OCR")
    mock_cleanup.return_value = MagicMock(message=MagicMock(content="Re-cleaned"))
    runner.invoke(app, ["pipeline", str(img), "--output-dir", str(out_dir), "--force"])
    mock_client.chat.assert_called_once()
    assert text_file.read_text(encoding="utf-8") == "Re-OCR"


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_collect_files_directory(mock_get_client, tmp_path):
    from artifice_ocr.pipeline import _collect_files

    scan_dir = tmp_path / "scans"
    scan_dir.mkdir()
    (scan_dir / "a.png").write_bytes(b"fake")
    (scan_dir / "b.pdf").write_bytes(b"fake")
    (scan_dir / "c.txt").write_text("unsupported")
    (scan_dir / "d.jpg").write_bytes(b"fake")

    files = _collect_files(str(scan_dir))
    names = [f.name for f in files]
    assert "a.png" in names
    assert "b.pdf" in names
    assert "d.jpg" in names
    assert "c.txt" not in names


@patch("artifice_ocr.stages.ocr._get_backend_client")
def test_collect_files_empty_directory_raises(mock_get_client, tmp_path):
    from artifice_ocr.pipeline import _collect_files

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    try:
        _collect_files(str(empty_dir))
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError as e:
        assert "No supported files" in str(e)


def test_resume_config_default():
    from artifice_ocr import config

    config.reset()
    assert config.get("resume") is True
    config.reset()


def test_max_ocr_workers_config_default():
    from artifice_ocr import config

    config.reset()
    assert config.get("max_ocr_workers") == 2
    config.reset()


# ---------------------------------------------------------------------------
# P3: Retry logic tests
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_first_attempt():
    from artifice_ocr._retry import retry

    call_count = 0

    @retry(max_attempts=3, base_delay=0.01, label="test")
    def succeed():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = succeed()
    assert result == "ok"
    assert call_count == 1


def test_retry_retries_on_failure_then_succeeds():
    from artifice_ocr._retry import retry

    call_count = 0

    @retry(max_attempts=3, base_delay=0.01, label="test")
    def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("transient")
        return "recovered"

    result = flaky()
    assert result == "recovered"
    assert call_count == 3


def test_retry_raises_after_max_attempts():
    from artifice_ocr._retry import retry

    @retry(max_attempts=2, base_delay=0.01, label="test")
    def always_fail():
        raise ConnectionError("permanent")

    try:
        always_fail()
        assert False, "Should have raised"
    except ConnectionError:
        pass


def test_retry_ignores_non_retryable_exceptions():
    from artifice_ocr._retry import retry

    @retry(max_attempts=3, base_delay=0.01, label="test")
    def value_error():
        raise ValueError("not retryable")

    try:
        value_error()
        assert False, "Should have raised"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# P3: Preflight command tests
# ---------------------------------------------------------------------------


@patch("artifice_ocr.cli.check_lm_studio")
@patch("artifice_ocr.cli.check_ollama")
def test_preflight_passes(mock_ollama, mock_lm):
    mock_lm.return_value = None
    mock_ollama.return_value = []

    result = runner.invoke(app, ["preflight"])
    assert result.exit_code == 0
    assert "All checks passed" in result.output


@patch("artifice_ocr.cli.check_lm_studio")
@patch("artifice_ocr.cli.check_ollama")
def test_preflight_shows_lm_failure(mock_ollama, mock_lm):
    mock_lm.return_value = "Cannot reach LM Studio"
    mock_ollama.return_value = []

    result = runner.invoke(app, ["preflight"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


# ---------------------------------------------------------------------------
# P3: --skip-cleanup and --skip-ocr CLI flags
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.ocr._get_backend_client")
@patch("artifice_ocr.stages.cleanup.ollama.Client")
@patch("artifice_ocr.cli.resolve_models_for_run")
def test_pipeline_skip_cleanup(mock_resolve, mock_cleanup, mock_get_client, tmp_path):
    mock_cleanup = mock_cleanup.return_value.chat
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("OCR text")
    mock_get_client.return_value = mock_client

    img = tmp_path / "doc.png"
    img.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    result = runner.invoke(
        app,
        ["pipeline", str(img), "--output-dir", str(out_dir), "--skip-cleanup", "--skip-translate"],
    )
    assert result.exit_code == 0
    assert (out_dir / "raw_ocr" / "text" / "doc.txt").exists()
    assert not (out_dir / "cleaned" / "text" / "doc.txt").exists()
    assert not (out_dir / "translated" / "text" / "doc.txt").exists()
    mock_cleanup.assert_not_called()


@patch("artifice_ocr.stages.ocr._get_backend_client")
@patch("artifice_ocr.stages.cleanup.ollama.Client")
@patch("artifice_ocr.cli.resolve_models_for_run")
def test_pipeline_skip_ocr(mock_resolve, mock_cleanup, mock_get_client, tmp_path):
    mock_cleanup = mock_cleanup.return_value.chat
    mock_cleanup.return_value = MagicMock(message=MagicMock(content="Cleaned text"))

    img = tmp_path / "doc.png"
    img.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    result = runner.invoke(
        app, ["pipeline", str(img), "--output-dir", str(out_dir), "--skip-ocr", "--skip-translate"]
    )
    assert result.exit_code == 0
    assert not (out_dir / "raw_ocr" / "text" / "doc.txt").exists()
    assert (out_dir / "cleaned" / "text" / "doc.txt").exists()
    mock_get_client.assert_not_called()


# ---------------------------------------------------------------------------
# P3: Config apply_overrides
# ---------------------------------------------------------------------------


def test_config_apply_overrides():
    from artifice_ocr import config

    config.reset()
    config.apply_overrides({"ocr_model": "custom-model", "resume": False})
    assert config.get("ocr_model") == "custom-model"
    assert config.get("resume") is False
    assert config.get("cleanup_model") == ""  # default preserved (resolved at run time)
    config.reset()


def test_save_user_settings_merges_rather_than_replaces(tmp_path, monkeypatch):
    """A caller saving one field must not wipe out other saved fields.

    Found via a real, if minor, incident: the web build's run-start handler
    persists just `output_dir` after every run. Before this test existed,
    `save_user_settings` overwrote the whole file, so that single-field save
    silently discarded a previously-saved `cleanup_model` (or anything else).
    """
    from artifice_ocr import config

    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")

    config.save_user_settings({"cleanup_model": "custom-model", "resume": False})
    config.save_user_settings({"output_dir": "somewhere-else"})

    saved = config.load_user_settings()
    assert saved["cleanup_model"] == "custom-model"
    assert saved["resume"] is False
    assert saved["output_dir"] == "somewhere-else"


def test_save_user_settings_still_drops_unknown_keys(tmp_path, monkeypatch):
    from artifice_ocr import config

    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")

    config.save_user_settings({"output_dir": "x", "not_a_real_setting": "y"})

    saved = config.load_user_settings()
    assert "not_a_real_setting" not in saved


# ---------------------------------------------------------------------------
# P4: Chunking tests
# ---------------------------------------------------------------------------


def test_chunk_text_short_text_unchanged():
    from artifice_ocr._chunking import chunk_text

    short = "Hello world. This is a test."
    chunks = chunk_text(short, max_tokens=100)
    assert len(chunks) == 1
    assert chunks[0] == short


def test_chunk_text_splits_long_text():
    from artifice_ocr._chunking import chunk_text

    # Create text that's ~500 tokens (well over 100-token limit)
    long_text = "This is a sentence. " * 200
    chunks = chunk_text(long_text, max_tokens=100, overlap_tokens=20)
    assert len(chunks) > 1
    # Reassembled should contain all original content
    reassembled = "\n\n".join(chunks)
    assert "sentence" in reassembled


def test_chunk_text_respects_paragraph_boundaries():
    from artifice_ocr._chunking import chunk_text

    paragraphs = ["Paragraph one. " * 50, "Paragraph two. " * 50]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, max_tokens=100, overlap_tokens=10)
    # Should have split into at least 2 chunks
    assert len(chunks) >= 2


def test_reassemble_joins_chunks():
    from artifice_ocr._chunking import reassemble

    chunks = ["Hello world", "Second chunk", "Third chunk"]
    result = reassemble(chunks)
    assert result == "Hello world\n\nSecond chunk\n\nThird chunk"


def test_estimate_tokens():
    from artifice_ocr._chunking import estimate_tokens

    # ~3.5 chars per token
    tokens = estimate_tokens("a" * 350)
    assert 90 < tokens < 110  # ~100 tokens


# ---------------------------------------------------------------------------
# P4: Confidence scoring tests
# ---------------------------------------------------------------------------


def test_heuristic_score_clean_text():
    from artifice_ocr._confidence import _heuristic_score

    clean_text = "This is a clear, well-written document with no issues."
    score, markers = _heuristic_score(clean_text)
    assert score >= 90
    assert len(markers) == 0


def test_heuristic_score_uncertain_text():
    from artifice_ocr._confidence import _heuristic_score

    uncertain_text = "I'm not sure about this part, it seems unclear and possibly damaged"
    score, markers = _heuristic_score(uncertain_text)
    assert score < 80
    assert len(markers) > 0


def test_evaluate_confidence():
    """LLM self-assessment blends into overall score via the harness."""
    mock_result = HarnessResult(
        data=MagicMock(score=85, reasoning="Good quality text"),
        mode_used=StructuredOutputMode.PROMPTED,
        model="translate-model",
        raw='{"score": 85, "reasoning": "Good quality text"}',
        repaired=False,
    )

    with patch("artifice_ocr._confidence.run_structured", return_value=mock_result):
        from artifice_ocr._confidence import evaluate_confidence

        result = evaluate_confidence(
            "Clean source text", "Clean translated text", enable_self_assessment=True
        )
    assert 0 <= result.overall_score <= 100
    assert result.reasoning == "Good quality text"


def test_evaluate_confidence_self_assessment_disabled():
    """Self-assessment is skipped entirely when disabled."""
    mock_result = HarnessResult(
        data=MagicMock(score=85, reasoning="Good quality text"),
        mode_used=StructuredOutputMode.PROMPTED,
        model="translate-model",
        raw="{}",
        repaired=False,
    )

    with patch("artifice_ocr._confidence.run_structured", return_value=mock_result) as mock_run:
        from artifice_ocr._confidence import evaluate_confidence

        result = evaluate_confidence("Clean text", "Clean output", enable_self_assessment=False)
    assert 0 <= result.overall_score <= 100
    mock_run.assert_not_called()


def test_evaluate_confidence_labels_schema_validation_failure():
    """A schema-validation failure degrades to a labelled 50, not a fabricated score."""
    with patch(
        "artifice_ocr._confidence.run_structured",
        side_effect=SchemaValidationFailed(
            "did not match schema",
            raw="garbage",
            mode=StructuredOutputMode.PROMPTED,
        ),
    ):
        from artifice_ocr._confidence import evaluate_confidence

        result = evaluate_confidence(
            "Clean source text", "Clean translated text", enable_self_assessment=True
        )
    assert result.score == 50
    assert "Self-assessment failed" in result.reasoning


def test_evaluate_confidence_labels_structured_output_unsupported():
    """The driver's actual bottom-of-the-ladder failure (StructuredOutputUnsupported,
    not SchemaValidationFailed — the driver degrades through the mode ladder and
    raises this one when every mode fails) also degrades to a labelled 50."""
    with patch(
        "artifice_ocr._confidence.run_structured",
        side_effect=StructuredOutputUnsupported("no mode produced valid output"),
    ):
        from artifice_ocr._confidence import evaluate_confidence

        result = evaluate_confidence(
            "Clean source text", "Clean translated text", enable_self_assessment=True
        )
    assert result.score == 50
    assert "Self-assessment failed" in result.reasoning


# ---------------------------------------------------------------------------
# P4: Prompt registry tests
# ---------------------------------------------------------------------------


def test_get_cleanup_prompt_default():
    from artifice_ocr._prompts import get_cleanup_prompt

    prompts = get_cleanup_prompt("default")
    assert "system" in prompts
    assert "user" in prompts
    assert "archivist" in prompts["system"].lower()


def test_get_cleanup_prompt_handwritten():
    from artifice_ocr._prompts import get_cleanup_prompt

    prompts = get_cleanup_prompt("handwritten")
    assert "paleographer" in prompts["system"].lower()


def test_get_cleanup_prompt_fallback():
    from artifice_ocr._prompts import get_cleanup_prompt

    prompts = get_cleanup_prompt("nonexistent_type")
    assert prompts["system"]  # should fall back to default


def test_get_translation_prompt_default():
    from artifice_ocr._prompts import get_translation_prompt

    prompts = get_translation_prompt("default")
    assert "translator" in prompts["system"].lower()


def test_get_translation_prompt_technical():
    from artifice_ocr._prompts import get_translation_prompt

    prompts = get_translation_prompt("technical")
    assert "technical" in prompts["system"].lower()


def test_list_document_types():
    from artifice_ocr._prompts import list_document_types

    types = list_document_types()
    assert "default" in types
    assert "handwritten" in types
    assert len(types) >= 6


def test_config_document_type_default():
    from artifice_ocr import config

    config.reset()
    assert config.get("document_type") == "default"
    config.reset()


def test_config_confidence_enabled_default():
    from artifice_ocr import config

    config.reset()
    assert config.get("confidence_enabled") is True
    config.reset()


# ---------------------------------------------------------------------------
# P4: CLI --doc-type and --no-confidence flags
# ---------------------------------------------------------------------------


@patch("artifice_ocr.stages.ocr._get_backend_client")
@patch("artifice_ocr.stages.cleanup.ollama.Client")
@patch("artifice_ocr.stages.translate.ollama.Client")
@patch("artifice_ocr.cli.resolve_models_for_run")
def test_pipeline_doc_type_flag(
    mock_resolve, mock_translate, mock_cleanup, mock_get_client, tmp_path
):
    mock_cleanup = mock_cleanup.return_value.chat
    mock_translate = mock_translate.return_value.chat
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("OCR text")
    mock_get_client.return_value = mock_client
    mock_cleanup.return_value = MagicMock(message=MagicMock(content="Cleaned"))
    mock_translate.side_effect = [
        MagicMock(message=MagicMock(content="de")),
        MagicMock(message=MagicMock(content="Translated")),
    ]

    img = tmp_path / "doc.png"
    img.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    result = runner.invoke(
        app, ["pipeline", str(img), "--output-dir", str(out_dir), "--doc-type", "handwritten"]
    )
    assert result.exit_code == 0
    # Verify the config was applied
    from artifice_ocr import config

    config.reset()


@patch("artifice_ocr.stages.ocr._get_backend_client")
@patch("artifice_ocr.stages.cleanup.ollama.Client")
@patch("artifice_ocr.stages.translate.ollama.Client")
@patch("artifice_ocr.cli.resolve_models_for_run")
def test_pipeline_no_confidence_flag(
    mock_resolve, mock_translate, mock_cleanup, mock_get_client, tmp_path
):
    mock_cleanup = mock_cleanup.return_value.chat
    mock_translate = mock_translate.return_value.chat
    mock_client = MagicMock()
    mock_client.chat.return_value = _mock_backend_response("OCR text")
    mock_get_client.return_value = mock_client
    mock_cleanup.return_value = MagicMock(message=MagicMock(content="Cleaned"))
    mock_translate.side_effect = [
        MagicMock(message=MagicMock(content="en")),
        MagicMock(message=MagicMock(content="Translated")),
    ]

    img = tmp_path / "doc.png"
    img.write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "output"

    result = runner.invoke(
        app,
        ["pipeline", str(img), "--output-dir", str(out_dir), "--no-confidence", "--skip-translate"],
    )
    assert result.exit_code == 0
    from artifice_ocr import config

    config.reset()


# ---------------------------------------------------------------------------
# audit-translations: find output already corrupted by the pre-fix
# already-English mistranslation bug
# ---------------------------------------------------------------------------


def _write_translated_json(out_dir: Path, stem: str, **fields):
    json_path = out_dir / "translated" / "json" / f"{stem}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(fields), encoding="utf-8")


def test_audit_translations_flags_real_english_translations(tmp_path):
    out_dir = tmp_path / "output"
    _write_translated_json(
        out_dir, "affected_doc", source_language="en", source_file="affected_doc.png"
    )
    _write_translated_json(
        out_dir,
        "properly_skipped",
        source_language="en",
        skipped_translation=True,
        source_file="properly_skipped.png",
    )
    _write_translated_json(
        out_dir,
        "genuinely_translated",
        source_language="de",
        source_file="genuinely_translated.png",
    )

    result = runner.invoke(app, ["audit-translations", "--output-dir", str(out_dir)])
    assert result.exit_code == 0
    assert "affected_doc" in result.output
    assert "properly_skipped" not in result.output
    assert "genuinely_translated" not in result.output
    assert "1 likely affected" in result.output


def test_audit_translations_json_output(tmp_path):
    out_dir = tmp_path / "output"
    _write_translated_json(
        out_dir, "affected_doc", source_language="en", source_file="affected_doc.png"
    )

    result = runner.invoke(app, ["audit-translations", "--output-dir", str(out_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["stem"] == "affected_doc"
    assert payload[0]["source_file"] == "affected_doc.png"


def test_audit_translations_reports_none_affected(tmp_path):
    out_dir = tmp_path / "output"
    _write_translated_json(out_dir, "fine", source_language="de", source_file="fine.png")

    result = runner.invoke(app, ["audit-translations", "--output-dir", str(out_dir)])
    assert result.exit_code == 0
    assert "None affected" in result.output


def test_audit_translations_handles_missing_output_dir(tmp_path):
    result = runner.invoke(app, ["audit-translations", "--output-dir", str(tmp_path / "nope")])
    assert result.exit_code == 0
    assert "No translated output found" in result.output


def test_audit_translations_recurses_into_subfolders(tmp_path):
    # Tropy items nest into one subfolder per item — the scan must find those.
    out_dir = tmp_path / "output"
    _write_translated_json(
        out_dir, "Some KV File/page_0001", source_language="en", source_file="page_0001.jpg"
    )

    result = runner.invoke(app, ["audit-translations", "--output-dir", str(out_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert "page_0001" in payload[0]["stem"]
