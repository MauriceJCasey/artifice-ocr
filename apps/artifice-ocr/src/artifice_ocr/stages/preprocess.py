# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic image pre-processing for the OCR stage (Phase 1).

Pure, model-free image clean-up applied to a page image *before* it is sent to
the vision model. Aimed at the failure mode where a bright / washed-out / low-
contrast photograph of typescript reads fine to a human but returns little from
the model: greyscale, even out uneven lighting, stretch contrast, optional
gamma.

Everything here is deterministic and testable without a model — which is the
whole reason it is a separate stage from the OCR call it feeds. It is **off by
default** (``preprocess_enabled``); with every step disabled it is a no-op and
the original image bytes are used unchanged.

Phase 2 (deskew, adaptive binarisation) is deliberately *not* here — it needs a
heavier dependency (OpenCV / scikit-image) and a measurement to justify it. See
``docs/OCR_PREPROCESSING_PLAN.md``.
"""

from __future__ import annotations

import io

import numpy as np
from artifice_ocr._logging import get_logger
from artifice_ocr.config import get as cfg
from PIL import Image, ImageFilter, ImageOps

log = get_logger("preprocess")


def is_enabled() -> bool:
    """Master toggle. When False, ``maybe_process`` is a no-op."""
    return bool(cfg("preprocess_enabled"))


def _normalise_illumination(img: Image.Image) -> Image.Image:
    """Flatten uneven lighting by dividing out a blurred background estimate.

    A bright corner or a gradient across the page washes out text unevenly.
    Estimating the background with a heavy Gaussian blur and dividing the image
    by it removes the low-frequency lighting while keeping the high-frequency
    strokes. Radius scales with the image so it behaves the same on a phone
    photo and a flatbed scan.
    """
    radius = max(3, (max(img.size) // 20))
    background = img.filter(ImageFilter.GaussianBlur(radius=radius))

    arr = np.asarray(img, dtype=np.float32)
    bg = np.asarray(background, dtype=np.float32)
    # Never divide by zero; a background pixel of 0 is black anyway.
    bg = np.clip(bg, 1.0, None)
    normalised = arr / bg
    peak = float(normalised.max())
    if peak <= 0:
        return img
    normalised = np.clip(normalised / peak * 255.0, 0, 255)
    return Image.fromarray(normalised.astype(np.uint8), mode="L")


def _apply_gamma(img: Image.Image, gamma: float) -> Image.Image:
    """Gamma < 1 lightens, gamma > 1 darkens mid-tones. Used to pull an
    over-exposed page back toward readable ink."""
    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = np.power(arr, gamma)
    return Image.fromarray(np.clip(arr * 255.0, 0, 255).astype(np.uint8), mode="L")


def process_pil(img: Image.Image) -> Image.Image:
    """Apply the enabled deterministic steps to a PIL image, returning a new
    single-channel (``L``) image. Order matters: greyscale first, then flatten
    lighting, then stretch contrast, then gamma."""
    # Greyscale is the foundation for the numeric steps below and removes colour
    # noise the model does not need for typescript. Kept behind a flag only so
    # the pipeline is fully configurable; on by default.
    if cfg("preprocess_grayscale", True):
        img = ImageOps.grayscale(img)
    elif img.mode != "L":
        img = img.convert("L")

    if cfg("preprocess_illumination", True):
        img = _normalise_illumination(img)

    if cfg("preprocess_autocontrast", True):
        # cutoff clips the most extreme 1% of the histogram before stretching —
        # a few stray black/white specks otherwise anchor the range and defeat
        # the stretch.
        img = ImageOps.autocontrast(img, cutoff=1)

    gamma = _gamma_value()
    if gamma is not None and gamma != 1.0:
        img = _apply_gamma(img, gamma)

    return img


def _gamma_value() -> float | None:
    try:
        value = float(cfg("preprocess_gamma", 1.0) or 1.0)
    except (TypeError, ValueError):
        return None
    # Reject nonsensical values rather than raising into the OCR run.
    if value <= 0 or value > 10:
        return None
    return value


def process_bytes(data: bytes) -> bytes:
    """Decode image bytes, apply the pipeline, and return PNG bytes."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        out = process_pil(im)
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()


def maybe_process(data: bytes) -> bytes | None:
    """Return processed PNG bytes when pre-processing is enabled, else ``None``.

    ``None`` is the signal to the caller to keep the *original* bytes and mime
    untouched — so a disabled pipeline changes nothing about the request, and a
    decode failure degrades to the raw image rather than failing the page.
    """
    if not is_enabled():
        return None
    try:
        return process_bytes(data)
    except Exception as exc:  # pragma: no cover - defensive
        # Pre-processing must never be the reason a page fails to OCR. If the
        # image cannot be decoded/processed, fall back to the original bytes.
        log.warning("Pre-processing failed, using original image: %s", exc)
        return None
