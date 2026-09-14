# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in contract test against a real, isolated Tropy process.

Run through ``scripts/interop/run-live-tropy.sh``. The normal test suite never
starts third-party desktop software and never writes to a user's Tropy profile.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from artifice_ocr import config
from artifice_ocr.tropy_api import connect
from artifice_ocr.tropy_jsonld import ExportPhoto, build_export
from artifice_ocr.web.routers import tropy_notes
from artifice_ocr.web.runtime import state
from artifice_ocr.web.server import app
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

pytestmark = [
    pytest.mark.live_interop,
    pytest.mark.skipif(
        os.environ.get("ARTIFICE_LIVE_TROPY") != "1",
        reason="set ARTIFICE_LIVE_TROPY=1 or use scripts/interop/run-live-tropy.sh",
    ),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _source() -> Path:
    raw = os.environ.get("ARTIFICE_TROPY_SOURCE")
    if not raw:
        pytest.skip("ARTIFICE_TROPY_SOURCE does not name a built Tropy checkout")
    source = Path(raw).resolve()
    if not (source / "scripts" / "db.js").is_file():
        pytest.fail(f"Tropy source is incomplete: {source}")
    return source


def _electron(source: Path) -> Path:
    override = os.environ.get("ARTIFICE_TROPY_EXECUTABLE")
    if override:
        executable = Path(override).resolve()
    elif os.name == "nt":
        executable = source / "node_modules" / "electron" / "dist" / "electron.exe"
    else:
        executable = source / "node_modules" / ".bin" / "electron"
    if not executable.is_file():
        pytest.fail(f"Tropy Electron executable was not built: {executable}")
    return executable


def _create_project(source: Path, project: Path) -> None:
    node = os.environ.get("ARTIFICE_TROPY_NODE") or shutil.which("node")
    if not node:
        pytest.fail("Node is unavailable; run scripts/interop/bootstrap-tropy.sh")
    subprocess.run(
        [
            node,
            str(source / "scripts" / "db.js"),
            "create",
            "--file",
            str(project),
            "--name",
            "Artifice Interop",
        ],
        cwd=source,
        check=True,
        timeout=30,
    )


def _wait_for_project(base_url: str, project: Path, process: subprocess.Popen, log: Path) -> dict:
    deadline = time.monotonic() + 45
    last_error = "Tropy did not answer"
    with httpx.Client(timeout=1, trust_env=False, follow_redirects=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = log.read_text(encoding="utf-8", errors="replace")[-4000:]
                pytest.fail(f"Tropy exited with {process.returncode}\n{output}")
            try:
                root = client.get(f"{base_url}/")
                if root.status_code == 200:
                    payload = root.json()
                    if Path(payload["project"]).resolve() == project.resolve():
                        named = client.get(f"{base_url}/project/current/")
                        if named.status_code == 200:
                            return named.json()
                        # Tropy 1.17 has no named-project routes. Its legacy
                        # collection becomes available only after the renderer
                        # has finished opening the database.
                        legacy = client.get(f"{base_url}/project/items")
                        if legacy.status_code == 200:
                            return payload
                        last_error = f"legacy API HTTP {legacy.status_code}"
                    else:
                        last_error = "API is ready but the requested project is not current"
                else:
                    last_error = f"HTTP {root.status_code}: {root.text[:200]}"
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = str(exc)
            time.sleep(0.25)
    pytest.fail(f"Tropy did not open the isolated project: {last_error}")


@contextmanager
def _running_tropy(source: Path, project: Path, runtime: Path, port: int):
    electron = _electron(source)
    data = runtime / "data"
    cache = runtime / "cache"
    logs = runtime / "logs"
    data.mkdir()
    cache.mkdir()
    logs.mkdir()
    process_log = runtime / "process.log"
    command = [str(electron)]
    if not os.environ.get("ARTIFICE_TROPY_EXECUTABLE"):
        command.extend(["--app", str(source), "--dev"])
    command.extend(
        [
            "--data",
            str(data),
            "--cache",
            str(cache),
            "--logs",
            str(logs),
            "--port",
            str(port),
            # Electron 38 no longer enables its software WebGL fallback
            # implicitly. The isolated process opens only our disposable,
            # trusted fixture project, so opt in explicitly. Do not also call
            # disableHardwareAcceleration: Pixi still requests WebGL and then
            # crashes when Electron reports it as unavailable.
            "--enable-unsafe-swiftshader",
            "--use-angle=swiftshader",
            "--use-gl=angle",
            "--ignore-gpu-blocklist",
            "--no-auto-updates",
            str(project),
        ]
    )
    with process_log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            command,
            cwd=source,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        try:
            payload = _wait_for_project(f"http://127.0.0.1:{port}", project, process, process_log)
            yield process, payload
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


@contextmanager
def _running_artifice_web():
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
        pytest.fail("Artifice OCR live UI server did not start")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_real_tropy_browse_queue_and_note_round_trip(tmp_path):
    """Browse a real project, queue its photo, then write and dedupe its OCR."""
    source = _source()
    project = tmp_path / "Artifice Contract.tropy"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _create_project(source, project)

    image = tmp_path / "contract.jpg"
    fixture_image = Image.new("RGB", (640, 360), "white")
    ImageDraw.Draw(fixture_image).text((24, 24), "Artifice live Tropy contract", fill="black")
    fixture_image.save(image, quality=90)
    exported = build_export(
        [
            ExportPhoto(
                abs_path=image,
                text="Imported OCR note",
                label="Contract image",
                language="en",
                item_node=None,
                group=None,
                photo_index=None,
                path_rel=None,
                checksum="",
                mimetype="image/jpeg",
            )
        ]
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    config.reset()
    config.load_config()
    config.apply_overrides(
        {"tropy_api_port": port, "history_db": str(tmp_path / "history.sqlite3")}
    )
    try:
        with _running_tropy(source, project, runtime, port) as (_process, discovery):
            assert discovery["version"]
            connection = connect(project)
            assert connection.port == port
            endpoint = f"{base_url}{connection.project_prefix}"
            with httpx.Client(timeout=10, trust_env=False) as client:
                imported = client.post(
                    f"{endpoint}/import",
                    data={"data": json.dumps(exported)},
                )
                assert imported.status_code == 200, imported.text

                items_response = client.get(f"{endpoint}/items")
                items_response.raise_for_status()
                items = items_response.json()
                assert len(items) == 1

                photos_response = client.get(f"{endpoint}/items/{items[0]['id']}/photos")
                photos_response.raise_for_status()
                photos = photos_response.json()
                assert len(photos) == 1

            if discovery["version"].startswith("1.17"):
                assert connection.project_prefix == "/project"
            else:
                assert connection.project_prefix == "/project/current"
            # The import above is fixture setup only. From here on this is the
            # production Tropy-first path: read the live project's SQLite
            # database without writing it, turn the selected photo into a
            # queue item, and send reviewed OCR back through the notes router.
            output = tmp_path / "output"
            output.mkdir()
            config.apply_overrides({"output_dir": str(output), "ocr_model": "live-ui-fixture"})
            with _running_artifice_web() as artifice_url, sync_playwright() as playwright:
                # Tropy's Electron renderer already uses WSL's software GPU.
                # Keep the simultaneous browser on CPU rendering so Chromium
                # cannot destabilise Tropy's GPU process during this gate.
                browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.goto(artifice_url, wait_until="domcontentloaded")
                    expect(page.locator('[data-shell-action="model"]')).to_have_attribute(
                        "data-state", re.compile("^(configured|unconfigured)$"), timeout=15_000
                    )
                    if page.locator(".byom-overlay").count():
                        page.locator(".byom-close").click()
                    page.locator("#btn-add-tropy").click()
                    expect(page.locator("#modal-tropy-add")).to_be_visible()
                    page.locator("#tropy-browse-path").fill(str(project))
                    page.locator("#btn-tropy-browse-load").click()
                    expect(
                        page.locator("#tropy-browse-item-list .tropy-browse-page-check")
                    ).to_have_count(1, timeout=15_000)
                    page.locator("#tropy-browse-item-list .tropy-browse-page-check").check()
                    page.locator("#btn-tropy-browse-enqueue").click()
                    expect(page.locator("#queue-body tr[data-id]")).to_have_count(1, timeout=10_000)
                finally:
                    browser.close()

            jobs = list(state.items)
            assert len(jobs) == 1
            assert jobs[0].source["origin"] == "tropy-live"
            assert jobs[0].source["photo_id"] == int(photos[0]["id"])
            jobs[0].results["raw"] = {
                "extracted_text": "Live production round-trip note",
                "engine": "live-contract",
            }
            jobs[0].language = "en"

            run_id = state.history.start_run(stages=["ocr"], output_dir=str(output), total=1)
            history_item_id = state.history.record_item(run_id, jobs[0])
            state.history.finish_run(run_id, succeeded=1, failed=0, elapsed=1.0)

            # Restart the isolated desktop between read and write. Besides
            # exercising reconnection, this avoids a Tropy 1.17/WSL renderer
            # crash that occurs roughly two seconds after first image import;
            # the Developer API itself persists the project correctly.
            if _process.poll() is None:
                os.killpg(_process.pid, signal.SIGTERM)
                _process.wait(timeout=10)
            write_runtime = tmp_path / "write-runtime"
            write_runtime.mkdir()
            write_port = _free_port()
            config.apply_overrides({"tropy_api_port": write_port})
            with _running_artifice_web() as artifice_url, sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.goto(artifice_url, wait_until="domcontentloaded")
                    expect(page.locator('[data-shell-action="model"]')).to_have_attribute(
                        "data-state", re.compile("^(configured|unconfigured)$"), timeout=15_000
                    )
                    if page.locator(".byom-overlay").count():
                        page.locator(".byom-close").click()
                    page.locator('.shell-nav a[href="/?view=history"]').click()
                    run = page.locator("#history-runs-body tr[data-id]").first
                    expect(run).to_be_visible(timeout=10_000)
                    run.click()
                    item = page.locator(f'#history-items-body tr[data-id="{history_item_id}"]')
                    expect(item).to_be_visible()
                    item.click()
                    expect(page.locator("#panel-history .compare-title")).to_have_text(jobs[0].name)
                    # Choose the only populated stage before Tropy starts. The
                    # hidden modal retains this value, avoiding a second preview
                    # request during Tropy 1.17's short stable API window.
                    stage = page.locator("#tropy-export-stage")
                    stage.evaluate("element => { element.value = 'raw_ocr'; }")

                    with _running_tropy(source, project, write_runtime, write_port):
                        send = page.locator("#btn-history-send-tropy")
                        expect(send).to_be_enabled()
                        send.click()
                        expect(page.locator("#modal-tropy-send")).to_be_visible()
                        expect(page.locator("#tropy-writeback-preview")).to_contain_text(
                            "1 ready", timeout=15_000
                        )
                        commit = page.locator("#btn-writeback-commit")
                        expect(commit).to_be_enabled()
                        commit.click()
                        expect(page.locator("#tropy-writeback-preview")).to_contain_text(
                            "1 added", timeout=15_000
                        )
                        duplicate = tropy_notes.tropy_notes_preview(
                            tropy_notes.TropyNotesRequest(
                                source="history",
                                item_ids=[str(history_item_id)],
                                stage="raw_ocr",
                            )
                        )
                        assert duplicate["write_count"] == 0
                        assert duplicate["counts"]["duplicate"] == 1
                finally:
                    browser.close()
    finally:
        state.clear()
        config.reset()
