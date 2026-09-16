# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared `client` fixture — an isolated TestClient around a fresh app and RunState."""

import pytest
from artifice_ocr import config
from artifice_ocr.web import runtime, server
from artifice_ocr.web.runtime import RunState
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # config.save_user_settings() always targets ~/.artifice_ocr/settings.json
    # by design (it's a per-user file, not something callers parameterise) —
    # so any test that reaches it must redirect the module constant itself,
    # or it will overwrite the developer's real saved settings.
    monkeypatch.setattr(config, "_SETTINGS_PATH", tmp_path / "settings.json")

    config.reset()
    config.load_config()
    config.apply_overrides({"history_db": str(tmp_path / "history.db")})

    fresh = RunState()
    monkeypatch.setattr(server, "app", server.create_app(runtime_state=fresh))

    # Reset pdf_export_state so no test inherits a prior run's output_path,
    # status or queued events (see test_pdf_export_download_404_before_compilation
    # which was order-dependent before this reset).
    import queue as _queue_mod

    pstate = runtime.pdf_export_state
    # Wait for any thread from a previous test that didn't clean up
    if pstate.thread is not None and pstate.thread.is_alive():
        pstate.thread.join(timeout=5)
    pstate.status = "idle"
    pstate.error = None
    pstate.output_path = None
    while True:
        try:
            pstate.events.get_nowait()
        except _queue_mod.Empty:
            break

    with TestClient(server.app) as c:
        yield c
