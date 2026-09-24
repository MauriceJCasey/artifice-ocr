# Artifice OCR

Local-first OCR for historical documents, built around a vision language model and
[Tropy](https://github.com/tropy/tropy). Nothing leaves your machine unless you point it at a
remote API yourself.

## What it does

- **OCR** scans and photographs with a vision model such as
  [olmOCR-2](https://github.com/allenai/olmocr) or [Churro](https://github.com/stanford-oval/Churro).
- **Segments** pages into regions first, if you want (multi-column layouts, marginalia).
- **Cleans up** and **translates** text in optional, guarded model passes: a change that drops
  words or rewrites valid ones is rejected and kept for review, never applied silently.
- **Reviews** results side by side with the scan, with in-place correction and a diff.
- **Exports** to PDF, Markdown, PAGE XML and JSON, and writes notes back to Tropy.

## Run it

From the repository root:

```bash
uv sync --extra ocr-web
uv run artifice-ocr-web      # opens http://localhost:8765
```

Then pick your models in **Settings**. Any Ollama, LM Studio or OpenAI-compatible server works;
nothing is preselected. The suite recommends `richardyoung/olmocr2:7b-q8` for OCR (about 12 GB
VRAM for full GPU offload) and `aya-expanse:8b` for translation.

There is also a command-line pipeline: `uv run artifice-ocr --help`.

## Test

```bash
cd apps/artifice-ocr && uv run pytest -q
```

## License

AGPL-3.0-or-later.
