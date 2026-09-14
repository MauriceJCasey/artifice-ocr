# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the shared text-diff helpers in ``artifice_ocr._diff``.

Originally tested via imports from ``gui.widgets.compare_view``, but the
functions themselves live in ``artifice_ocr._diff`` — the web frontend uses
them directly, so these tests must be kept.
"""

from artifice_ocr._diff import diff_ranges, marker_ranges


def test_diff_ranges_flags_changed_words():
    raw = "the quick br0wn fox"
    cleaned = "the quick brown fox"
    raw_ranges, clean_ranges = diff_ranges(raw, cleaned)

    assert raw_ranges and clean_ranges
    start, end, tag = clean_ranges[0]
    assert cleaned[start:end].strip() == "brown"
    assert tag == "replace_"


def test_diff_ranges_identical_text_has_no_highlights():
    text = "identical in both panes"
    assert diff_ranges(text, text) == ([], [])


def test_marker_ranges_finds_uncertainty_markers():
    text = "The date is [illegible] and the name is unclear."
    ranges = marker_ranges(text)
    found = {text[s:e].lower() for s, e, _ in ranges}

    assert "[illegible]" in found
    assert "unclear" in found
    assert all(tag == "marker" for _, _, tag in ranges)
