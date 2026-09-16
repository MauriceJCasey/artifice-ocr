# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pydantic request/response models for all web routes.

Consolidated here so every router file can import them without circular
references or duplicating model definitions across modules.
"""

from pydantic import BaseModel, Field


class AddPathsRequest(BaseModel):
    paths: list[str]


class RemoveRequest(BaseModel):
    ids: list[str]


class StartRunRequest(BaseModel):
    stages: list[str]
    output_dir: str = "output"
    project: str | None = None
    force: bool = False
    segmentation_provider: str | None = None
    # Only meaningful when segmentation_provider == "diff-residual", which
    # needs a clean, unannotated scan of the same page to diff against. Any
    # other provider ignores this field.
    segmentation_reference_image: str | None = None


class SkipRequest(BaseModel):
    id: str


class RawTextRequest(BaseModel):
    text: str


class RegionTextRequest(BaseModel):
    """Retype/edit one region's text at one PAGE stage index.

    ``stage`` is one of ``"raw"`` / ``"cleaned"`` / ``"translated"`` — the keys
    of ``artifice_ocr.pagexml.STAGE_TEXT_EQUIV_INDEX``. A correction updates
    only the selected index; every other stage on that region and every other
    region are left untouched.
    """

    stage: str
    text: str


class RegionReorderRequest(BaseModel):
    """An explicit new reading order, as the page's region ids.

    Must be an exact permutation of the document's region ids — the server
    rejects (400) a list that is missing, adding, or duplicating any id.
    """

    region_ids: list[str]


class FabricatedResultRequest(BaseModel):
    fabricated: bool


class PdfExportRequest(BaseModel):
    folder: str
    stage: str = "cleaned"
    structure: bool = True
    output: str | None = None
    manifest: str | None = None
    format: str = "pdf"
    style: str = "readable"
    bilingual: bool = False


class PageExportRequest(BaseModel):
    output_dir: str
    stems: list[str]
    output: str | None = None


class ReorderRequest(BaseModel):
    drag_id: str
    drop_id: str
    before: bool = True


class ReprocessRequest(BaseModel):
    from_stage: str
    stages: list[str]


class BatchReplaceRequest(BaseModel):
    find: str
    replace: str
    stages: list[str]
    item_ids: list[str] | None = None


class TropyBrowseRequest(BaseModel):
    path: str


class TropyEnqueueRequest(BaseModel):
    path: str
    item_ids: list[int] = Field(default_factory=list)
    photo_ids: list[int] | None = None
    output_dir: str = "output"
