# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run-control guardrails and start-run validation tests."""

import os
from pathlib import Path

from artifice_ocr import config
from artifice_ocr.web import runtime

# --------------------------------------------------------------------------- #
# run control guardrails (no real run is started — no model calls in tests)
# --------------------------------------------------------------------------- #


def test_start_run_rejects_empty_queue(client):
    res = client.post("/api/run/start", json={"stages": ["ocr"]})
    assert res.status_code == 409
    assert "empty" in res.json()["detail"].lower()


def test_start_run_rejects_no_stages(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    res = client.post("/api/run/start", json={"stages": []})
    assert res.status_code == 409
    assert "ocr is required" in res.json()["detail"].lower()


def test_start_run_rejects_postprocessing_without_ocr(client, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    res = client.post("/api/run/start", json={"stages": ["cleanup", "translate"]})
    assert res.status_code == 409
    assert "ocr is required" in res.json()["detail"].lower()


def test_skip_unknown_item_reports_not_ok(client):
    res = client.post("/api/run/skip", json={"id": "does-not-exist"})
    assert res.json() == {"ok": False}


def test_retry_resets_finished_items(client, tmp_path):
    from artifice_ocr.jobs import State

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    added = client.post("/api/queue/add-paths", json={"paths": [str(f)]}).json()
    item_id = added["items"][0]["id"]

    runtime.state.get(item_id).state = State.DONE

    res = client.post("/api/run/retry", json=[item_id])
    assert res.json() == {"ok": True}
    assert runtime.state.get(item_id).state == State.PENDING


def test_pause_resume_cancel_are_no_ops_without_a_run(client):
    # None of these should raise just because nothing is running yet.
    for path in ("/api/run/pause", "/api/run/resume", "/api/run/cancel"):
        assert client.post(path).json() == {"ok": True}


def test_start_run_preflight_failure_returns_409_with_url(client, tmp_path, monkeypatch):
    """A preflight failure (unreachable endpoint) yields a 409 naming the URL.

    Resolution is stubbed to succeed (populating the role cache) so the failure
    provably comes from the preflight check, not from model resolution.
    """
    from artifice_ocr import _resolution
    from artifice_ocr._resolution import _RoleResolution
    from model_harness.discovery import ProbeResult
    from model_harness.resolution import ResolutionSource

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    config.apply_overrides({"ocr_backend": "ollama", "ocr_model": "llava:7b"})

    def fake_resolve(*, stages=None):
        _resolution.reset()
        _resolution._cache["vision"] = _RoleResolution(
            model="llava:7b", backend="ollama", source=ResolutionSource.USER_CHOICE
        )

    monkeypatch.setattr(_resolution, "resolve_models_for_run", fake_resolve)
    monkeypatch.setattr(
        _resolution,
        "probe_endpoint_sync",
        lambda *a, **k: ProbeResult(url="http://localhost:11434", reachable=False, hint="down"),
    )

    res = client.post("/api/run/start", json={"stages": ["ocr"]})
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "http://localhost:11434" in detail
    assert "Cannot reach OCR endpoint" in detail


# --------------------------------------------------------------------------- #
# path validation — output_dir
# --------------------------------------------------------------------------- #


def test_start_run_refuses_output_dir_outside_allowed_roots(client, tmp_path):
    """An output directory outside the permitted roots is refused before
    any run is started."""
    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": "/opt/rejected/output",
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "outside the directories this server is permitted" in detail.lower()
    assert str(Path.home()) not in detail
    assert "allowed:" not in detail.lower()


def test_start_run_refuses_hidden_output_dir(client, tmp_path):
    """A hidden output directory is refused at validation time."""
    hidden = tmp_path / ".hidden_out"
    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": str(hidden),
        },
    )
    assert res.status_code == 400
    assert "hidden" in res.json()["detail"].lower()


def test_start_run_accepts_normal_output_dir(client, tmp_path):
    """A normal output directory passes validation (the run then fails because
    the queue is empty, but that means the path check succeeded)."""
    out = tmp_path / "out"
    out.mkdir()

    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": str(out),
        },
    )
    # 409 = queue is empty, but validation passed (otherwise 400)
    assert res.status_code == 409
    assert "empty" in res.json()["detail"].lower()


