# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the cleanup content-preservation guard.

The fixtures are real failure cases from the ISK archive audit, not invented
ones: each of these is something gemma4:12b actually did to a page.
"""

from unittest.mock import MagicMock, patch

import pytest

from artifice_ocr import _guard, config


@pytest.fixture(autouse=True)
def strict_guard():
    """Run each test against the shipped defaults."""
    config.apply_overrides(
        {
            "cleanup_guard": True,
            "cleanup_guard_max_deleted_words": 2,
            "cleanup_guard_min_length_ratio": 0.97,
            "cleanup_guard_protect_nouns": True,
        }
    )
    yield
    config.reset()
    config.load_config()


# --------------------------------------------------------------------------- #
# things the guard must reject
# --------------------------------------------------------------------------- #


def test_rejects_deleted_clause():
    """IMG_7186: a fragmentary SOE cable had whole clauses removed."""
    raw = (
        "ly safe in Italian occupied zone. Unwilling tzerland as he sees no "
        "useful activity here. to join you but only if he can work."
    )
    cleaned = "ly safe in Italian occupied zone. Unwilling to join you but only if he can work."

    result = _guard.check(raw, cleaned)

    assert result.ok is False
    assert result.words_deleted >= 8
    assert any("deleted" in r for r in result.reasons)


def test_rejects_corrupted_place_name_even_when_other_copies_survive():
    """IMG_7317: Elsass appeared five times; only the third was corrupted.

    A set-based check misses this entirely, which is why nouns are counted.
    """
    raw = (
        "Chef der Zivilverwaltung im Elsass (Abteilung...) einreichen. "
        "Dienststellen im Elsass melden. Verwaltung im Elsass bestimmt."
    )
    cleaned = (
        "Chef der Zivilverwaltung im Elsass (Abteilung...) einreichen. "
        "Dienststellen im Elsass melden. Verwaltung im Elass bestimmt."
    )

    result = _guard.check(raw, cleaned)

    assert result.ok is False
    assert "Elsass" in result.nouns_dropped


def test_rejects_corruption_of_an_already_correct_word():
    """IMG_7278: Gewerkschaftern -> Gewerkshaftern, a word that was fine."""
    raw = "zwischen den ehemaligen Gewerkschaftern und den Arbeiternachwuchs"
    cleaned = "zwischen den ehemaligen Gewerkshaftern und den Arbeiternachwuchs"

    result = _guard.check(raw, cleaned)

    assert result.ok is False
    assert "Gewerkschaftern" in result.nouns_dropped


def test_rejects_wholesale_shrinkage():
    raw = "Ein sehr langer Absatz mit vielen Woertern, der erhalten bleiben muss."
    cleaned = "Ein kurzer Absatz."

    result = _guard.check(raw, cleaned)

    assert result.ok is False
    assert result.length_ratio < 0.97


def test_rejects_introduced_accents():
    raw = "Der Bericht ueber die Taetigkeit war unvollstandig."
    cleaned = "Der Bericht über die Tätigkeit war unvollständig."

    result = _guard.check(raw, cleaned)

    assert result.ok is False
    assert any("accent" in r for r in result.reasons)


def test_rejects_empty_output():
    result = _guard.check("Some real archival text here.", "   ")

    assert result.ok is False
    assert "output empty" in result.reasons


# --------------------------------------------------------------------------- #
# things the guard must allow
# --------------------------------------------------------------------------- #


def test_allows_hyphenated_line_rejoin():
    """The repair cleanup exists to make must survive the guard."""
    raw = "Der Be-\nricht war unvollstandig und teilweise unklar."
    cleaned = "Der Bericht war unvollstandig und teilweise unklar."

    result = _guard.check(raw, cleaned)

    assert result.ok is True, result.reasons


def test_allows_lowercase_typo_fix():
    raw = "Es scheint, dass die Verbindung unterbrocheu war."
    cleaned = "Es scheint, dass die Verbindung unterbrochen war."

    result = _guard.check(raw, cleaned)

    assert result.ok is True, result.reasons


def test_capitalised_word_protection_covers_all_german_nouns():
    """A documented consequence, not an accident.

    German capitalises every noun, so protecting capitalised words means the
    model may not touch German nouns at all — including the rn->m repair in
    "Narnen" -> "Namen", which is a fix we would otherwise want. That is the
    price of blocking "Elsass" -> "Elass", and it is why the rejected text is
    kept in the JSON for review and why the rule has an off switch.
    """
    raw = "Die Narnen der Mitglieder konnten nicht ermittelt werden."
    cleaned = "Die Namen der Mitglieder konnten nicht ermittelt werden."

    assert _guard.check(raw, cleaned).ok is False

    config.apply_overrides({"cleanup_guard_protect_nouns": False})
    assert _guard.check(raw, cleaned).ok is True


def test_allows_whitespace_only_change():
    raw = "Es  scheint , dass die Verbindung  unterbrochen war."
    cleaned = "Es scheint, dass die Verbindung unterbrochen war."

    result = _guard.check(raw, cleaned)

    assert result.ok is True, result.reasons


def test_allows_unchanged_text():
    text = "Der Bericht ueber die Taetigkeit der Gruppe war unvollstandig."

    assert _guard.check(text, text).ok is True


def test_noun_protection_can_be_disabled():
    """Turning the rule off should let a name fix through (CAHTOLIC -> CATHOLIC)."""
    raw = "ADDRESS OF THE CAHTOLIC MISSION IN BERNE"
    cleaned = "ADDRESS OF THE CATHOLIC MISSION IN BERNE"

    assert _guard.check(raw, cleaned).ok is False

    config.apply_overrides({"cleanup_guard_protect_nouns": False})
    assert _guard.check(raw, cleaned).ok is True


# --------------------------------------------------------------------------- #
# integration with the stage
# --------------------------------------------------------------------------- #


@patch("artifice_ocr.stages.cleanup.ollama.Client")
def test_stage_keeps_raw_text_when_guard_rejects(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    from artifice_ocr.stages import cleanup

    raw = (
        "ly safe in Italian occupied zone. Unwilling tzerland as he sees no "
        "useful activity here. to join you but only if he can work."
    )
    lossy = "ly safe in Italian occupied zone. Unwilling to join you."
    mock_chat.return_value = MagicMock(message=MagicMock(content=lossy))

    result = cleanup.perform(raw, source_file="page.tif", output_dir=str(tmp_path))

    assert result["cleaned_text"] == raw  # raw survives
    assert result["guard"]["ok"] is False
    assert result["rejected_cleaned_text"] == lossy  # kept for review

    written = (tmp_path / "cleaned" / "text" / "page.txt").read_text(encoding="utf-8")
    assert written == raw


@patch("artifice_ocr.stages.cleanup.ollama.Client")
def test_stage_accepts_a_safe_repair(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    from artifice_ocr.stages import cleanup

    raw = "Der Be-\nricht war unvollstandig."
    good = "Der Bericht war unvollstandig."
    mock_chat.return_value = MagicMock(message=MagicMock(content=good))

    result = cleanup.perform(raw, source_file="page.tif", output_dir=str(tmp_path))

    assert result["cleaned_text"] == good
    assert result["guard"]["ok"] is True
    assert "rejected_cleaned_text" not in result


@patch("artifice_ocr.stages.cleanup.ollama.Client")
def test_guard_can_be_switched_off_entirely(mock_chat, tmp_path):
    mock_chat = mock_chat.return_value.chat
    from artifice_ocr.stages import cleanup

    config.apply_overrides({"cleanup_guard": False})
    raw = "A much longer piece of archival text that should have been preserved."
    mock_chat.return_value = MagicMock(message=MagicMock(content="short"))

    result = cleanup.perform(raw, source_file="page.tif", output_dir=str(tmp_path))

    assert result["cleaned_text"] == "short"


# --------------------------------------------------------------------------- #
# OCR degeneracy guard: greedy decoding looping on hallucinated filler
# --------------------------------------------------------------------------- #
#
# Real failure mode, not a hypothetical: a Tropy page scanned upside-down
# (R_58_373_0060), with nothing anywhere — not Tropy's own orientation
# metadata, not the file's EXIF — saying so. Fed to the OCR model as-is, it
# hallucinated a plausible-sounding German sentence and then had no way to
# stop repeating it: 900+ lines of one sentence on that page, a 3-line cycle
# repeated ~30 times on another in the same folder. Unlike the cleanup/
# structure guards above, there is no source text to fall back to here.


def test_repetition_guard_rejects_a_single_line_looped():
    looped = "\n\n".join(["Verwaltung Werte von Welleben eine Grundlage setzen."] * 50)

    result = _guard.check_no_repetition_loop(looped)

    assert result.ok is False
    assert "unique" in result.reasons[0]


def test_repetition_guard_rejects_a_short_cycle():
    """A same-line-N-times-in-a-row check would miss this: no single line
    ever repeats twice in a row, only the 3-line cycle as a whole does."""
    cycle = [
        "First line of the hallucinated cycle.",
        "Second line of the hallucinated cycle.",
        "Third line of the hallucinated cycle.",
    ]
    looped = "\n\n".join(cycle * 15)

    result = _guard.check_no_repetition_loop(looped)

    assert result.ok is False


def test_repetition_guard_accepts_real_varied_text():
    real = "\n\n".join(
        f"This is genuinely distinct archival sentence number {i} of the page." for i in range(40)
    )

    result = _guard.check_no_repetition_loop(real)

    assert result.ok is True, result.reasons


def test_repetition_guard_ignores_short_output():
    """Too little text for 'repetition' to mean anything — a short page
    legitimately repeating a couple of header lines must not trip this."""
    short = "Abschrift\n\naus\n\nden Akten 303/4 - Württemberg\n\nAbschrift der Anlage"

    result = _guard.check_no_repetition_loop(short)

    assert result.ok is True


def test_repetition_guard_rejects_empty_output():
    result = _guard.check_no_repetition_loop("   ")

    assert result.ok is False
    assert "output empty" in result.reasons


# --------------------------------------------------------------------------- #
# word-level repetition guard: a loop packed into continuous prose with no
# line breaks, which the line-based check above cannot see at all
# --------------------------------------------------------------------------- #
#
# Real failure mode, not a hypothetical: allenai/olmocr-2-7b via LM Studio has
# looped on a short phrase, clause, or sentence repeated hundreds of times
# with nothing splitting the output into separate lines, sometimes 20,000+
# characters on one page. Because text.split("\n") produces only a handful of
# "lines" (or one), len(lines) < min_lines is true and the line-based check
# above returns ok=True without ever inspecting the content. These fixtures
# use the actual repeated units reconstructed from real flagged production
# outputs, at realistic repeat counts.


def test_repetition_guard_rejects_short_phrase_looped_with_no_line_breaks():
    """The exact blind spot: one repeated short phrase, no newlines at all,
    so the old line-based check would have seen a single "line" and returned
    early without looking at the content."""
    looped = "der Naturwissenschaften, " * 150

    result = _guard.check_no_repetition_loop(looped)

    assert result.ok is False
    assert any("4-word" in r or "unique" in r for r in result.reasons)


def test_repetition_guard_rejects_alternating_sentence_pair():
    """The hardest case for a naive fixed-stride repeat check: two sentences
    alternating with only one word different between them. Most of their
    words are still identical, so most of their 4-grams still collide,
    which is exactly why the ratio-based approach catches it uniformly."""
    looped = (
        "Manche meinen, es sei eine neue Welle von Nationalsozialisten, die "
        "sich in England ausbreiten. Andere meinen, es sei eine neue Welle "
        "von Nationalsozialisten, die sich in England ausbreiten. "
    ) * 75

    result = _guard.check_no_repetition_loop(looped)

    assert result.ok is False


def test_repetition_guard_accepts_genuine_long_prose_with_no_line_breaks():
    """A true negative for the word-level check: a substantial, genuinely
    varied passage with no repeated phrasing and no line breaks at all (so
    the line-based check is skipped and only the n-gram check runs)."""
    prose = (
        "Der Ausschuss trat am Dienstag in einem kleinen Saal ueber der "
        "Bibliothek zusammen, um die eingegangenen Berichte aus den "
        "Grenzgebieten zu pruefen. Mehrere Delegierte hatten lange Reisen "
        "hinter sich und brachten Aufzeichnungen mit, die von den "
        "oertlichen Gruppen mit grosser Sorgfalt gefuehrt worden waren. "
        "Ein Redner aus Stuttgart schilderte die Schwierigkeiten, denen "
        "sich die Gewerkschaften seit dem vergangenen Jahr gegenuebersahen, "
        "waehrend ein anderer aus Frankfurt auf die veraenderte Haltung der "
        "oertlichen Behoerden hinwies. Man diskutierte ausfuehrlich, welche "
        "Massnahmen geeignet waeren, um den Zusammenhalt der verstreuten "
        "Mitglieder zu staerken, ohne dabei die Sicherheit einzelner "
        "Personen zu gefaehrden. Ein Vorschlag betraf die Einrichtung eines "
        "neuen Verbindungsweges ueber die Schweiz, der es erlauben sollte, "
        "Nachrichten schneller und zuverlaessiger zu uebermitteln als "
        "bisher. Ein weiterer Antrag forderte, dass kuenftige "
        "Zusammenkuenfte in kleineren Kreisen abgehalten werden sollten, um "
        "das Risiko einer Entdeckung zu verringern. Nach langer Aussprache "
        "einigte man sich darauf, eine kleine Arbeitsgruppe einzusetzen, "
        "die binnen eines Monats einen ausfuehrlichen Plan vorlegen sollte. "
        "Am Ende der Sitzung dankte der Vorsitzende allen Anwesenden fuer "
        "ihre Geduld und betonte, wie wichtig es sei, trotz aller "
        "Widrigkeiten den Kontakt untereinander nicht abreissen zu lassen. "
        "Die Sitzung wurde kurz nach Mitternacht geschlossen, und die "
        "Teilnehmer verliessen das Gebaeude einzeln und in groesseren "
        "Zeitabstaenden, wie es die Vorsicht seit langem geboten erscheinen "
        "liess. Ein kurzer schriftlicher Bericht ueber die Beschluesse "
        "wurde anschliessend an mehrere befreundete Gruppen im Ausland "
        "weitergeleitet, damit auch dort die neuesten Entwicklungen bekannt "
        "wuerden und man sich gegenseitig ueber die Lage auf dem Laufenden "
        "halten konnte, soweit die Umstaende dies zuliessen."
    )

    result = _guard.check_no_repetition_loop(prose)

    assert result.ok is True, result.reasons


def test_repetition_guard_rejects_a_real_flagged_production_sample():
    """The actual failure, not a lookalike: a real olmOCR-2/LM Studio output
    the maintainer hand-flagged as fabricated (Tropy item 12135, "England
    spricht"). The genuine title and opening two sentences are copied
    verbatim, followed by the real alternating-sentence loop the model
    actually produced, at a repeat count representative of the real
    20,000+-character page (the ratio this check measures stabilises long
    before that count, so this is not a weaker test than the full page —
    see test_repetition_guard_rejects_alternating_sentence_pair, which
    already proves that at 75 repeats).

    This is the single strongest piece of evidence that the fix works: it
    is not a reconstruction of the failure pattern, it is the failure."""
    real_sample = (
        "England spricht\n\n"
        'In England spricht man von einer "neuen Welle" der '
        "Nationalsozialisten. Es ist nicht ganz klar, was man damit meint. "
        + (
            "Manche meinen, es sei eine neue Welle von Nationalsozialisten, "
            "die sich in England ausbreiten. Andere meinen, es sei eine neue "
            "Welle von Nationalsozialisten, die sich in England ausbreiten. "
        )
        * 300
    )

    result = _guard.check_no_repetition_loop(real_sample)

    assert result.ok is False


def test_repetition_guard_accepts_repeated_template_with_varying_numbers():
    """A genuine archival pattern the n-gram check must not misfire on: the
    same sentence template repeated with only a date or quantity differing
    each time — e.g. a table-like passage of yearly production figures.

    _words() (used by the proper-noun check) deliberately strips digits, so
    reusing it here would make every occurrence of "Im Jahre ... betrug ...
    kg." collapse to an identical word sequence once the year/quantity is
    removed, misflagging real varied text as a loop. check_no_repetition_loop
    tokenizes via _ngram_tokens instead, which keeps digit-sequences as
    distinct tokens, so the varying numbers count toward uniqueness."""
    varied = " ".join(
        f"Im Jahre {1930 + i} betrug die Foerderung {1500 + i * 10} kg." for i in range(40)
    )

    result = _guard.check_no_repetition_loop(varied)

    assert result.ok is True, result.reasons


def test_repetition_guard_word_level_check_ignores_short_repeated_output():
    """Below _MIN_WORDS_FOR_NGRAM_CHECK, the word-level check does not fire
    even on text that would otherwise look like a loop — the same "too short
    to be meaningful" floor as min_lines above, documented rather than left
    as an implicit edge case."""
    short_repeat = "eine kurze Wiederholung " * 10  # 30 words, under the 40 floor

    result = _guard.check_no_repetition_loop(short_repeat)

    assert result.ok is True
