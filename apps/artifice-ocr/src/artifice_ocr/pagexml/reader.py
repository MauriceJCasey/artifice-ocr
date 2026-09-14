# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read PRImA PAGE 2019 XML into the Artifice PAGE model."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from .model import (
    PAGE_NAMESPACE,
    PageDocument,
    PageMetadata,
    Point,
    ProcessingStep,
    TextEquiv,
    TextLine,
    TextRegion,
)

_NS = {"pc": PAGE_NAMESPACE}


class PageXMLParseError(ValueError):
    """The input is XML, but not a supported PAGE 2019 document."""


def _required(element: ET.Element | None, description: str) -> ET.Element:
    if element is None:
        raise PageXMLParseError(f"Missing required PAGE element: {description}")
    return element


def _text(parent: ET.Element, name: str) -> str:
    element = _required(parent.find(f"pc:{name}", _NS), name)
    return element.text or ""


def _timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PageXMLParseError(f"Invalid PAGE timestamp: {value}") from exc


def _points(value: str) -> list[Point]:
    try:
        points: list[Point] = []
        for token in value.split():
            x_value, y_value = token.split(",")
            points.append((int(x_value), int(y_value)))
        return points
    except (TypeError, ValueError) as exc:
        raise PageXMLParseError(f"Invalid PAGE coordinates: {value}") from exc


def _text_equivs(parent: ET.Element) -> list[TextEquiv]:
    result: list[TextEquiv] = []
    for element in parent.findall("pc:TextEquiv", _NS):
        unicode_element = _required(element.find("pc:Unicode", _NS), "TextEquiv/Unicode")
        try:
            index = int(element.get("index", "0"))
        except ValueError as exc:
            raise PageXMLParseError("TextEquiv index must be an integer") from exc
        result.append(TextEquiv(index=index, unicode=unicode_element.text or ""))
    return result


def _line(element: ET.Element) -> TextLine:
    coords = _required(element.find("pc:Coords", _NS), "TextLine/Coords")
    baseline = element.find("pc:Baseline", _NS)
    return TextLine(
        id=element.get("id", ""),
        polygon=_points(coords.get("points", "")),
        baseline=_points(baseline.get("points", "")) if baseline is not None else None,
        text_equivs=_text_equivs(element),
    )


def from_element(root: ET.Element) -> PageDocument:
    """Parse a PAGE root element."""
    if root.tag != f"{{{PAGE_NAMESPACE}}}PcGts":
        raise PageXMLParseError(f"Expected PAGE 2019 PcGts root, got {root.tag!r}")

    metadata_element = _required(root.find("pc:Metadata", _NS), "Metadata")
    steps = [
        ProcessingStep(
            name=item.get("name", ""),
            value=item.get("value", ""),
            date=_timestamp(item.get("date", "")) if item.get("date") else None,
        )
        for item in metadata_element.findall("pc:MetadataItem", _NS)
        if item.get("type") == "processingStep"
    ]
    metadata = PageMetadata(
        creator=_text(metadata_element, "Creator"),
        created=_timestamp(_text(metadata_element, "Created")),
        last_change=_timestamp(_text(metadata_element, "LastChange")),
        processing_steps=steps,
    )

    page = _required(root.find("pc:Page", _NS), "Page")
    try:
        width = int(page.get("imageWidth", ""))
        height = int(page.get("imageHeight", ""))
    except ValueError as exc:
        raise PageXMLParseError("Page imageWidth and imageHeight must be integers") from exc

    regions: list[TextRegion] = []
    for element in page.findall("pc:TextRegion", _NS):
        coords = _required(element.find("pc:Coords", _NS), "TextRegion/Coords")
        confidence = coords.get("conf")
        regions.append(
            TextRegion(
                id=element.get("id", ""),
                type=element.get("type", "other"),
                custom=element.get("custom", ""),
                polygon=_points(coords.get("points", "")),
                lines=[_line(line) for line in element.findall("pc:TextLine", _NS)],
                text_equivs=_text_equivs(element),
                confidence=float(confidence) if confidence is not None else None,
            )
        )

    ordered_group = page.find("pc:ReadingOrder/pc:OrderedGroup", _NS)
    if ordered_group is None:
        reading_order = [region.id for region in regions]
        group_id = "reading-order"
    else:
        refs = ordered_group.findall("pc:RegionRefIndexed", _NS)
        refs.sort(key=lambda ref: int(ref.get("index", "0")))
        reading_order = [ref.get("regionRef", "") for ref in refs]
        group_id = ordered_group.get("id", "reading-order")

    return PageDocument(
        image_filename=page.get("imageFilename", ""),
        image_width=width,
        image_height=height,
        metadata=metadata,
        regions=regions,
        reading_order=reading_order,
        reading_order_group_id=group_id,
        pc_gts_id=root.get("pcGtsId"),
    )


def parse(path: str | Path) -> PageDocument:
    """Read a PAGE XML file from disk."""
    try:
        return from_element(ET.parse(path).getroot())
    except ET.ParseError as exc:
        raise PageXMLParseError(f"Invalid XML: {exc}") from exc


def parse_bytes(value: bytes) -> PageDocument:
    """Read PAGE XML bytes."""
    try:
        return from_element(ET.fromstring(value))
    except ET.ParseError as exc:
        raise PageXMLParseError(f"Invalid XML: {exc}") from exc


def parse_string(value: str) -> PageDocument:
    """Read PAGE XML text."""
    return parse_bytes(value.encode("utf-8"))
