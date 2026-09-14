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


class SkipRequest(BaseModel):
    id: str


class RawTextRequest(BaseModel):
    text: str


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
