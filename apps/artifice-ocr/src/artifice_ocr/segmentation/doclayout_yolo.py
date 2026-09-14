# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DocLayout-YOLO segmentation provider (Commit 4).

``DocLayoutYoloProvider`` runs DocLayout-YOLO's DocStructBench checkpoint
(``juliozhao/DocLayout-YOLO-DocStructBench``) over a page and maps each
detected layout region into the neutral
:class:`~artifice_ocr.segmentation.model.Region` contract.

The optional package (``doclayout-yolo``) and the Hugging Face weights are both
imported / resolved lazily inside ``segment``/``is_available``, so importing
this module — and the registry that merely lists it — never forces either
(the plan's compatibility invariant #3). ``huggingface_hub`` is already a base
dependency (used by ``_backend.py``); it is imported where used for weight
retrieval, and adds no new dependency on its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..pagexml.geometry import restore_original_coordinates
from .model import Region, SegmentationProvider

# The DocStructBench class vocabulary carried by DocLayout-YOLO's published
# checkpoint. Sourced from PDF-Extract-Kit's
# ``pdf_extract_kit/tasks/layout_detection/models/yolo.py`` ``id_to_names``
# map, which is the canonical 10-class list for this checkpoint.
#
# Every class maps to ``"unclassified"`` on purpose: ``ARTIFICE_REGION_TYPES``
# is an *annotation* vocabulary (marginalia, interlinear, underline, ...), not
# a document-layout vocabulary — it has no text/title/figure/table category,
# so DocStructBench's layout classes do not overlap it at all. Per the plan's
# own instruction ("Unknown labels become ``unclassified``") the correct,
# intended mapping is uniformly ``"unclassified"``. The dict is written out
# explicitly (not collapsed to a bare fallback) so a future commit that
# extends ``ARTIFICE_REGION_TYPES`` has exactly one place to edit.
_DOCLAYOUT_YOLO_LABEL_MAP: Final[Mapping[str, str]] = {
    "title": "unclassified",
    "plain text": "unclassified",
    "abandon": "unclassified",
    "figure": "unclassified",
    "figure_caption": "unclassified",
    "table": "unclassified",
    "table_caption": "unclassified",
    "table_footnote": "unclassified",
    "isolate_formula": "unclassified",
    "formula_caption": "unclassified",
}

_DEFAULT_REPO_ID = "juliozhao/DocLayout-YOLO-DocStructBench"
_DEFAULT_FILENAME = "doclayout_yolo_docstructbench_imgsz1024.pt"
_DEFAULT_IMGSZ = 1024
_DEFAULT_CONFIDENCE = 0.2
_DEFAULT_DEVICE = "cpu"


@dataclass(frozen=True)
class Detection:
    """One raw DocLayout-YOLO detection, in model-input space.

    ``box`` is ``(x1, y1, x2, y2)``; ``class_id`` indexes ``names``. This
    shape is the stable seam the mapping function consumes, so tests can feed
    synthetic DocLayout-YOLO output without touching the model or its weights.
    """

    box: tuple[float, float, float, float]
    class_id: int
    confidence: float


def detections_to_regions(
    detections: Sequence[Detection],
    *,
    names: Mapping[int, str],
    model_size: tuple[int, int],
    original_size: tuple[int, int],
) -> list[Region]:
    """Map raw model-space detections into neutral regions in original space.

    Each detection becomes one :class:`Region` whose polygon is its axis-aligned
    box, restored from ``model_size`` to ``original_size`` pixel space through
    :func:`~artifice_ocr.pagexml.geometry.restore_original_coordinates`
    (integer, clamped — compatibility invariant #6). ``order`` is left unset so
    the pipeline's centralised :func:`~artifice_ocr.pagexml.convert.order_regions`
    applies its stable top-to-bottom/left-to-right fallback; DocLayout-YOLO's
    raw detection order is arbitrary and not a meaningful reading order.

    A class whose name is absent from ``names`` — or present but absent from
    the label map — normalises to ``"unclassified"``.
    """
    regions: list[Region] = []
    for index, det in enumerate(detections, start=1):
        x1, y1, x2, y2 = det.box
        label = names.get(int(det.class_id), "")
        artifact_type = _DOCLAYOUT_YOLO_LABEL_MAP.get(label, "unclassified")
        polygon = restore_original_coordinates(
            [(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
            model_size=model_size,
            original_size=original_size,
        )
        regions.append(
            Region(
                id=f"doclayout-{index:04d}",
                type=artifact_type,
                polygon=polygon,
                confidence=float(det.confidence),
                order=None,
            )
        )
    return regions


class DocLayoutYoloProvider(SegmentationProvider):
    name = "doclayout-yolo"
    requirements = (
        "DocLayout-YOLO is an optional dependency. Install it with "
        'pip install "artifice-ocr[segmentation-doclayout-yolo]".'
    )

    def __init__(self) -> None:
        # Set during ``segment``: the resolved weights artifact identity
        # (``repo_id@revision`` or ``file:<path>``) surfaced into PAGE
        # metadata by the caller.
        self.resolved_weights: str | None = None
        self.weights_path: Path | None = None

    def is_available(self) -> bool:
        """True only if ``doclayout_yolo`` imports; never raises."""
        try:
            import doclayout_yolo  # noqa: F401
        except Exception:
            return False
        return True

    def segment(self, image_path: Path, options: dict[str, Any]) -> list[Region]:
        """Run DocLayout-YOLO and map detections to neutral regions.

        Provider options (all optional, sane defaults when absent):

        - ``device`` (str) — default ``"cpu"``; a GPU device is only ever used
          when explicitly requested (the safe default).
        - ``confidence_threshold`` (float) — default ``0.2``.
        - ``imgsz`` (int) — the model's square input size, default ``1024``.
        - ``model`` (str) — Hugging Face repo id or a local ``.pt`` path,
          default ``juliozhao/DocLayout-YOLO-DocStructBench``.
        - ``revision`` (str | None) — explicit HF revision, default the repo's
          main branch.
        - ``filename`` (str) — HF filename within ``model``, default the
          DocStructBench checkpoint name.
        """
        from PIL import Image

        with Image.open(image_path) as img:
            width, height = img.size

        from doclayout_yolo import YOLOv10

        weights_path, _ = self._resolve_weights(options)

        device = str(options.get("device", _DEFAULT_DEVICE))
        confidence = float(options.get("confidence_threshold", _DEFAULT_CONFIDENCE))
        imgsz = int(options.get("imgsz", _DEFAULT_IMGSZ))

        model = YOLOv10(str(weights_path))
        results = model.predict(
            str(image_path),
            imgsz=imgsz,
            conf=confidence,
            device=device,
        )
        result = results[0]
        names = _names_from(model, result)

        boxes = result.boxes
        detections = [
            Detection(
                box=_box_tuple(box),
                class_id=int(cls),
                confidence=float(conf),
            )
            for box, cls, conf in zip(boxes.xyxy, boxes.cls, boxes.conf, strict=True)
        ]

        # DocLayout-YOLO (an ultralytics fork) scales its boxes back to the
        # original image space during postprocess (``scale_boxes``), so the
        # boxes are already original-space floats. Passing a unit
        # ``model_size == original_size`` makes restore_original_coordinates
        # round and clamp them to integer original-space pixels (invariant #6)
        # without a second scaling.
        return detections_to_regions(
            detections,
            names=names,
            model_size=(width, height),
            original_size=(width, height),
        )

    def _resolve_weights(self, options: dict[str, Any]) -> tuple[Path, str]:
        """Resolve the checkpoint to a local path and an identity string.

        A local file path (``model`` pointing at an existing file) is used as
        is. Anything else is treated as a Hugging Face repo id and fetched
        through ``huggingface_hub.hf_hub_download``; the identity records the
        *resolved* revision (the repo's commit sha) rather than whatever the
        caller typed, so PAGE metadata names the exact artifact that ran.
        """
        model = str(options.get("model", _DEFAULT_REPO_ID))
        model_path = Path(model)
        if model_path.is_file():
            self.weights_path = model_path.resolve()
            self.resolved_weights = f"file:{self.weights_path}"
            return self.weights_path, self.resolved_weights

        filename = str(options.get("filename", _DEFAULT_FILENAME))
        revision = options.get("revision")

        from huggingface_hub import hf_hub_download, model_info

        local = Path(hf_hub_download(repo_id=model, filename=filename, revision=revision))
        try:
            resolved = model_info(model, revision=revision).sha
        except Exception:
            # Best-effort: an offline/unauthorised metadata lookup must not
            # fail the whole run after the weights already downloaded.
            resolved = revision or "main"

        self.weights_path = local
        self.resolved_weights = f"{model}@{resolved}"
        return local, self.resolved_weights


def _box_tuple(box: Any) -> tuple[float, float, float, float]:
    """Coerce a four-value box row (tensor/ndarray/list) to a float 4-tuple."""
    x1, y1, x2, y2 = box
    return (float(x1), float(y1), float(x2), float(y2))


def _names_from(model: Any, result: Any) -> Mapping[int, str]:
    """Return the class-index → name mapping from a loaded model/results.

    Both the model and the results carry ``names``; prefer the model's, which
    is available as soon as the checkpoint loads, and fall back to the
    result's (or an empty map, which the label map turns into
    ``"unclassified"``). Tolerates either a ``{index: name}`` dict or an
    ordered ``[name, ...]`` list, since the ultralytics fork has surfaced both.
    """
    for source in (model, result):
        names = getattr(source, "names", None)
        if not names:
            continue
        if isinstance(names, Mapping):
            return {int(index): str(name) for index, name in names.items()}
        return {index: str(name) for index, name in enumerate(names)}
    return {}
