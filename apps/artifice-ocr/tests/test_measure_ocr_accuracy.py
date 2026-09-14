# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Character-error-rate harness. See scripts/measure_ocr_accuracy.py for why
CER, not SpACER — this repo has no accuracy harness at all today
(docs/superpowers/plans/2026-09-09-olmocr2-optimisation.md, Task 5, "the
blocker under all of this"), so a correct-but-imperfect metric that exists
beats a perfect one that doesn't."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from measure_ocr_accuracy import character_error_rate, measure_page  # noqa: E402


def test_identical_strings_have_zero_error():
    assert character_error_rate("hello world", "hello world") == 0.0


def test_completely_different_strings_have_high_error():
    assert character_error_rate("abc", "xyz") == 1.0


def test_empty_reference_and_empty_hypothesis_is_zero_error():
    assert character_error_rate("", "") == 0.0


def test_empty_reference_nonempty_hypothesis_is_full_error():
    assert character_error_rate("", "abc") == 1.0


def test_single_substitution_out_of_ten_chars():
    assert character_error_rate("abcdefghij", "abcdefghiX") == 0.1


def test_measure_page_reports_error_rate_and_wall_time(tmp_path, monkeypatch):
    gt_path = tmp_path / "page.txt"
    gt_path.write_text("hello world", encoding="utf-8")
    img_path = tmp_path / "page.png"
    img_path.write_bytes(b"fake-png")

    import measure_ocr_accuracy as mod

    monkeypatch.setattr(mod, "_ocr_page", lambda path, orientation: "hello world")

    result = measure_page(img_path, gt_path)

    assert result["cer"] == 0.0
    assert result["wall_time_s"] >= 0.0
    assert result["page"] == "page.png"
