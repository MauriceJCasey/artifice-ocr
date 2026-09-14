# Artifice OCR

Local-first, bring-your-own-model OCR pipeline for historical documents. OCR → guarded cleanup →
optional structuring/titling → optional translation, with two-way Tropy integration and a
citable PAGE XML record of every page. Everything runs against a model you choose — nothing
leaves your machine unless you point it at a cloud API yourself.

Part of the [Artifice Suite](../../README.md).

## How it works

1. **OCR** — a vision model (e.g. olmOCR-2 via Ollama or LM Studio) reads the page image.
2. **Cleanup** (optional, on by default) — a chat model repairs OCR artifacts. Guarded: if it
   deletes more than a couple of words, mangles capitalized nouns, or transliterates umlauts, the
   edit is rejected and the raw text is kept instead — nothing is silently rewritten.
3. **Structuring / titling** (optional) — paragraph breaks and an auto-generated page title.
   Structuring is guarded to word-for-word equality; only newlines may be added.
4. **Translation** (optional) — a multilingual model translates the cleaned text. Skipped
   automatically when the source is confidently detected as already English.

Every stage's output, including rejected guard attempts, is written to disk with full JSON
provenance (prompt, model, timings, guard result).

## PAGE XML

Every page's authoritative record is a [PRImA PAGE 2019](https://www.primaresearch.org/)
XML document — raw, cleaned, and translated text all live in the same file, indexed by stage,
so a correction to one never overwrites another. PAGE is what resume, export, and Tropy note
write-back all read from; the legacy `.txt`/JSON stage outputs are still written unchanged
alongside it for backward compatibility.

## Tropy integration

- **Live browse** — open a Tropy `.tpy` project read-only, browse lists/items/photos, and enqueue
  pages for OCR directly, with no manual export step.
- **JSON-LD import** — alternatively, import from a Tropy JSON-LD export file (File → Export →
  JSON-LD in Tropy).
- **Note write-back** — write OCR results back to the original Tropy photos as notes, through
  Tropy's Developer API, with a preview step and duplicate detection before anything is written.

## Exports

- **PDF / Markdown** — a continuous, typeset reading document (Playfair Display / Libre
  Baskerville), optionally bilingual (cleaned + translated side by side).
- **PAGE XML** — download a single page's record, or write one file per page for a batch
  selection.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). From the monorepo root:

```bash
uv sync --extra ocr-web
```

Point the app at a model in **Settings** — nothing is preselected. Recommendations (and why) live
in `packages/model-harness/src/model_harness/registry.py`, the suite's single source of truth for
model choices.

```bash
ollama pull richardyoung/olmocr2:7b-q8   # OCR (olmOCR-2)
ollama pull aya-expanse:8b               # translation
```

LM Studio works as an alternative for the vision stage — load a vision-capable model and set its
port in Settings. Note LM Studio fixes a model's context window when it loads it; raise that there
(`lms load <model> --context-length 8192`), not in Artifice's Settings.

## Usage

Web UI (recommended):

```bash
uv run artifice-ocr-web
# → http://127.0.0.1:8765
```

CLI, for scripting a single file or stage:

```bash
artifice-ocr pipeline path/to/scan.png
artifice-ocr ocr path/to/scan.png
artifice-ocr cleanup output/raw_ocr/text/scan.txt
artifice-ocr translate output/cleaned/text/scan.txt
artifice-ocr compile-pdf output/cleaned/text/Collection --stage cleaned
```

Run `artifice-ocr --help` for the full command list.

## Configuration

Key settings (`configs/default.yaml`, environment variables, or the Settings UI):

| Key | Default | Description |
| --- | --- | --- |
| `ocr_model` / `cleanup_model` / `translate_model` | empty | Ships unset — nothing runs on a model you didn't choose |
| `ocr_backend` / `cleanup_backend` / `translate_backend` | `auto` | `auto` probes Ollama and LM Studio; or set explicitly |
| `lm_studio_url` | `http://localhost:1234/v1` | LM Studio endpoint |
| `ollama_url` | `http://localhost:11434` | Ollama endpoint |
| `context_size` | `8192` | Model context window in tokens (Ollama only) |
| `cleanup_guard` / `structure_guard` | `true` | Content-preservation guards |
| `title_enabled` | `false` | Auto-generated page titles |
| `translate_enabled` | `true` | Translation stage |

## Testing

```bash
uv run pytest apps/artifice-ocr/tests/
```
