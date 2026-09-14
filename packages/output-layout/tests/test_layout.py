# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

from artifice_output import ProjectLayout, discover_projects, layout_for_path, slugify


def test_slugify_and_layout(tmp_path: Path):
    assert slugify("My Archive: Part I") == "my-archive-part-i"
    layout = ProjectLayout(tmp_path, "My Archive", create=True)
    assert layout.stage_text("raw-ocr") == layout.project_dir / "pipeline" / "raw-ocr" / "text"
    assert layout.export_dir("pdf") == layout.project_dir / "exports" / "pdf"
    assert layout.create_run({"status": "started"}).exists()


def test_discovery_and_path_lookup(tmp_path: Path):
    layout = ProjectLayout(tmp_path, "Archive", create=True)
    projects = discover_projects(tmp_path)
    assert projects[0]["slug"] == "archive"
    assert layout_for_path(layout.project_dir / "exports" / "pdf") is not None


def test_path_under_unrelated_projects_directory_is_not_canonical(tmp_path: Path):
    checkout = tmp_path / "projects" / "artifice-suite"
    checkout.mkdir(parents=True)

    assert layout_for_path(checkout / "out") is None
