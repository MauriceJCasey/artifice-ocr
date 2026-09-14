# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_user_configuration(tmp_path, monkeypatch):
    """Keep the suite independent of a developer's real saved settings.

    ``config.load_config()`` deliberately merges ``~/.artifice_ocr/settings.json``
    and model endpoint environment variables. Tests that replace ``_DEFAULTS``
    otherwise still receive the maintainer's real URLs, making a clean CI run
    pass while the same suite fails on a configured workstation.
    """
    from artifice_ocr import config

    user_dir = tmp_path / "artifice-user"
    monkeypatch.setattr(config, "_USER_DIR", user_dir)
    monkeypatch.setattr(config, "_SETTINGS_PATH", user_dir / "settings.json")
    for name in (
        "ARTIFICE_OCR_CONFIG",
        "OCR_MODEL",
        "CLEANUP_MODEL",
        "TRANSLATE_MODEL",
        "LM_STUDIO_URL",
        "OLLAMA_URL",
        "OUTPUT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    config.reset()
    yield
    config.reset()


@pytest.fixture
def safe_tmp_path():
    """Temp directory NOT under any blocked root or blocked home child.

    pytest's ``tmp_path`` resolves to a system temp directory that may be
    under a POSIX blocked root (e.g. ``/private/var`` on macOS) or a
    Windows blocked home child (e.g. ``AppData``), causing the pathcheck
    to reject legitimate test photo paths.

    This fixture places temp directories under ``~/.artifice_ocr_test_tmp/``
    which is not in any blocklist and not under any blocked root.
    """
    base = Path.home() / ".artifice_ocr_test_tmp"
    base.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=str(base)))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Live-gate page sizing
# --------------------------------------------------------------------------- #
#
# The archive scan that exposed the context-overflow bug was 4653x3445 —
# 16.0 MPix. The committed fixture is 1280x1657, 2.1 MPix: plenty to exercise
# OCR, but roughly 7.5x too small to ever overflow a 4096-token context
# window. That is precisely why the live gate passed for weeks while a real
# user's page failed on a stock install. A gate whose page cannot reach the
# failing regime is not a gate for this class of bug.

LIVE_FIXTURE = Path(__file__).parent / "fixtures" / "proceedings_usnm_173.jpg"

ARCHIVE_PAGE_LONGEST_EDGE = 4653
"""Longest edge of the real scan that overflowed the context window."""

ARCHIVE_PAGE_MIN_PIXELS = 15_000_000
"""Floor for "an archive-resolution page", just under the real scan's 16.0 MPix."""


def make_archive_resolution_page(
    dest: Path, *, longest_edge: int = ARCHIVE_PAGE_LONGEST_EDGE
) -> Path:
    """Write *dest* as the live fixture upscaled to real archive resolution.

    Upscaling adds no detail, and is not meant to. What this exercises is the
    *visual token budget*, which is driven by pixel dimensions alone — an
    upscaled page costs the model exactly as many visual tokens as a natively
    huge one. Committing a genuine 16 MPix fixture would add several MB to the
    repository to test a property a resize reproduces exactly.
    """
    from PIL import Image

    with Image.open(LIVE_FIXTURE) as img:
        scale = longest_edge / max(img.size)
        size = (round(img.width * scale), round(img.height * scale))
        resized = img.resize(size, Image.LANCZOS)
        if resized.mode != "RGB":
            resized = resized.convert("RGB")
        resized.save(dest, quality=95)
    return dest


@pytest.fixture
def live_gate_page_spec():
    """The size contract the live gate's page has to meet.

    Exposed as a fixture because ``tests`` is not an importable package — a
    test that needs these constants gets them from here rather than reaching
    into conftest.
    """
    return {
        "fixture": LIVE_FIXTURE,
        "longest_edge": ARCHIVE_PAGE_LONGEST_EDGE,
        "min_pixels": ARCHIVE_PAGE_MIN_PIXELS,
    }


@pytest.fixture
def archive_resolution_page():
    """Factory writing an archive-resolution page to a caller-chosen path."""
    return make_archive_resolution_page


@pytest.fixture
def sent_vision_image_sizes(monkeypatch):
    """Record the ``(w, h)`` of every image actually handed to the vision model.

    The live gate must assert on what *reached* the model, not merely that the
    run finished. With a large enough context window an uncapped page can still
    succeed, which would let a regression that silently drops the resize sail
    through a green gate — the same "it passed, so it must be right" failure
    that let the original bug ship.

    Only the vision path is recorded: the Tesseract path passes ``max_edge=None``
    and deliberately keeps full resolution.
    """
    import base64
    import io

    from artifice_ocr.stages import ocr as ocr_stage
    from PIL import Image

    sizes: list[tuple[int, int]] = []
    real_encode = ocr_stage._encode_image

    def _spy(path, orientation=1, *, max_edge=None):
        b64, mime = real_encode(path, orientation, max_edge=max_edge)
        if max_edge is not None:
            with Image.open(io.BytesIO(base64.standard_b64decode(b64))) as img:
                sizes.append(img.size)
        return b64, mime

    monkeypatch.setattr(ocr_stage, "_encode_image", _spy)
    return sizes


@pytest.fixture(autouse=True)
def isolate_logging(tmp_path, monkeypatch):
    """Keep the suite from writing into the developer's real log directory.

    ``setup_logging`` now installs a rotating file handler under
    ``~/.artifice_ocr/logs`` by default. Without this the test suite would
    append to the user's actual application log — and, worse, a test asserting
    on log contents would read whatever a previous real run had left there.
    """
    from artifice_ocr import _logging

    monkeypatch.setenv("ARTIFICE_OCR_LOG_DIR", str(tmp_path / "logs"))
    _logging.reset()
    yield
    _logging.reset()
