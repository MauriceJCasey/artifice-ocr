# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the title stage — Pydantic schema, perform() with mocked
harness, guard behaviour, pipeline integration, and resume/skip logic."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from model_harness.contract import (
    HarnessResult,
    StructuredOutputMode,
    StructuredOutputUnsupported,
)
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _set_resolved_config():
    """Title reads model/backend through the resolver accessors, which fall
    back to real config (not the module's ``cfg`` that individual tests patch
    for the other keys).  Provide the model/backend the tests expect."""
    from artifice_ocr import _resolution, config

    config.reset()
    _resolution.reset()
    config.apply_overrides({"cleanup_model": "gemma4:12b", "cleanup_backend": "ollama"})
    yield
    config.reset()
    _resolution.reset()


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestPageTitleSchema:
    def test_accepts_valid_title(self):
        from artifice_ocr.stages.title import PageTitleSchema

        data = PageTitleSchema(title="Meeting Minutes of the Board", language="en")
        assert data.title == "Meeting Minutes of the Board"
        assert data.language == "en"

    def test_rejects_missing_title(self):
        from artifice_ocr.stages.title import PageTitleSchema

        with pytest.raises(ValidationError):
            PageTitleSchema(language="en")  # type: ignore[arg-type]

    def test_rejects_missing_language(self):
        from artifice_ocr.stages.title import PageTitleSchema

        with pytest.raises(ValidationError):
            PageTitleSchema(title="Some Title")  # type: ignore[arg-type]

    def test_rejects_title_over_120_chars(self):
        from artifice_ocr.stages.title import PageTitleSchema

        long_title = "A" * 121
        with pytest.raises(ValidationError):
            PageTitleSchema(title=long_title, language="en")

    def test_accepts_title_at_120_chars(self):
        from artifice_ocr.stages.title import PageTitleSchema

        title = "A" * 120
        data = PageTitleSchema(title=title, language="de")
        assert len(data.title) == 120


# ---------------------------------------------------------------------------
# perform() — mocked harness calls
# ---------------------------------------------------------------------------


