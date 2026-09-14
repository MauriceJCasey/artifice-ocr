# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for pipeline.py's skip-reason reporting and identity-aware OCR resume.

A resumed run that reuses existing output must say *why* it skipped a stage
("not selected by the user" vs "output already exists"), and the OCR resume
check must not silently reuse another photo's output when two Tropy photos
collide on the same output stem — but it must still treat every pre-existing
sidecar-less output as valid (the non-destructive fallback).
"""

import json
import time
from pathlib import Path
from unittest.mock import patch

from artifice_ocr.pipeline import (
    SKIP_ALREADY_EXISTS,
    SKIP_NOT_SELECTED,
    _run_phase,
    run_cleanup_step,
    run_ocr_step,
    run_pipeline_batch,
    run_title_step,
    run_translate_step,
)


def _write_existing_ocr_output(output_dir, stem, text="Existing text", identity=None):
    text_dir = Path(output_dir) / "raw_ocr" / "text"
    json_dir = Path(output_dir) / "raw_ocr" / "json"
    text_path = text_dir / f"{stem}.txt"
    json_path = json_dir / f"{stem}.json"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8")
    data = {"source_file": "orig.png", "stage": "raw_ocr", "extracted_text": text}
    if identity:
        data.update(identity)
    json_path.write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------------- #
# run_ocr_step — skip reasons
# --------------------------------------------------------------------------- #


class TestOcrStepSkipReason:
    def test_skip_ocr_reports_not_selected(self, tmp_path):
        result = run_ocr_step("doc.png", str(tmp_path), skip_ocr=True)
        assert result["_skipped"] is True
        assert result["_skip_reason"] == SKIP_NOT_SELECTED

    def test_resume_with_no_sidecar_identity_falls_back_to_existence(self, tmp_path):
        """No `source` and/or a sidecar with no identity fields (every file
        that predates this feature) must still resume — non-destructive."""
        _write_existing_ocr_output(tmp_path, "doc")
        result = run_ocr_step("doc.png", str(tmp_path), resume=True, stem="doc")
        assert result["_skipped"] is True
        assert result["_skip_reason"] == SKIP_ALREADY_EXISTS
        assert result["_skip_key"] == "doc"

    def test_resume_skips_when_sidecar_identity_matches(self, tmp_path):
        _write_existing_ocr_output(tmp_path, "doc", identity={"checksum": "abc123"})
        result = run_ocr_step(
            "doc.png",
            str(tmp_path),
            resume=True,
            stem="doc",
            source={"checksum": "abc123"},
        )
        assert result["_skipped"] is True
        assert result["_skip_reason"] == SKIP_ALREADY_EXISTS
        assert result["_skip_key"] == "doc"

    def test_resume_reocrs_when_sidecar_identity_differs(self, tmp_path):
        """Two colliding photos sharing a stem: the sidecar records the FIRST
        photo's checksum, and a SECOND, different photo must not silently
        reuse its text."""
        _write_existing_ocr_output(tmp_path, "doc", identity={"checksum": "old-checksum"})
        img = tmp_path / "doc.png"
        img.write_bytes(b"fake-image-bytes")

        with patch("artifice_ocr.pipeline.ocr.perform") as mock_perform:
            mock_perform.return_value = {
                "source_file": str(img),
                "stage": "raw_ocr",
                "extracted_text": "fresh text",
            }
            result = run_ocr_step(
                str(img),
                str(tmp_path),
                resume=True,
                stem="doc",
                source={"checksum": "new-checksum"},
            )

        mock_perform.assert_called_once()
        assert result["extracted_text"] == "fresh text"
        assert result.get("_skipped") is not True

    def test_resume_falls_back_to_existence_when_current_source_has_no_identity(self, tmp_path):
        """The sidecar has identity, but the *current* photo carries none
        (e.g. an ad-hoc, non-Tropy file) — nothing to compare, so existence
        alone must still decide."""
        _write_existing_ocr_output(tmp_path, "doc", identity={"checksum": "abc123"})
        result = run_ocr_step("doc.png", str(tmp_path), resume=True, stem="doc", source=None)
        assert result["_skipped"] is True
        assert result["_skip_reason"] == SKIP_ALREADY_EXISTS

    def test_force_bypasses_resume_even_with_matching_identity(self, tmp_path):
        _write_existing_ocr_output(tmp_path, "doc", identity={"checksum": "abc123"})
        img = tmp_path / "doc.png"
        img.write_bytes(b"fake-image-bytes")

        with patch("artifice_ocr.pipeline.ocr.perform") as mock_perform:
            mock_perform.return_value = {
                "source_file": str(img),
                "stage": "raw_ocr",
                "extracted_text": "new text",
            }
            result = run_ocr_step(
                str(img),
                str(tmp_path),
                resume=True,
                force=True,
                stem="doc",
                source={"checksum": "abc123"},
            )

        mock_perform.assert_called_once()
        assert result.get("_skipped") is not True

    def test_no_existing_output_does_not_skip(self, tmp_path):
        img = tmp_path / "doc.png"
        img.write_bytes(b"fake-image-bytes")
        with patch("artifice_ocr.pipeline.ocr.perform") as mock_perform:
            mock_perform.return_value = {
                "source_file": str(img),
                "stage": "raw_ocr",
                "extracted_text": "new text",
            }
            result = run_ocr_step(str(img), str(tmp_path), resume=True, stem="doc")
        mock_perform.assert_called_once()
        assert result.get("_skipped") is not True

    def test_source_passed_through_to_ocr_perform(self, tmp_path):
        """`source` must reach `ocr.perform` so the OCR sidecar can record
        the identity of a freshly-OCR'd photo for future resumes."""
        img = tmp_path / "doc.png"
        img.write_bytes(b"fake-image-bytes")
        with patch("artifice_ocr.pipeline.ocr.perform") as mock_perform:
            mock_perform.return_value = {
                "source_file": str(img),
                "stage": "raw_ocr",
                "extracted_text": "text",
            }
            run_ocr_step(
                str(img),
                str(tmp_path),
                resume=True,
                stem="doc",
                source={"checksum": "zzz"},
            )
        _args, kwargs = mock_perform.call_args
        assert kwargs.get("source") == {"checksum": "zzz"}


