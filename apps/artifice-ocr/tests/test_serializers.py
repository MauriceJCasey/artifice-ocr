# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for web/serializers.py's exposure of stage skip reasons.

A resumed run that reuses existing output must be visible to the browser
client, not just to the server-side log — the queue serialiser is what the
frontend's stage badges read from.
"""

from artifice_ocr.jobs import JobItem, State
from artifice_ocr.web.serializers import serialize_item, serialize_item_preview


def test_serialize_item_exposes_skip_reason_and_key():
    item = JobItem(path="a.png")
    item.stages["ocr"].state = State.SKIPPED
    item.stages["ocr"].skip_reason = "already_exists"
    item.stages["ocr"].skip_key = "Item/page1"

    data = serialize_item(item)

    assert data["stages"]["ocr"]["skip_reason"] == "already_exists"
    assert data["stages"]["ocr"]["skip_key"] == "Item/page1"


def test_serialize_item_skip_fields_default_empty():
    item = JobItem(path="a.png")

    data = serialize_item(item)

    assert data["stages"]["ocr"]["skip_reason"] == ""
    assert data["stages"]["ocr"]["skip_key"] == ""


def test_serialize_item_preview_exposes_skip_reason_too():
    item = JobItem(path="a.png")
    item.stages["ocr"].state = State.SKIPPED
    item.stages["ocr"].skip_reason = "already_exists"
    item.stages["ocr"].skip_key = "Item/page1"

    data = serialize_item_preview(item)

    assert data["stages"]["ocr"]["skip_reason"] == "already_exists"
    assert data["stages"]["ocr"]["skip_key"] == "Item/page1"