class TestPerform:
    """Tests for title.perform() with mocked run_structured."""

    def test_generates_title_and_writes_output(self, tmp_path):
        """Happy path: model returns a valid title, output files written."""
        valid_data = MagicMock(title="Minutes of the Housing Committee", language="en")

        mock_result = HarnessResult(
            data=valid_data,
            mode_used=StructuredOutputMode.PROMPTED,
            model="gemma4:12b",
            raw='{"title": "Minutes of the Housing Committee", "language": "en"}',
            repaired=False,
        )

        with (
            patch(
                "artifice_ocr.stages.title.run_structured",
                return_value=mock_result,
            ),
            patch(
                "artifice_ocr.stages.title.cfg",
                lambda k: {
                    "cleanup_model": "gemma4:12b",
                    "cleanup_backend": "ollama",
                    "ollama_url": "http://localhost:11434",
                    "lm_studio_url": "http://localhost:1234/v1",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "title_max_chars": 120,
                }.get(k),
            ),
        ):
            from artifice_ocr.stages.title import perform

            result = perform(
                "The Housing Committee met on Tuesday...",
                source_file="/path/to/doc.jpg",
                output_dir=str(tmp_path),
            )

        assert result["title"] == "Minutes of the Housing Committee"
        assert result["language"] == "en"
        assert result["generated_by_model"] is True
        assert result["model"] == "gemma4:12b"
        assert result["mode_used"] == "prompted"
        assert result["repaired"] is False

        # Output files
        text_path = tmp_path / "title" / "text" / "doc.txt"
        json_path = tmp_path / "title" / "json" / "doc.json"
        assert text_path.exists()
        assert text_path.read_text(encoding="utf-8") == "Minutes of the Housing Committee"
        assert json_path.exists()
        saved = json.loads(json_path.read_text(encoding="utf-8"))
        assert saved["generated_by_model"] is True
        assert saved["model"] == "gemma4:12b"

    def test_falls_back_on_structured_output_unsupported(self, tmp_path):
        """When the harness raises StructuredOutputUnsupported, fall back to basename."""
        with (
            patch(
                "artifice_ocr.stages.title.run_structured",
                side_effect=StructuredOutputUnsupported("no structured output"),
            ),
            patch(
                "artifice_ocr.stages.title.cfg",
                lambda k: {
                    "cleanup_model": "gemma4:12b",
                    "cleanup_backend": "ollama",
                    "ollama_url": "http://localhost:11434",
                    "lm_studio_url": "http://localhost:1234/v1",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "title_max_chars": 120,
                }.get(k),
            ),
        ):
            from artifice_ocr.stages.title import perform

            result = perform(
                "Some text...",
                source_file="/path/to/doc.jpg",
                output_dir=str(tmp_path),
            )

        assert result["title"] == "doc"
        assert result["generated_by_model"] is False
        assert result["error"] is not None
        assert "no structured output" in result["error"]

    def test_falls_back_on_general_exception(self, tmp_path):
        """Any unexpected exception should also fall back gracefully."""
        with (
            patch(
                "artifice_ocr.stages.title.run_structured",
                side_effect=RuntimeError("connection refused"),
            ),
            patch(
                "artifice_ocr.stages.title.cfg",
                lambda k: {
                    "cleanup_model": "gemma4:12b",
                    "cleanup_backend": "ollama",
                    "ollama_url": "http://localhost:11434",
                    "lm_studio_url": "http://localhost:1234/v1",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "title_max_chars": 120,
                }.get(k),
            ),
        ):
            from artifice_ocr.stages.title import perform

            result = perform(
                "Some text...",
                source_file="/path/to/doc.jpg",
                output_dir=str(tmp_path),
            )

        assert result["generated_by_model"] is False
        assert "connection refused" in result["error"]
        # Fallback title written to disk
        text_path = tmp_path / "title" / "text" / "doc.txt"
        assert text_path.read_text(encoding="utf-8") == "doc"

    def test_truncates_overlong_title(self, tmp_path):
        """Title > max_chars should be truncated, not rejected."""
        long_title = (
            "A Very Long Title That Exceeds The Maximum Character Count "
            "And Should Be Truncated Down To Size Without Failing The Pipeline Entirely"
        )
        assert len(long_title) > 120

        valid_data = MagicMock(title=long_title, language="en")
        mock_result = HarnessResult(
            data=valid_data,
            mode_used=StructuredOutputMode.PROMPTED,
            model="gemma4:12b",
            raw="{}",
            repaired=False,
        )

        with (
            patch(
                "artifice_ocr.stages.title.run_structured",
                return_value=mock_result,
            ),
            patch(
                "artifice_ocr.stages.title.cfg",
                lambda k: {
                    "cleanup_model": "gemma4:12b",
                    "cleanup_backend": "ollama",
                    "ollama_url": "http://localhost:11434",
                    "lm_studio_url": "http://localhost:1234/v1",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "title_max_chars": 120,
                }.get(k),
            ),
        ):
            from artifice_ocr.stages.title import perform

            result = perform(
                "Some text...",
                source_file="/path/to/doc.jpg",
                output_dir=str(tmp_path),
            )

        assert len(result["title"]) == 120
        assert result["guard"].get("truncated") is True

    def test_rejects_repetitive_title(self, tmp_path):
        """A title of repeated identical substrings falls back to basename."""
        valid_data = MagicMock(title="word word word word word", language="en")
        mock_result = HarnessResult(
            data=valid_data,
            mode_used=StructuredOutputMode.PROMPTED,
            model="gemma4:12b",
            raw="{}",
            repaired=False,
        )

        with (
            patch(
                "artifice_ocr.stages.title.run_structured",
                return_value=mock_result,
            ),
            patch(
                "artifice_ocr.stages.title.cfg",
                lambda k: {
                    "cleanup_model": "gemma4:12b",
                    "cleanup_backend": "ollama",
                    "ollama_url": "http://localhost:11434",
                    "lm_studio_url": "http://localhost:1234/v1",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "title_max_chars": 120,
                }.get(k),
            ),
        ):
            from artifice_ocr.stages.title import perform

            result = perform(
                "Some text...",
                source_file="/path/to/doc.jpg",
                output_dir=str(tmp_path),
            )

        assert result["title"] == "doc"
        assert result["generated_by_model"] is False

    def test_does_not_reject_good_repetition(self, tmp_path):
        """A title with natural repetition (non-looping) should pass."""
        valid_data = MagicMock(title="The The End of Days Report", language="en")
        mock_result = HarnessResult(
            data=valid_data,
            mode_used=StructuredOutputMode.PROMPTED,
            model="gemma4:12b",
            raw="{}",
            repaired=False,
        )

        with (
            patch(
                "artifice_ocr.stages.title.run_structured",
                return_value=mock_result,
            ),
            patch(
                "artifice_ocr.stages.title.cfg",
                lambda k: {
                    "cleanup_model": "gemma4:12b",
                    "cleanup_backend": "ollama",
                    "ollama_url": "http://localhost:11434",
                    "lm_studio_url": "http://localhost:1234/v1",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "title_max_chars": 120,
                }.get(k),
            ),
        ):
            from artifice_ocr.stages.title import perform

            result = perform(
                "Report text...",
                source_file="/path/to/doc.jpg",
                output_dir=str(tmp_path),
            )

        # "The" appears twice but that's not a loop — 2/6 < 0.5
        assert result["generated_by_model"] is True
        assert result["title"] == "The The End of Days Report"

    def test_provenance_recorded(self, tmp_path):
        """Result must carry generated_by_model, model, mode_used, repaired."""
        valid_data = MagicMock(title="Test Title", language="fr")
        mock_result = HarnessResult(
            data=valid_data,
            mode_used=StructuredOutputMode.JSON_OBJECT,
            model="gemma4:12b",
            raw="{}",
            repaired=True,
        )

        with (
            patch(
                "artifice_ocr.stages.title.run_structured",
                return_value=mock_result,
            ),
            patch(
                "artifice_ocr.stages.title.cfg",
                lambda k: {
                    "cleanup_model": "gemma4:12b",
                    "cleanup_backend": "ollama",
                    "ollama_url": "http://localhost:11434",
                    "lm_studio_url": "http://localhost:1234/v1",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "title_max_chars": 120,
                }.get(k),
            ),
        ):
            from artifice_ocr.stages.title import perform

            result = perform(
                "French text...",
                source_file="/path/to/doc.jpg",
                output_dir=str(tmp_path),
            )

        assert result["generated_by_model"] is True
        assert result["model"] == "gemma4:12b"
        assert result["mode_used"] == "json_object"
        assert result["repaired"] is True
        assert result["language"] == "fr"


