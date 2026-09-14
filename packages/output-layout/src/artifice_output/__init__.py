# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Portable project-oriented output paths for Artifice applications."""

from .layout import LayoutError, ProjectLayout, discover_projects, layout_for_path, slugify

__all__ = ["LayoutError", "ProjectLayout", "discover_projects", "layout_for_path", "slugify"]
