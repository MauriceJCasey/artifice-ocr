# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import ollama

from artifice_ocr import _llm
from artifice_ocr._chunking import chunk_text, reassemble, estimate_tokens
from artifice_ocr._confidence import evaluate_confidence
from artifice_ocr._logging import get_logger
from artifice_ocr._prompts import get_translation_prompt, get_lang_detect_prompt
from artifice_ocr._resolution import backend_for, model_for
from artifice_ocr._retry import retry
from artifice_ocr.config import get as cfg
from artifice_ocr.output import write_stage_output

log = get_logger("translate")

COMMON_LANGUAGES = {
    "de": "German",
    "fr": "French",
    "en": "English",
    "it": "Italian",
    "ja": "Japanese",
    "ru": "Russian",
    "es": "Spanish",
    "pt": "Portuguese",
    "pl": "Polish",
    "nl": "Dutch",
    "la": "Latin",
}


@retry(max_attempts=3, base_delay=1.0, label="Lang detect")
def _call_lang_detect(text: str, doc_type: str = "default") -> str:
    model = model_for("translation")
    backend = backend_for("translation")
    prompt = get_lang_detect_prompt(doc_type)
    response = _llm.chat(
        backend=backend,
        model=model,
        messages=[
            {"role": "user", "content": prompt.format(text=text[:2000])},
        ],
        temperature=0,
        think=cfg("ollama_think"),
    )
    return response.message.content.strip().lower()


def detect_language(text: str, doc_type: str = "default") -> str:
    """Detect source language via LLM. Returns ISO 639-1 code.

    `doc_type="multi_lang"` asks the model for several comma-separated codes
    in prevalence order (see its prompt in `_prompts.py`) — the first is
    taken as the primary/majority language. Previously any comma in the
    response failed the `isalpha()` check below, so multi_lang detection
    silently always returned "unknown"; splitting first fixes that.
    """
    try:
        raw = _call_lang_detect(text, doc_type)
        code = raw.split(",")[0].strip()
        if len(code) <= 3 and code.isalpha():
            return code
        return "unknown"
    except Exception:
        return "unknown"


@retry(max_attempts=4, base_delay=1.0, label="Translate")
def _call_translate_chunk(
    cleaned_text: str,
    system_prompt: str,
    user_template: str,
) -> str:
    """Translate a single chunk of text."""
    user_prompt = user_template.replace("{text}", cleaned_text)
    model = model_for("translation")
    backend = backend_for("translation")

    response = _llm.chat(
        backend=backend,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        think=cfg("ollama_think"),
        num_predict=cfg("max_output_tokens"),
    )

    return response.message.content


def _translate_with_chunking(
    cleaned_text: str,
    system_prompt: str,
    user_template: str,
) -> str:
    """Translate text, chunking if it exceeds the model's context window."""
    max_tokens = cfg("chunk_max_tokens")
    overlap_tokens = cfg("chunk_overlap_tokens")

    est_tokens = estimate_tokens(cleaned_text)
    if est_tokens <= max_tokens:
        return _call_translate_chunk(cleaned_text, system_prompt, user_template)

    log.info(
        "Text too long (%d est. tokens), chunking into pieces...",
        est_tokens,
    )
    chunks = chunk_text(cleaned_text, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    log.info("Translating %d chunk(s)...", len(chunks))

    translated_chunks = []
    for i, chunk in enumerate(chunks):
        log.info("  Chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
        translated = _call_translate_chunk(chunk, system_prompt, user_template)
        translated_chunks.append(translated)

    return reassemble(translated_chunks)


def perform(
    cleaned_text: str,
    *,
    source_file: str = "",
    output_dir: str = "output",
    stem: str | None = None,
) -> Dict[str, Any]:
    """Translate cleaned text into English.

    `stem` overrides the output filename (derived from `source_file` by
    default) and may contain a relative subdirectory.
    """
    doc_type = cfg("document_type")
    log.info("Detecting source language (doc_type=%s)...", doc_type)
    detected_lang = detect_language(cleaned_text, doc_type)
    lang_name = COMMON_LANGUAGES.get(detected_lang, detected_lang)
    log.info("Detected language: %s (%s)", lang_name, detected_lang)

    # A model asked to "translate into English" text that is already English
    # has nothing to genuinely translate, and reliably "helps" instead —
    # rewording, dropping, or otherwise rewriting an already-correct document.
    # Only skip on a *confident* "en" result; "unknown" (detection failed)
    # still goes through translation as before, since we can't be sure.
    skip_translation = detected_lang == "en" and cfg("skip_translation_if_english")

    system_prompt = None
    if skip_translation:
        log.info(
            "Source already English — skipping translation, passing cleaned text through unchanged"
        )
        translated_text = cleaned_text
    else:
        prompts = get_translation_prompt(doc_type)
        system_prompt = prompts["system"]
        user_template = prompts["user"]

        log.info("Translating with %s (doc_type=%s)", model_for("translation"), doc_type)
        translated_text = _translate_with_chunking(
            cleaned_text,
            system_prompt,
            user_template,
        )

    # Confidence scoring — meaningless when nothing was actually translated.
    confidence = None
    if cfg("confidence_enabled") and not skip_translation:
        log.info("Evaluating confidence...")
        confidence = evaluate_confidence(
            cleaned_text,
            translated_text,
            enable_self_assessment=True,
        )

    base_name = stem or (Path(source_file).stem if source_file else "unknown")

    data = {
        "source_file": source_file,
        "stage": "translated",
        "source_language": detected_lang,
        "source_language_name": lang_name,
        "cleaned_text": cleaned_text,
        "translated_text": translated_text,
        "engine": backend_for("translation"),
        "model": model_for("translation"),
        "system_prompt": system_prompt,
        "document_type": doc_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if skip_translation:
        data["skipped_translation"] = True
        data["skip_reason"] = "source_already_english"

    if confidence:
        data["confidence"] = confidence.to_dict()

    write_stage_output(
        output_dir, "translated", base_name, text_content=translated_text, metadata=data
    )

    log.info("Translation complete (%d -> %d chars)", len(cleaned_text), len(translated_text))
    return data
