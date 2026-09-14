# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Diff-residual segmentation provider (Commit 6).

``DiffResidualProvider`` detects hand-added annotations — underlines and
marginalia — by aligning a *reference image* (a clean scan of the same page,
with no annotations) against the input page, subtracting the two, and
extracting the visual difference as connected components.

It is a real image-processing algorithm, not a pretrained-model wrapper: the
alignment is phase correlation (fast, translation-only) or ORB/SIFT feature
matching plus a RANSAC homography, the residual is an absolute per-channel
difference thresholded and morphologically cleaned, and the components are
classified with deliberately conservative geometry into the two annotation
categories ``ARTIFICE_REGION_TYPES`` already carries (``underline`` and
``marginalia``); anything ambiguous falls back to ``"unclassified"``.

The optional package (``cv2``, i.e. ``opencv-python-headless``) is imported
lazily inside ``segment``/``is_available``/the alignment helpers, so importing
this module — and the registry that merely lists it — never forces OpenCV at
startup (the plan's compatibility invariant #3). ``numpy`` is a *base*
dependency of ``artifice-ocr`` (not optional) and is imported at the top.

Coordinate convention: the reference is warped *into the input image's own
pixel space* and the residual mask is computed at the input's native
resolution, so every connected component is already an integer bounding box in
original-image space. No resize of the input ever happens, so there is nothing
to restore through :func:`~artifice_ocr.pagexml.geometry.restore_original_coordinates`;
the pipeline's own unit-scale ``_clamp`` applies the shared rounding/clamping
convention downstream, exactly as it does for the passthrough provider.

Alignment honesty: alignment is the load-bearing step and can legitimately
fail. Rather than emit plausible-looking but untrustworthy regions, the
provider raises :class:`DiffResidualError` with an actionable message whenever
a reliable transform cannot be established (insufficient feature matches, a
failed RANSAC consensus, a phase-correlation response below threshold, or a
missing/unreadable reference). That is the "fail loudly, not wrongly" contract
the plan requires; a page whose reference is a featureless blank (or the wrong
image) will error, not silently return a garbage diff.
"""

from __future__ import annotations

import math
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from .model import Region, SegmentationProvider

# --------------------------------------------------------------------------- #
# Options and defaults
# --------------------------------------------------------------------------- #

_DEFAULT_ALIGNMENT_METHOD = "auto"
_ALIGNMENT_METHODS: Final[frozenset[str]] = frozenset({"auto", "phase_correlation", "orb", "sift"})
_DEFAULT_MIN_COMPONENT_AREA = 30
_DEFAULT_COLOUR_CHANNEL = "gray"
_DEFAULT_THRESHOLD = 30
_DEFAULT_DEBUG = False

# ``colour_channel`` accepts a name (gray/grey or an OpenCV BGR name) or a raw
# BGR index 0/1/2. Names normalise to "gray" or a BGR index key.
_CHANNEL_INDEX: Final[dict[str, int]] = {"blue": 0, "green": 1, "red": 2}
_CHANNEL_ALIASES: Final[dict[str, str]] = {
    "gray": "gray",
    "grey": "gray",
    "blue": "blue",
    "green": "green",
    "red": "red",
    "0": "blue",
    "1": "green",
    "2": "red",
}

# --------------------------------------------------------------------------- #
# Alignment thresholds (documented, deliberately conservative)
# --------------------------------------------------------------------------- #

# ``cv2.phaseCorrelate`` returns a normalised peak ``response`` in [0, 1];
# ~1.0 means a clean translation, ~0 means no coherent translation at all.
# Below this the "fast path" is rejected and the auto method falls back to
# feature matching. Phase correlation is only *attempted* when the two images
# already share a size (pure translation), so there is no resize to confound it.
_PHASE_RESPONSE_THRESHOLD = 0.15

# Minimum number of ratio-test-surviving matches required before a homography
# is even attempted; below this the page is treated as lacking the structure to
# align at all. Also the minimum number of RANSAC inliers the homography must
# retain after estimation.
_MIN_GOOD_MATCHES = 10

# Lowe's ratio test threshold for the kNN matcher.
_LOWE_RATIO = 0.75

# --------------------------------------------------------------------------- #
# Residual and classification thresholds (documented, deliberately conservative)
# --------------------------------------------------------------------------- #

# Dark pixels (ink) in the aligned reference, used for the "under existing text"
# and "text block" geometry. A reference scan is dark-on-light.
_INK_THRESHOLD = 128

# A component is "underline" only when ALL of these hold, plus reference ink
# directly above it (see _UNDERLINE_INK_BAND_FACTOR):
_UNDERLINE_MIN_HEIGHT = 2  # at least a real 2px stroke, not a 1px speck
_UNDERLINE_MAX_HEIGHT = 12  # "thin"
_UNDERLINE_MIN_ASPECT = 5.0  # width / height — "wide"
_UNDERLINE_MAX_DEVIATION = 20.0  # long-axis angle within 20° of horizontal

# How far above the component (as a multiple of its own height) to look for
# reference ink when deciding "positioned under existing text".
_UNDERLINE_INK_BAND_FACTOR = 2.0

# Mean residual intensity within a component below which it is treated as an
# *alignment halo* rather than a real difference and dropped. A hand-added mark
# is fully absent from the reference, so its residual pixels are ~255; a
# sub-pixel misregistration halo is only a partial, anti-aliased overlap and
# reads tens of levels (measured 40-100 in the ORB non-square-transform case).
# This is the single most important guard against the ORB path emitting a halo
# around every text glyph as a region.
_SOLID_DIFF_THRESHOLD = 150.0

# Morphological cleaning kernel (open then close, a 3×3 rectangle): the open
# removes single-pixel noise spewed by small alignment residuals, the close
# bridges the thin gaps a threshold can leave inside one annotation stroke.
_MORPH_KERNEL = (3, 3)


class DiffResidualError(RuntimeError):
    """A diff-residual run cannot produce trustworthy regions.

    Raised for a missing/unreadable reference, an unsupported option value, or
    an alignment that could not be established reliably. This is deliberate:
    the provider fails loudly rather than returning a garbage diff.
    """


@dataclass(frozen=True)
class Component:
    """One connected component of the residual mask, in original-image space.

    ``bbox`` is ``(x, y, w, h)`` from OpenCV's ``connectedComponentsWithStats``.
    ``centroid`` is the rounded component centroid. ``deviation_deg`` is the
    angle of the component's long axis from horizontal, in ``[0, 90]`` (computed
    with ``cv2.fitLine``); 0 means horizontal, 90 means vertical.
    ``has_ink_above`` is True when the aligned reference has ink directly above
    the component. ``mean_intensity`` is the mean residual intensity inside the
    component (a solid annotation reads ~255; an alignment halo reads tens of
    levels). This shape is the stable seam the mapping function consumes, so
    tests can feed synthetic components without importing OpenCV.
    """

    bbox: tuple[int, int, int, int]
    centroid: tuple[int, int]
    deviation_deg: float
    has_ink_above: bool
    mean_intensity: float = 255.0


def classify_component(
    *,
    width: int,
    height: int,
    deviation_deg: float,
    centroid: tuple[int, int],
    has_ink_above: bool,
    text_bbox: tuple[int, int, int, int] | None,
) -> str:
    """Classify one component into an Artifice region type, conservatively.

    ``underline`` requires a thin, wide, roughly-horizontal stroke sitting under
    existing reference text (ink directly above it). ``marginalia`` requires the
    component's centroid to fall outside the reference text block — i.e. in a
    page margin. Everything else is ``"unclassified"``. The checks are ordered
    and all-or-nothing on purpose: an ambiguous shape falls through rather than
    being force-fit into one of the two real categories.
    """
    if (
        _UNDERLINE_MIN_HEIGHT <= height <= _UNDERLINE_MAX_HEIGHT
        and width / height >= _UNDERLINE_MIN_ASPECT
        and deviation_deg <= _UNDERLINE_MAX_DEVIATION
        and has_ink_above
    ):
        return "underline"

    if text_bbox is not None:
        tx0, ty0, tx1, ty1 = text_bbox
        cx, cy = centroid
        if cx < tx0 or cx > tx1 or cy < ty0 or cy > ty1:
            return "marginalia"

    return "unclassified"


def components_to_regions(
    components: Sequence[Component],
    *,
    text_bbox: tuple[int, int, int, int] | None,
) -> list[Region]:
    """Map residual components to neutral regions in original-image space.

    Each component becomes one :class:`Region` whose polygon is its
    axis-aligned bounding box (already integer original-space pixels — no
    resize ever happens). ``order`` is left unset so the pipeline's centralised
    reading-order fallback applies; ``confidence`` is not meaningful here.
    """
    regions: list[Region] = []
    for index, comp in enumerate(components, start=1):
        x, y, w, h = comp.bbox
        artifice_type = classify_component(
            width=w,
            height=h,
            deviation_deg=comp.deviation_deg,
            centroid=comp.centroid,
            has_ink_above=comp.has_ink_above,
            text_bbox=text_bbox,
        )
        regions.append(
            Region(
                id=f"diffres-{index:04d}",
                type=artifice_type,
                polygon=[(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                order=None,
            )
        )
    return regions


class DiffResidualProvider(SegmentationProvider):
    name = "diff-residual"
    requirements = (
        "OpenCV (opencv-python-headless) is an optional dependency. Install it "
        'with pip install "artifice-ocr[segmentation-diff-residual]".'
    )

    def __init__(self) -> None:
        # Set during ``segment``: the alignment method actually used and the
        # resolved OpenCV version, surfaced into PAGE metadata by the caller
        # through the same ``resolved_weights`` hook every provider uses.
        self.resolved_weights: str | None = None
        self.alignment_method_used: str | None = None
        self.opencv_version: str | None = None
        # Set during ``segment`` in debug mode: the fresh temp directory the
        # three debug artifacts were written into. Never either source image's
        # own directory.
        self.debug_dir: Path | None = None

    def is_available(self) -> bool:
        """True only if the ``cv2`` package imports; never raises."""
        try:
            import cv2  # noqa: F401
        except Exception:
            return False
        return True

    def segment(self, image_path: Path, options: dict[str, Any]) -> list[Region]:
        """Align a reference scan, extract the residual, return its components.

        Provider options:

        - ``reference_image`` (str, **required**) — path to the clean reference
          scan of the same page. Missing/unreadable → :class:`DiffResidualError`.
        - ``alignment_method`` (str) — ``"auto"`` (default),
          ``"phase_correlation"``, ``"orb"``, or ``"sift"``. ``"auto"`` tries
          phase correlation first (same size + strong response) and falls back
          to ORB; ``"sift"`` is only used when explicitly selected *and*
          present in the installed OpenCV build.
        - ``min_component_area`` (int) — default ``30``; components smaller than
          this many pixels are dropped as noise.
        - ``colour_channel`` (str) — default ``"gray"``; also ``"blue"``/
          ``"green"``/``"red"`` or a raw BGR index ``0``/``1``/``2``. The
          channel is extracted from both images before subtraction, so a faint
          coloured annotation can be targeted with the channel it differs in.
        - ``threshold`` (int) — default ``30``; the residual intensity above
          which a pixel counts as a difference.
        - ``debug`` (bool) — default ``False``; write aligned reference,
          residual mask, and overlay into a fresh ``ocr_diffres_`` temp dir
          (recorded on ``self.debug_dir``). Never alters either source image.

        Never modifies ``image_path`` or the reference image.
        """
        import cv2

        self.opencv_version = cv2.__version__
        self.debug_dir = None
        self.alignment_method_used = None

        reference = self._resolve_reference(options)
        method = self._normalise_method(options)
        min_area = int(options.get("min_component_area", _DEFAULT_MIN_COMPONENT_AREA))
        channel = _normalise_channel(options.get("colour_channel", _DEFAULT_COLOUR_CHANNEL))
        threshold = int(options.get("threshold", _DEFAULT_THRESHOLD))
        debug = bool(options.get("debug", _DEFAULT_DEBUG))

        input_img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if input_img is None:
            raise DiffResidualError(f"Input image could not be read: {image_path}")
        ref_img = cv2.imread(str(reference), cv2.IMREAD_COLOR)
        if ref_img is None:
            raise DiffResidualError(
                f"Reference image could not be decoded as an image: {reference}"
            )

        aligned, method_used = self._align(ref_img, input_img, method)
        self.alignment_method_used = method_used
        self.resolved_weights = f"alignment:{method_used};opencv:{self.opencv_version}"

        input_ch = _channel(input_img, channel)
        ref_ch = _channel(aligned, channel)
        residual = cv2.absdiff(input_ch, ref_ch)
        _, mask = cv2.threshold(residual, threshold, 255, cv2.THRESH_BINARY)
        mask = _clean_mask(mask)

        # Reference ink (in input space, from the aligned reference) drives the
        # "under existing text" and "text block" geometry.
        ref_ink = _channel(aligned, "gray") <= _INK_THRESHOLD
        text_bbox = _text_bbox(ref_ink)

        components = _extract_components(mask, ref_ink, residual, min_area)

        if debug:
            self.debug_dir = _write_debug_artifacts(input_img, aligned, mask, components)

        return components_to_regions(components, text_bbox=text_bbox)

    # -- reference and option validation ----------------------------------- #

    def _resolve_reference(self, options: dict[str, Any]) -> Path:
        """Validate ``reference_image`` and return its resolved path.

        The option must be present, point at an existing file, and decode as an
        image. Each failure raises :class:`DiffResidualError` with an actionable
        message rather than leaking a bare ``FileNotFoundError``/``OSError``.
        """
        reference = options.get("reference_image")
        if not reference:
            raise DiffResidualError(
                "The diff-residual provider requires 'reference_image': the path "
                "to a clean reference scan of the same page (no annotations)."
            )
        path = Path(str(reference)).expanduser()
        if not path.is_file():
            raise DiffResidualError(f"Reference image not found: {path}")

        from PIL import Image

        try:
            with Image.open(path) as img:
                img.verify()
        except Exception as exc:
            raise DiffResidualError(
                f"Reference image could not be read as an image: {path} ({exc})"
            ) from exc
        return path.resolve()

    def _normalise_method(self, options: dict[str, Any]) -> str:
        method = str(options.get("alignment_method", _DEFAULT_ALIGNMENT_METHOD)).lower()
        if method not in _ALIGNMENT_METHODS:
            raise DiffResidualError(
                f"Invalid alignment_method {method!r}; expected one of {sorted(_ALIGNMENT_METHODS)}"
            )
        return method

    # -- alignment --------------------------------------------------------- #

    def _align(self, ref_img: Any, input_img: Any, method: str) -> tuple[Any, str]:
        """Align ``ref_img`` into ``input_img`` space; return ``(aligned, used)``.

        ``"auto"`` tries the phase-correlation fast path first and falls back to
        ORB when it is not applicable (different sizes) or not reliable (weak
        response). Explicit methods never fall back: a request that cannot be
        satisfied raises rather than silently switching algorithm.
        """
        if method == "auto":
            if ref_img.shape[:2] == input_img.shape[:2]:
                try:
                    return self._phase_correlate(ref_img, input_img), "phase_correlation"
                except DiffResidualError:
                    pass
            return self._feature_align(ref_img, input_img, "orb"), "orb"
        if method == "phase_correlation":
            return self._phase_correlate(ref_img, input_img), "phase_correlation"
        if method == "orb":
            return self._feature_align(ref_img, input_img, "orb"), "orb"
        return self._feature_align(ref_img, input_img, "sift"), "sift"

    def _phase_correlate(self, ref_img: Any, input_img: Any) -> Any:
        """Translation-only alignment via ``cv2.phaseCorrelate``.

        Requires equal sizes; computes the sub-pixel shift of ``input_img``
        relative to ``ref_img`` and warps ``ref_img`` by it. Raises
        :class:`DiffResidualError` when sizes differ or the correlation peak is
        too weak to trust (auto mode then falls back to feature matching).
        """
        import cv2

        if ref_img.shape[:2] != input_img.shape[:2]:
            raise DiffResidualError(
                "phase_correlation requires the reference and input to share "
                f"dimensions (got {ref_img.shape[:2]} vs {input_img.shape[:2]}); "
                "use alignment_method='orb' or 'sift' for a resized reference."
            )
        ref_gray = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        in_gray = cv2.cvtColor(input_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        (dx, dy), response = cv2.phaseCorrelate(ref_gray, in_gray)
        if response < _PHASE_RESPONSE_THRESHOLD:
            raise DiffResidualError(
                f"Phase-correlation response {response:.3f} is below the "
                f"reliability threshold {_PHASE_RESPONSE_THRESHOLD}; the images "
                "do not look like pure translations of one another."
            )
        height, width = input_img.shape[:2]
        matrix = np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float32)
        # White border: the exposed edge of a translated document scan is paper,
        # not black. The default (0) would leave a dark frame that reads as a
        # full-page residual.
        return cv2.warpAffine(ref_img, matrix, (width, height), borderValue=(255, 255, 255))

    def _feature_align(self, ref_img: Any, input_img: Any, detector_name: str) -> Any:
        """Feature-match + RANSAC homography alignment (ORB or SIFT)."""
        import cv2

        detector = self._make_detector(detector_name)
        keypoints_ref, desc_ref = detector.detectAndCompute(ref_img, None)
        keypoints_in, desc_in = detector.detectAndCompute(input_img, None)
        if desc_ref is None or desc_in is None or len(keypoints_ref) < 2 or len(keypoints_in) < 2:
            raise DiffResidualError(
                f"{detector_name.upper()} found too few features to align "
                f"({len(keypoints_ref)} in the reference, {len(keypoints_in)} in "
                "the input); the page lacks the texture a reliable alignment needs."
            )

        norm = cv2.NORM_L2 if detector_name == "sift" else cv2.NORM_HAMMING
        matcher = cv2.BFMatcher(norm)
        raw = matcher.knnMatch(desc_ref, desc_in, k=2)
        good = [
            m
            for pair in raw
            if len(pair) == 2
            for m, n in (pair,)
            if m.distance < _LOWE_RATIO * n.distance
        ]
        if len(good) < _MIN_GOOD_MATCHES:
            raise DiffResidualError(
                f"Insufficient reliable feature matches ({len(good)} < "
                f"{_MIN_GOOD_MATCHES}); the reference and input do not appear to "
                "be the same page, or the page has too little texture to align."
            )

        src = np.array([keypoints_ref[m.queryIdx].pt for m in good], dtype=np.float32).reshape(
            -1, 1, 2
        )
        dst = np.array([keypoints_in[m.trainIdx].pt for m in good], dtype=np.float32).reshape(
            -1, 1, 2
        )
        homography, inliers = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        if homography is None or inliers is None or int(inliers.sum()) < _MIN_GOOD_MATCHES:
            raise DiffResidualError(
                "RANSAC could not establish a homography with enough inliers; "
                "the reference and input do not appear to be the same page."
            )
        height, width = input_img.shape[:2]
        # White border, for the same reason as the phase-correlation warp: an
        # exposed edge of a document scan is paper, not black.
        return cv2.warpPerspective(
            ref_img, homography, (width, height), borderValue=(255, 255, 255)
        )

    def _make_detector(self, name: str) -> Any:
        """Instantiate the feature detector; SIFT is checked, not assumed."""
        import cv2

        if name == "sift":
            if not hasattr(cv2, "SIFT_create"):
                raise DiffResidualError(
                    "SIFT is not available in this OpenCV build; install "
                    "opencv-python-headless (>=4.4) or use 'orb'."
                )
            try:
                return cv2.SIFT_create()
            except Exception as exc:
                raise DiffResidualError(f"SIFT could not be initialised: {exc}") from exc
        # A dense feature budget matters for homography accuracy: ORB's default
        # 500 corners on a text page yields a rough H whose residual halos swamp
        # the real annotations. 8000 keeps the registration tight enough that
        # the solidity filter can separate marks from halos.
        return cv2.ORB_create(nfeatures=8000)


# --------------------------------------------------------------------------- #
# Residual → components (image-space helpers; cv2/numpy already available)
# --------------------------------------------------------------------------- #


def _normalise_channel(value: Any) -> str:
    key = str(value).strip().lower()
    if key not in _CHANNEL_ALIASES:
        raise DiffResidualError(
            f"Invalid colour_channel {value!r}; expected 'gray', 'blue', 'green', "
            "'red', or a BGR index 0/1/2."
        )
    return _CHANNEL_ALIASES[key]


def _channel(bgr: Any, channel: str) -> Any:
    """Return the selected single-channel view of a BGR image."""
    import cv2

    if channel == "gray":
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return bgr[:, :, _CHANNEL_INDEX[channel]]


def _clean_mask(mask: Any) -> Any:
    """Open then close the threshold mask with a small 3×3 kernel.

    Open removes single-pixel noise (small alignment residuals); close bridges
    thin gaps inside one annotation stroke.
    """
    import cv2

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, _MORPH_KERNEL)
    opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel)


def _extract_components(mask: Any, ref_ink: Any, residual: Any, min_area: int) -> list[Component]:
    """Extract connected components of ``mask`` at/above ``min_area``.

    ``ref_ink`` is the aligned reference's ink boolean array, used to decide
    whether each component sits under existing text. ``residual`` is the raw
    (pre-threshold) difference image; its mean inside each component separates
    solid hand-added marks from sub-pixel alignment halos — a component whose
    mean residual is below ``_SOLID_DIFF_THRESHOLD`` is dropped.
    """
    import cv2

    num, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    components: list[Component] = []
    for label in range(1, num):
        x, y, w, h, area = stats[label]
        if int(area) < min_area:
            continue
        cx, cy = centroids[label]
        mean_intensity = float(residual[labels == label].mean())
        if mean_intensity < _SOLID_DIFF_THRESHOLD:
            continue  # an alignment halo, not a real difference
        points = np.argwhere(labels == label)  # (row, col) pairs
        components.append(
            Component(
                bbox=(int(x), int(y), int(w), int(h)),
                centroid=(round(float(cx)), round(float(cy))),
                deviation_deg=_deviation_deg(points),
                has_ink_above=_has_ink_above(ref_ink, int(x), int(y), int(w), int(h)),
                mean_intensity=mean_intensity,
            )
        )
    return components


def _deviation_deg(points: Any) -> float:
    """Long-axis angle from horizontal via ``cv2.fitLine``, in ``[0, 90]``."""
    import cv2

    if len(points) < 2:
        return 0.0
    pts = np.column_stack((points[:, 1], points[:, 0])).astype(np.float32).reshape(-1, 1, 2)
    vx, vy, _x0, _y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
    angle = abs(math.degrees(math.atan2(float(vy), float(vx)))) % 180.0
    return min(angle, 180.0 - angle)


def _has_ink_above(ref_ink: Any, x: int, y: int, w: int, h: int) -> bool:
    """True if the aligned reference has ink in a band directly above the bbox."""
    band = int(round(_UNDERLINE_INK_BAND_FACTOR * h))
    top = max(0, y - band)
    if top >= y:  # component starts at the very top of the page
        return False
    right = min(ref_ink.shape[1], x + w)
    return bool(np.any(ref_ink[top:y, x:right]))


def _text_bbox(ref_ink: Any) -> tuple[int, int, int, int] | None:
    """Inclusive bounding box of reference ink; ``None`` for a blank reference."""
    rows = np.any(ref_ink, axis=1)
    cols = np.any(ref_ink, axis=0)
    if not rows.any() or not cols.any():
        return None
    r = np.where(rows)[0]
    c = np.where(cols)[0]
    return (int(c[0]), int(r[0]), int(c[-1]), int(r[-1]))


def _write_debug_artifacts(
    input_img: Any, aligned: Any, mask: Any, components: Sequence[Component]
) -> Path:
    """Write aligned reference, residual mask, and overlay into a fresh temp dir.

    The overlay draws each component's bounding box on a copy of the *input*
    image. Both source images are left untouched (the debug dir is a newly
    created ``ocr_diffres_`` temp directory).
    """
    import cv2

    debug_dir = Path(tempfile.mkdtemp(prefix="ocr_diffres_"))
    cv2.imwrite(str(debug_dir / "aligned_reference.png"), aligned)
    cv2.imwrite(str(debug_dir / "residual_mask.png"), mask)
    overlay = input_img.copy()
    for comp in components:
        x, y, w, h = comp.bbox
        cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 255, 0), 2)
    cv2.imwrite(str(debug_dir / "overlay.png"), overlay)
    return debug_dir