def test_start_run_with_segmentation_provider_does_not_crash(client, tmp_path):
    """segmentation_provider is a config.py override, not a start_run() kwarg.

    A prior version threaded it straight into state.start_run(**run_opts),
    whose signature only accepts stages/output_dir/force — that raised
    TypeError (surfaced as an unhandled 500) the moment a client actually set
    a provider. Reaching the same "queue is empty" 409 the plain-stages case
    gets (rather than a 500) proves the request is accepted and routed
    through config.apply_overrides instead.
    """
    out = tmp_path / "out"
    out.mkdir()
    from artifice_ocr import config

    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": str(out),
            "segmentation_provider": "passthrough",
        },
    )
    assert res.status_code == 409
    assert "empty" in res.json()["detail"].lower()
    assert config.get("segmentation_enabled") is True
    assert config.get("segmentation_provider") == "passthrough"


def test_start_run_without_segmentation_provider_disables_it(client, tmp_path):
    """Omitting segmentation_provider must explicitly disable segmentation,
    not merely leave a prior run's enabled setting in place."""
    out = tmp_path / "out"
    out.mkdir()
    from artifice_ocr import config

    config.apply_overrides({"segmentation_enabled": True, "segmentation_provider": "passthrough"})

    res = client.post(
        "/api/run/start",
        json={"stages": ["ocr"], "output_dir": str(out)},
    )
    assert res.status_code == 409
    assert config.get("segmentation_enabled") is False


def test_start_run_diff_residual_without_reference_image_is_rejected(client, tmp_path):
    """diff-residual needs a reference scan — refuse the run before it starts
    rather than let it fail loudly per-page deep in the pipeline."""
    out = tmp_path / "out"
    out.mkdir()

    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": str(out),
            "segmentation_provider": "diff-residual",
        },
    )
    assert res.status_code == 400
    assert "reference scan" in res.json()["detail"].lower()


def test_start_run_diff_residual_with_reference_image_sets_segmentation_options(
    client, tmp_path
):
    """A supplied reference image is validated, stored, and reaches the
    provider via segmentation_options — the same path segment.py reads."""
    out = tmp_path / "out"
    out.mkdir()
    ref = tmp_path / "reference.png"
    ref.write_bytes(b"x")

    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": str(out),
            "segmentation_provider": "diff-residual",
            "segmentation_reference_image": str(ref),
        },
    )
    assert res.status_code == 409  # empty queue — proves the request routed through
    assert config.get("segmentation_options") == {"reference_image": str(ref)}


def test_start_run_non_diff_residual_provider_clears_stale_reference_image(client, tmp_path):
    """A reference image left over from a prior diff-residual run must not
    silently apply to a run using a different provider."""
    out = tmp_path / "out"
    out.mkdir()
    from artifice_ocr import config as config_module

    config_module.apply_overrides(
        {"segmentation_options": {"reference_image": "/some/stale/path.png"}}
    )

    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": str(out),
            "segmentation_provider": "passthrough",
        },
    )
    assert res.status_code == 409
    assert config.get("segmentation_options") == {}


def test_start_run_refuses_windows_style_output_dir(client):
    """A Windows-style output directory path is rejected.

    Platform-dependent for the same reason as
    ``test_add_paths_refuses_windows_style_path`` — see its docstring. The 400
    is the invariant; the message is not.
    """
    res = client.post(
        "/api/run/start",
        json={
            "stages": ["ocr"],
            "output_dir": "C:\\SystemFolder\\output",
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"].lower()
    if os.name == "posix":
        assert "not valid on this platform" in detail
    else:
        assert "outside the directories this server is permitted" in detail


# --------------------------------------------------------------------------- #
# run controls — OCR is required and cannot be disabled (Commit 7)
# --------------------------------------------------------------------------- #


def test_start_run_unknown_stages_do_not_bypass_ocr_requirement(client, tmp_path):
    """A stages list of only unknown names filters down to nothing, so the
    OCR-required 409 fires exactly as for an empty list — no value in
    ``req.stages`` can smuggle a run past the enforcement."""
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    client.post("/api/queue/add-paths", json={"paths": [str(f)]})

    res = client.post("/api/run/start", json={"stages": ["bogus", "also-bogus"]})
    assert res.status_code == 409
    assert "ocr is required" in res.json()["detail"].lower()


def test_start_run_accepts_ocr_without_falling_to_ocr_required(client, tmp_path):
    """A request that *does* include ``ocr`` clears the gate — the 409 it
    receives is the next validation down (empty queue), not the OCR one."""
    # No items added: the empty-queue 409 proves the OCR gate was passed.
    res = client.post("/api/run/start", json={"stages": ["ocr"]})
    assert res.status_code == 409
    assert "ocr is required" not in res.json()["detail"].lower()
    assert "empty" in res.json()["detail"].lower()
