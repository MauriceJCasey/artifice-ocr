# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the deterministic OCR pre-pass (_normalise).

Verifies each of the seven rules individually, the rule-ordering
guarantees, and the end-to-end behaviour over the proceedings_usnm_173
fixture pair.
"""

from pathlib import Path

import pytest

from artifice_ocr._normalise import (
    _collapse_spaces,
    _fix_space_before_punct,
    _join_emdash_break,
    _join_mid_sentence_breaks,
    _keep_hyphen_prefix,
    _keep_hyphen_upper,
    _rejoin_hyphenated_lower,
    normalise,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Rule 1: hyphen prefix keep
# --------------------------------------------------------------------------- #


class TestPrefixKeep:
    """Known prefixes keep their hyphen on lowercase continuation."""

    def test_self_lowercase(self):
        text, n = _keep_hyphen_prefix("self-\nevident")
        assert text == "self-evident"
        assert n == 1

    def test_self_capitalized(self):
        """Case-insensitive: 'Self-' is the same prefix."""
        text, n = _keep_hyphen_prefix("Self-\nevident")
        assert text == "Self-evident"
        assert n == 1

    def test_well_known(self):
        text, n = _keep_hyphen_prefix("a well-\nknown fact")
        assert text == "a well-known fact"
        assert n == 1

    def test_co_author(self):
        text, n = _keep_hyphen_prefix("co-\nauthor")
        assert text == "co-author"
        assert n == 1

    def test_re_edit(self):
        text, n = _keep_hyphen_prefix("re-\nedit")
        assert text == "re-edit"
        assert n == 1

    def test_non_issue(self):
        text, n = _keep_hyphen_prefix("non-\nissue")
        assert text == "non-issue"
        assert n == 1

    def test_ex_wife(self):
        text, n = _keep_hyphen_prefix("ex-\nwife")
        assert text == "ex-wife"
        assert n == 1

    def test_anti_virus(self):
        text, n = _keep_hyphen_prefix("anti-\nvirus")
        assert text == "anti-virus"
        assert n == 1

    def test_pre_existing(self):
        text, n = _keep_hyphen_prefix("pre-\nexisting")
        assert text == "pre-existing"
        assert n == 1

    def test_post_war(self):
        text, n = _keep_hyphen_prefix("post-\nwar")
        assert text == "post-war"
        assert n == 1

    def test_semi_automatic(self):
        text, n = _keep_hyphen_prefix("semi-\nautomatic")
        assert text == "semi-automatic"
        assert n == 1

    def test_quasi_official(self):
        text, n = _keep_hyphen_prefix("quasi-\nofficial")
        assert text == "quasi-official"
        assert n == 1

    def test_pseudo_science(self):
        text, n = _keep_hyphen_prefix("pseudo-\nscience")
        assert text == "pseudo-science"
        assert n == 1

    def test_selbst_verstaendlich(self):
        """German equivalent of 'self-'."""
        text, n = _keep_hyphen_prefix("selbst-\nverständlich")
        assert text == "selbst-verständlich"
        assert n == 1

    def test_prefix_not_inside_longer_word(self):
        """'re' inside 'care-' is NOT a prefix and must not match."""
        text, n = _keep_hyphen_prefix("care-\nfully")
        assert text == "care-\nfully"  # unchanged
        assert n == 0

    def test_prefix_not_in_list(self):
        """'North' is not a prefix — must be left for rule 2a."""
        text, n = _keep_hyphen_prefix("North-\nwest")
        assert text == "North-\nwest"  # unchanged
        assert n == 0

    def test_spaces_around_line_break_stripped(self):
        """Horizontal whitespace around the break is removed."""
        text, n = _keep_hyphen_prefix("self-  \n  evident")
        assert text == "self-evident"
        assert n == 1

    def test_word_after_break_with_punctuation(self):
        """Word after break may end with punctuation."""
        text, n = _keep_hyphen_prefix("self-\nevident,")
        assert text == "self-evident,"
        assert n == 1


# --------------------------------------------------------------------------- #
# Rule 2a: hyphen rejoin (lowercase continuation)
# --------------------------------------------------------------------------- #


class TestHyphenRejoin:
    def test_lowercase_continuation(self):
        text, n = _rejoin_hyphenated_lower("Be-\nricht")
        assert text == "Bericht"
        assert n == 1

    def test_uppercase_continuation_not_matched(self):
        """Uppercase after break → this rule ignores it."""
        text, n = _rejoin_hyphenated_lower("Kaiser-\nWilhelm")
        assert text == "Kaiser-\nWilhelm"  # unchanged
        assert n == 0

    def test_german_umlaut_continuation(self):
        text, n = _rejoin_hyphenated_lower("über-\nflüssig")
        assert text == "überflüssig"
        assert n == 1

    def test_spaces_stripped(self):
        text, n = _rejoin_hyphenated_lower("ascer-  \n  tained")
        assert text == "ascertained"
        assert n == 1

    def test_windows_line_ending(self):
        text, n = _rejoin_hyphenated_lower("word-\r\nbreak")
        assert text == "wordbreak"
        assert n == 1


# --------------------------------------------------------------------------- #
# Rule 2b: hyphen keep (uppercase continuation)
# --------------------------------------------------------------------------- #


class TestHyphenKeepUpper:
    def test_uppercase_compound(self):
        text, n = _keep_hyphen_upper("Kaiser-\nWilhelm")
        assert text == "Kaiser-Wilhelm"
        assert n == 1

    def test_lowercase_not_matched(self):
        text, n = _keep_hyphen_upper("ascer-\ntained")
        assert text == "ascer-\ntained"  # unchanged
        assert n == 0

    def test_windows_line_ending(self):
        text, n = _keep_hyphen_upper("Kaiser-\r\nWilhelm")
        assert text == "Kaiser-Wilhelm"
        assert n == 1


# --------------------------------------------------------------------------- #
# Rule 3: em-dash line break
# --------------------------------------------------------------------------- #


class TestEmdashBreak:
    def test_emdash_at_line_end_lowercase_next(self):
        text, n = _join_emdash_break("eggs—\nthere")
        assert text == "eggs—there"
        assert n == 1

    def test_emdash_at_line_end_uppercase_next(self):
        """Em-dash join doesn't discriminate by case — always no space."""
        text, n = _join_emdash_break("done.—\nNext")
        assert text == "done.—Next"
        assert n == 1

    def test_spaces_stripped(self):
        text, n = _join_emdash_break("word—  \n  next")
        assert text == "word—next"
        assert n == 1

    def test_windows_line_ending(self):
        text, n = _join_emdash_break("eggs—\r\nthere")
        assert text == "eggs—there"
        assert n == 1

    def test_emdash_midline_not_touched(self):
        """Em-dash not at line end is left alone."""
        text, n = _join_emdash_break("birds—are unidentified")
        assert text == "birds—are unidentified"
        assert n == 0

    def test_emdash_not_confused_with_hyphen(self):
        """Prove the em-dash (U+2014) is never treated as hyphenation."""
        text = "word—\ncontinuation"
        # Run hyphen rejoin — must NOT match em-dash
        result, n = _rejoin_hyphenated_lower(text)
        assert result == text  # unchanged
        assert n == 0
        # Run hyphen keep upper — must NOT match em-dash
        result2, n2 = _keep_hyphen_upper(text)
        assert result2 == text
        assert n2 == 0


