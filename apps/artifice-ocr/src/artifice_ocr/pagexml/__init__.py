# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""PRImA PAGE XML document model and serialisation helpers."""

from .geometry import restore_original_coordinates
from .model import (
    ARTIFICE_REGION_TYPES,
    PAGE_NAMESPACE,
    STAGE_TEXT_EQUIV_INDEX,
    PageDocument,
    PageMetadata,
    ProcessingStep,
    TextEquiv,
    TextLine,
    TextRegion,
    artifice_custom,
    page_region_type,
)
from .reader import PageXMLParseError, parse, parse_bytes, parse_string
from .writer import schema_path, serialize, write

__all__ = [
    "ARTIFICE_REGION_TYPES",
    "PAGE_NAMESPACE",
    "STAGE_TEXT_EQUIV_INDEX",
    "PageDocument",
    "PageMetadata",
    "PageXMLParseError",
    "ProcessingStep",
    "TextEquiv",
    "TextLine",
    "TextRegion",
    "artifice_custom",
    "page_region_type",
    "parse",
    "parse_bytes",
    "parse_string",
    "restore_original_coordinates",
    "schema_path",
    "serialize",
    "write",
]
