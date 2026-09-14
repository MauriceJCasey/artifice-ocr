# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Text chunking for models with context-window limits.

Splits text into pieces that respect paragraph/sentence boundaries,
then reassembles after processing.
"""

from artifice_ocr._logging import get_logger

log = get_logger("chunking")

# Approximate chars-per-token ratio for English (safe under-estimate)
_CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    """Rough token count estimate based on character length."""
    return int(len(text) / _CHARS_PER_TOKEN)


def chunk_text(
    text: str,
    max_tokens: int = 3500,
    overlap_tokens: int = 200,
    separator: str = "\n\n",
) -> list[str]:
    """Split *text* into chunks that stay under *max_tokens* (estimated).

    Strategy:
      1. Split on *separator* (default: double-newline paragraph break).
      2. Greedily pack paragraphs into chunks.
      3. If a single paragraph exceeds *max_tokens*, hard-split it on
         sentence boundaries (`. `), then on word boundaries as a last resort.
      4. *overlap_tokens* chars of trailing context is carried into the next
         chunk so the model has continuity.

    Returns a list of chunk strings.  If the text already fits, returns
    a single-element list.
    """
    max_chars = int(max_tokens * _CHARS_PER_TOKEN)
    overlap_chars = int(overlap_tokens * _CHARS_PER_TOKEN)

    if estimate_tokens(text) <= max_tokens:
        return [text]

    # Phase 1: split into paragraphs
    paragraphs = text.split(separator)

    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Paragraph fits in current chunk
        if len(current) + len(para) + len(separator) <= max_chars:
            current = (current + separator + para).strip() if current else para
            continue

        # Current chunk is non-empty — flush it
        if current:
            chunks.append(current)
            # Carry overlap from tail
            if overlap_chars and len(current) > overlap_chars:
                current = current[-overlap_chars:] + separator + para
            else:
                current = para
            # If even after flush the paragraph itself is too big, sub-split
            if len(current) > max_chars:
                chunks.extend(_split_long_paragraph(current, max_chars, overlap_chars, separator))
                current = ""
            continue

        # Current is empty and paragraph alone is too big
        if len(para) > max_chars:
            chunks.extend(_split_long_paragraph(para, max_chars, overlap_chars, separator))
            current = ""
        else:
            current = para

    if current.strip():
        chunks.append(current.strip())

    log.info(
        "Split %d chars into %d chunk(s) (max %d chars)",
        len(text), len(chunks), max_chars,
    )
    return chunks


def _split_long_paragraph(
    text: str,
    max_chars: int,
    overlap_chars: int,
    separator: str,
) -> list[str]:
    """Handle a single paragraph that exceeds the max chunk size."""
    # Try sentence boundaries first
    sentences = _split_sentences(text)
    if len(sentences) > 1:
        return _pack_fragments(sentences, max_chars, overlap_chars, separator)

    # Last resort: split on word boundaries
    words = text.split()
    if len(words) > 1:
        return _pack_fragments(words, max_chars, overlap_chars, " ")

    # Truly single token — return as-is (will truncate at model level)
    return [text]


def _split_sentences(text: str) -> list[str]:
    """Split text on sentence boundaries."""
    import re
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [p for p in parts if p.strip()]


def _pack_fragments(
    fragments: list[str],
    max_chars: int,
    overlap_chars: int,
    sep: str,
) -> list[str]:
    """Pack pre-split fragments into chunks."""
    chunks: list[str] = []
    current = ""

    for frag in fragments:
        frag = frag.strip()
        if not frag:
            continue

        if len(current) + len(frag) + len(sep) <= max_chars:
            current = (current + sep + frag).strip() if current else frag
        else:
            if current:
                chunks.append(current)
                if overlap_chars and len(current) > overlap_chars:
                    current = current[-overlap_chars:] + sep + frag
                else:
                    current = frag
            else:
                # Single fragment still too big — force-split on words
                words = frag.split()
                sub = ""
                for w in words:
                    if len(sub) + len(w) + 1 <= max_chars:
                        sub = (sub + " " + w).strip() if sub else w
                    else:
                        if sub:
                            chunks.append(sub)
                        sub = w
                current = sub

    if current.strip():
        chunks.append(current.strip())

    return chunks


def reassemble(chunks: list[str], separator: str = "\n\n") -> str:
    """Join processed chunks back together."""
    return separator.join(c for c in chunks if c.strip())
