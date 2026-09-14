# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prompt registry for document-type-specific prompt variations.

Allows selecting prompt templates based on document characteristics
(handwritten, typed, dialect, era, etc.).
"""

import importlib.resources
from typing import Any

from artifice_ocr._logging import get_logger

log = get_logger("prompts")

# Resolved through importlib.resources, NOT a __file__-relative path.  These
# prompts must survive being packaged: this app is distributed as a frozen
# .exe/.dmg, where __file__ points inside a temporary extraction directory and
# any ``.parent.parent.parent`` walk lands outside the package — so prompt
# files are excluded from the wheel and prompt loading breaks at runtime in
# every installed copy while working perfectly in a source checkout.
#
# The previous form was
# ``Path(__file__).resolve().parent.parent.parent / "prompts"``, which put
# the prompts under ``apps/artifice-ocr/prompts/`` — outside the package.
PROMPT_DIR = importlib.resources.files("artifice_ocr") / "prompts"

# --------------------------------------------------------------------------- #
# Prompt definitions per document type
# --------------------------------------------------------------------------- #

_CLEANUP_PROMPTS: dict[str, dict[str, str]] = {
    "default": {
        "system": (
            "You are an archivist performing mechanical OCR artifact repair on "
            "early 20th-century documents. You are not an editor: you never "
            "modernise spelling, never alter proper nouns, and never translate "
            "or rephrase. When in doubt, leave the text unchanged."
        ),
        "user_template": None,  # loaded from file
    },
    "handwritten": {
        "system": (
            "You are an archivist and paleographer repairing OCR artifacts in "
            "handwritten historical documents. Preserve abbreviations, archaic "
            "spellings and dialect features exactly. Fix only clear scanning "
            "errors; never modernise spelling or alter names. When in doubt, "
            "leave the text unchanged."
        ),
        "user_template": None,
    },
    "typed_clean": {
        "system": (
            "You are a document editor for clean typed archival documents. "
            "Perform minimal cleanup — only fix obvious OCR misreads "
            "(e.g., 'l' for '1', 'O' for '0'). Preserve original formatting and punctuation."
        ),
        "user_template": None,
    },
    "technical": {
        "system": (
            "You are a technical editor specializing in scientific and engineering documents. "
            "Clean OCR text while carefully preserving numbers, units, measurements, "
            "chemical formulas, and mathematical notation. Do not alter technical terms."
        ),
        "user_template": None,
    },
    "multi_lang": {
        "system": (
            "You are an archivist expert in multilingual historical documents. "
            "The source text may contain multiple languages or dialects mixed together. "
            "Perform conservative cleanup while preserving all languages as-is. "
            "Do not translate — only fix OCR errors."
        ),
        "user_template": None,
    },
}

_STRUCTURE_PROMPTS: dict[str, dict[str, str]] = {
    "default": {
        "system": (
            "You are an archivist structuring text for reading. You are not an "
            "editor: you never alter a single word, never reorder sentences, "
            "never translate, modernise, or rewrite. You may only add paragraph "
            "breaks and blank lines to improve readability. When in doubt, "
            "leave the text as one continuous paragraph."
        ),
        "user_template": None,  # loaded from file
    },
}

_TRANSLATION_PROMPTS: dict[str, dict[str, str]] = {
    "default": {
        "system": (
            "You are a translator specializing in historical documents. "
            "You translate text into English while preserving tone and style."
        ),
        "user_template": None,
    },
    "handwritten": {
        "system": (
            "You are a translator of handwritten historical documents. "
            "Translate into English while preserving the author's voice, "
            "including hesitations, corrections, and informal register. "
            "When a word is unclear, provide your best guess and mark it with [?]."
        ),
        "user_template": None,
    },
    "formal": {
        "system": (
            "You are a formal literary translator. "
            "Translate this text into polished, formal English. "
            "Preserve the structure and tone of the original. "
            "Use appropriate archaic or formal register where the original does."
        ),
        "user_template": None,
    },
    "technical": {
        "system": (
            "You are a technical translator specializing in scientific and engineering documents. "
            "Translate into English while carefully preserving all numbers, units, "
            "chemical formulas, measurements, and technical terminology. "
            "Use standard English technical vocabulary for domain-specific terms."
        ),
        "user_template": None,
    },
    "casual": {
        "system": (
            "You are a translator of informal historical correspondence. "
            "Translate into natural, conversational English. "
            "Preserve the casual tone, colloquialisms, and personal voice of the writer."
        ),
        "user_template": None,
    },
    "multi_lang": {
        "system": (
            "You are a translator of multilingual historical documents. "
            "The text may contain multiple languages. Translate ALL non-English "
            "portions into English. Leave already-English text as-is. "
            "If a language switch is detected, note it with [translated from <language>]."
        ),
        "user_template": None,
    },
}

LANG_DETECT_PROMPT: dict[str, str] = {
    "default": (
        "Identify the primary language of the following text. "
        "Reply with ONLY the ISO 639-1 language code (e.g. 'de', 'fr', 'en', 'ja', 'ru'). "
        "If the text contains multiple languages, reply with the majority language code. "
        "Do not explain.\n\n{text}"
    ),
    "multi_lang": (
        "Identify ALL languages present in the following text. "
        "Reply with a comma-separated list of ISO 639-1 codes in order of prevalence "
        "(e.g. 'de,en,fr'). If uncertain, provide your best guess.\n\n{text}"
    ),
}

DOCUMENT_TYPES = {
    "default": "General historical documents (default)",
    "handwritten": "Handwritten letters, notes, manuscripts",
    "typed_clean": "Clean typed/printed documents",
    "technical": "Scientific, engineering, or technical documents",
    "formal": "Formal/official documents, legal text",
    "casual": "Informal correspondence, personal letters",
    "multi_lang": "Documents with multiple languages mixed",
}


def _load_prompt_file(filename: str) -> str:
    """Load the user portion of a prompt from file."""
    path = PROMPT_DIR / filename
    if path.exists():
        template = path.read_text(encoding="utf-8")
        lines = template.splitlines()
        user_lines = [l for l in lines if not l.startswith("SYSTEM_PROMPT:")]
        return "\n".join(user_lines).strip()
    return ""


def get_cleanup_prompt(doc_type: str = "default") -> dict[str, str]:
    """Get system + user prompt template for cleanup stage.

    Falls back to 'default' if doc_type not found.
    """
    prompts = _CLEANUP_PROMPTS.get(doc_type, _CLEANUP_PROMPTS["default"])
    user_template = prompts["user_template"]
    if user_template is None:
        user_template = _load_prompt_file("cleanup_prompt.txt")
    return {"system": prompts["system"], "user": user_template}


def get_structure_prompt(doc_type: str = "default") -> dict[str, str]:
    """Get system + user prompt template for structure stage.

    Falls back to 'default' if doc_type not found.
    """
    prompts = _STRUCTURE_PROMPTS.get(doc_type, _STRUCTURE_PROMPTS["default"])
    user_template = prompts["user_template"]
    if user_template is None:
        user_template = _load_prompt_file("structure_prompt.txt")
    return {"system": prompts["system"], "user": user_template}


def get_translation_prompt(doc_type: str = "default") -> dict[str, str]:
    """Get system + user prompt template for translation stage.

    Falls back to 'default' if doc_type not found.
    """
    prompts = _TRANSLATION_PROMPTS.get(doc_type, _TRANSLATION_PROMPTS["default"])
    user_template = prompts["user_template"]
    if user_template is None:
        user_template = _load_prompt_file("translation_prompt.txt")
    return {"system": prompts["system"], "user": user_template}


def get_lang_detect_prompt(doc_type: str = "default") -> str:
    """Get the language detection prompt for the given document type."""
    return LANG_DETECT_PROMPT.get(doc_type, LANG_DETECT_PROMPT["default"])


def list_document_types() -> dict[str, str]:
    """Return available document types with descriptions."""
    return dict(DOCUMENT_TYPES)
