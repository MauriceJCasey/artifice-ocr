# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Security tests for OCR: credential redaction and file permissions."""

from __future__ import annotations

import json

from secure_io import is_restricted


class TestConfigFilePermissions:
    """Restrictive file permissions for settings.json — finding #3."""

    def test_save_user_settings_creates_restricted_file(self, tmp_path, monkeypatch):
        """save_user_settings must write a file restricted to the current user."""
        from artifice_ocr import config

        monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.setattr(config, "_USER_DIR", tmp_path)

        # Save a setting that includes a credential-like value.
        config.save_user_settings({"api_key": "sk-test-secret", "output_dir": "/tmp"})

        settings_file = tmp_path / "settings.json"
        assert settings_file.exists()
        assert is_restricted(settings_file)

    def test_contents_are_preserved(self, tmp_path, monkeypatch):
        """The restrictive open must still write correct JSON."""
        from artifice_ocr import config

        monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.setattr(config, "_USER_DIR", tmp_path)

        config.save_user_settings({"api_key": "sk-test-123", "output_dir": "/out"})
        config.save_user_settings({"output_dir": "/overwritten"})

        data = json.loads((tmp_path / "settings.json").read_text())
        assert data["api_key"] == "sk-test-123"
        assert data["output_dir"] == "/overwritten"

    def test_saved_settings_are_loaded_after_restart(self, tmp_path, monkeypatch):
        from artifice_ocr import config

        monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.setattr(config, "_USER_DIR", tmp_path)
        config.save_user_settings({"ocr_model": "local-vision", "output_dir": "/archive"})

        config.reset()
        loaded = config.load_config()

        assert loaded["ocr_model"] == "local-vision"
        assert loaded["output_dir"] == "/archive"


class TestLoadTimePermissionRepair:
    """Permissions on pre-existing loose files are repaired at load time.

    These tests exercise the POSIX branch (os.chmod).  The Windows
    branch (icacls) is verified manually on native Windows.
    """

    def test_loose_file_is_repaired_on_load(self, tmp_path, monkeypatch):
        """A file created at 0o644 is tightened to 0o600 on load."""
        import os

        from artifice_ocr import config

        settings_file = tmp_path / "settings.json"
        monkeypatch.setattr(config, "_SETTINGS_PATH", settings_file)
        monkeypatch.setattr(config, "_USER_DIR", tmp_path)

        # Create a loose file with a credential.
        settings_file.write_text('{"api_key": "sk-old-secret"}')
        os.chmod(settings_file, 0o644)
        assert not is_restricted(settings_file)

        # Loading must repair it.
        result = config.load_user_settings()
        assert is_restricted(settings_file)
        assert result["api_key"] == "sk-old-secret"

    def test_load_succeeds_when_repair_raises(self, tmp_path, monkeypatch, caplog):
        """Load must still return the data even when the repair fails."""
        import os

        from artifice_ocr import config

        settings_file = tmp_path / "settings.json"
        monkeypatch.setattr(config, "_SETTINGS_PATH", settings_file)
        monkeypatch.setattr(config, "_USER_DIR", tmp_path)

        # Create a loose file.
        settings_file.write_text('{"api_key": "sk-old-secret"}')
        os.chmod(settings_file, 0o644)

        def _failing_restrict(_path):
            raise OSError("Simulated ACL failure — exFAT volume")

        monkeypatch.setattr("secure_io.restrict_to_current_user", _failing_restrict)

        # Load must succeed despite the repair failure.
        result = config.load_user_settings()
        assert result["api_key"] == "sk-old-secret"

        # The warning must have been logged.
        assert "Could not restrict permissions" in caplog.text


class TestCredentialRedaction:
    """API key redaction in config endpoint — finding #5."""

    def test_get_config_redacts_api_key(self, monkeypatch, tmp_path):
        """GET /api/config must return a placeholder, not the real key."""
        from artifice_ocr import config
        from artifice_ocr.web.routers.settings import get_config

        monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.setattr(config, "_USER_DIR", tmp_path)
        config.reset()
        config.load_config()
        config.apply_overrides({"api_key": "sk-real-secret"})

        result = get_config()
        assert result["api_key"] == "*" * 12

    def test_get_config_redacts_huggingface_token(self, monkeypatch, tmp_path):
        """Hugging Face tokens must also be redacted."""
        from artifice_ocr import config
        from artifice_ocr.web.routers.settings import get_config

        monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.setattr(config, "_USER_DIR", tmp_path)
        config.reset()
        config.load_config()
        config.apply_overrides({"huggingface_token": "hf_secret_token_abc"})

        result = get_config()
        assert result["huggingface_token"] == "*" * 12

    def test_get_config_preserves_empty_key(self, monkeypatch, tmp_path):
        """An empty/None api_key must not be redacted to asterisks."""
        from artifice_ocr import config
        from artifice_ocr.web.routers.settings import get_config

        monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.setattr(config, "_USER_DIR", tmp_path)
        config.reset()
        config.load_config()
        config.apply_overrides({"api_key": ""})

        result = get_config()
        assert result["api_key"] == ""