# --------------------------------------------------------------------------- #
# Rule 4: mid-sentence join
# --------------------------------------------------------------------------- #


class TestMidSentence:
    def test_lowercase_to_lowercase(self):
        text, n = _join_mid_sentence_breaks("der Bericht\nwar")
        assert text == "der Bericht war"
        assert n == 1

    def test_comma_end(self):
        text, n = _join_mid_sentence_breaks("es scheint,\ndass")
        assert text == "es scheint, dass"
        assert n == 1

    def test_closing_quote_end(self):
        text, n = _join_mid_sentence_breaks("said\u201d\nhe")
        assert text == "said\u201d he"
        assert n == 1

    def test_sentence_end_not_matched(self):
        """Period before line break → no join."""
        text, n = _join_mid_sentence_breaks("end.\nNew sentence")
        assert text == "end.\nNew sentence"
        assert n == 0

    def test_digits_not_matched(self):
        """Digit before line break → no join."""
        text, n = _join_mid_sentence_breaks("page 173\ncontinued")
        assert text == "page 173\ncontinued"
        assert n == 0


# --------------------------------------------------------------------------- #
# Rule 5a: collapse spaces
# --------------------------------------------------------------------------- #


class TestCollapseSpaces:
    def test_double_space(self):
        text, n = _collapse_spaces("word.  Next")
        assert text == "word. Next"
        assert n == 1

    def test_triple_space(self):
        text, n = _collapse_spaces("word.   Next")
        assert text == "word. Next"
        assert n == 1

    def test_newline_preserved(self):
        text, n = _collapse_spaces("line1.  \nline2")
        # Only the two spaces before \n are collapsed; the \n stays
        assert text == "line1. \nline2"
        assert n == 1


