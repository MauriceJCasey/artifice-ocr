# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared isolated HTTP server and browser fixtures for UI stress tests."""

import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from artifice_ocr import config
from artifice_ocr.jobs import JobItem, State
from artifice_ocr.web.runtime import state
from artifice_ocr.web.server import app
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def seed_completed_queue(folder: Path, count: int = 4) -> list[JobItem]:
    """Create realistic completed items without contacting an inference server."""
    state.clear()
    items = []
    for index in range(count):
        image_path = folder / f"stress-page-{index + 1}.png"
        image = Image.new("RGB", (640, 360), "white")
        ImageDraw.Draw(image).text((24, 24), f"Archival page {index + 1}", fill="black")
        image.save(image_path)
        raw = f"Archival text {index + 1}: ye olde sample"
        item = JobItem(
            path=str(image_path),
            state=State.DONE,
            confidence=82 + index,
            language="English",
            results={
                "raw": {"extracted_text": raw},
                "cleaned": {"cleaned_text": raw.replace("ye olde", "the old")},
                "translated": {"translated_text": f"Translated {index + 1}"},
            },
            source={
                "origin": "tropy-live",
                "photo_id": 1000 + index,
                "item_id": 2000 + index,
            },
        )
        for stage in item.stages.values():
            stage.state = State.DONE
            stage.chars = len(raw)
        items.append(item)
    state.add_items(items)
    return items


@pytest.fixture
def stress_server(tmp_path):
    """Serve the real FastAPI app on an ephemeral loopback port."""
    config.apply_overrides(
        {"ocr_model": "stress-model", "history_db": str(tmp_path / "history.sqlite3")}
    )
    state.clear()
    state.runner = None
    state.run_id = None
    if state._history is not None:
        state._history.close()
        state._history = None
    items = seed_completed_queue(tmp_path)
    run_id = state.history.start_run(
        stages=["ocr", "cleanup", "translate"],
        output_dir=str(tmp_path / "output"),
        total=len(items),
    )
    for item in items:
        state.history.record_item(run_id, item)
    state.history.finish_run(run_id, succeeded=len(items), failed=0, elapsed=1.25)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/api/queue", timeout=0.25).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("isolated OCR stress server did not start")

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)
    state.clear()
    state.runner = None
    if state._history is not None:
        state._history.close()
        state._history = None


@pytest.fixture(scope="session")
def chromium_browser():
    """Share the browser process while isolating every seed in its own context."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
        yield browser
        browser.close()