# --------------------------------------------------------------------------- #
# run_cleanup_step / run_title_step / run_translate_step — skip reasons
# --------------------------------------------------------------------------- #


class TestCleanupStepSkipReason:
    def test_skip_cleanup_reports_not_selected(self, tmp_path):
        raw_data = {"source_file": "x", "extracted_text": "raw"}
        result = run_cleanup_step(raw_data, "doc", str(tmp_path), skip_cleanup=True)
        assert result["_skip_reason"] == SKIP_NOT_SELECTED

    def test_resume_cleanup_reports_already_exists(self, tmp_path):
        d = Path(tmp_path) / "cleaned" / "text"
        d.mkdir(parents=True)
        (d / "doc.txt").write_text("cleaned", encoding="utf-8")
        raw_data = {"source_file": "x", "extracted_text": "raw"}
        result = run_cleanup_step(raw_data, "doc", str(tmp_path), resume=True)
        assert result["_skip_reason"] == SKIP_ALREADY_EXISTS
        assert result["_skip_key"] == "doc"


class TestTitleStepSkipReason:
    def test_skip_title_reports_not_selected(self, tmp_path):
        cleaned_data = {"source_file": "x", "cleaned_text": "text"}
        result = run_title_step(cleaned_data, "doc", str(tmp_path), skip_title=True)
        assert result["_skip_reason"] == SKIP_NOT_SELECTED

    def test_resume_title_reports_already_exists(self, tmp_path):
        d = Path(tmp_path) / "title" / "text"
        d.mkdir(parents=True)
        (d / "doc.txt").write_text("A Title", encoding="utf-8")
        cleaned_data = {"source_file": "x", "cleaned_text": "text"}
        result = run_title_step(cleaned_data, "doc", str(tmp_path), resume=True)
        assert result["_skip_reason"] == SKIP_ALREADY_EXISTS
        assert result["_skip_key"] == "doc"


