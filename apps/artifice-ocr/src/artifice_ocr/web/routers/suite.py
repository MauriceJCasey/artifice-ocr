# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Suite-shell routes: the app switcher and shared UI preferences.

shared-ui's shell.js calls these on every page. Without them the app switcher
reads "Suite status is unavailable" and the theme choice never persists.
"""

from fastapi import APIRouter, Body, HTTPException
from shared_ui.suite import get_preferences, suite_apps, update_preferences

router = APIRouter(tags=["suite"])


@router.get("/api/suite/apps")
def get_suite_apps() -> list[dict[str, object]]:
    """Return the shared launcher model for the suite switcher."""
    return suite_apps()


@router.get("/api/ui/preferences")
def get_ui_preferences() -> dict[str, object]:
    """Return non-sensitive preferences shared by every Artifice app."""
    return get_preferences()


@router.patch("/api/ui/preferences")
def patch_ui_preferences(
    patch: dict[str, object] = Body(...),  # noqa: B008
) -> dict[str, object]:
    """Validate and persist a partial shared UI preference update."""
    try:
        return update_preferences(patch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
