# OCR accuracy evaluation corpus

Empty by design. `scripts/measure_ocr_accuracy.py` reads pairs from this
directory:

- `<stem>.png` (or `.jpg`/`.jpeg`/`.tif`/`.tiff`) — the scanned page
- `<stem>.txt` — its exact ground-truth transcription
- `<stem>.orientation` — optional, a single integer 1-8 (Tropy/EXIF
  convention; default 1 if absent)

**This corpus cannot be generated automatically.** Per
`docs/superpowers/plans/2026-09-09-olmocr2-optimisation.md`, populate it
with 10-20 pages representative of real use: typescript, handwriting, a
table, a multi-column layout, a blank verso, and a mis-oriented scan, each
with a verified ground-truth transcription. Run:

    uv run python scripts/measure_ocr_accuracy.py eval_corpus/

**Metric caveat:** this harness scores character error rate (CER), which
assumes a fixed linear reading order. [CENT] (arXiv:2608.30616, s5.1) notes
CER "does not hold for scattered annotations" — treat a high CER on a page
with marginalia or non-linear layout with that in mind, not as a flat
"worse" verdict.
