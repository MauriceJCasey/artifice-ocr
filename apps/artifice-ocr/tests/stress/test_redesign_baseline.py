# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stage 0 browser evidence using the existing isolated OCR server."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from artifice_ocr import config
from artifice_ocr.output import page_path
from artifice_ocr.pagexml import PageDocument, write
from artifice_ocr.web.runtime import state
from playwright.sync_api import expect

from .test_seeded_ui import _dismiss_onboarding, _tab


@pytest.mark.ui_stress
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_review_and_page_export_baseline(stress_server, chromium_browser, tmp_path, theme):
    output = tmp_path / "output"
    config.apply_overrides({"output_dir": str(output), "approved_folders": [str(tmp_path)]})
    item = state.items[0]
    persisted = page_path(output, item.stem)
    persisted.parent.mkdir(parents=True, exist_ok=True)
    # The export contract is a verbatim copy of persisted PAGE, without inference.
    write(
        PageDocument(image_filename="synthetic.png", image_width=640, image_height=360), persisted
    )
    xml = persisted.read_text(encoding="utf-8")
    artifacts = Path(__file__).resolve().parents[4] / ".artifacts/ui-redesign/ocr"
    artifacts.mkdir(parents=True, exist_ok=True)
    context = chromium_browser.new_context(viewport={"width": 1440, "height": 900})
    errors = []
    context.route(
        "**/api/ui/preferences",
        lambda route: route.fulfill(json={"theme": theme, "reduced_motion": True}),
    )
    # Model readiness is synthetic; never probe a developer's local model service.
    context.route(
        "**/api/byom/state",
        lambda route: route.fulfill(
            json={
                "configured": True,
                "reachable": True,
                "model": "fixture-model",
            }
        ),
    )
    context.route(
        "**/api/local-models?*",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "backend": parse_qs(urlsplit(route.request.url).query)["backend"][0],
                "models": ["fixture-model"],
            }
        ),
    )
    page = context.new_page()
    page.on(
        "console", lambda message: errors.append(message.text) if message.type == "error" else None
    )
    page.on(
        "requestfailed",
        lambda request: (
            errors.append(f"{request.failure} {request.url}")
            if request.url.startswith(stress_server) and request.failure != "net::ERR_ABORTED"
            else None
        ),
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "response",
        lambda response: (
            errors.append(f"{response.status} {response.url}")
            if response.status >= 400 and response.url.startswith(stress_server)
            else None
        ),
    )
    try:
        page.goto(stress_server)
        _dismiss_onboarding(page)
        expect(page.locator("#queue-body tr[data-id]")).to_have_count(4)
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        page.screenshot(path=str(artifacts / f"{theme}-populated.png"), full_page=True)
        _tab(page, "preview")
        page.locator("#preview-item-select").select_option(str(id(item)))
        text = page.locator('#panel-preview .compare-pane[data-pane="raw"] textarea')
        expect(text).to_have_value(item.results["raw"]["extracted_text"])
        page.locator("#preview-reading-size").select_option("24px")
        expect(page.locator("html")).to_have_css("--review-text-size", "24px")
        corrected = "Synthetic correction: names and dates checked."
        text.fill(corrected)
        with page.expect_response(
            lambda response: response.url.endswith(f"/{str(id(item))}/raw-text")
        ) as saved:
            page.locator("#btn-save-raw").click()
        assert saved.value.ok
        assert json.loads(saved.value.request.post_data) == {"text": corrected}
        expect(page.locator("#btn-save-raw")).to_be_disabled()
        page.reload()
        _dismiss_onboarding(page)
        expect(text).to_have_value(corrected)
        _tab(page, "preview")
        expect(page.locator("#preview-reading-size")).to_have_value("24px")
        _tab(page, "settings")
        page.screenshot(path=str(artifacts / f"{theme}-settings.png"), full_page=True)
        _tab(page, "main")
        page.locator("#output-dir").fill(str(output))
        page.locator(f'#queue-body tr[data-id="{str(id(item))}"] .row-select').check()
        page.locator("#btn-export-page").click()
        with page.expect_download() as download:
            page.locator("#btn-page-export-start").click()
        assert Path(download.value.path()).read_text(encoding="utf-8") == xml
        assert download.value.suggested_filename == f"{Path(item.stem).name}.xml"
        page.locator("#btn-page-export-close").click()
        state.clear()
        page.reload()
        _dismiss_onboarding(page)
        expect(page.locator("#queue-body tr[data-id]")).to_have_count(0)
        page.screenshot(path=str(artifacts / f"{theme}-empty.png"), full_page=True)
        assert not errors, errors
    finally:
        context.close()
