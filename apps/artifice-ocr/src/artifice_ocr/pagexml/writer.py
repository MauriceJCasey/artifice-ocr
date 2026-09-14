# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Serialise the Artifice PAGE model as PRImA PAGE 2019 XML."""

from __future__ import annotations

import importlib.resources
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from .model import PAGE_NAMESPACE, PageDocument, Point, TextEquiv

_XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", PAGE_NAMESPACE)
ET.register_namespace("xsi", _XSI_NAMESPACE)


def _tag(local: str) -> str:
    return f"{{{PAGE_NAMESPACE}}}{local}"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _points(points: list[Point]) -> str:
    return " ".join(f"{x},{y}" for x, y in points)


def _append_text_equivs(parent: ET.Element, values: list[TextEquiv]) -> None:
    for value in sorted(values, key=lambda item: item.index):
        equiv = ET.SubElement(parent, _tag("TextEquiv"), {"index": str(value.index)})
        ET.SubElement(equiv, _tag("Unicode")).text = value.unicode


def to_element(document: PageDocument) -> ET.Element:
    """Build an ElementTree element for *document*."""
    root_attrs = {
        f"{{{_XSI_NAMESPACE}}}schemaLocation": f"{PAGE_NAMESPACE} {PAGE_NAMESPACE}/pagecontent.xsd"
    }
    if document.pc_gts_id:
        root_attrs["pcGtsId"] = document.pc_gts_id
    root = ET.Element(_tag("PcGts"), root_attrs)

    metadata = ET.SubElement(root, _tag("Metadata"))
    ET.SubElement(metadata, _tag("Creator")).text = document.metadata.creator
    ET.SubElement(metadata, _tag("Created")).text = _timestamp(document.metadata.created)
    ET.SubElement(metadata, _tag("LastChange")).text = _timestamp(document.metadata.last_change)
    for step in document.metadata.processing_steps:
        attrs = {"type": "processingStep", "name": step.name, "value": step.value}
        if step.date is not None:
            attrs["date"] = _timestamp(step.date)
        ET.SubElement(metadata, _tag("MetadataItem"), attrs)

    page = ET.SubElement(
        root,
        _tag("Page"),
        {
            "imageFilename": document.image_filename,
            "imageWidth": str(document.image_width),
            "imageHeight": str(document.image_height),
        },
    )
    order = document.reading_order or [region.id for region in document.regions]
    if order:
        reading_order = ET.SubElement(page, _tag("ReadingOrder"))
        group = ET.SubElement(
            reading_order, _tag("OrderedGroup"), {"id": document.reading_order_group_id}
        )
        for index, region_id in enumerate(order):
            ET.SubElement(
                group,
                _tag("RegionRefIndexed"),
                {"index": str(index), "regionRef": region_id},
            )

    for region in document.regions:
        attrs = {"id": region.id, "type": region.type}
        if region.custom:
            attrs["custom"] = region.custom
        region_element = ET.SubElement(page, _tag("TextRegion"), attrs)
        coord_attrs = {"points": _points(region.polygon)}
        if region.confidence is not None:
            coord_attrs["conf"] = str(region.confidence)
        ET.SubElement(region_element, _tag("Coords"), coord_attrs)
        for line in region.lines:
            line_element = ET.SubElement(region_element, _tag("TextLine"), {"id": line.id})
            ET.SubElement(line_element, _tag("Coords"), {"points": _points(line.polygon)})
            if line.baseline:
                ET.SubElement(line_element, _tag("Baseline"), {"points": _points(line.baseline)})
            _append_text_equivs(line_element, line.text_equivs)
        _append_text_equivs(region_element, region.text_equivs)
    return root


def serialize(document: PageDocument, *, pretty: bool = True) -> bytes:
    """Return a UTF-8 PAGE XML document, including its XML declaration."""
    root = to_element(document)
    if pretty:
        ET.indent(root, space="  ")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return data + b"\n" if pretty else data


def write(document: PageDocument, path: str | Path) -> Path:
    """Write *document* to *path* and return the resulting path."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(serialize(document))
    return destination


def schema_path() -> Path:
    """Return the packaged, offline PAGE 2019 XSD path."""
    resource = importlib.resources.files("artifice_ocr.pagexml") / "schema" / "pagecontent.xsd"
    return Path(str(resource))
