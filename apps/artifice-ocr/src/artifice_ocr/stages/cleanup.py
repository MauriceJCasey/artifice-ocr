# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import ollama

from artifice_ocr import _guard, _llm
from artifice_ocr._chunking import chunk_text, reassemble, estimate_tokens
from artifice_ocr._logging import get_logger
from artifice_ocr._normalise import normalise
from artifice_ocr._prompts import get_cleanup_prompt
from artifice_ocr._resolution import backend_for, model_for
from artifice_ocr._retry import retry
from artifice_ocr.config import get as cfg
from artifice_ocr.output import write_stage_output

log = get_logger("cleanup")


@retry(max_attempts=4, base_delay=1.0, label="Cleanup")
def _call_cleanup_chunk(
    raw_text: str,
    system_prompt: str,
    user_template: str,
) -> str:
    """Clean up a single chunk of text."""
    user_prompt = user_template.replace("{raw_text}", raw_text)
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


def _cleanup_with_chunking(
    raw_text: str,
    system_prompt: str,
    user_template: str,
) -> str:
    """Clean text, chunking if it exceeds the model's context window."""
    max_tokens = cfg("chunk_max_tokens")
    overlap_tokens = cfg("chunk_overlap_tokens")

    est_tokens = estimate_tokens(raw_text)
    if est_tokens <= max_tokens:
        return _call_cleanup_chunk(raw_text, system_prompt, user_template)

    log.info(
        "Text too long (%d est. tokens), chunking for cleanup...",
        est_tokens,
    )
    chunks = chunk_text(raw_text, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    log.info("Cleaning %d chunk(s)...", len(chunks))

    cleaned_chunks = []
    for i, chunk in enumerate(chunks):
        log.info("  Chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
        cleaned = _call_cleanup_chunk(chunk, system_prompt, user_template)
        cleaned_chunks.append(cleaned)

    return reassemble(cleaned_chunks)


def perform(
    raw_text: str,
    *,
    source_file: str = "",
    output_dir: str = "output",
    stem: str | None = None,
) -> Dict[str, Any]:
    """Clean raw OCR text.

    `stem` overrides the output filename (derived from `source_file` by
    default) and may contain a relative subdirectory.
    """
    doc_type = cfg("document_type")
    prompts = get_cleanup_prompt(doc_type)
    system_prompt = prompts["system"]
    user_template = prompts["user"]

    model = model_for("chat")
    log.info("Cleaning with %s (doc_type=%s)", model, doc_type)

    # Deterministic pre-pass: repair hyphenation, em-dash breaks, whitespace
    # artifacts before the text reaches the model.  Must run on the full text
    # before chunking so a hyphenated word split across a chunk boundary is
    # still repaired.
    normalised_text, normalise_counts = normalise(raw_text)
    if any(normalise_counts.values()):
        log.info(
            "Pre-pass repairs: %s", ", ".join(f"{k}={v}" for k, v in normalise_counts.items() if v)
        )

    model_text = _cleanup_with_chunking(normalised_text, system_prompt, user_template)

    # The guard compares model output against the text the model *received*
    # (the normalised text), not the original raw text.  Comparing against
    # raw would see pre-pass repairs (hyphen joins, em-dash joins, space
    # collapse) as model changes and falsely reject clean output.
    #
    # When the guard rejects, we keep the normalised text rather than the
    # raw text — the pre-pass repairs are deterministic and safe, so the
    # user still sees a better page than if they were discarded.
    cleaned_text, guard_result = _guard.apply(normalised_text, model_text)

    base_name = stem or (Path(source_file).stem if source_file else "unknown")

    data = {
        "source_file": source_file,
        "stage": "cleaned",
        "raw_text": raw_text,
        "cleaned_text": cleaned_text,
        "engine": backend_for("chat"),
        "model": model,
        "system_prompt": system_prompt,
        "document_type": doc_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "guard": guard_result.to_dict(),
        "pre_pass": normalise_counts,
    }
    if not guard_result.ok:
        data["rejected_cleaned_text"] = model_text

    write_stage_output(output_dir, "cleaned", base_name, text_content=cleaned_text, metadata=data)

    if guard_result.ok:
        log.info("Cleanup complete (%d -> %d chars)", len(raw_text), len(cleaned_text))
    else:
        log.info("Cleanup rejected for %s, raw text kept (%d chars)", base_name, len(cleaned_text))
    return data