# --------------------------------------------------------------------------- #
# Rule 5b: space before punctuation
# --------------------------------------------------------------------------- #


class TestSpaceBeforePunct:
    def test_comma(self):
        text, n = _fix_space_before_punct("scheint , dass")
        assert text == "scheint, dass"
        assert n == 1

    def test_full_stop(self):
        text, n = _fix_space_before_punct("ende .")
        assert text == "ende."
        assert n == 1

    def test_no_space_unchanged(self):
        text, n = _fix_space_before_punct("scheint, dass")
        assert text == "scheint, dass"
        assert n == 0


# --------------------------------------------------------------------------- #
# Rule ordering
# --------------------------------------------------------------------------- #


class TestRuleOrder:
    """Rule ordering is load-bearing: later rules must not undo earlier ones."""

    def test_prefix_then_rejoin(self):
        """Prefix keep runs first, so self-evident stays hyphenated."""
        result, counts = normalise("self-\nevident")
        assert result == "self-evident"
        assert counts["hyphen_prefix_keep"] == 1
        assert counts["hyphen_rejoin"] == 0  # already consumed

    def test_rejoin_then_mid_sentence(self):
        """Hyphen rejoin runs first, so the joined word is whole for rule 4."""
        result, counts = normalise("Der Be-\nricht war\nunklar")
        assert "Bericht war" in result
        assert "unklar" in result
        assert counts["hyphen_rejoin"] == 1
        assert counts["line_break_join"] == 1

    def test_emdash_then_mid_sentence(self):
        """Em-dash join runs before mid-sentence, so em-dash doesn't
        trigger a mid-sentence join with a space."""
        result, counts = normalise("the end—\nthere was more")
        assert result == "the end—there was more"
        assert counts["emdash_break_join"] == 1
        # line_break_join should be 0 because the
        # em-dash rule consumed the line break
        assert counts["line_break_join"] == 0

    def test_spaces_last(self):
        """Space fixes run last and don't break earlier joins."""
        result, counts = normalise("word-  \n  break.  Next")
        assert "wordbreak. Next" in result
        assert counts["hyphen_rejoin"] == 1
        assert counts["spaces_collapsed"] >= 1


# --------------------------------------------------------------------------- #
# Integration: full normalise() on proceedings_usnm_173 fixture
# --------------------------------------------------------------------------- #


