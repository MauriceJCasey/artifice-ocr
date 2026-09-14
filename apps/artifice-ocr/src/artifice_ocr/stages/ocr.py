# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import base64
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from artifice_ocr import _guard
from artifice_ocr import _rotation
from artifice_ocr import _tesseract
from artifice_ocr._backend import get_client as _get_backend_client
from artifice_ocr._blank import is_near_blank
from artifice_ocr._logging import get_logger
from artifice_ocr._resolution import backend_for, model_for
from artifice_ocr._retry import retry
from artifice_ocr.config import get as cfg
from artifice_ocr.output import record_dir, write_stage_output
from artifice_ocr.stages import preprocess as _preprocess

log = get_logger("ocr")

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}

OCR_PROMPT = (
    "OCR: Extract all visible text from this document image. "
    "Return only the raw text exactly as it appears. "
    "Do not add commentary, labels, or formatting."
)

# Experimental only — see docs/superpowers/plans/2026-09-09-olmocr2-optimisation.md's
# discussion of §3 (the original findings document this cites was never
# checked into version control; that plan is the closest surviving in-repo
# reference). olmOCR-2 was SFT'd/RLVR'd toward YAML-front-matter +
# Markdown-body output; this addendum asks for that shape instead of the
# raw-text default. Gated by ocr_prompt_style, default "raw". DO NOT flip
# the default without measured
# results from scripts/measure_ocr_accuracy.py AND explicit maintainer
# sign-off — switching stage 1's output shape changes the contract with
# cleanup/structure/pdf_export downstream, which nothing here has verified.
_STRUCTURED_PROMPT_ADDENDUM = (
    "Return your transcription as YAML front matter (between --- lines) "
    "describing the page, followed by the transcribed content as Markdown, "
    "preserving tables, headers, and reading order."
)

_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

_PDF_ZOOM_DPI = 200  # 72 DPI is PDF's native unit; this scales to 200 DPI for OCR legibility
_PDF_PAGE_FILENAME_TEMPLATE = "page_{page:04d}.png"
_PAGE_BREAK = "\n\n--- Page Break ---\n\n"


def _effective_prompt(instruction: str, *, style: str = "raw") -> str:
    """OCR_PROMPT, plus an optional appended domain instruction, plus an
    experimental structured-output addendum when ``style="structured"``.

    Appended, never a replacement: the base prompt's "return only raw text,
    no commentary/labels/formatting" contract must survive regardless of
    ``instruction``, since downstream stages (cleanup, structure) depend on
    it. ``style="structured"`` is an intentional, explicit exception to that
    contract for the purpose of measuring it — see _STRUCTURED_PROMPT_ADDENDUM.
    """
    parts = [OCR_PROMPT]
    if style == "structured":
        parts.append(_STRUCTURED_PROMPT_ADDENDUM)
    instruction = (instruction or "").strip()
    if instruction:
        parts.append(instruction)
    return "\n\n".join(parts)


def _exif_orientation_matrix(orientation: int, width: float, height: float):
    """Matrix that corrects a Tropy `photos.orientation` value for a
    page/image of the given (pre-transform) size. Tropy uses the same 1-8
    convention as the EXIF Orientation tag — 1 is normal, 3 is a 180°
    rotation, and so on. Returns None for 1 or an unrecognised value: better
    to OCR the image as scanned than guess wrong about a value this project
    has never seen.

    Confirmed necessary on a real archive page (Tropy orientation 1/normal,
    i.e. nobody had flagged it) that was actually scanned upside down: fed
    to the model as-is, it hallucinated plausible-sounding filler instead of
    transcribing, then looped on it — see `_guard.check_no_repetition_loop`.
    Each of the 8 matrices below was checked against Pillow's reference
    `Image.transpose()` output before shipping.
    """
    import fitz  # PyMuPDF

    if orientation == 1:
        return None
    w, h = width, height
    matrices = {
        2: fitz.Matrix(-1, 0, 0, 1, w, 0),  # mirrored horizontal
        3: fitz.Matrix(1, 1).prerotate(180),  # rotated 180°
        4: fitz.Matrix(1, 0, 0, -1, 0, h),  # mirrored vertical
        5: fitz.Matrix(0, 1, 1, 0, 0, 0),  # mirrored + rotated 270°
        6: fitz.Matrix(1, 1).prerotate(90),  # rotated 90° CW
        7: fitz.Matrix(0, -1, -1, 0, h, w),  # mirrored + rotated 90° CW
        8: fitz.Matrix(1, 1).prerotate(270),  # rotated 270° CW
    }
    return matrices.get(orientation)


