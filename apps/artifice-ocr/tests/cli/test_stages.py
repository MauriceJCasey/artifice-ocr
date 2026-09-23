# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OCR, cleanup and translate stage tests, plus language detection."""

import json
from unittest.mock import MagicMock, patch

from artifice_ocr.cli import app
from typer.testing import CliRunner

runner = CliRunner()


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
        raise AssertionError("Should have raised ValueError")
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
    cleanup.perform(raw, source_file="report.tif", output_dir=str(out_dir))

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
    raise AssertionError("Translation call with system message not found")


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
    translate.perform(cleaned, source_file="report.txt", output_dir=str(out_dir))

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
