# model-harness

Shared connector surface for the Artifice Suite apps. Every model interaction
in `artifice-ocr`, `artifice-draft`, and `artifice-graph` must be issued
through structured, schema-validated calls rather than freeform chat — see
`CLAUDE.md` at the repo root. `artifice-transcribe` uses this package's
`ModelConnectorConfig` for its `openai`-compatible summarization calls, in
addition to its own Whisper/pyannote pipeline.

This package currently defines the shared configuration and request/response
contract (`ModelConnectorConfig`, `SchemaCall`); provider-specific transport
(Ollama, LM Studio, generic API) is implemented per-app today and is the
planned next step to consolidate here.

Install into an app's environment with:

```
pip install -e ../../packages/model-harness
```
