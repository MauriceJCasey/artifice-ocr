# ArtificeOCR

**Local-First Archival OCR, Preservation Cleanup & Translation for Historical Research**

*Part of the [Artifice Suite](../../README.md) — Local-First, Model-Agnostic Software Harnesses for Humanities Research.*

---

## 🏛️ Philosophy: The Software Harness vs. The Chatbot

ArtificeOCR is a local-first pipeline built specifically for processing, cleaning, structuring, translating, and publishing historical documents—engineered for archival research, not demos. It operates around Joseph Weizenbaum’s anti-ELIZA principle: **software should perform deterministic computing tasks, and AI models should be invoked strictly as guarded text transformation engines.**

┌────────────────────────────────────────────────────────────────────────────┐
│                          ArtificeOCR Harness                               │
│                                                                            │
│   1. Vision OCR Extraction (olmocr-2-7b via LM Studio)                     │
│   2. Guarded Text Cleanup (Gemma 4 via Ollama - Capitalisation/Umlaut Guard)│
│   3. Guarded Text Structuring (Gemma 4 via Ollama - Word-for-Word Guard)   │
│   4. Auto-Generated Page Titles (optional, via cleanup model)              │
│   5. Historical Translation (TranslateGemma via Ollama - German to English)│
│   6. Multi-Format Export (PDF / Markdown / Tropy Writeback)                │
└────────────────────────────────────────────────────────────────────────────┘

1. **Deterministic Execution, No Conversational Noise:** ArtificeOCR never "chats" about documents. It processes images or archival manifests through a strict multi-stage pipeline and outputs structured JSON, Markdown, or PDF assets.
2. **Preservation Over Prettiness (The Guard System):** Historical texts—especially fragmentary 1920s–1940s German archival records—contain fragile spellings, capitalized nouns, and OCR artifacts. The cleanup and structuring stages are strictly *guarded*. If a model attempts to alter valid words, capitalized German nouns, or delete text beyond tight thresholds, the modification is rejected and saved as `rejected_*` for review—nothing is silently lost or rewritten.
3. **Local-First & Archival Privacy:** All vision and language models run locally on your GPU via **LM Studio** and **Ollama**. Confidential archival findings and copyright-restricted manuscript photos never leave your hardware.
4. **Editorial Visual Identity:** Built using **The New Masses Design System** (`packages/shared-ui`)—a warm, paper-and-ink interface inspired by 1930s radical editorial design and Soviet Constructivism.

---

## ✨ Core Capabilities

### 1. Guarded 5-Stage Processing Pipeline
Runs entirely on local GPU hardware with complete JSON metadata outputs (prompts, confidence, guard results, timings) at every stage:
* **Stage 1 — Vision OCR:** Converts document scans and photos into raw text using `allenai/olmocr-2-7b` via LM Studio.
* **Stage 2 — Guarded Cleanup:** Repairs OCR artifacts using a local chat model via Ollama. Guarded against word deletions, umlaut transliteration corruptions (`ueber` $\rightarrow$ `über`), and loss of capitalized German nouns.
* **Stage 3 — Guarded Structuring:** Adds paragraph breaks for human readability using the same chat model. Guarded by **word-for-word equality**—only newline insertions are allowed.
* **Stage 4 — Auto-Generated Page Titles (optional):** Generates short archival titles (≤120 chars) per page using the configured `cleanup_model` via `model_harness.contract`. Opt-in via `title_enabled` config (default off). Guarded by length cap + truncation, accent warnings, and repetition rejection; falls back to basename on any failure. Outputs to `title/text/` and `title/json/`.
* **Stage 5 — Historical Translation:** Optional translation (e.g., German to English) using a multilingual model such as `aya-expanse:8b` via Ollama.

