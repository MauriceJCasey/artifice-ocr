# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The subset of the PAGE 2019 model used by Artifice OCR."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

PAGE_NAMESPACE: Final = "http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15"

# PAGE says the lowest TextEquiv index is the primary text. Artifice keeps
# each textual pipeline result instead of overwriting its predecessor.
STAGE_TEXT_EQUIV_INDEX: Final[Mapping[str, int]] = MappingProxyType(
    {"raw": 0, "cleaned": 1, "translated": 2}
)

ARTIFICE_REGION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "marginalia",
        "interlinear",
        "underline",
        "strikethrough",
        "circling",
        "symbol",
        "unclassified",
    }
)

# The 2019 schema happens to include marginalia. The remaining annotation
# types have no precise legal TextRegion/@type and therefore use "other".
_PAGE_TYPE_BY_ARTIFICE_TYPE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "marginalia": "marginalia",
        "interlinear": "other",
        "underline": "other",
        "strikethrough": "other",
        "circling": "other",
        "symbol": "other",
        "unclassified": "other",
    }
)

Point = tuple[int, int]


def page_region_type(region_type: str) -> str:
    """Return a legal PAGE TextRegion type for an Artifice region type."""
    return _PAGE_TYPE_BY_ARTIFICE_TYPE.get(region_type, "other")


def artifice_custom(region_type: str) -> str:
    """Encode Artifice's finer type using Transkribus-compatible syntax."""
    normalised = region_type if region_type in ARTIFICE_REGION_TYPES else "unclassified"
    return f"structure {{type:{normalised};}}"


@dataclass(frozen=True)
class TextEquiv:
    index: int
    unicode: str


@dataclass
class TextLine:
    id: str
    polygon: list[Point]
    baseline: list[Point] | None = None
    text_equivs: list[TextEquiv] = field(default_factory=list)


@dataclass
class TextRegion:
    id: str
    type: str
    custom: str
    polygon: list[Point]
    text_equivs: list[TextEquiv] = field(default_factory=list)
    lines: list[TextLine] = field(default_factory=list)
    confidence: float | None = None

    def stage_text(self, stage: str) -> str | None:
        """Return this region's text for an Artifice pipeline stage."""
        index = STAGE_TEXT_EQUIV_INDEX[stage]
        return next((item.unicode for item in self.text_equivs if item.index == index), None)

    def set_stage_text(self, stage: str, text: str) -> None:
        """Replace one stage index while preserving every other stage."""
        index = STAGE_TEXT_EQUIV_INDEX[stage]
        self.text_equivs = [item for item in self.text_equivs if item.index != index]
        self.text_equivs.append(TextEquiv(index=index, unicode=text))
        self.text_equivs.sort(key=lambda item: item.index)


@dataclass(frozen=True)
class ProcessingStep:
    name: str
    value: str
    date: datetime | None = None


@dataclass
class PageMetadata:
    creator: str = "Artifice"
    created: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_change: datetime = field(default_factory=lambda: datetime.now(UTC))
    processing_steps: list[ProcessingStep] = field(default_factory=list)


@dataclass
class PageDocument:
    image_filename: str
    image_width: int
    image_height: int
    metadata: PageMetadata = field(default_factory=PageMetadata)
    regions: list[TextRegion] = field(default_factory=list)
    reading_order: list[str] = field(default_factory=list)
    reading_order_group_id: str = "reading-order"
    pc_gts_id: str | None = None

    def text(self, stage: str, *, separator: str = "\n\n") -> str:
        """Return region text for a named stage in reading order."""
        _ = STAGE_TEXT_EQUIV_INDEX[stage]  # validate the stage even for an empty page
        by_id = {region.id: region for region in self.regions}
        ordered = [by_id[value] for value in self.reading_order if value in by_id]
        ordered.extend(region for region in self.regions if region.id not in self.reading_order)
        values: list[str] = []
        for region in ordered:
            match = region.stage_text(stage) or ""
            if match:
                values.append(match)
        return separator.join(values)
