# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic OCR artifact repair — pre-pass for the cleanup stage.

This module handles four of the five repairs the cleanup prompt previously
delegated to a language model, applying them with exact regex rules that
cannot alter proper nouns, dates, numbers, reference codes, or transliterated
umlauts the way a model can.

Rules (in application order):
    1. Keep hyphens after known prefixes (self-, well-, co-, …).
    2. Rejoin words hyphenated across a line break.
    3. Join em-dashes that break across a line end.
    4. Join line breaks that fall mid-sentence.
    5. Collapse doubled spaces and remove space before punctuation.

Rule 2 applies a two-case heuristic for German:
    - Lowercase continuation after the break → the hyphen was a soft
      line-break artifact; remove it and join the word.
    - Uppercase continuation → the hyphen is a real compound hyphen
      (e.g. "Kaiser-\\nWilhelm"); keep the hyphen, only strip the line
      break.

Rule 1 catches the exception: some English prefixes ("self-", "well-",
"co-", …) keep their hyphen even when the following word is lowercase.
"self-\\nevident" → "self-evident", not "selfevident".  The list is
explicit and dependency-free.

Only letter-shape misreadings (rn→m, u→n, l→1, 0→O) remain for the model
— that genuinely requires context to disambiguate.
"""

import re

# --------------------------------------------------------------------------- #
# Character class helpers
# --------------------------------------------------------------------------- #

# German lowercase letters including umlauts and sharp-s.
_LC = r"a-zäöüß"
# German uppercase letters including umlauts.
_UC = r"A-ZÄÖÜ"
# Any German letter.
_L = rf"{_LC}{_UC}"

# --------------------------------------------------------------------------- #
# Defensive pre-pass: leaked YAML front matter
# --------------------------------------------------------------------------- #

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?\n)?---\s*\n", re.DOTALL)


def _strip_leaked_front_matter(text: str) -> tuple[str, bool]:
    """Remove a leading ``---``-delimited YAML block if present.

    OCR_PROMPT asks for raw text with no formatting, but a model RLVR'd
    toward YAML-front-matter-plus-Markdown output (see
    docs/superpowers/plans/2026-09-09-olmocr2-optimisation.md's discussion
    of §3) can still emit one. Nothing else in
    this pipeline recognises it, so left unstripped it is written to
    raw_ocr/text/*.txt as if it were transcribed page content. Requires a
    matching closing ``---`` — a page whose genuine content happens to open
    with a horizontal rule (a single ``---`` with no closer) is left alone.
    """
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return text, False
    return text[match.end() :], True


# --------------------------------------------------------------------------- #
# Rule 1: Known hyphenated prefixes — keep the hyphen always
# --------------------------------------------------------------------------- #

# Prefixes whose hyphen must be kept regardless of what follows.  Without
# this, "self-\\nevident" would be rejoined to "selfevident" by rule 2a.
#
# Sorted longest-first so the regex alternation tries "pseudo" before
# "co" and produces the correct match when a longer prefix is a superset
# of a shorter one (none at time of writing, but safe practice).
#
# German equivalents of "self-" and well-established borrowings are
# included.  Most Latinate prefixes ("co-", "ex-", "anti-", …) are
# shared across the two languages.
_HYPHEN_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "anti",
            "co",
            "ex",
            "non",
            "post",
            "pre",
            "pseudo",
            "quasi",
            "re",
            "self",
            "selbst",
            "semi",
            "well",
        ),
        key=len,
        reverse=True,
    )
)

_PREFIX_PATTERN = "|".join(_HYPHEN_PREFIXES)

# Negative lookbehind prevents matching a prefix that is actually the tail
# of a longer word (e.g. "re" inside "care-").  re.IGNORECASE catches both
# "self-" and "Self-".
_RE_HYPHEN_PREFIX_KEEP = re.compile(
    rf"(?<![{_L}])({_PREFIX_PATTERN})- *\r?\n *([{_LC}][{_L}]*)",
    re.IGNORECASE,
)


def _keep_hyphen_prefix(text: str) -> tuple[str, int]:
    """Keep the hyphen when a known prefix sits at a line break.

    ``self-\\nevident`` → ``self-evident`` (prefix in list; keep '-').
    ``North-\\nwest`` is *not* matched (``North`` is not a prefix).
    """
    result, n = _RE_HYPHEN_PREFIX_KEEP.subn(r"\1-\2", text)
    return result, n


# --------------------------------------------------------------------------- #
# Rule 2: Rejoin hyphenated line breaks
# --------------------------------------------------------------------------- #

# ---- 2a: soft hyphen — continuation is lowercase, strip the hyphen ----

_RE_HYPHEN_REJOIN = re.compile(rf"([{_L}])- *\r?\n *([{_LC}])")


def _rejoin_hyphenated_lower(text: str) -> tuple[str, int]:
    """Rejoin words where a soft hyphen at line end breaks a single word.

    ``Be-\\nricht`` → ``Bericht`` (lowercase 'r' → join).
    ``Kaiser-\\nWilhelm`` is *not* matched here (uppercase 'W').
    Prefix-keep cases (``self-\\nevident``) are already consumed by
    ``_keep_hyphen_prefix`` and do not reach this rule.
    """
    result, n = _RE_HYPHEN_REJOIN.subn(r"\1\2", text)
    return result, n


# ---- 2b: real compound hyphen — continuation is uppercase, keep it ----

_RE_HYPHEN_KEEP = re.compile(rf"([{_L}])- *\r?\n *([{_UC}])")


def _keep_hyphen_upper(text: str) -> tuple[str, int]:
    """Keep the hyphen but strip the line break when a genuine compound
    word happens to break at its hyphen.

    ``Kaiser-\\nWilhelm`` → ``Kaiser-Wilhelm`` (uppercase 'W' → keep '-').
    """
    result, n = _RE_HYPHEN_KEEP.subn(r"\1-\2", text)
    return result, n


# --------------------------------------------------------------------------- #
# Rule 3: Em-dash at line end
# --------------------------------------------------------------------------- #

# An em-dash (U+2014) at the end of a line is never a hyphenation artifact.
# The document convention is a closed em-dash (no space on either side), so
# the line break is removed without inserting a space.
#
# This is distinct from rule 4 (mid-sentence joins), which correctly inserts
# a space — an em-dash is a single glyph that bridges two parts of the same
# clause, not a word boundary.

_RE_EMDASH_LINEBREAK = re.compile(r"—[ \t]*\r?\n[ \t]*")


def _join_emdash_break(text: str) -> tuple[str, int]:
    """Remove a line break directly after an em-dash, no space inserted.

    ``eggs—\\nthere`` → ``eggs—there``.
    """
    result, n = _RE_EMDASH_LINEBREAK.subn("—", text)
    return result, n


# --------------------------------------------------------------------------- #
# Rule 4: Join mid-sentence line breaks
# --------------------------------------------------------------------------- #

# A line that ends with a character that can appear mid-sentence (lowercase
# letter, comma, semicolon, colon, closing quote or bracket) and is followed
# by a line starting with a lowercase letter is a mid-sentence break.
#
# Digits are deliberately excluded from the "can end a line" set to avoid
# joining page numbers / reference codes with body text.
#
# Sentence-ending punctuation (``. ! ?``) is excluded: a line ending with a
# full stop, exclamation or question mark is a genuine sentence boundary.
#
# The replace preserves the end-of-line character and inserts a single space,
# stripping any leading/trailing horizontal whitespace around the break.

_RE_MID_SENTENCE = re.compile(
    rf"([{_LC},;:\u00bb\u201c\u201d\u201e\)\]])[ \t]*\r?\n[ \t]*([{_LC}])"
)


def _join_mid_sentence_breaks(text: str) -> tuple[str, int]:
    """Join line breaks that fall in the middle of a sentence.

    ``Der Bericht war unvollständig und\\nteilweise unklar.``
    → ``Der Bericht war unvollständig und teilweise unklar.``
    """
    result, n = _RE_MID_SENTENCE.subn(r"\1 \2", text)
    return result, n


# --------------------------------------------------------------------------- #
# Rule 5: Normalise whitespace around punctuation
# --------------------------------------------------------------------------- #

# ---- 5a: collapse runs of horizontal whitespace to a single space ----

_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def _collapse_spaces(text: str) -> tuple[str, int]:
    """Collapse runs of horizontal whitespace into a single space.

    Does not touch newlines (``\\n``, ``\\r``).
    """
    result, n = _RE_MULTI_SPACE.subn(" ", text)
    return result, n


# ---- 5b: remove space immediately before punctuation ----

_RE_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,.;:!?])")


def _fix_space_before_punct(text: str) -> tuple[str, int]:
    """Remove stray space(s) before a comma, full stop, semicolon, colon,
    exclamation or question mark.

    ``Es scheint , dass ...`` → ``Es scheint, dass ...``
    """
    result, n = _RE_SPACE_BEFORE_PUNCT.subn(r"\1", text)
    return result, n


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def normalise(text: str) -> tuple[str, dict]:
    """Apply deterministic OCR artifact repairs to *text*.

    Returns ``(normalised_text, edit_counts)`` where *edit_counts* is a
    dict mapping rule names to the number of edits made.

    This is a pure function: no model, no I/O, no config lookup.
    It is designed to be called on the full text before chunking so that
    a hyphenated word broken across a chunk boundary is still repaired.
    """
    counts: dict[str, int] = {}

    # Rule order matters.
    #
    # Front-matter stripping runs before every artifact rule: leaked YAML
    # is not page content, and no artifact rule may see it.
    #
    # Prefix-keep runs first among the artifact rules: a prefix-hyphen at
    # line end must be protected before the general rejoin rule strips it.
    #
    # Hyphenation (rejoin + keep-upper) runs before mid-sentence joins:
    # a ``-\\n`` that rule 2 resolves should not also be considered by rule 4.
    #
    # Em-dash join runs after hyphen rules (the em-dash glyph is never a
    # hyphen) and before mid-sentence joins so the em-dash terminator is
    # already in place.
    #
    # Space normalisation runs last because earlier rules insert spaces.

    text, stripped = _strip_leaked_front_matter(text)
    counts["front_matter_stripped"] = stripped

    text, n = _keep_hyphen_prefix(text)
    counts["hyphen_prefix_keep"] = n

    text, n = _rejoin_hyphenated_lower(text)
    counts["hyphen_rejoin"] = n

    text, n = _keep_hyphen_upper(text)
    counts["hyphen_keep"] = n

    text, n = _join_emdash_break(text)
    counts["emdash_break_join"] = n

    text, n = _join_mid_sentence_breaks(text)
    counts["line_break_join"] = n

    text, n = _collapse_spaces(text)
    counts["spaces_collapsed"] = n

    text, n = _fix_space_before_punct(text)
    counts["space_before_punct"] = n

    return text, counts