### 2. Deep Tropy Archive Integration
Connects to [Tropy](https://tropy.org) historical research archives via a **JSON-LD file bridge** — export from Tropy, import into ArtificeOCR, process, export back:
* **Import Preview:** `tropy-import` scans a Tropy JSON-LD export and surfaces groups (`@type: Collection`), items, and photo paths before any file is touched. Path validation (`_tropy_pathcheck`) rejects entries whose absolute paths fall outside the configured allow-list root.
* **Import Add:** Selected items are imported as pipeline-eligible job items with full provenance (`origin: "tropy-jsonld"`, `tropy_group`, `tropy_item_id`), mirroring Tropy's item/page structure on disk.
* **Export & Export History:** `tropy-export` writes processed OCR text (structured, cleaned, or translated) into a new JSON-LD envelope as Tropy notes. `tropy-export-history` exports only items that already exist in the local run history, enabling incremental re-export.
* **Live Read-Only `.tpy` Browse:** Opens Tropy `.tpy` SQLite databases directly in read-only mode (feature-flagged via `ARTIFICE_OCR_TROPY_LIVE_READ`). Browse projects, lists, tags, items, and photos — then enqueue directly into the OCR pipeline without manual JSON-LD export.
* **Safe Note Write-Back:** Previews and attaches OCR text to the original Tropy photos through Tropy's Developer API, verifies the open project, and skips identical notes. JSON-LD export remains available for creating new items; direct database writes are an advanced, default-off fallback.
* **Inline Warning Surfacing:** Missing photos and pathcheck rejections render as inline warnings in the import modal, giving immediate feedback before enqueue.
* **Workflow Memory:** Persists last Tropy import path and export path in user settings across sessions.

### 3. Multi-Format Publishing Exports
* **Typeset PDF Compilation:** Generates continuous reading PDFs with section headings per item, provenance page markers (`[page1]`), and Playfair/Libre Baskerville typography.
* **Markdown Export:** Exports cleaned and structured text as portable `.md` files.
* **Structured JSON Data:** Full audit logs for every page, preserving raw OCR text, accepted cleanup, rejected model attempts, and guard status logs.

#### Guided PDF export and output folders

The web interface's **Compile PDF** workspace previews available pages and
stages before starting, warns about missing translations, streams progress,
and supports cancellation. Defaults write PDFs to the project's
`exports/pdf/` folder and Markdown to `exports/markdown/`.

New runs use the project layout described in
[`docs/OUTPUT_LAYOUT.md`](../../docs/OUTPUT_LAYOUT.md): intermediate files are
under `pipeline/<stage>/{text,records}/`, while files intended for people or
other applications are under `exports/<type>/`. Existing `output/<stage>` and
Graph `data/output` folders remain readable and are never moved automatically.

---

## 🛡️ The Guard System (Preservation Details)

| Stage | Guard Implementation | Protection Objective |
| :--- | :--- | :--- |
| **Cleanup** | `_guard.check_cleanup` | Protects German capitalized nouns, forbids umlaut transliteration (`ueber` $\rightarrow$ `über`), enforces word deletion thresholds (default $\le$ 2 words), and maintains length ratios ($\ge$ 97% letters retained). |
| **Structure** | `_guard.check_structure_only` | Enforces **word-for-word equality**—only newlines may be added. Any word alteration triggers a rejection, retaining raw text and saving the attempt as `rejected_structured_text`. |
| **Title** | Length cap + truncation, accent warning, repetition rejection, provenance marker | Cap at `title_max_chars` (default 120); warns on replaced non-ASCII accents; rejects repeated-phrase fill; records `generated_by_model: true` with model name. Falls back to basename on any failure. |

---

## 🎨 Design System (`packages/shared-ui`)

All visual interfaces in ArtificeOCR adhere to **The New Masses Design System**:
* **Palette:** Warm cream paper (`#f6f3ea`), deep warm black ink (`#1b1813`), Esperanto green accents (`#2f7d45`), and antique gold highlights (`#bf9b30`).
* **Typography:** Playfair Display (Display/Headings), Libre Baskerville (Body/Manuscripts), and Archivo (UI Labels/Buttons).
* **Surface Depth & Motion:** Paper-like diffused shadows (`shadow-paper`), card lifts (4px), rule draw-in animations, and tactile button presses.

---

## 📂 Monorepo Architecture

ArtificeOCR is located at `apps/artifice-ocr` within the Artifice Suite monorepo and shares core dependencies with partner applications:

```
artifice-suite/
├── apps/
│   └── artifice-ocr/
│       ├── src/
│       │   ├── cli.py                 # Typer CLI entry point
│       │   ├── pipeline.py            # Stage orchestration (shared by CLI/GUI/Web)
│       │   ├── jobs.py                # Threaded JobRunner with pause/cancel
│       │   ├── history.py             # SQLite run history
│       │   ├── tropy_jsonld.py        # JSON-LD file bridge (import + export)
│       │   ├── tropy_db.py            # Live read-only .tpy browser (feature-flagged)
│       │   ├── _tropy_pathcheck.py    # Photo-path safety validation
│       │   ├── pdf_export.py          # Guided PDF/Markdown compilation
│       │   ├── output.py              # Canonical/legacy stage path resolver
│       │   ├── _guard.py              # Content preservation guards
│       │   ├── _diff.py               # Diff & marker highlighting
│       │   ├── stages/                # OCR, Cleanup, Title, Structure, Translate
│       │   └── web/                   # FastAPI server & vanilla JS SPA
│       │       └── routers/
│       │           └── tropy_browse.py
│       ├── tests/                     # Pytest suite
│       └── README.md
└── packages/
    ├── shared-ui/                     # The New Masses CSS tokens, web components, upload guards
    ├── model-harness/                 # BYOM connectors (Ollama/LM Studio)
    └── secure-io/                     # Hardened file and path I/O
```

---

## 🚀 Setup & Hardware Requirements

### Prerequisites & Dependencies
Ensure **Python 3.11+** is installed. The suite is a [uv](https://docs.astral.sh/uv/)
workspace — the bootstrap script installs `uv` if it is missing. From the monorepo root:

```bash
bash scripts/install.sh artifice-ocr
```

PowerShell:

```powershell
.\scripts\install.ps1 artifice-ocr
```

This runs `uv tool install --editable` against the local workspace, so the app
stays linked to your clone. Uninstall with `bash scripts/uninstall.sh artifice-ocr`.

> Use `uv` workspace commands, not bare `pip install`. A `pip install -e` line
> here previously named a `packages/core-types` that does not exist, so it failed
> outright for anyone who followed it.

### Engine Setup & Model Provisioning

The app is model-agnostic — set whatever you have in **Settings**. These are the
suite's recommendations, and they come from
`packages/model-harness/src/model_harness/registry.py`, which is the single
source of truth. It records provenance badges alongside each entry, because a
model whose training data cannot be inspected cannot be cited honestly in a
methods section.

- **Ollama** on port `11434`:
  ```bash
  ollama pull richardyoung/olmocr2:7b-q8   # OCR — Allen AI olmOCR-2, Strict Open Data
  ollama pull aya-expanse:8b               # translation — Open Science Lab
  ```
  olmOCR-2 wants ~12 GB VRAM for full GPU offload; it runs on 8 GB with CPU
  fallback at reduced throughput.
- **LM Studio** on port `1234`, as an alternative: load `allenai/olmocr-2-7b`.
  Note that LM Studio fixes a model's **context window when it loads it** — if a
  page fails with "exceeds the available context size", raise it there
  (`lms load <model> --context-length 8192`), not in Artifice's Settings.

### Cross-Platform & macOS Apple Silicon Notes
- **Linux / Windows (CUDA)**: Native GPU acceleration via CUDA drivers.
- **macOS (Apple Silicon Metal)**: Run **LM Studio** and **Ollama** natively on the host to leverage Apple Metal Performance Shaders (MPS) and Unified Memory. If running containers via Docker, connect to host models using `http://host.docker.internal:1234/v1` and `http://host.docker.internal:11434`.

### Hardware Recommendations
| Tier | GPU VRAM | System RAM | Notes |
| :--- | :--- | :--- | :--- |
| **Recommended** | 16 GB (RTX 4060 Ti 16G / 4070+) | 32 GB | All models resident simultaneously. |
| **Minimum (GPU)** | 12 GB (RTX 3060 / 4070) | 16 GB | Requires sequential model offloading between stages. |
| **Apple Silicon** | 24 GB Unified (M3/M4 Pro) | — | Unified memory handles vision and LLM weights comfortably. |

---

## 🖥️ Usage & Interfaces

### 1. CLI Commands
```bash
# Process single image through full pipeline:
artifice-ocr pipeline path/to/document.png --skip-translate

# Execute individual processing stages:
artifice-ocr ocr path/to/image.png
artifice-ocr cleanup output/raw_ocr/text/file.txt
artifice-ocr structure output/structured/text/file.txt
artifice-ocr translate output/structured/text/file.txt

# Tropy JSON-LD Workflows:
artifice-ocr tropy-import "path/To/Export.jsonld"
artifice-ocr tropy-export --output tropy-notes.jsonld

# Export Compilation:
artifice-ocr compile-pdf output/cleaned/text/Collection --stage cleaned
artifice-ocr export-md output/cleaned/text/Collection --author "Fritz Eberhard" --date "1936-1939"
```

### 2. Web UI (Recommended)
Launches the FastAPI server with The New Masses editorial layout, real Google Fonts, and Server-Sent Events (SSE) live progress tracking:
```bash
python -m artifice_ocr.web
# → Access at http://127.0.0.1:8765
```

---

## ⚙️ Configuration
Set defaults via `configs/default.yaml` or environment variables:

| Variable / Key | Default | Description |
| :--- | :--- | :--- |
| `lmstudio_base_url` | `http://localhost:1234/v1` | LM Studio vision OCR endpoint |
| `ollama_host` | `http://localhost:11434` | Ollama LLM endpoint |
| `ocr_model` | *(empty)* | Vision model for the OCR stage. Recommended: `richardyoung/olmocr2:7b-q8` |
| `cleanup_model` | *(empty)* | Model for guarded cleanup and structuring |
| `translate_model` | *(empty)* | Model for translation. Recommended: `aya-expanse:8b` |
| `context_size` | `0` | Model context window in tokens. `0` leaves it to the model. Ollama only — LM Studio and hosted APIs set it themselves |
| `cleanup_guard` | `true` | Enable German noun / umlaut protection guard |
| `structure_guard` | `true` | Enable word-for-word equality guard |
| `ollama_think` | `false` | Disable reasoning tokens (13× speedup during cleanup) |

The three model settings ship **empty** — nothing is preselected, so the app
never silently uses a model you did not choose. Set them in **Settings**, or via
`OCR_MODEL` / `CLEANUP_MODEL` / `TRANSLATE_MODEL`.

---

## 🛠️ Open-Source Extension Points

We welcome contributions from historians, archivists, and software engineers!

1. **Custom Stage Guards (`apps/artifice-ocr/src/_guard.py`)**: Implement specialized content-preservation rules for specific languages or historical writing styles (e.g., Fraktur font artifacts, early modern orthography).
2. **Archival Exporters (`apps/artifice-ocr/src/`)**: Add writeback connectors for other archival software (e.g., Omeka, Arches, or custom IIIF manifests).
3. **Vision OCR Connectors (`apps/artifice-ocr/src/stages/ocr.py`)**: Extend vision stage adapters to support additional local vision-language models.

---

## 🧪 Testing

Run the full pytest suite covering CLI commands, guard validation logic, Tropy JSON-LD bridge, and export compilers:
```bash
pytest apps/artifice-ocr/tests/
```