class TestProceedingsFixture:
    """End-to-end test over the real ground-truth fixture."""

    @pytest.fixture
    def raw(self):
        return _load_fixture("proceedings_usnm_173.raw.txt")

    @pytest.fixture
    def groundtruth(self):
        return _load_fixture("proceedings_usnm_173.groundtruth.txt")

    def test_edit_counts(self, raw):
        _, counts = normalise(raw)
        # Three hyphenated line breaks rejoined
        assert counts["hyphen_rejoin"] == 3
        # No uppercase-continuation hyphens on this page
        assert counts["hyphen_keep"] == 0
        # One em-dash at line end joined
        assert counts["emdash_break_join"] == 1
        # Four mid-sentence line breaks joined
        assert counts["line_break_join"] == 4
        # Six doubled spaces collapsed
        assert counts["spaces_collapsed"] == 6
        # No prefix-hyphen words exist on this page
        assert counts["hyphen_prefix_keep"] == 0

    def test_hyphen_joins(self, raw):
        result, _ = normalise(raw)
        assert "ascertained" in result
        assert "confirmation" in result
        assert "Northwest" in result

    def test_emdash_join(self, raw):
        result, _ = normalise(raw)
        assert "eggs—there" in result

    def test_mid_sentence_joins(self, raw):
        result, _ = normalise(raw)
        assert "no authority" in result
        assert "stops at" in result
        assert "conjectural and" in result

    def test_doubled_spaces_collapsed(self, raw):
        result, _ = normalise(raw)
        # No runs of two or more horizontal spaces should remain
        import re

        assert not re.search(r"[ \t]{2,}", result)

    def test_ocr_errors_left_unchanged(self, raw):
        """These are the model's job — the pre-pass must not touch them."""
        result, _ = normalise(raw)
        assert "fourided" in result
        assert "Eio" in result

    def test_emdash_errors_left_unchanged(self, raw):
        """The em-dashes at 'Grande"—not' and 'positive.—T. M. B.'
        are mid-line — they must not be altered or have spaces inserted."""
        result, _ = normalise(raw)
        assert 'Grande"—not' in result
        assert "positive.—T. M. B." in result

    def test_running_head_not_joined_to_body(self, raw):
        """The running head on line 1 must not be joined to body text."""
        result, _ = normalise(raw)
        # After normalise, the running head line should still be separate
        assert "PROCEEDINGS OF UNITED STATES NATIONAL MUSEUM. 173" in result
        # The body starts on a new line
        assert "\nAmerican fauna" in result

    def test_dates_unchanged(self, raw, groundtruth):
        """Dates like 'AUGUST 1, 1878' and 'Ibis, 1866' are never altered."""
        result, _ = normalise(raw)
        assert "AUGUST 1, 1878" in result
        assert "Ibis, 1866" in result

    def test_no_line_break_hyphens_remain(self, raw, groundtruth):
        """After normalise, no ASCII-hyphen-at-line-break should survive."""
        import re

        result, _ = normalise(raw)
        assert not re.search(r"-\s*\r?\n", result)


# --------------------------------------------------------------------------- #
# Defensive pre-pass: leaked YAML front matter
# --------------------------------------------------------------------------- #


def test_leaked_yaml_front_matter_is_stripped_before_normalisation():
    from artifice_ocr._normalise import normalise

    text = (
        "---\nprimary_language: en\nis_rotation_valid: true\n---\nThis is the actual page text.\n"
    )
    cleaned, stats = normalise(text)

    assert "primary_language" not in cleaned
    assert "---" not in cleaned
    assert "This is the actual page text." in cleaned
    assert stats.get("front_matter_stripped") is True


def test_text_without_front_matter_is_unaffected():
    from artifice_ocr._normalise import normalise

    text = "Just ordinary transcribed text, no --- anywhere relevant.\n"
    cleaned, stats = normalise(text)

    assert cleaned.strip() == text.strip()
    assert stats.get("front_matter_stripped", False) is False


def test_a_lone_leading_dash_line_is_not_mistaken_for_front_matter():
    """A page whose real content starts with a horizontal rule must survive."""
    from artifice_ocr._normalise import normalise

    text = "---\nNo closing delimiter follows, so this is not front matter.\n"
    cleaned, stats = normalise(text)

    assert cleaned.strip().startswith("---")
    assert stats.get("front_matter_stripped", False) is False
