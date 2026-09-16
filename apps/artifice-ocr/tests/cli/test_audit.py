# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""audit-translations command tests."""

import json
from pathlib import Path

from artifice_ocr.cli import app
from typer.testing import CliRunner

runner = CliRunner()


# ---------------------------------------------------------------------------
# audit-translations: find output already corrupted by the pre-fix
# already-English mistranslation bug
# ---------------------------------------------------------------------------


def _write_translated_json(out_dir: Path, stem: str, **fields):
    json_path = out_dir / "translated" / "json" / f"{stem}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(fields), encoding="utf-8")


def test_audit_translations_flags_real_english_translations(tmp_path):
    out_dir = tmp_path / "output"
    _write_translated_json(
        out_dir, "affected_doc", source_language="en", source_file="affected_doc.png"
    )
    _write_translated_json(
        out_dir,
        "properly_skipped",
        source_language="en",
        skipped_translation=True,
        source_file="properly_skipped.png",
    )
    _write_translated_json(
        out_dir,
        "genuinely_translated",
        source_language="de",
        source_file="genuinely_translated.png",
    )

    result = runner.invoke(app, ["audit-translations", "--output-dir", str(out_dir)])
    assert result.exit_code == 0
    assert "affected_doc" in result.output
    assert "properly_skipped" not in result.output
    assert "genuinely_translated" not in result.output
    assert "1 likely affected" in result.output


def test_audit_translations_json_output(tmp_path):
    out_dir = tmp_path / "output"
    _write_translated_json(
        out_dir, "affected_doc", source_language="en", source_file="affected_doc.png"
    )

    result = runner.invoke(app, ["audit-translations", "--output-dir", str(out_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["stem"] == "affected_doc"
    assert payload[0]["source_file"] == "affected_doc.png"


def test_audit_translations_reports_none_affected(tmp_path):
    out_dir = tmp_path / "output"
    _write_translated_json(out_dir, "fine", source_language="de", source_file="fine.png")

    result = runner.invoke(app, ["audit-translations", "--output-dir", str(out_dir)])
    assert result.exit_code == 0
    assert "None affected" in result.output


def test_audit_translations_handles_missing_output_dir(tmp_path):
    result = runner.invoke(app, ["audit-translations", "--output-dir", str(tmp_path / "nope")])
    assert result.exit_code == 0
    assert "No translated output found" in result.output


def test_audit_translations_recurses_into_subfolders(tmp_path):
    # Tropy items nest into one subfolder per item — the scan must find those.
    out_dir = tmp_path / "output"
    _write_translated_json(
        out_dir, "Some KV File/page_0001", source_language="en", source_file="page_0001.jpg"
    )

    result = runner.invoke(app, ["audit-translations", "--output-dir", str(out_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert "page_0001" in payload[0]["stem"]
