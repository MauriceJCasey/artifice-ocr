# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Structure stage: adds paragraph breaks and blank lines for reading.

This stage reflows already-finished text — it does NOT repair OCR errors
(that is cleanup's job) and it does NOT rewrite (that is translation's job).
It only adds paragraph breaks and blank lines to make the text readable.

The structuring pass is guarded: if the model alters any word, the original
text is kept instead, so a page is either structured or untouched — never
reworded.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import ollama

from artifice_ocr import _guard, _llm
from artifice_ocr._chunking import chunk_text, reassemble, estimate_tokens
from artifice_ocr._logging import get_logger
from artifice_ocr._prompts import get_structure_prompt
from artifice_ocr._resolution import backend_for, model_for
from artifice_ocr._retry import retry
from artifice_ocr.config import get as cfg
from artifice_ocr.output import stage_dir, write_stage_output

log = get_logger("structure")


@retry(max_attempts=4, base_delay=1.0, label="Structure")
def _call_structure_chunk(
    raw_text: str,
    system_prompt: str,
    user_template: str,
) -> str:
    """Structure a single chunk of text."""
    user_prompt = user_template.replace("{text}", raw_text)
    model = model_for("chat")
    backend = backend_for("chat")

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


def _structure_with_chunking(
    raw_text: str,
    system_prompt: str,
    user_template: str,
) -> str:
    """Structure text, chunking if it exceeds the model's context window."""
    max_tokens = cfg("chunk_max_tokens")
    overlap_tokens = cfg("chunk_overlap_tokens")

    est_tokens = estimate_tokens(raw_text)
    if est_tokens <= max_tokens:
        return _call_structure_chunk(raw_text, system_prompt, user_template)

    log.info(
        "Text too long (%d est. tokens), chunking for structuring...",
        est_tokens,
    )
    chunks = chunk_text(raw_text, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    log.info("Structuring %d chunk(s)...", len(chunks))

    structured_chunks = []
    for i, chunk in enumerate(chunks):
        log.info("  Chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
        structured = _call_structure_chunk(chunk, system_prompt, user_template)
        structured_chunks.append(structured)

    return reassemble(structured_chunks)


def _output_exists(stem: str, output_dir: str) -> bool:
    """Check if structured output for a stem already exists."""
    p = stage_dir(output_dir, "structured") / "text" / f"{stem}.txt"
    return p.exists()


def _load_existing_text(stem: str, output_dir: str) -> str:
    """Load existing structured text for a stem."""
    p = stage_dir(output_dir, "structured") / "text" / f"{stem}.txt"
    return p.read_text(encoding="utf-8")


def perform(
    text: str,
    *,
    source_file: str = "",
    output_dir: str = "output",
    stem: str | None = None,
) -> Dict[str, Any]:
    """Add paragraph breaks and blank lines for reading.

    `stem` overrides the output filename (derived from `source_file` by
    default) and may contain a relative subdirectory.

    Returns a dict with the structured text and guard result.
    """
    doc_type = cfg("document_type")
    prompts = get_structure_prompt(doc_type)
    system_prompt = prompts["system"]
    user_template = prompts["user"]

    model = model_for("chat")
    base_name = stem or (Path(source_file).stem if source_file else "unknown")

    # Resume: skip if structured output already exists
    if cfg("resume") and _output_exists(base_name, output_dir):
        log.info("Structure %s [skip — already done]", base_name)
        existing = _load_existing_text(base_name, output_dir)
        return {
            "source_file": source_file,
            "stage": "structured",
            "raw_text": text,
            "structured_text": existing,
            "engine": backend_for("chat"),
            "model": model,
            "system_prompt": system_prompt,
            "document_type": doc_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "guard": {"ok": True, "reasons": ["resume: existing output kept"]},
            "_skipped": True,
        }

    log.info("Structuring with %s (doc_type=%s)", model, doc_type)

    structured_text = _structure_with_chunking(text, system_prompt, user_template)

    # The guard verifies that no word was changed — only whitespace was added.
    guard_result = _guard.check_structure_only(text, structured_text)

    if guard_result.ok:
        final_text = structured_text
    else:
        log.warning(
            "Structure rejected for %s, keeping original: %s",
            base_name,
            "; ".join(guard_result.reasons),
        )
        final_text = text

    data = {
        "source_file": source_file,
        "stage": "structured",
        "raw_text": text,
        "structured_text": final_text,
        "engine": backend_for("chat"),
        "model": model,
        "system_prompt": system_prompt,
        "document_type": doc_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "guard": guard_result.to_dict(),
    }
    if not guard_result.ok:
        data["rejected_structured_text"] = structured_text

    write_stage_output(output_dir, "structured", base_name, text_content=final_text, metadata=data)

    if guard_result.ok:
        log.info("Structure complete (%d -> %d chars)", len(text), len(final_text))
    else:
        log.info(
            "Structure rejected for %s, original text kept (%d chars)",
            base_name,
            len(final_text),
        )
    return data