def _configured_max_image_edge() -> int | None:
    """The longest-edge pixel cap for the vision request, or ``None`` for none.

    ``None`` rather than ``0`` for "do not resize", for the same reason
    :func:`artifice_ocr._backend._configured_context_size` returns ``None``:
    ``0`` reads well in the UI but is a valid-looking pixel count, and two
    call sites writing ``if cap:`` and ``if cap is not None:`` would disagree.

    A malformed or non-positive setting reads as ``None`` rather than raising —
    a bad value must not fail a page, it must fall back to the behaviour from
    before the setting existed.
    """
    try:
        value = int(cfg("ocr_max_image_edge", 1288) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _resize_to_max_edge(data: bytes, max_edge: int) -> tuple[bytes, str] | None:
    """Downscale *data* so its longest edge is *max_edge*, as PNG bytes.

    Returns ``None`` when no change is warranted — the image already fits, or
    it could not be decoded. ``None`` is the signal to keep the original bytes
    and mime untouched, so a decode failure degrades to the previous behaviour
    rather than failing the page (the same contract
    :func:`artifice_ocr.stages.preprocess.maybe_process` uses).

    Only ever shrinks. Upscaling a small page invents detail the scan does not
    contain and costs visual tokens for nothing.
    """
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            longest = max(img.size)
            if longest <= max_edge:
                return None
            scale = max_edge / longest
            size = (
                max(1, round(img.width * scale)),
                max(1, round(img.height * scale)),
            )
            resized = img.resize(size, Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG")
            log.debug(
                "Capped page image %sx%s -> %sx%s for the vision request",
                img.width,
                img.height,
                size[0],
                size[1],
            )
            return buf.getvalue(), "image/png"
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not resize page image, sending it unchanged: %s", exc)
        return None


def _encode_image(
    path: Path, orientation: int = 1, *, max_edge: int | None = None
) -> tuple[str, str]:
    """Returns (base64, mime). Applies an orientation correction (see
    `_exif_orientation_matrix`) when `orientation` isn't 1 — the corrected
    bytes are always a fresh PNG render regardless of the source format.

    Rendered at the source's own native resolution, not fitz's default
    ~72dpi page-point size: a pure rotation/mirror matrix with no
    accompanying scale factor was measured losing ~5.5x resolution on a
    real 3458x5067 scan (rendered at 623x913 instead) — confirmed to make
    the model report the page as unreadable where the uncorrected,
    full-resolution image did not. The scale factor is uniform (x and y),
    so it commutes with any rotation angle and composition order here
    doesn't matter, unlike a non-uniform scale would.
    """
    data: bytes | None = None
    mime: str | None = None

    if orientation != 1:
        import fitz  # PyMuPDF

        doc = fitz.open(str(path))
        try:
            page = doc[0]
            orient_mat = _exif_orientation_matrix(orientation, page.rect.width, page.rect.height)
            if orient_mat is not None:
                native = fitz.Pixmap(str(path))
                zoom = native.width / page.rect.width if page.rect.width else 1.0
                mat = fitz.Matrix(zoom, zoom) * orient_mat
                pix = page.get_pixmap(matrix=mat)
                data, mime = pix.tobytes("png"), "image/png"
        finally:
            doc.close()

    if data is None:
        with open(path, "rb") as f:
            data = f.read()
        mime = _MIME_MAP.get(path.suffix.lower(), "image/png")

    # Optional deterministic pre-processing (greyscale / contrast / illumination
    # / gamma) to rescue bright, washed-out or low-contrast pages before the
    # model sees them. Off by default; returns None when disabled or on any
    # decode failure, in which case the original bytes and mime are used
    # unchanged. See stages/preprocess.py.
    processed = _preprocess.maybe_process(data)
    if processed is not None:
        data, mime = processed, "image/png"

    # Longest-edge cap for the vision model, applied last so it bounds whatever
    # the orientation correction and pre-processing produced. ``max_edge`` is
    # None on the Tesseract path, which must keep full resolution — see
    # _tesseract_from_image and the ocr_max_image_edge comment in config.py.
    if max_edge is not None:
        capped = _resize_to_max_edge(data, max_edge)
        if capped is not None:
            data, mime = capped

    return base64.standard_b64encode(data).decode("utf-8"), mime


@retry(max_attempts=4, base_delay=2.0, label="OCR")
def _ocr_vision(image_path: Path, orientation: int = 1) -> str:
    """Send a single image to the vision-model OCR backend and return text.

    Every backend is called with the same OpenAI-shaped message: images as
    ``image_url`` content blocks. LM Studio, Hugging Face and the generic
    API-key backend take that shape natively. Ollama does not — but unlike
    the OpenAI-compatible cloud backends, its ``/v1`` endpoint silently
    ignores the context-window request (``extra_body.options.num_ctx``;
    measured on live Ollama 0.33.2 — see the ``OllamaOpenAIBackend``
    docstring in ``_backend.py``), so an ``ollama`` backend is routed to the
    *native* ``ollama`` client instead of ``ollama_openai``.
    :func:`artifice_ocr._backend._to_native_messages` converts the OpenAI
    content blocks this function builds into Ollama's native
    ``{"content": str, "images": [base64, ...]}`` shape before the request
    is sent.

    Do not remap ``ollama`` to ``ollama_openai`` here again: that remap is
    exactly what made "Context size" a no-op for OCR, because Ollama does
    not honour ``num_ctx`` on the OpenAI-compatible endpoint.
    """
    image_b64, mime = _encode_image(image_path, orientation, max_edge=_configured_max_image_edge())
    backend = backend_for("vision")
    model = model_for("vision")
    client = _get_backend_client(backend)

    def _call(temperature: float) -> str:
        response = client.chat(
            model=model,
            messages=[
                {
                    "role": "user",
                    # Order is load-bearing: olmOCR-2 was trained with text
                    # before image (arXiv:2510.19817 s4, "Better prompting") —
                    # reversing it is a training/inference mismatch that
                    # measurably hurt benchmark performance upstream. Do not
                    # "clean up" this ordering in a refactor.
                    "content": [
                        {
                            "type": "text",
                            "text": _effective_prompt(
                                cfg("ocr_prompt_instruction", ""),
                                style=cfg("ocr_prompt_style", "raw"),
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                        },
                    ],
                }
            ],
            temperature=temperature,
        )
        return response.message.content or ""

    if not cfg("ocr_temperature_ladder_enabled"):
        return _call(0.0)

    start = float(cfg("ocr_temperature_ladder_start", 0.1) or 0.1)
    step = float(cfg("ocr_temperature_ladder_step", 0.1) or 0.1)
    ceiling = float(cfg("ocr_temperature_ladder_max", 0.8) or 0.8)
    guard_on = bool(cfg("ocr_repetition_guard"))

    # A malformed negative step would move the temperature AWAY from the
    # ceiling on every rung, so the loop condition below never became false
    # — a genuine infinite loop (``or`` above already excludes 0, but not
    # negatives; NaN fails ``> 0`` too, so it is caught here as well).
    # Clamp to a positive floor so the temperature always rises, and cap the
    # rung count so no configuration — however malformed, including an
    # ``inf`` ceiling — can loop unboundedly. Sane values are untouched:
    # 0.1..0.8 at step 0.1 is still exactly 8 rungs.
    step = max(step, 0.01) if step > 0 else 0.01
    rungs_left = 50 if start <= ceiling else 0

    text = ""
    temperature = start
    last_guard = None
    # Ladder: sample at rising temperatures; a page that never trips the
    # repetition guard returns on the first rung, matching the pre-ladder
    # cost exactly. Rejection has no source text to keep — see
    # _guard.check_no_repetition_loop's docstring — so each rung fully
    # replaces the previous rung's output.
    while temperature <= ceiling + 1e-9 and rungs_left > 0:
        text = _call(temperature)
        if not guard_on:
            return text
        last_guard = _guard.check_no_repetition_loop(text)
        if last_guard.ok:
            return text
        temperature += step
        rungs_left -= 1

    reasons = "; ".join(last_guard.reasons) if last_guard else "unknown"
    raise RuntimeError(
        f"OCR repetition guard rejected every temperature rung up to {ceiling}: {reasons}"
    )


def _tesseract_from_image(image_path: Path, orientation: int = 1) -> str:
    """Run Tesseract on an image, reusing ``_encode_image`` so the bytes carry
    the same orientation correction and (when enabled) deterministic
    pre-processing the vision path would apply.

    Deliberately passes no ``max_edge``: the 1288px cap is a Qwen2.5-VL
    constraint, not a general one. Tesseract benefits from *more* resolution,
    so capping here would degrade the very fallback that exists to rescue a
    page the vision model could not read.
    """
    image_b64, _mime = _encode_image(image_path, orientation)
    return _tesseract.ocr_bytes(base64.standard_b64decode(image_b64))


def _ocr_single_image(image_path: Path, orientation: int = 1) -> tuple[str, str]:
    """OCR one image and return ``(text, engine)``.

    ``engine`` records provenance for the page metadata:
      - ``"tesseract"`` when Tesseract is the selected primary engine,
      - the vision backend name (e.g. ``"ollama"``) on the normal path,
      - ``"tesseract-fallback"`` when the vision path failed and the
        ``tesseract_fallback_on_failure`` safety net recovered the page.

    A historian citing a transcription needs to know which engine read each
    page, so this is deliberately explicit rather than assumed.
    """
    if cfg("ocr_blank_page_skip"):
        raw_bytes = image_path.read_bytes()
        if is_near_blank(raw_bytes):
            log.info("Skipping OCR for near-blank page %s", getattr(image_path, "name", image_path))
            return "", "blank-skip"

    if cfg("ocr_engine", "vision_model") == "tesseract":
        return _tesseract_from_image(image_path, orientation), "tesseract"

    try:
        return _ocr_vision(image_path, orientation), backend_for("vision")
    except Exception as exc:
        if cfg("tesseract_fallback_on_failure") and _tesseract.is_available():
            log.warning(
                "Vision OCR failed for %s (%s) — falling back to Tesseract",
                getattr(image_path, "name", image_path),
                exc,
            )
            text = _tesseract_from_image(image_path, orientation)
            # Defence in depth: the fallback must never replace the vision
            # error with an error of its own. If Tesseract yields nothing
            # usable, the original — actionable — failure is what the user
            # needs to see.
            if (text or "").strip():
                return text, "tesseract-fallback"
            log.warning("Tesseract fallback produced no text; re-raising vision failure")
        raise


def _summarise_engines(engines: list[str]) -> str:
    """Collapse per-page engine tags into one readable provenance label,
    preserving order and de-duplicating (e.g. ``"ollama+tesseract-fallback"``)."""
    if not engines:
        return backend_for("vision")
    return "+".join(dict.fromkeys(engines))


def _ocr_document_via_tesseract(
    path: Path, *, page: int | None, is_pdf: bool, orientation: int
) -> str:
    """Re-OCR a whole document with Tesseract by re-rendering its page image(s).

    Used by the ``tesseract_fallback_on_failure`` safety net when the vision
    model produced a degenerate result the repetition guard rejected — the page
    images from the first pass are already gone, so they are rendered again.
    """
    if is_pdf and page is not None:
        img_path, _total = _pdf_single_page_image(path, page, orientation)
        try:
            return _tesseract_from_image(img_path, orientation)
        finally:
            img_path.unlink(missing_ok=True)
    if is_pdf:
        texts = []
        for img_path in _pdf_to_page_images(path, orientation):
            try:
                texts.append(_tesseract_from_image(img_path, orientation))
            finally:
                img_path.unlink(missing_ok=True)
        return _PAGE_BREAK.join(texts)
    return _tesseract_from_image(path, orientation)


def _pdf_to_page_images(pdf_path: Path, orientation: int = 1) -> list[Path]:
    """Render each PDF page as a temporary PNG. Returns list of paths."""
    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf_path))
    page_images = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_pdf_"))
    zoom = fitz.Matrix(_PDF_ZOOM_DPI / 72, _PDF_ZOOM_DPI / 72)

    for page_num in range(len(doc)):
        page = doc[page_num]
        orient_mat = _exif_orientation_matrix(orientation, page.rect.width, page.rect.height)
        mat = orient_mat * zoom if orient_mat is not None else zoom
        pix = page.get_pixmap(matrix=mat)
        img_path = tmp_dir / _PDF_PAGE_FILENAME_TEMPLATE.format(page=page_num + 1)
        pix.save(str(img_path))
        page_images.append(img_path)

    doc.close()
    return page_images


def _pdf_single_page_image(
    pdf_path: Path, page_index: int, orientation: int = 1
) -> tuple[Path, int]:
    """Render one page of a PDF. Returns (temp PNG path, total page count).

    Used when a caller addresses pages individually — Tropy stores one row per
    page, so rendering all 275 pages to OCR one of them would be absurd.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf_path))
    total = len(doc)
    try:
        if not 0 <= page_index < total:
            raise ValueError(
                f"Page {page_index + 1} out of range for {pdf_path.name} ({total} page(s))"
            )
        page = doc[page_index]
        zoom = fitz.Matrix(_PDF_ZOOM_DPI / 72, _PDF_ZOOM_DPI / 72)
        orient_mat = _exif_orientation_matrix(orientation, page.rect.width, page.rect.height)
        mat = orient_mat * zoom if orient_mat is not None else zoom
        pix = page.get_pixmap(matrix=mat)
        tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_pdf_"))
        img_path = tmp_dir / _PDF_PAGE_FILENAME_TEMPLATE.format(page=page_index + 1)
        pix.save(str(img_path))
    finally:
        doc.close()
    return img_path, total


def image_dimensions(
    path: str | Path,
    *,
    page: int | None = None,
    orientation: int = 1,
) -> tuple[int, int]:
    """Return the pixel ``(width, height)`` of the image handed to the OCR
    backend for *path* — the original-image space a PAGE document's implicit
    region must cover.

    Mirrors, without re-running OCR, the exact render geometry of the input
    path through this stage:

    - plain image, ``orientation == 1``: the source image's own pixel size
      (``PIL.Image.open(path).size``);
    - plain image, ``orientation != 1``: the rotated/mirrored render produced
      by :func:`_encode_image` at native resolution (the same orientation
      matrix and native-scale zoom);
    - PDF page (``page`` selects one): the 200-DPI render produced by
      :func:`_pdf_single_page_image` / :func:`_pdf_to_page_images`.

    Dimensions are pre-``ocr_max_image_edge`` resize — the region describes the
    *original image space*, not the downscaled vision request. The probe and
    the real OCR call share the same matrix math, so they cannot disagree.
    """
    p = Path(path)
    is_pdf = p.suffix.lower() == ".pdf"

    if not is_pdf and orientation == 1:
        from PIL import Image

        with Image.open(p) as img:
            return img.size

    import fitz  # PyMuPDF

    doc = fitz.open(str(p))
    try:
        if is_pdf:
            index = page if page is not None else 0
            if not 0 <= index < len(doc):
                raise ValueError(f"Page {index + 1} out of range for {p.name} ({len(doc)} page(s))")
            page_obj = doc[index]
            zoom = fitz.Matrix(_PDF_ZOOM_DPI / 72, _PDF_ZOOM_DPI / 72)
            orient_mat = _exif_orientation_matrix(
                orientation, page_obj.rect.width, page_obj.rect.height
            )
            mat = orient_mat * zoom if orient_mat is not None else zoom
        else:
            page_obj = doc[0]
            orient_mat = _exif_orientation_matrix(
                orientation, page_obj.rect.width, page_obj.rect.height
            )
            if orient_mat is None:
                # An unrecognised orientation falls back to the raw bytes in
                # _encode_image — the native, unrotated image size.
                from PIL import Image

                with Image.open(p) as img:
                    return img.size
            native = fitz.Pixmap(str(p))
            zoom = native.width / page_obj.rect.width if page_obj.rect.width else 1.0
            mat = fitz.Matrix(zoom, zoom) * orient_mat

        pix = page_obj.get_pixmap(matrix=mat)
        return pix.width, pix.height
    finally:
        doc.close()


def _source_identity_fields(source: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the checksum / photo id off a JobItem source dict, dropping
    anything absent or falsy — a sidecar with no identity fields at all
    (every file OCR'd before this existed) is the legacy shape resume falls
    back to, so this must never write an empty placeholder."""
    fields: dict[str, Any] = {}
    if not source:
        return fields
    checksum = source.get("checksum")
    if checksum:
        fields["checksum"] = checksum
    photo_id = source.get("photo_id")
    if photo_id is not None:
        fields["photo_id"] = photo_id
    return fields


def perform(
    input_path: str,
    *,
    output_dir: str = "output",
    page: int | None = None,
    stem: str | None = None,
    orientation: int = 1,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OCR a document.

    `page` selects a single 0-based page of a PDF instead of the whole file.
    `stem` overrides the output filename, and may contain a relative
    subdirectory (``"Item Title/file_p0002"``) to group results.
    `orientation` is Tropy's `photos.orientation` value (see
    `_exif_orientation_matrix`); 1 (the default) means no correction.
    `source` is the JobItem's own source dict (checksum / photo id, when the
    item came from Tropy) — when present, its identity fields are recorded in
    the raw_ocr sidecar JSON so a future resume can tell two photos that
    collide on the same output stem apart instead of silently reusing one
    photo's text for another. Absent (a plain, non-Tropy file), no identity
    fields are written at all — the sidecar looks exactly as it always has.
    """
    path = Path(input_path).resolve()
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {path.suffix}. Supported: {SUPPORTED_EXTENSIONS}")

    log.info("Starting OCR for %s", path.name)

    is_pdf = path.suffix.lower() == ".pdf"

    # The OSD probe is an image-bytes check (it writes whatever it is given
    # to a .png-suffixed temp file for Tesseract) — raw PDF bytes are not
    # image bytes, and a PDF's pages are rendered to proper images below, so
    # there is nothing valid to probe here.
    if orientation == 1 and not is_pdf and cfg("ocr_auto_rotation_detect"):
        try:
            detected = _rotation.detect_orientation(path.read_bytes())
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Rotation auto-detection failed for %s: %s", path.name, exc)
            detected = None
        if detected is not None:
            log.info("Auto-detected rotation for %s: orientation %d", path.name, detected)
            orientation = detected

    model = model_for("vision")
    page_number = 1
    # Provenance: which engine(s) actually read the page(s). Collected across
    # pages so a per-page Tesseract fallback is not lost in a whole-document
    # label. Written to the "engine" field below.
    engines_used: list[str] = []

    if is_pdf and page is not None:
        img_path, num_pages = _pdf_single_page_image(path, page, orientation)
        log.info("OCR page %d/%d of %s", page + 1, num_pages, path.name)
        try:
            extracted_text, engine = _ocr_single_image(img_path)
            engines_used.append(engine)
        finally:
            img_path.unlink(missing_ok=True)
        page_number = page + 1
    elif is_pdf:
        page_images = _pdf_to_page_images(path, orientation)
        page_texts = []
        for i, img_path in enumerate(page_images):
            log.info("OCR page %d/%d of %s", i + 1, len(page_images), path.name)
            text, engine = _ocr_single_image(img_path)
            page_texts.append(text)
            engines_used.append(engine)
            img_path.unlink(missing_ok=True)

        extracted_text = _PAGE_BREAK.join(page_texts)
        num_pages = len(page_texts)
    else:
        extracted_text, engine = _ocr_single_image(path, orientation)
        engines_used.append(engine)
        num_pages = 1

    engine_used = _summarise_engines(engines_used)

    # The model has no fallback text to revert to here (unlike cleanup/
    # structure, which can keep the source on rejection) — a degenerate OCR
    # result has nothing safe to substitute, so a guard failure fails the
    # item outright rather than writing it to raw_ocr/ looking like a
    # success. See _guard.check_no_repetition_loop's docstring for the real
    # failure this catches.
    guard_result = None
    if cfg("ocr_repetition_guard"):
        guard_result = _guard.check_no_repetition_loop(extracted_text)
        if not guard_result.ok:
            # Safety net: a degenerate vision result the guard rejected (e.g. a
            # repetition loop) can often be recovered by re-reading the page(s)
            # with Tesseract. Only when the fallback is enabled, Tesseract is
            # available, and the primary engine was not already Tesseract (which
            # would just repeat the same read). The recovered text is re-checked
            # by the same guard so a fallback cannot itself smuggle in a loop.
            recovered_ok = False
            if (
                cfg("tesseract_fallback_on_failure")
                and "tesseract" not in engine_used
                and _tesseract.is_available()
            ):
                log.warning(
                    "Vision OCR for %s was rejected by the repetition guard — "
                    "falling back to Tesseract",
                    path.name,
                )
                recovered = _ocr_document_via_tesseract(
                    path, page=page, is_pdf=is_pdf, orientation=orientation
                )
                recheck = _guard.check_no_repetition_loop(recovered)
                if (recovered or "").strip() and recheck.ok:
                    extracted_text = recovered
                    engine_used = "tesseract-fallback"
                    guard_result = recheck
                    recovered_ok = True

            if not recovered_ok:
                base_output_dir = Path(output_dir)
                json_dir = record_dir(base_output_dir, "raw_ocr")
                json_dir.mkdir(parents=True, exist_ok=True)
                base_name = stem or path.stem
                json_path = json_dir / f"{base_name}.json"
                json_path.parent.mkdir(parents=True, exist_ok=True)
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "source_file": str(path),
                            "stage": "raw_ocr",
                            "rejected_extracted_text": extracted_text,
                            "engine": engine_used,
                            "model": model,
                            "ocr_prompt": _effective_prompt(
                                cfg("ocr_prompt_instruction", ""),
                                style=cfg("ocr_prompt_style", "raw"),
                            ),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "page": page_number,
                            "total_pages": num_pages,
                            "guard": guard_result.to_dict(),
                        },
                        f,
                        indent=2,
                    )
                raise RuntimeError(
                    f"OCR rejected for {path.name}: {'; '.join(guard_result.reasons)}"
                )

    base_name = stem or path.stem

    data = {
        "source_file": str(path),
        "stage": "raw_ocr",
        "extracted_text": extracted_text,
        "engine": engine_used,
        "model": model,
        "ocr_prompt": _effective_prompt(
            cfg("ocr_prompt_instruction", ""), style=cfg("ocr_prompt_style", "raw")
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "page": page_number,
        "total_pages": num_pages,
    }
    if guard_result is not None:
        data["guard"] = guard_result.to_dict()
    data.update(_source_identity_fields(source))

    write_stage_output(output_dir, "raw_ocr", base_name, text_content=extracted_text, metadata=data)

    log.info("OCR complete for %s (%d chars, %d pages)", path.name, len(extracted_text), num_pages)
    return data
