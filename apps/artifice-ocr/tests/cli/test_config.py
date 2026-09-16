# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Config loading, preflight command, and apply_overrides tests."""

from unittest.mock import patch

from artifice_ocr.cli import app
from typer.testing import CliRunner

runner = CliRunner()


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