# ---------------------------------------------------------------------------
# run_title_step() — pipeline integration
# ---------------------------------------------------------------------------


class TestRunTitleStep:
    def test_skip_title(self, tmp_path):
        """skip_title=True returns basename and marks _skipped."""
        from artifice_ocr.pipeline import run_title_step

        cleaned_data = {
            "source_file": "/path/to/doc.jpg",
            "cleaned_text": "Some text",
        }
        result = run_title_step(
            cleaned_data,
            "doc",
            str(tmp_path),
            skip_title=True,
        )
        assert result["title"] == "doc"
        assert result.get("_skipped") is True

    @patch("artifice_ocr.pipeline._output_exists")
    @patch("artifice_ocr.pipeline._load_existing_text")
    def test_resume_loads_existing(self, mock_load, mock_exists, tmp_path):
        """With resume=True and existing output, load from disk."""
        mock_exists.return_value = True
        mock_load.return_value = "Previously Generated Title"

        from artifice_ocr.pipeline import run_title_step

        cleaned_data = {
            "source_file": "/path/to/doc.jpg",
            "cleaned_text": "Some text",
        }
        result = run_title_step(
            cleaned_data,
            "doc",
            str(tmp_path),
            resume=True,
            force=False,
        )
        assert result["title"] == "Previously Generated Title"
        assert result.get("_skipped") is True
        mock_exists.assert_called_once()
        mock_load.assert_called_once()

    @patch("artifice_ocr.pipeline._output_exists")
    def test_force_bypasses_resume(self, mock_exists, tmp_path):
        """force=True should re-run even if output exists."""
        mock_exists.return_value = True

        valid_data = MagicMock(title="Fresh Title", language="en")
        mock_result = HarnessResult(
            data=valid_data,
            mode_used=StructuredOutputMode.PROMPTED,
            model="gemma4:12b",
            raw="{}",
            repaired=False,
        )

        with (
            patch(
                "artifice_ocr.stages.title.run_structured",
                return_value=mock_result,
            ),
            patch(
                "artifice_ocr.stages.title.cfg",
                lambda k: {
                    "cleanup_model": "gemma4:12b",
                    "cleanup_backend": "ollama",
                    "ollama_url": "http://localhost:11434",
                    "lm_studio_url": "http://localhost:1234/v1",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "title_max_chars": 120,
                }.get(k),
            ),
        ):
            from artifice_ocr.pipeline import run_title_step

            cleaned_data = {
                "source_file": "/path/to/doc.jpg",
                "cleaned_text": "Some text",
            }
            result = run_title_step(
                cleaned_data,
                "doc",
                str(tmp_path),
                resume=True,
                force=True,
            )
            assert result["title"] == "Fresh Title"
            assert result.get("_skipped") is not True  # not skipped — it ran


# ---------------------------------------------------------------------------
# Repetition helper
# ---------------------------------------------------------------------------


class TestCheckTitleRepetition:
    def test_detects_all_same_word(self):
        from artifice_ocr.stages.title import _check_title_repetition

        assert _check_title_repetition("word word word word word") is True

    def test_allows_normal_title(self):
        from artifice_ocr.stages.title import _check_title_repetition

        assert _check_title_repetition("Minutes of the Board Meeting") is False

    def test_short_title_bypasses_check(self):
        from artifice_ocr.stages.title import _check_title_repetition

        # 2 words — too short for meaningful check
        assert _check_title_repetition("same same") is False

    def test_case_insensitive(self):
        from artifice_ocr.stages.title import _check_title_repetition

        assert _check_title_repetition("Word word Word word Word") is True
