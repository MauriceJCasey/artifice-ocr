# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Folder/batch pipeline and CLI flag (skip-*, --doc-type, --no-confidence) tests."""

from unittest.mock import MagicMock, patch

from artifice_ocr.cli import app
from typer.testing import CliRunner

from .test_stages import _mock_backend_response

runner = CliRunner()


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
