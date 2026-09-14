# Diff-and-Crop Arbitration — plan and assessment

**Status:** Proposal. Not started. Opinion + design; no code yet.
**Date:** 2026-08-26
**Depends on:** the Tesseract engine (#86) and the pre-processing work (#84).

> Verify premises against the code before building. This references work that is
> in flight (the Tesseract engine) and one capability the engine does **not** yet
> have (word bounding boxes — see "What has to be built first").

## The idea, in one line

Run Tesseract (deterministic, with coordinates) **and** a vision-language model
(fluent, but prone to hallucinating plausible words) on the same page; use a
deterministic diff to find where they disagree; and re-read **only those spots**
with a tightly-cropped, context-free VLM query — so the model transcribes pixels,
not context. A deterministic **referee** that decides *where* to look; the VLM as
a constrained *witness*.

## Verdict

**Feasible: yes, with one genuinely hard part. Desirable: yes, specifically for
this corpus.** This is a strong fit for 1930s degraded German resistance
mimeographs, where the high-value tokens — dates, pseudonyms, place-names — are
exactly the ones a fluent VLM will confidently invent, and exactly the ones a
historian must be able to cite honestly. It aligns with the suite's ethos:
auditable provenance, never a confident guess dressed as a reading.

It is **not** a general accuracy win to switch on for every document — it roughly
doubles the OCR cost and adds N cropped re-queries per page. It is a
**precision instrument for high-stakes pages**, opt-in, and it earns its cost on
exactly the material you describe.

One framing correction worth keeping: the *system* is deterministic in where it
looks and what it flags; the arbitration re-read is still a (better-constrained)
VLM call. So it is a deterministic **referee over a non-deterministic witness**,
not a fully deterministic transcriber. Say that plainly in the UI and the docs —
overclaiming determinism would be the one way this undermines the trust it exists
to build.

## Where it is genuinely hard (be honest before building)

1. **Alignment is the make-or-break.** Aligning Tesseract's token stream to the
   VLM's free-form Markdown is the crux, and it is hard precisely on degraded
   pages. The VLM reflows lines, imposes structure (headings, tables), may
   reorder multi-column text, and drops or invents whole spans. Word-index
   alignment breaks on all of these. `rapidfuzz`'s `editops`/`opcodes` over a
   normalised token sequence is a sound *start*, but it must be anchored (align
   on high-confidence matching runs first, then diff within the gaps) and must
   degrade gracefully when the two streams diverge structurally rather than
   producing garbage substitutions.

2. **Bounding boxes exist only for Tesseract tokens.** You can crop a divergence
   only where Tesseract detected *something* at known coordinates. Two
   consequences:
   - A VLM **pure hallucination with no Tesseract anchor** (the model invented a
     word in a spot Tesseract read as nothing) has no box to crop — it cannot be
     arbitrated this way, only flagged. Partial coverage by design.
   - On heavily degraded mimeographs Tesseract's own segmentation and coordinates
     are shaky, so a crop can land off-target. **This is why the pre-processing
     work matters here**: deskew + binarisation (Phase 2 of the pre-processing
     plan) materially improves Tesseract's box accuracy, which is the input this
     whole pipeline stands on. Build that first, or the crops wander.

3. **Reconciliation must not corrupt Markdown.** Splicing arbitrated text back by
   character span is fiddly around table pipes, emphasis, and headings. The right
   discipline: track `(start, end)` spans of *words* in the raw Markdown, and
   splice only within a word span, never across a syntax token. Re-validate the
   Markdown parses after each splice.

4. **Cost and latency.** Two full OCR passes plus N crop re-queries per page. The
   RTX 5070 Ti and `ThreadPoolExecutor` batching to Ollama make it viable as an
   **offline/batch** step, not interactive. Bound the concurrency to avoid
   thrashing VRAM.

## Is it worth it? Measure first, and start minimal

Honest counterweight to the ambition above — recorded here so the cheap path is
on the record next to the elaborate one.

**The full automated pipeline's hardest part coincides with its motivating
case.** It works best when Tesseract is *decent* and the VLM occasionally invents
a confident word — then the diff is real signal. On the worst degraded
mimeographs, Tesseract's segmentation and coordinates are themselves unreliable,
so the "deterministic referee" is weakest exactly where it is needed most, and
the alignment produces noise rather than divergences. There is a real risk of
building an elaborate machine that helps most on pages that were already fine.
The riskiest, least-valuable part is **auto-reconciliation** — silently splicing
machine edits into a transcription is precisely what a historian should distrust.

### Measure before building (an afternoon, not a sprint)

Run the current Tesseract + VLM on ~20 representative pages of the real corpus and
count: **how often does the VLM hallucinate a high-entropy token (date,
pseudonym, place) that Tesseract read correctly?** That single number decides the
whole investment:

- **~1 per several pages** → the minimal version below is more than enough; the
  full pipeline is over-engineering.
- **pervasive** → automating the *flagging* (diff/align) starts to pay off — but
  as "flag for review", never as silent reconciliation.

`jiwer` (dev-only) or manual checking answers this before any weeks are spent.

### The minimal version (build this first)

A **human-triggered crop re-query** in the correction/preview UI — ~15% of the
work for ~80% of the value, and a better ethos fit because the tool shows its
working and the human adjudicates:

1. **Select-to-re-read.** In the preview/correction view, the historian selects a
   distrusted word or drags a box over a region.
2. **Crop + strict query.** The backend crops that region from the *pre-processed*
   page image, sends it through `query_vlm_crop(bytes, STRICT_PROMPT)` — the same
   context-free "transcribe only these pixels, output [ILLEGIBLE] if you cannot"
   prompt — over the existing vision backend.
3. **Propose, never replace.** Show the crop's reading beside the original; the
   historian accepts or edits. Nothing is auto-written.
4. **Provenance.** Record that the token was crop-arbitrated (and whether a human
   accepted it).

**Why it is cheap:** no alignment, no reconciliation, no `rapidfuzz`, no OpenCV
required. It needs only the `query_vlm_crop` wrapper (a thin call over the
existing vision path) and one UI selection hook. `get_tesseract_data` is optional
— if present, snap the human's selection to the nearest Tesseract word box;
otherwise the human's box is enough. It targets exactly the tokens that matter,
because the human — who knows which date or name is load-bearing for a citation —
chooses them.

### Escalation ladder

1. Ship the Tesseract engine + fallback (#86) — already recovers looped pages
   cheaply.
2. Measure the hallucination rate on real pages.
3. Build the minimal human-triggered crop re-query above.
4. **Only if the numbers justify it**, add automated *flagging* of divergences
   (diff/align surfaced for review) — and even then, never silent
   auto-reconciliation into the Markdown.

## What has to be built first

- **Tesseract word data with coordinates.** The current `_tesseract.py` (#86)
  returns text via `tesseract … stdout`. Arbitration needs the per-word dict the
  prompt assumes (`text, x, y, w, h, conf`). Add a `get_tesseract_data(image)`
  that runs `tesseract … tsv` (or uses `pytesseract.image_to_data`) and parses
  the TSV. This is a small, self-contained addition to the existing engine.
- **A crop-query entry point.** The plan's `query_vlm_crop(image, prompt)` maps
  cleanly onto the existing vision path — `_encode_image` already turns an image
  into the base64 the backend wants; a thin wrapper that takes crop bytes + a
  fixed prompt and calls `_ocr_vision`'s client is most of it.
- **Pre-processing Phase 2** (deskew/binarise) for reliable Tesseract boxes.

## Proposed architecture (matches your five stages)

1. **Baselines.** `get_tesseract_data(image)` → word dicts with coords + conf;
   `get_vlm_markdown(image)` → full-page Markdown (olmOCR via Ollama, or the
   existing vision engine).
2. **Align & diff.** Tokenise the Markdown while recording each word's raw
   `(start, end)` char span. Normalise (strip Markdown, casefold, fold
   punctuation) into a comparison sequence. Anchor on matching runs, then use
   `rapidfuzz` editops within gaps. Flag substitutions and above-threshold
   divergences; **weight by Tesseract confidence** (high Tesseract conf + high
   divergence = strong hallucination signal; low Tesseract conf = distrust the
   referee too).
3. **Crop.** For each flagged, Tesseract-anchored divergence, crop the box from
   the *pre-processed* image with configurable padding (default 15px), clamped to
   image bounds. In-memory (`io.BytesIO`), never to disk.
4. **Arbitrate.** Concurrent (`ThreadPoolExecutor`, bounded) context-free crop
   queries with the strict "transcribe only what is visible; output [ILLEGIBLE]
   if you cannot" prompt.
5. **Reconcile.** Splice arbitrated text back into the raw Markdown by span,
   preserving surrounding syntax; re-validate the Markdown.

## Refinements worth adopting

- **Three outcomes, not two.** agree → trust; disagree-and-arbitrated → record;
  disagree-and-illegible or no-anchor → **surface to the historian** in the UI
  rather than silently choosing. This is the harness move: the tool adjudicates
  what it can and *shows its working* on what it cannot.
- **Provenance per arbitrated token.** Extend the page metadata (the `engine`
  field already records this kind of thing) with a list of arbitrated spans —
  original VLM word, Tesseract read, arbitrated result, confidence — so a methods
  section can cite exactly what was decided and how.
- **Confidence-gated scope.** Only arbitrate the token classes that matter
  (dates, capitalised proper nouns, digits) by default, to control cost; make the
  net widenable.
- **olmOCR caveat.** Confirm olmOCR actually runs under your Ollama setup and
  returns usable Markdown before committing to it; it is a specific model with
  its own prompt/format expectations. The pipeline should stay model-agnostic
  (any vision engine for the full-page pass and the crop pass), consistent with
  the suite's BYOM stance and its lean toward openly-provenanced models.

## Dependencies

Every dependency is a real weight-and-packaging cost in the frozen (PyInstaller)
build, and the dependency-audit gate keeps that honest — so this is grouped by
value, not just listed.

### Worth adding (high value, low cost)

- **`rapidfuzz`** — the alignment/distance workhorse. Small, C-backed, permissive
  licence.
- **`difflib` (stdlib, zero cost)** — use *alongside* rapidfuzz, not instead.
  `SequenceMatcher.get_matching_blocks()` anchors on high-confidence matching
  runs first; rapidfuzz `editops` then diffs only the gaps. This anchoring is the
  single biggest robustness win for the alignment problem, and it costs nothing.
- **`unicodedata` (stdlib, zero cost)** — NFKD normalisation for the comparison
  sequence, so German folding (Müller/Mueller, ß/ss, accents) flags *real*
  divergences instead of umlaut noise.
- **Pillow** — already added (#84). Crop + in-memory `BytesIO` buffer need nothing
  more.

### The one dependency that actually decides quality

- **`opencv-python-headless`** — belongs to **pre-processing Phase 2** (deskew +
  adaptive binarisation), which arbitration *depends on*: Tesseract's word boxes
  are only as good as the image handed to it, and on degraded mimeographs OpenCV
  is what makes deskew/threshold reliable. Use the **`-headless`** variant — it
  drops the GUI/Qt deps that matter for a frozen, server-style app. Cost is real
  (~40–60 MB frozen, occasional PyInstaller hook coaxing), so it is a deliberate
  Phase 2 decision, not a casual add. `scikit-image` is the alternative but drags
  in scipy — a heavier transitive tree; prefer headless OpenCV.

### Situational (add only when the specific need appears)

- **`markdown-it-py`** — only if the VLM emits **tables or nested structure** that
  must be preserved. Its token stream carries source positions, so arbitrated
  words splice into text spans without ever touching a table pipe or emphasis
  marker, and the result can be re-parsed to confirm the Markdown still validates.
  For mostly-paragraph output, regex span-tracking suffices — skip it.
- **`pytesseract`** — optional convenience for the `image_to_data` (TSV + coords)
  step in Phase A. The alternative — parsing `tesseract … tsv` stdout directly —
  keeps binary discovery under `_tesseract.resolve_binary`'s single control and
  avoids pytesseract's own path-hunting; prefer that, but pytesseract is a small,
  reasonable dep if TSV parsing is unwelcome.

### Dev / evaluation only (never shipped)

- **`jiwer`** — CER/WER against a small hand-labelled sample, to *prove* the
  arbitration improves accuracy rather than relocating errors. Keep it a
  test/eval dependency, out of the runtime wheel.

### Actively avoid

- Heavy ML/layout stacks — `layoutparser`, `detectron2`, the `olmocr` toolkit
  itself, `biopython` aligners. All torch-scale or worse; wrong for a local frozen
  app. Since olmOCR runs **through Ollama**, call it via the existing backend, not
  the `olmocr` package.

### Architecture note (not a dependency)

Route the crop queries through the existing `_backend` / model-harness layer, not
a direct `import ollama`. That layer carries the endpoint policy and the
openly-provenanced-model stance, and keeps the crop pass model-agnostic (any
vision engine, not hard-wired to Ollama). No new dependency — just reuse.

**Net:** `rapidfuzz` + stdlib `difflib`/`unicodedata` cover the whole diff/align
core; `opencv-python-headless` is the one weighty add, and it is really a Phase 2
pre-processing decision arbitration rides on; everything else is situational.

## Testing

- Alignment: deterministic unit tests on synthetic Tesseract-stream / Markdown
  pairs covering substitution, insertion, deletion, reflowed lines, and a table —
  the alignment is pure and must be tested hard, independently of any model.
- Crop math: bbox + padding clamping at every image edge; in-memory bytes are a
  valid image.
- Reconciliation: span-splice never breaks Markdown structure (round-trip parse).
- Arbitration and the VLM crop query: mocked — no live model in CI.
- Cost guard: assert concurrency is bounded.

## Phasing

- **Phase A** — `get_tesseract_data` (TSV + coords) and `query_vlm_crop`; the two
  primitives, fully tested. Small, and independently useful.
- **Phase B** — alignment + diff + flagging (the hard, pure, testable core).
- **Phase C** — crop + concurrent arbitration + reconciliation.
- **Phase D** — UI: opt-in toggle, the "surfaced for human review" list, and
  per-token provenance. Gate the whole feature behind pre-processing Phase 2 for
  reliable boxes.

## Open questions for the maintainer

1. Default token scope — arbitrate everything, or only high-entropy classes
   (dates/proper nouns/digits) to start?
2. When the arbiter returns `[ILLEGIBLE]` or there is no Tesseract anchor: keep
   the VLM's original word (flagged), blank it, or force human review?
3. Is olmOCR confirmed working under your Ollama, or should the full-page pass
   stay on the existing vision engine for now?
