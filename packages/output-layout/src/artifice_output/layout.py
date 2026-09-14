# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

LAYOUT_VERSION = 1
STAGES = ("raw-ocr", "cleaned", "structured", "translated", "titles")
EXPORTS = (
    "pdf",
    "markdown",
    "tropy",
    "ludwiglang",
    "graph",
    "obsidian",
    "transcript",
    "draft",
    "page",
)


class LayoutError(ValueError):
    """Raised when a project/output path cannot be represented safely."""


def slugify(value: str, *, fallback: str = "project") -> str:
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[-\s]+", "-", value.strip().lower()).strip("-")
    return value[:80] or fallback


class ProjectLayout:
    """Resolve all generated files beneath one stable project directory."""

    def __init__(self, root: str | Path, project: str, *, create: bool = False) -> None:
        self.root = Path(root).expanduser()
        self.project_slug = slugify(project)
        self.project_dir = self.root / "projects" / self.project_slug
        if create:
            self.ensure()

    def ensure(self) -> Path:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "run-history").mkdir(exist_ok=True)
        (self.project_dir / "pipeline").mkdir(exist_ok=True)
        (self.project_dir / "exports").mkdir(exist_ok=True)
        metadata = self.project_dir / "project.json"
        if not metadata.exists():
            metadata.write_text(
                json.dumps(
                    {
                        "layout_version": LAYOUT_VERSION,
                        "slug": self.project_slug,
                        "name": self.project_slug.replace("-", " ").title(),
                        "created": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        return self.project_dir

    def stage(self, name: str) -> Path:
        if name not in STAGES:
            raise LayoutError(f"Unknown pipeline stage: {name}")
        return self.project_dir / "pipeline" / name

    def stage_text(self, name: str) -> Path:
        return self.stage(name) / "text"

    def stage_records(self, name: str) -> Path:
        return self.stage(name) / "records"

    def export_dir(self, kind: str) -> Path:
        if kind not in EXPORTS:
            raise LayoutError(f"Unknown export type: {kind}")
        return self.project_dir / "exports" / kind

    def create_run(self, metadata: dict[str, object] | None = None) -> Path:
        self.ensure()
        run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        path = self.project_dir / "run-history" / f"{run_id}.json"
        payload = {
            "run_id": run_id,
            "layout_version": LAYOUT_VERSION,
            "started": datetime.now(UTC).isoformat(timespec="seconds"),
            **(metadata or {}),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path


def discover_projects(root: str | Path) -> list[dict[str, str]]:
    projects = Path(root).expanduser() / "projects"
    if not projects.is_dir():
        return []
    found: list[dict[str, str]] = []
    for directory in sorted(p for p in projects.iterdir() if p.is_dir()):
        metadata = directory / "project.json"
        name = directory.name
        if metadata.is_file():
            try:
                data = json.loads(metadata.read_text(encoding="utf-8"))
                name = str(data.get("name") or name)
            except (OSError, ValueError):
                pass
        found.append({"slug": directory.name, "name": name, "path": str(directory)})
    return found


def layout_for_path(path: str | Path) -> ProjectLayout | None:
    """Return the project layout containing *path*, if it is canonical."""
    candidate = Path(path).expanduser().resolve()
    for parent in (candidate, *candidate.parents):
        # A directory called ``projects`` is not sufficient evidence: the
        # maintainer's source checkout itself lives under ~/projects, which
        # previously made every relative output path look canonical and sent
        # exports into the repository. ``ensure()`` writes this marker for
        # every real Artifice project.
        if parent.parent.name == "projects" and (parent / "project.json").is_file():
            return ProjectLayout(parent.parent.parent, parent.name)
    return None
