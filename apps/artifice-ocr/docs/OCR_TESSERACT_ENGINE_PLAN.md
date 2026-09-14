# Adding Tesseract as an OCR engine option — plan

**Status:** Implemented (detect-installed engine + fallback-on-failure). Built
on the pre-processing branch; see the outcome note below.
**Date:** 2026-08-26
**Owner:** maintainer.

> ## Outcome, 2026-08-26
>
> Built as planned, with one improvement: **no new Python dependency.** Rather
> than `pytesseract`, the engine subprocesses the `tesseract` binary directly
> (`_tesseract.py`), so nothing new ships in the wheel/onedir and the
> dependency audit stays clean. It is driven by image *bytes* from
> `ocr._encode_image`, so it inherits orientation correction and — because this
> landed on the pre-processing branch — the Phase 1 pre-processing for free.
>
> Shipped: `ocr_engine` (vision_model | tesseract), `tesseract_lang`,
> `tesseract_path`, `tesseract_fallback_on_failure`; detection via
> `_tesseract.resolve_binary` (config path → PATH → known Windows locations);
> a `/api/tesseract/status` route and a live "found / not found" line in
> Settings; the fallback for both a per-page vision exception and a
> repetition-guard rejection (re-rendering the page images), with provenance
> recorded as `tesseract-fallback` in the page metadata and a re-check so a
> fallback cannot itself smuggle in a loop. 15 new tests, all mocked — no real
> binary required in CI.

> **Maintainer decisions, 2026-08-26:** build the **detect-installed** path (not
> bundled). Additionally offer Tesseract as an **automatic fallback on a failed
> vision-model page** — see "Tesseract as a fallback" below.

> Verify premises against the code before acting. Written alongside
> `OCR_PREPROCESSING_PLAN.md`, on which this deliberately depends.

## The idea

Offer **Tesseract** as an alternative OCR engine to the current vision-LLM
path — a fast, fully-offline, deterministic transcriber the user can pick per
run. The maintainer already has the Tesseract binary installed, which is
exactly the situation this suits.

## Is it a lot of work? — honest assessment

**The code is modest (roughly a focused day). The distribution and support
burden is the real cost.** Split them out, because they pull in opposite
directions:

- **Code:** small. One engine branch, one detection helper, a config key, a UI
  selector, tests. Tesseract benefits from the same deterministic
  pre-processing Phase 1 adds, so most of the image work is already done.
- **Distribution:** this is where it earns the "extra work" label. `pytesseract`
  is a thin pip wrapper that shells out to the **`tesseract` binary**, which is
  *not* a Python package and cannot be `pip`/`uv`-installed into the frozen
  build. Two ways to handle it, and the choice is the whole decision:

| Approach | What it means | Cost |
|---|---|---|
| **Detect an installed binary** *(recommended)* | Find `tesseract` on `PATH` or a user-set path; if absent, say so and link install instructions | No binary shipped; depends on the user having installed it (the maintainer has). Mirrors how the suite already detects installed apps by their shims (`scripts`, PR #74) |
| **Bundle the binary + tessdata** into the onedir | Ship `tesseract` and language packs inside the PyInstaller build | +30–100 MB per platform; **per-platform binaries** — you cannot build the macOS binary from Windows (the suite already cannot cross-compile the freeze); language-data management; larger attack surface. Licensing is fine (Tesseract is Apache-2.0) but the packaging is not |

**Recommendation: detect-installed, off by default, clearly labelled.** It keeps
the frozen build lean, avoids the cross-compile wall, and fits a user who
already has Tesseract. Bundling is a later option only if "zero-install OCR" is
judged worth the size and per-platform build work.

## Why it is a genuinely different engine, not another backend

The current OCR path always calls a **vision model**:
`_ocr_single_image` → `_encode_image` → a chat call with an `image_url` block
(`stages/ocr.py`). The existing `ocr_backend` choices (ollama / lm_studio /
huggingface / api_key) all select *which model server* answers — they are all
the same "send an image to an LLM" path.

Tesseract is not a model server. It takes an image and returns text with **no
LLM, no network, no prompt**. So it is a new **engine** concept sitting *above*
the backend choice, not another backend value. Model provenance, the OCR
prompt, and the vision-model degeneracy guard (`ocr_repetition_guard`) do not
apply to it; it has its own failure modes (gibberish on a bad binarisation,
empty output, wrong language pack) and its own confidence signal
(`image_to_data` gives per-word confidences).

## Where it hooks in

Introduce an `ocr_engine` config key (`"vision_model"` default, `"tesseract"`
opt-in). Branch at the single image chokepoint:

- `_ocr_single_image(image_path, orientation)` in `stages/ocr.py` is where every
  path already converges. Add, at the top: if `ocr_engine == "tesseract"`, run
  `pytesseract.image_to_string(pil_image, lang=…)` on the **pre-processed**
  image (Phase 1's `_encode_image`/preprocess output) and return — never
  touching the backend/model resolution below.
- Because the branch is at the chokepoint, PDF (all-pages and single-page) and
  standalone-image inputs are all covered at once.

**Depend on Phase 1 deliberately.** Tesseract is far more sensitive to contrast,
skew and binarisation than a vision model. Ship the deterministic pre-processing
first; Tesseract then reuses it, and Phase 2 (deskew + adaptive threshold)
becomes the thing that makes Tesseract actually good rather than merely present.

## Tesseract as a fallback on failed runs

A vision model does not fail cleanly — it can hallucinate filler and loop
(exactly what `ocr_repetition_guard` catches and rejects), or exhaust its
retries, leaving a page with **no** transcription. Tesseract, being
deterministic, is a good safety net for precisely those pages: better a plain
Tesseract read than a blank.

**Design:**

- A separate opt-in toggle, `tesseract_fallback_on_failure` (default off,
  independent of the primary `ocr_engine` choice). It only does anything when
  Tesseract is detected.
- The single failure funnel is already in `perform()`: a page fails when the
  repetition guard rejects it (raises today) or when the vision retries are
  exhausted. Wrap that: on failure, if the fallback is on and Tesseract is
  available, re-run **the same pre-processed image** through Tesseract and, if
  it yields usable text, accept that page instead of failing it.
- **Label the provenance.** The page's JSON metadata must record
  `engine: "tesseract-fallback"` (not the vision model), and the run log should
  say so plainly: "Page 12 failed vision OCR — recovered with Tesseract." A
  historian citing the transcription needs to know which engine actually read
  each page; silently swapping engines would corrupt the methods trail this
  suite exists to keep honest.
- Keep it a *fallback*, not a silent primary: if Tesseract also returns nothing
  usable, the page still fails as it does today, with both failures logged.

**Cost:** small once the engine exists — it reuses the detection helper, the
engine call, and the pre-processed image. The only new surface is the failure
funnel wrapper and the provenance labelling. Build it in the same pass as the
engine, behind its own toggle.

## Config and UI

- `ocr_engine`: `"vision_model"` | `"tesseract"` (default `"vision_model"`).
- `tesseract_lang`: e.g. `"eng"`, `"deu"`, or `"deu+eng"`. The user must have
  the matching `*.traineddata`; for the historical German typescript in the
  test corpus, `"deu"` (plus the Fraktur/`script/Fraktur` model if the source
  is blackletter — call this out in the UI, don't guess).
- `tesseract_path`: optional explicit binary path when it is not on `PATH`.
- `tesseract_fallback_on_failure`: `bool` (default off) — the safety-net toggle
  described above, usable even when the primary engine stays `vision_model`.
- UI: a small **OCR engine** selector in Settings. When "Tesseract" is chosen,
  show a live **detection status** ("Found tesseract 5.x" / "Not found — install
  it or set the path") and the language field. A control that silently no-ops
  when the binary is missing is worse than none — detect and say so, the same
  principle the context-size hint follows.

## Detection helper

A small, testable function: resolve the binary from `tesseract_path` →
`PATH` (`shutil.which`) → known install locations, return version or a clear
"not found". Keep it deterministic and unit-testable by injecting the lookup.
Never shell out to an unvalidated user-supplied path without the existing
path-validation ruleset.

## Testing

- Detection helper: found on PATH, found at explicit path, not found — all
  without a real binary (inject the resolver).
- Engine branch: with `ocr_engine="tesseract"` and `pytesseract` monkeypatched,
  `_ocr_single_image` calls Tesseract and never the backend client; with the
  default it calls the client and never Tesseract.
- Language plumbing: the configured `tesseract_lang` reaches the call.
- **Do not require a real Tesseract binary in CI** — mock it. An optional,
  marker-gated integration test can run against a real binary locally.
- Frozen-build note: if the detect-installed approach is used, nothing new ships
  in the wheel/onedir, so the packaging tests are unaffected — state that
  explicitly so a future audit does not "fix" a missing bundled binary that was
  never meant to be there.

## Effort summary

- **Detect-installed path:** ~1 focused day of code + tests + docs; ongoing
  support cost of a second engine (install help, language data, distinct
  failure modes).
- **Bundled path:** add several days and a per-platform build story; defer
  unless zero-install OCR is a stated goal.

Recommend building the detect-installed version after Phase 1, measuring it on
the maintainer's real bright/German corpus against the vision model, and only
then deciding whether bundling is worth it.