class TestTranslateStepSkipReason:
    def test_resume_translate_reports_already_exists(self, tmp_path):
        d = Path(tmp_path) / "translated" / "text"
        d.mkdir(parents=True)
        (d / "doc.txt").write_text("Translated", encoding="utf-8")
        cleaned_data = {"source_file": "x", "cleaned_text": "text"}
        result = run_translate_step(cleaned_data, "doc", str(tmp_path), resume=True)
        assert result["_skip_reason"] == SKIP_ALREADY_EXISTS
        assert result["_skip_key"] == "doc"


# --------------------------------------------------------------------------- #
# ocr.perform — the raw_ocr sidecar records the effective prompt
# --------------------------------------------------------------------------- #


def test_perform_sidecar_records_the_effective_ocr_prompt(tmp_path, monkeypatch):
    """With ``ocr_prompt_instruction`` set, the sidecar's ``ocr_prompt`` must
    record the prompt actually sent (base prompt + instruction), not the bare
    ``OCR_PROMPT`` constant."""
    from artifice_ocr.stages import ocr

    monkeypatch.setattr(
        ocr,
        "cfg",
        lambda key, default=None: {
            "ocr_prompt_instruction": "Expect handwritten German Kurrentschrift.",
            "ocr_repetition_guard": False,
        }.get(key, default),
    )
    monkeypatch.setattr(ocr, "_ocr_single_image", lambda path, orientation=1: ("text", "ollama"))

    img = tmp_path / "doc.png"
    img.write_bytes(b"fake-image-bytes")
    data = ocr.perform(str(img), output_dir=str(tmp_path / "out"), stem="doc")

    effective = ocr._effective_prompt("Expect handwritten German Kurrentschrift.")
    assert data["ocr_prompt"] == effective
    assert data["ocr_prompt"] != ocr.OCR_PROMPT

    sidecar_path = tmp_path / "out" / "raw_ocr" / "json" / "doc.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["ocr_prompt"] == effective


# --------------------------------------------------------------------------- #
# _run_phase — the shared helper behind run_pipeline_batch's four phase loops
# --------------------------------------------------------------------------- #


class TestRunPhase:
    def test_on_result_callback_fires_once_per_file_with_correct_args(self, tmp_path):
        """The one genuinely new piece of behaviour: on_result must see
        (file, result, elapsed) for every file, in order."""
        files = [tmp_path / "a.png", tmp_path / "b.png"]
        calls = []

        def step_fn(f):
            return {"_skipped": f.name == "b.png", "name": f.name}

        results, timings = _run_phase(
            files, step_fn, on_result=lambda f, r, e: calls.append((f, r, e))
        )

        assert len(calls) == 2
        for i, f in enumerate(files):
            called_file, called_result, called_elapsed = calls[i]
            assert called_file == f
            assert called_result == results[str(f)]
            assert isinstance(called_elapsed, float)
            assert called_elapsed >= 0
        # sanity: results/timings still keyed and populated as expected
        assert results[str(files[1])]["_skipped"] is True
        assert timings[str(files[1])] == 0

    def test_no_on_result_is_optional(self, tmp_path):
        """Omitting on_result must not raise — it's only invoked when given."""
        files = [tmp_path / "a.png"]
        results, timings = _run_phase(files, lambda f: {"_skipped": False})
        assert results[str(files[0])] == {"_skipped": False}
        assert str(files[0]) in timings


