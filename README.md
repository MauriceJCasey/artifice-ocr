# Artifice OCR

[![safety-tests](https://github.com/MauriceJCasey/artifice-ocr/actions/workflows/safety-tests.yml/badge.svg)](https://github.com/MauriceJCasey/artifice-ocr/actions/workflows/safety-tests.yml)
[![REUSE status](https://api.reuse.software/badge/github.com/MauriceJCasey/artifice-ocr)](https://api.reuse.software/info/github.com/MauriceJCasey/artifice-ocr)

A local-first, bring-your-own-model (BYOM) pipeline for historical document OCR, cleanup, and
translation. Runs against a local LLM server (Ollama, LM Studio, or any OpenAI-compatible
endpoint) — no document ever leaves your machine unless you point it at a remote API yourself.

## ⚠️ Development status

**Active development, early stage.** This is provided as-is for developers and advanced users
comfortable troubleshooting local models. Expect bugs, breaking changes, and rough edges.

## Getting started

```bash
uv sync --extra web
uv run artifice-ocr-web
```

See `apps/artifice-ocr/` for the application itself and `packages/` for the shared support
libraries it depends on (model harness, output layout, secure I/O, shared UI assets).

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

<!-- BEGIN GENERATED DEPENDENCIES (see scripts/export-to-public-repos.sh) -->
## Dependencies

This list is generated directly from [`apps/artifice-ocr/pyproject.toml`](apps/artifice-ocr/pyproject.toml) — the actual `dependencies`/`optional-dependencies` tables, not hand-maintained. `.github/workflows/dependency-guard.yml` fails the build if `uv.lock` ever drifts from this file, so any dependency change (including an unreviewed addition) shows up as an explicit, reviewable diff.

**Core:**

- `typer`
- `ollama`
- `openai`
- `python-dotenv`
- `pyyaml`
- `PyMuPDF`
- `Pillow`
- `numpy`
- `reportlab`
- `huggingface_hub`
- `artifice-secure-io>=0.2.0`
- `artifice-output-layout>=0.1.0`
- `artifice-shared-ui>=0.2.0`
- `artifice-model-harness>=0.2.0`
- `jinja2>=3.0`

**`web` extra:**

- `fastapi`
- `httpx>=0.27`
- `uvicorn[standard]`
- `python-multipart>=0.0.9`

**`window` extra:**

- `pywebview>=5.0`

**`segmentation-doclayout-yolo` extra:**

- `doclayout-yolo>=0.0.4`

**`segmentation-kraken` extra:**

- `kraken>=6.0.3,<7; python_version < '3.13'`

**`segmentation-diff-residual` extra:**

- `opencv-python-headless>=4.10`

**`segmentation` extra:**

- `artifice-ocr[segmentation-doclayout-yolo]`
- `artifice-ocr[segmentation-kraken]`
- `artifice-ocr[segmentation-diff-residual]`

For the complete resolved dependency tree (including transitive dependencies), see `uv.lock` at the repo root, or run `uv tree`.
<!-- END GENERATED DEPENDENCIES -->
