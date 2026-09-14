# Artifice OCR

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