class TestPhaseTimingZeroOnSkipIsUniform:
    """Regression test for the bug this dedup fixes: the OCR phase used to
    store a skipped file's real (small, resume-check-only) elapsed time,
    while cleanup/title/translate all zeroed it. Both run_ocr_step and
    run_cleanup_step, driven through _run_phase, must now agree."""

    def test_ocr_phase_zeroes_a_skipped_files_timing(self, tmp_path):
        skipped_file = tmp_path / "skipped.png"
        fresh_file = tmp_path / "fresh.png"
        fresh_file.write_bytes(b"fake-image-bytes")
        _write_existing_ocr_output(tmp_path, "skipped")

        def slow_perform(*_args, **_kwargs):
            time.sleep(0.02)
            return {
                "source_file": str(fresh_file),
                "stage": "raw_ocr",
                "extracted_text": "fresh text",
            }

        with patch("artifice_ocr.pipeline.ocr.perform", side_effect=slow_perform):
            results, timings = _run_phase(
                [skipped_file, fresh_file],
                lambda f: run_ocr_step(f, str(tmp_path), resume=True),
            )

        assert results[str(skipped_file)]["_skipped"] is True
        assert timings[str(skipped_file)] == 0
        assert results[str(fresh_file)].get("_skipped") is not True
        assert timings[str(fresh_file)] > 0

    def test_cleanup_phase_zeroes_a_skipped_files_timing(self, tmp_path):
        d = tmp_path / "cleaned" / "text"
        d.mkdir(parents=True)
        (d / "skipped.txt").write_text("already cleaned", encoding="utf-8")

        raw_data = {"source_file": "x", "extracted_text": "raw text"}

        def slow_perform(*_args, **_kwargs):
            time.sleep(0.02)
            return {"source_file": "x", "stage": "cleaned", "cleaned_text": "cleaned text"}

        with patch("artifice_ocr.pipeline.cleanup.perform", side_effect=slow_perform):
            results, timings = _run_phase(
                [Path("skipped"), Path("fresh")],
                lambda f: run_cleanup_step(raw_data, f.stem, str(tmp_path), resume=True),
            )

        assert results[str(Path("skipped"))]["_skipped"] is True
        assert timings[str(Path("skipped"))] == 0
        assert results[str(Path("fresh"))].get("_skipped") is not True
        assert timings[str(Path("fresh"))] > 0


# --------------------------------------------------------------------------- #
# run_pipeline_batch — shape of the returned dict is unchanged by the refactor
# --------------------------------------------------------------------------- #


class TestRunPipelineBatchShape:
    def test_all_stages_not_selected_batch_shape(self, tmp_path):
        """Cheap end-to-end pass: every stage opted out (skip_ocr,
        skip_cleanup, skip_translate; title_enabled defaults False), so no
        real model call happens anywhere. Confirms run_pipeline_batch's
        returned shape — files/batch_size/batch_elapsed/timings, and the
        now-uniform zero-on-skip timing rule — survived the loop-to-helper
        extraction."""
        file_a = tmp_path / "a.png"
        file_b = tmp_path / "b.png"

        batch = run_pipeline_batch(
            [str(file_a), str(file_b)],
            str(tmp_path),
            skip_translate=True,
            skip_cleanup=True,
            skip_ocr=True,
        )

        assert batch["batch_size"] == 2
        assert isinstance(batch["batch_elapsed"], float)
        assert set(batch["files"].keys()) == {str(file_a.resolve()), str(file_b.resolve())}
        assert set(batch["timings"].keys()) == {str(file_a.resolve()), str(file_b.resolve())}

        for fpath in batch["files"]:
            file_result = batch["files"][fpath]
            assert file_result["raw"]["_skipped"] is True
            assert file_result["cleaned"]["_skipped"] is True
            assert "title" not in file_result
            assert "translated" not in file_result
            # both ocr and cleanup were skipped-by-user: ocr's timing key is
            # always present (zeroed); cleanup's is omitted entirely, since
            # the assemble-results section only adds it when truthy.
            assert batch["timings"][fpath] == {"ocr": 0}
