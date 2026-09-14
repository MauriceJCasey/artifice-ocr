# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pure text-diffing helpers shared by every frontend.

Originally lived inside `gui/widgets/compare_view.py`, which is otherwise a
tkinter module. Moved here, with no logic changed, so the web frontend's
Preview tab can compute the identical highlight ranges without importing
tkinter — `difflib` and `re` are the only dependencies either frontend needs
for this.
"""

import difflib
import re

from ._confidence import _UNCERTAINTY_MARKERS


def confidence_tier(confidence: int | None) -> str:
    """Bucket a confidence score for colour-coding. Threshold source of truth
    for every frontend — desktop maps tiers to theme colours, the web build
    maps them to CSS classes."""
    if confidence is None:
        return "none"
    if confidence >= 80:
        return "high"
    if confidence >= 55:
        return "medium"
    return "low"


def diff_ranges(raw: str, cleaned: str) -> tuple[list, list]:
    """Character ranges that differ between raw and cleaned text.

    Diffing on words rather than characters keeps the highlight readable —
    character-level opcodes on OCR text produce confetti.
    """
    raw_words = re.findall(r"\S+\s*", raw)
    clean_words = re.findall(r"\S+\s*", cleaned)

    raw_offsets, pos = [], 0
    for w in raw_words:
        raw_offsets.append(pos)
        pos += len(w)
    clean_offsets, pos = [], 0
    for w in clean_words:
        clean_offsets.append(pos)
        pos += len(w)

    matcher = difflib.SequenceMatcher(
        None, [w.strip() for w in raw_words], [w.strip() for w in clean_words],
        autojunk=False,
    )

    raw_ranges, clean_ranges = [], []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        tag = f"{op}_"
        if op in ("delete", "replace") and i1 < len(raw_offsets):
            start = raw_offsets[i1]
            end = raw_offsets[i2 - 1] + len(raw_words[i2 - 1]) if i2 > i1 else start
            raw_ranges.append((start, end, "delete_" if op == "delete" else tag))
        if op in ("insert", "replace") and j1 < len(clean_offsets):
            start = clean_offsets[j1]
            end = clean_offsets[j2 - 1] + len(clean_words[j2 - 1]) if j2 > j1 else start
            clean_ranges.append((start, end, "insert_" if op == "insert" else tag))

    return raw_ranges, clean_ranges


def marker_ranges(text: str) -> list[tuple[int, int, str]]:
    """Highlight the uncertainty markers the confidence scorer looks for."""
    ranges = []
    lowered = text.lower()
    for marker in _UNCERTAINTY_MARKERS:
        if marker == "...":  # too common in ordinary prose to be worth flagging
            continue
        start = 0
        while True:
            idx = lowered.find(marker, start)
            if idx == -1:
                break
            ranges.append((idx, idx + len(marker), "marker"))
            start = idx + len(marker)
    return ranges
