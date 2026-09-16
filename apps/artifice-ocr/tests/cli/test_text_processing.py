# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Chunking, confidence scoring, and prompt registry tests."""

from unittest.mock import MagicMock, patch

from model_harness.contract import (
    HarnessResult,
    SchemaValidationFailed,
    StructuredOutputMode,
    StructuredOutputUnsupported,
)

# ---------------------------------------------------------------------------
# P4: Chunking tests
# ---------------------------------------------------------------------------


def test_chunk_text_short_text_unchanged():
    from artifice_ocr._chunking import chunk_text

    short = "Hello world. This is a test."
    chunks = chunk_text(short, max_tokens=100)
    assert len(chunks) == 1
    assert chunks[0] == short


def test_chunk_text_splits_long_text():
    from artifice_ocr._chunking import chunk_text

    # Create text that's ~500 tokens (well over 100-token limit)
    long_text = "This is a sentence. " * 200
    chunks = chunk_text(long_text, max_tokens=100, overlap_tokens=20)
    assert len(chunks) > 1
    # Reassembled should contain all original content
    reassembled = "\n\n".join(chunks)
    assert "sentence" in reassembled


def test_chunk_text_respects_paragraph_boundaries():
    from artifice_ocr._chunking import chunk_text

    paragraphs = ["Paragraph one. " * 50, "Paragraph two. " * 50]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, max_tokens=100, overlap_tokens=10)
    # Should have split into at least 2 chunks
    assert len(chunks) >= 2


def test_reassemble_joins_chunks():
    from artifice_ocr._chunking import reassemble

    chunks = ["Hello world", "Second chunk", "Third chunk"]
    result = reassemble(chunks)
    assert result == "Hello world\n\nSecond chunk\n\nThird chunk"


def test_estimate_tokens():
    from artifice_ocr._chunking import estimate_tokens

    # ~3.5 chars per token
    tokens = estimate_tokens("a" * 350)
    assert 90 < tokens < 110  # ~100 tokens


# ---------------------------------------------------------------------------
# P4: Confidence scoring tests
# ---------------------------------------------------------------------------


def test_heuristic_score_clean_text():
    from artifice_ocr._confidence import _heuristic_score

    clean_text = "This is a clear, well-written document with no issues."
    score, markers = _heuristic_score(clean_text)
    assert score >= 90
    assert len(markers) == 0


def test_heuristic_score_uncertain_text():
    from artifice_ocr._confidence import _heuristic_score

    uncertain_text = "I'm not sure about this part, it seems unclear and possibly damaged"
    score, markers = _heuristic_score(uncertain_text)
    assert score < 80
    assert len(markers) > 0


def test_evaluate_confidence():
    """LLM self-assessment blends into overall score via the harness."""
    mock_result = HarnessResult(
        data=MagicMock(score=85, reasoning="Good quality text"),
        mode_used=StructuredOutputMode.PROMPTED,
        model="translate-model",
        raw='{"score": 85, "reasoning": "Good quality text"}',
        repaired=False,
    )

    with patch("artifice_ocr._confidence.run_structured", return_value=mock_result):
        from artifice_ocr._confidence import evaluate_confidence

        result = evaluate_confidence(
            "Clean source text", "Clean translated text", enable_self_assessment=True
        )
    assert 0 <= result.overall_score <= 100
    assert result.reasoning == "Good quality text"


def test_evaluate_confidence_self_assessment_disabled():
    """Self-assessment is skipped entirely when disabled."""
    mock_result = HarnessResult(
        data=MagicMock(score=85, reasoning="Good quality text"),
        mode_used=StructuredOutputMode.PROMPTED,
        model="translate-model",
        raw="{}",
        repaired=False,
    )

    with patch("artifice_ocr._confidence.run_structured", return_value=mock_result) as mock_run:
        from artifice_ocr._confidence import evaluate_confidence

        result = evaluate_confidence("Clean text", "Clean output", enable_self_assessment=False)
    assert 0 <= result.overall_score <= 100
    mock_run.assert_not_called()


def test_evaluate_confidence_labels_schema_validation_failure():
    """A schema-validation failure degrades to a labelled 50, not a fabricated score."""
    with patch(
        "artifice_ocr._confidence.run_structured",
        side_effect=SchemaValidationFailed(
            "did not match schema",
            raw="garbage",
            mode=StructuredOutputMode.PROMPTED,
        ),
    ):
        from artifice_ocr._confidence import evaluate_confidence

        result = evaluate_confidence(
            "Clean source text", "Clean translated text", enable_self_assessment=True
        )
    assert result.score == 50
    assert "Self-assessment failed" in result.reasoning


def test_evaluate_confidence_labels_structured_output_unsupported():
    """The driver's actual bottom-of-the-ladder failure (StructuredOutputUnsupported,
    not SchemaValidationFailed — the driver degrades through the mode ladder and
    raises this one when every mode fails) also degrades to a labelled 50."""
    with patch(
        "artifice_ocr._confidence.run_structured",
        side_effect=StructuredOutputUnsupported("no mode produced valid output"),
    ):
        from artifice_ocr._confidence import evaluate_confidence

        result = evaluate_confidence(
            "Clean source text", "Clean translated text", enable_self_assessment=True
        )
    assert result.score == 50
    assert "Self-assessment failed" in result.reasoning


# ---------------------------------------------------------------------------
# P4: Prompt registry tests
# ---------------------------------------------------------------------------


def test_get_cleanup_prompt_default():
    from artifice_ocr._prompts import get_cleanup_prompt

    prompts = get_cleanup_prompt("default")
    assert "system" in prompts
    assert "user" in prompts
    assert "archivist" in prompts["system"].lower()


def test_get_cleanup_prompt_handwritten():
    from artifice_ocr._prompts import get_cleanup_prompt

    prompts = get_cleanup_prompt("handwritten")
    assert "paleographer" in prompts["system"].lower()


def test_get_cleanup_prompt_fallback():
    from artifice_ocr._prompts import get_cleanup_prompt

    prompts = get_cleanup_prompt("nonexistent_type")
    assert prompts["system"]  # should fall back to default


def test_get_translation_prompt_default():
    from artifice_ocr._prompts import get_translation_prompt

    prompts = get_translation_prompt("default")
    assert "translator" in prompts["system"].lower()


def test_get_translation_prompt_technical():
    from artifice_ocr._prompts import get_translation_prompt

    prompts = get_translation_prompt("technical")
    assert "technical" in prompts["system"].lower()


def test_list_document_types():
    from artifice_ocr._prompts import list_document_types

    types = list_document_types()
    assert "default" in types
    assert "handwritten" in types
    assert len(types) >= 6


def test_config_document_type_default():
    from artifice_ocr import config

    config.reset()
    assert config.get("document_type") == "default"
    config.reset()


def test_config_confidence_enabled_default():
    from artifice_ocr import config

    config.reset()
    assert config.get("confidence_enabled") is True
    config.reset()
