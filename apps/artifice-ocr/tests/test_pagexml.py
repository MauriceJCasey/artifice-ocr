# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime
from pathlib import Path

import pytest
from artifice_ocr.pagexml import (
    STAGE_TEXT_EQUIV_INDEX,
    PageDocument,
    PageMetadata,
    ProcessingStep,
    TextEquiv,
    TextLine,
    TextRegion,
    parse,
    parse_bytes,
    restore_original_coordinates,
    schema_path,
    serialize,
)
from lxml import etree

FIXTURES = Path(__file__).parent / "fixtures" / "pagexml"
STAMP = datetime(2026, 9, 14, 10, 30, tzinfo=UTC)


def _passthrough_document() -> PageDocument:
    return PageDocument(
        pc_gts_id="page-scan-001",
        image_filename="scan-001.png",
        image_width=1200,
        image_height=1800,
        metadata=PageMetadata(
            created=STAMP,
            last_change=STAMP,
            processing_steps=[
                ProcessingStep(
                    name="ocr",
                    value='{"backend":"ollama","model":"olmocr2"}',
                    date=STAMP,
                )
            ],
        ),
        regions=[
            TextRegion(
                id="region-001",
                type="paragraph",
                custom="structure {type:unclassified;}",
                polygon=[(0, 0), (1200, 0), (1200, 1800), (0, 1800)],
                text_equivs=[
                    TextEquiv(index=0, unicode="Raw transcription."),
                    TextEquiv(index=1, unicode="Cleaned transcription."),
                    TextEquiv(index=2, unicode="Translated transcription."),
                ],
            )
        ],
        reading_order=["region-001"],
    )


def _annotated_document() -> PageDocument:
    return PageDocument(
        image_filename="notes.jpg",
        image_width=997,
        image_height=613,
        metadata=PageMetadata(created=STAMP, last_change=STAMP),
        regions=[
            TextRegion(
                id="main-text",
                type="paragraph",
                custom="structure {type:unclassified;}",
                polygon=[(80, 40), (700, 40), (700, 540), (80, 540)],
                lines=[
                    TextLine(
                        id="line-001",
                        polygon=[(90, 70), (680, 70), (680, 110), (90, 110)],
                        baseline=[(92, 103), (678, 104)],
                        text_equivs=[TextEquiv(0, "A manuscript line")],
                    )
                ],
                text_equivs=[TextEquiv(0, "A manuscript line")],
                confidence=0.91,
            ),
            TextRegion(
                id="margin-note",
                type="marginalia",
                custom="structure {type:marginalia;}",
                polygon=[(730, 100), (970, 90), (975, 400), (740, 410)],
                text_equivs=[TextEquiv(0, "Nota bene")],
            ),
        ],
        reading_order=["main-text", "margin-note"],
    )


@pytest.mark.parametrize(
    ("document", "golden_name"),
    [
        (_passthrough_document(), "passthrough.xml"),
        (_annotated_document(), "annotated.xml"),
        (
            PageDocument(
                image_filename="blank.tif",
                image_width=640,
                image_height=480,
                metadata=PageMetadata(created=STAMP, last_change=STAMP),
            ),
            "blank.xml",
        ),
    ],
)
def test_writer_matches_golden_file(document: PageDocument, golden_name: str):
    assert serialize(document) == (FIXTURES / golden_name).read_bytes()


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.xml")))
def test_golden_page_validates_offline_against_vendored_schema(path: Path):
    schema = etree.XMLSchema(etree.parse(str(schema_path())))
    schema.assertValid(etree.parse(str(path)))


def test_parse_serialize_round_trip_is_semantically_equivalent():
    original = parse(FIXTURES / "annotated.xml")
    reparsed = parse_bytes(serialize(original))
    assert reparsed == original


def test_stage_indices_are_centralised_and_stable():
    assert dict(STAGE_TEXT_EQUIV_INDEX) == {"raw": 0, "cleaned": 1, "translated": 2}
    with pytest.raises(TypeError):
        STAGE_TEXT_EQUIV_INDEX["corrected"] = 3  # type: ignore[index]


def test_correction_replaces_only_the_selected_stage_index():
    region = _passthrough_document().regions[0]
    region.set_stage_text("cleaned", "Reviewer correction.")
    assert region.stage_text("raw") == "Raw transcription."
    assert region.stage_text("cleaned") == "Reviewer correction."
    assert region.stage_text("translated") == "Translated transcription."
    assert [item.index for item in region.text_equivs] == [0, 1, 2]


def test_restore_coordinates_for_non_square_non_integer_resize():
    assert restore_original_coordinates(
        [(0, 0), (111, 72), (333, 217), (-4, 300)],
        model_size=(333, 217),
        original_size=(1000, 653),
    ) == [(0, 0), (333, 217), (1000, 653), (0, 653)]


@pytest.mark.parametrize("model_size", [(0, 10), (10, -1)])
def test_restore_coordinates_rejects_invalid_dimensions(model_size: tuple[int, int]):
    with pytest.raises(ValueError, match="dimensions must be positive"):
        restore_original_coordinates([(1, 1)], model_size=model_size, original_size=(100, 100))
