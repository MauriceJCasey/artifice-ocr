# Deterministic image pre-processing for OCR — plan

**Status:** Proposal. Not started. Work for a later session.
**Date:** 2026-08-26
**Owner:** maintainer, to brief `lead-engineer` when the fleet has credit.

> Verify every premise here against the code before acting — file/line
> references drift. The hook points cited were true at
> `src/artifice_ocr/stages/ocr.py` on 2026-08-26.

## The symptom

On **very bright / washed-out photographs** of typescript (over-exposed phone
photos, pale paper, low ink contrast) the OCR stage returns little or nothing.
The example that prompted this: a bright, evenly-lit photo of a carbon-copy
typescript where a human reads the text easily but the model returns a fraction
of it.

## Why it happens here specifically

The OCR "engine" in this app is a **vision LLM** (`model_for("vision")`,
routed through the OpenAI-compatible `image_url` path —
`stages/ocr.py:120-138`), **not** Tesseract or another classical OCR engine.
That changes which pre-processing actually helps:

- **Contrast / exposure is the dominant lever for this symptom.** A vision
  model reads faint grey-on-white far worse than it reads black-on-white.
  Normalising illumination and stretching contrast is the fix most likely to
  move the needle on bright images.
- **Deskew matters less than it would for Tesseract.** Vision models tolerate
  moderate rotation. Deskew is still worth having — it helps line-level
  fidelity and downstream structuring — but for the *bright-image* failure it
  is secondary. (The maintainer's instinct to deskew is sound; contrast is
  just the bigger win for this particular symptom.)
- **Binarisation is a double-edged tool.** Classic adaptive thresholding
  (Sauvola/Otsu) can rescue a washed page, but it can also destroy a faint
  page if tuned wrong. It should be opt-in and off by default.

## Where it hooks in

Every OCR path renders or loads a page to a PNG, then calls
`_ocr_single_image(img_path)`. There are three call sites in
`stages/ocr.py`:

| Branch | Lines (2026-08-26) | Image source |
|---|---|---|
| Single PDF page (Tropy) | `perform()` ~221 | `_pdf_single_page_image` → temp PNG |
| All PDF pages | `perform()` ~230 | `_pdf_to_page_images` → temp PNGs |
| Non-PDF image | `perform()` ~239 | the source file directly |

A single new function — `preprocess(image_path) -> Path` — inserted immediately
before each `_ocr_single_image(...)` call covers all three. It takes an image
path, applies the enabled deterministic steps, writes a new temp PNG, and
returns its path (or returns the input unchanged when every step is disabled).
Because it is pure and deterministic, it is trivially unit-testable against
fixture images — unlike the model call it feeds.

**Do not preprocess the file the user pointed at in place.** The non-PDF branch
passes the *source* path straight to `_ocr_single_image`; preprocessing must
write to a temp file and never touch the original, then `unlink` it like the
PDF branches already do.

## The packaging decision that gates everything

**This app currently has no image-processing library.** `pyproject.toml`
dependencies are PyMuPDF, reportlab, the model SDKs — **no Pillow, no numpy, no
OpenCV, no scikit-image.** PyMuPDF renders straight to PNG via `pix.save()`.

So step one is a deliberate dependency choice, and it is a real cost in a
**frozen (PyInstaller) build**, which is the install pattern the maintainer
ships:

| Option | Adds | Can do | Frozen-build weight |
|---|---|---|---|
| **PyMuPDF-only** | nothing | crude: greyscale, invert, gamma via `Pixmap`; no deskew, no adaptive threshold | none |
| **Pillow + numpy** | ~2 deps | auto-contrast, histogram stretch, gamma, simple global threshold, rotate-by-known-angle | modest (~15–25 MB) |
| **OpenCV (headless) + numpy** | `opencv-python-headless` | CLAHE, adaptive threshold, Hough/`minAreaRect` deskew, illumination flattening, denoise | **large (~40–60 MB)** and a frequent PyInstaller hook headache |
| **scikit-image + numpy** | scipy chain | Sauvola/Niblack threshold, deskew, rescale | large, heavy transitive tree |

**Recommendation to decide tomorrow:** start with **Pillow + numpy**. It
delivers the contrast/exposure fixes that address the actual symptom, keeps the
frozen build lean, and avoids OpenCV's packaging friction. Reach for OpenCV
only if a measured before/after on real bright-image fixtures shows Pillow's
global operations are not enough and adaptive thresholding / deskew are needed.
**Measure before adding the heavy dependency**, not after.

## Proposed techniques, ranked for the bright-image symptom

1. **Auto-contrast / histogram stretch** (Pillow `ImageOps.autocontrast`) —
   highest expected value, cheap, safe. Rescales the tonal range so faint text
   darkens toward black.
2. **Illumination normalisation** (subtract a blurred background estimate) —
   flattens uneven lighting so a bright corner does not wash out a whole
   region. Doable in numpy without OpenCV.
3. **Gamma / exposure correction** — pull mid-tones down on over-exposed
   scans.
4. **Greyscale** before the above — removes colour noise; vision models do not
   need colour for typescript.
5. **Deskew** (needs OpenCV or scikit-image for angle detection) — Phase 2,
   gated on the packaging decision.
6. **Adaptive binarisation** (Sauvola) — Phase 2, opt-in, off by default,
   because it can wreck a faint page.

Steps 1–4 are the Pillow+numpy Phase 1. Steps 5–6 are the Phase 2 that only
happens if measurement justifies a heavier dependency.

## Config and UI

- Config keys under the existing `config` module, defaulting **off** so no
  existing run changes behaviour: `preprocess_enabled` (master toggle),
  then per-step booleans / small numeric params.
- Surface a single **"Enhance faint/bright scans"** toggle in the OCR settings
  UI (Settings tab), consistent with how `context_size` was added — one honest
  control, not six sliders. Advanced params can stay config-only initially.
- The toggle must read as a harness control, not a chat affordance
  (Design_Philosophy.md).

## Testing

- Pure-function unit tests on `preprocess()` against a handful of committed
  fixture images (one bright/washed, one already-good, one skewed): assert the
  output is darker/higher-contrast by a measurable histogram metric, and that a
  disabled config returns the input path unchanged.
- A guard test that the source file is never modified in place.
- **Do not assert on OCR *accuracy* in CI** — that needs a live model. Keep the
  deterministic transform and the model call testable separately; this is the
  whole reason to make preprocessing a pure function.
- Frozen-build check: if a new dependency is added, extend the wheel/onedir
  inspection so the image library and its data files actually ship. Tests run
  against `src/` and cannot see a packaging miss (see the suite's standing note
  on test-invisible packaging bugs).

## Phasing

- **Phase 1 (Pillow + numpy):** greyscale → auto-contrast → illumination
  normalise → gamma, one UI toggle, fixture tests. Measure on the maintainer's
  real bright-image samples.
- **Phase 2 (only if measured necessary):** deskew + adaptive threshold, which
  brings OpenCV/scikit-image and its frozen-build cost. Record the measurement
  that justified it before adding the dependency.
