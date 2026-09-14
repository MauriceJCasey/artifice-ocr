# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Content-preservation guards: never trust a model's output just because
the prompt asked nicely.

`check()` guards the cleanup stage. Cleanup asks a language model to rewrite
a page of archival text. Measured over 130 real pages, it left 64% untouched,
made genuine repairs on some of the rest (`CAHTOLIC` -> `CATHOLIC`,
`ERNSTFRIEDRHCH` -> `ERNSTFRIEDRICH`), and on a minority did real damage: it
corrupted words that were already correct (`Gewerkschaftern` ->
`Gewerkshaftern`), altered a place name (`Elsass` -> `Elass`), and on
fragmentary pages deleted whole clauses it could not parse.

The guard makes that failure mode safe. It compares the model's output against
the source and, if the output looks lossy or has altered a proper noun,
discards it and keeps the raw text. A page is therefore either cleaned or
untouched — never quietly truncated.

The check is deliberately whole-page rather than per-edit: reverting individual
edits would produce a text that never existed in either version, which is worse
than a clean no-op and much harder to audit.

`check_structure_only()` guards the structure stage with a stricter,
whitespace-only version of the same idea. `check_no_repetition_loop()`
guards the OCR stage itself against a different failure mode entirely — not
a model rewriting real content, but a model hallucinating filler and
looping on it when the source image gives it nothing real to transcribe
(confirmed on a page scanned upside-down with no orientation metadata
anywhere to say so). There is no source text to fall back to for that one;
see its own docstring.
"""

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ._logging import get_logger
from .config import get as cfg

log = get_logger("guard")

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_UMLAUT = re.compile(r"[äöüÄÖÜßéèêàâçñ]")

# Letters AND digit-sequences both count as tokens — deliberately different
# from _WORD above. _WORD excludes digits because it feeds the proper-noun
# check, where a number is never a name worth protecting. The n-gram
# repetition check below has the opposite requirement: a genuine archival
# page can legitimately repeat the same sentence template with only a date,
# quantity, or page number differing ("Im Jahre 1936 ... 1733 kg." / "Im
# Jahre 1938 ... 1543 kg."). Stripping those digits first would make such a
# page collapse into an apparent short repeating cycle and get misflagged as
# a decoding loop. Keeping digits as tokens costs nothing against the real
# loop patterns this check exists to catch — none of the calibration
# samples in check_no_repetition_loop's docstring contain any numbers at
# all — but closes that false-positive path.
_NGRAM_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

# Capitalised tokens shorter than this are too noisy to protect (initials,
# "Der", "Am", roman numerals).
_MIN_NOUN_LEN = 4

# Word-level repetition-loop detection (see check_no_repetition_loop): the
# n-gram width, the distinct-ratio cutoff below which text is flagged, and
# the minimum word count before the check is meaningful at all.
_NGRAM_N = 4
_MAX_DISTINCT_NGRAM_RATIO = 0.20
_MIN_WORDS_FOR_NGRAM_CHECK = 40


@dataclass
class GuardResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    words_deleted: int = 0
    nouns_dropped: list[str] = field(default_factory=list)
    length_ratio: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": self.reasons,
            "words_deleted": self.words_deleted,
            "nouns_dropped": self.nouns_dropped[:20],
            "length_ratio": round(self.length_ratio, 4),
        }


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _ngram_tokens(text: str) -> list[str]:
    """Tokenize for `check_no_repetition_loop`'s n-gram measurement — unlike
    `_words`, digit-sequences count as tokens too. See `_NGRAM_TOKEN`."""
    return _NGRAM_TOKEN.findall(text)


def _distinct_ngram_ratio(words: list[str], n: int) -> float:
    """Fraction of overlapping *n*-word windows in *words* that are unique.

    1.0 means every n-gram is distinct (normal prose); a value near 0 means
    the text is dominated by a small, repeating set of n-grams — the
    signature of a decoding loop. See `check_no_repetition_loop`'s docstring
    for how the cutoff that uses this was calibrated.
    """
    if len(words) < n:
        return 1.0
    grams = [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]
    return len(set(grams)) / len(grams)


def _proper_nouns(text: str) -> Counter:
    """Capitalised tokens, excluding ones that merely start a sentence.

    Counted rather than collected into a set: a page may use a name several
    times and have only one occurrence corrupted. `Elsass` appearing five
    times and `Elass` once still leaves `Elsass` present, so set membership
    would miss the corruption entirely.
    """
    nouns: Counter = Counter()
    for match in _WORD.finditer(text):
        token = match.group()
        if len(token) < _MIN_NOUN_LEN or not token[:1].isupper():
            continue
        preceding = text[max(0, match.start() - 2) : match.start()]
        if preceding.strip() in ("", ".", "!", "?", ":", ";"):
            continue  # sentence-initial; capitalisation carries no information
        nouns[token] += 1
    return nouns


def check(raw: str, cleaned: str) -> GuardResult:
    """Decide whether `cleaned` is a safe replacement for `raw`."""
    result = GuardResult(ok=True)

    if not cleaned.strip():
        result.ok = False
        result.reasons.append("output empty")
        return result

    raw_words, clean_words = _words(raw), _words(cleaned)

    # Measured in letters, not characters. A correct repair removes hyphens,
    # newlines and doubled spaces, so a raw-character ratio punishes exactly
    # the behaviour cleanup exists for: rejoining "Be-\nricht" into "Bericht"
    # is a 2-character loss and zero letters lost.
    raw_letters = sum(len(w) for w in raw_words)
    clean_letters = sum(len(w) for w in clean_words)
    result.length_ratio = (clean_letters / raw_letters) if raw_letters else 1.0

    # 1. Whole words removed. Replacements are edits; deletions are losses.
    #    A rejoined hyphenated word shows up as a replace, not a delete, so
    #    legitimate repairs do not trip this.
    matcher = difflib.SequenceMatcher(None, raw_words, clean_words, autojunk=False)
    deleted = sum(i2 - i1 for op, i1, i2, _, _ in matcher.get_opcodes() if op == "delete")
    result.words_deleted = deleted

    max_deleted = cfg("cleanup_guard_max_deleted_words")
    if deleted > max_deleted:
        result.ok = False
        result.reasons.append(f"{deleted} word(s) deleted from the source (limit {max_deleted})")

    # 2. Shrinkage, which catches losses spread across many small edits.
    min_ratio = cfg("cleanup_guard_min_length_ratio")
    if result.length_ratio < min_ratio:
        result.ok = False
        result.reasons.append(
            f"output keeps {result.length_ratio:.0%} of the source letters (limit {min_ratio:.0%})"
        )

    # 3. Proper nouns. A wrong "correction" to a name is invisible to a reader
    #    and breaks full-text search, so names are not the model's to edit.
    if cfg("cleanup_guard_protect_nouns"):
        # Counter subtraction keeps only positive residues, i.e. nouns that
        # lost at least one occurrence.
        missing = _proper_nouns(raw) - _proper_nouns(cleaned)
        dropped = sorted(missing)
        result.nouns_dropped = dropped
        if dropped:
            result.ok = False
            result.reasons.append(f"proper noun(s) altered or dropped: {', '.join(dropped[:5])}")

    # 4. Modernisation: accents appearing where the source had none.
    if _UMLAUT.search(cleaned) and not _UMLAUT.search(raw):
        result.ok = False
        result.reasons.append("accents introduced where the source had none")

    return result


def check_structure_only(original: str, structured: str) -> GuardResult:
    """Verify that structuring preserved every word of the original.

    This is stricter than check() — it requires exact word-sequence equality
    (whitespace-insensitive). The structuring stage must never alter a word;
    it may only add paragraph breaks and blank lines.
    """
    result = GuardResult(ok=True)

    if not structured.strip():
        result.ok = False
        result.reasons.append("output empty")
        return result

    original_words = _words(original)
    structured_words = _words(structured)

    if len(original_words) != len(structured_words):
        result.ok = False
        result.words_deleted = abs(len(original_words) - len(structured_words))
        if len(original_words) > len(structured_words):
            result.reasons.append(
                f"structured text has {result.words_deleted} fewer word(s) than original"
            )
        else:
            result.reasons.append(
                f"structured text has {result.words_deleted} more word(s) than original"
            )
        return result

    for i, (orig, struct) in enumerate(zip(original_words, structured_words)):
        if orig != struct:
            result.ok = False
            result.reasons.append(f"word {i + 1} differs: '{orig}' -> '{struct}'")
            break

    return result


def check_no_repetition_loop(
    text: str,
    *,
    min_lines: int = 20,
    max_unique_ratio: float = 0.3,
) -> GuardResult:
    """Detect a degenerate decoding loop in raw OCR output: the model
    repeating the same line, or cycling through a short handful of lines,
    over and over instead of transcribing real content.

    Confirmed live on a real archive page fed to the OCR stage upside-down
    (Tropy `orientation` was 1/normal — nobody had flagged the scan, so
    nothing told the model or the pipeline it was inverted). With no
    genuine signal to transcribe, greedy decoding (`temperature=0`, no
    frequency/presence penalty) hallucinated a plausible-sounding German
    sentence and then had no mechanism to stop repeating it — 900+ lines of
    the same sentence on one page, a 3-line cycle repeated ~30 times on
    another. A same-line-N-times-in-a-row check catches the first pattern
    but not the second, so this counts overall line diversity instead:
    across 9 real pages from that one archive folder, genuinely garbled
    pages sat between 0.3% and 4% unique lines; genuinely good ones sat at
    60%+. The 30% cutoff below sits with a wide margin on both sides of
    that real data.

    Unlike `check()`/`check_structure_only()`, there is no source text to
    fall back to here — a rejected OCR page has nothing safe to substitute,
    so the caller should treat `not result.ok` as a hard failure, not a
    silent revert.

    The line-based check above has a blind spot: it counts non-blank lines,
    so a loop that produces continuous prose with no line breaks at all —
    a short phrase or sentence repeated hundreds of times with nothing
    splitting it into separate lines — collapses to a handful of "lines"
    (sometimes exactly one) and never reaches the line-diversity check at
    all. This is a second real production failure mode, not a hypothetical
    one: `allenai/olmocr-2-7b` via LM Studio has looped this way on live
    pages, sometimes producing 20,000+ characters of one repeated unit on a
    single page with the line check reporting `ok=True` throughout because
    `len(lines) < min_lines` was true from the start. To catch it, this also
    measures the fraction of overlapping 4-word windows (n-grams) across the
    whole text that are unique — a repeating unit, however it's split across
    lines, drives that ratio toward zero, while genuine prose stays close to
    1.0. Calibrated directly against real data: a genuine archival-prose
    sample from the same corpus and OCR engine scored 99.7% distinct
    4-grams, while four loop patterns reconstructed from real flagged
    production failures (a short repeated phrase, a long repeated clause, a
    repeated full sentence, and a pair of near-duplicate sentences
    alternating with one word swapped between them) scored 0.7-1.0% —
    a ~100x margin, wider than the line-based check's own. The 20% cutoff
    sits comfortably inside that margin. Below `_MIN_WORDS_FOR_NGRAM_CHECK`
    words this check is skipped, matching the same "too short to be
    meaningful" philosophy as `min_lines` above.

    Tokenized via `_ngram_tokens`, not `_words` — digit-sequences count as
    tokens here (see `_NGRAM_TOKEN`), so a genuine page that legitimately
    repeats a sentence template with only a date or quantity changing does
    not collapse into an apparent loop once the numbers are stripped.
    """
    result = GuardResult(ok=True)

    if not text.strip():
        result.ok = False
        result.reasons.append("output empty")
        return result

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) >= min_lines:
        counts = Counter(lines)
        unique_ratio = len(counts) / len(lines)
        if unique_ratio < max_unique_ratio:
            worst_line, worst_count = counts.most_common(1)[0]
            result.ok = False
            result.reasons.append(
                f"only {unique_ratio:.0%} of {len(lines)} line(s) are unique "
                f"('{worst_line[:60]}' repeats {worst_count} times) — looks like "
                f"a decoding loop, not real transcription"
            )
            return result

    ngram_words = _ngram_tokens(text)
    if len(ngram_words) >= _MIN_WORDS_FOR_NGRAM_CHECK:
        ratio = _distinct_ngram_ratio(ngram_words, _NGRAM_N)
        if ratio < _MAX_DISTINCT_NGRAM_RATIO:
            result.ok = False
            result.reasons.append(
                f"only {ratio:.0%} of {_NGRAM_N}-word sequences are unique "
                f"across {len(ngram_words)} words — looks like a decoding "
                f"loop packed into too few lines for the line-level check "
                f"to see, not real transcription"
            )

    return result


def apply(raw: str, cleaned: str) -> tuple[str, GuardResult]:
    """Return the text to keep, plus the verdict.

    When the guard is disabled the cleaned text passes through unexamined.
    """
    if not cfg("cleanup_guard"):
        return cleaned, GuardResult(ok=True, reasons=["guard disabled"])

    result = check(raw, cleaned)
    if result.ok:
        return cleaned, result

    log.warning("Cleanup rejected, keeping raw text: %s", "; ".join(result.reasons))
    return raw, result
