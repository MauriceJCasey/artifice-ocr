# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for shared_ui.uploads."""

from __future__ import annotations

import asyncio
import tempfile

import pytest
import shared_ui.uploads as uploads_module
from shared_ui.uploads import UploadTooLarge, read_capped, read_capped_to_tempfile

_CHUNK = 64 * 1024


class _RecordingUpload:
    """Serves a fixed body, recording the size of every ``read()`` request."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._offset = 0
        self.requested_sizes: list[int] = []

    @property
    def served(self) -> int:
        """Number of body bytes handed out so far."""
        return self._offset

    async def read(self, size: int = -1) -> bytes:
        self.requested_sizes.append(size)
        if self._offset >= len(self._body):
            return b""
        if size == -1:
            chunk = self._body[self._offset :]
            self._offset = len(self._body)
            return chunk
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class TestReadCapped:
    """Tests for read_capped()."""

    def test_body_under_limit_returned_intact(self) -> None:
        body = b"hello world" * 100
        assert asyncio.run(read_capped(_RecordingUpload(body), 100_000)) == body

    def test_body_exactly_at_limit_is_allowed(self) -> None:
        body = b"x" * 1000
        assert asyncio.run(read_capped(_RecordingUpload(body), 1000)) == body

    def test_empty_upload_returns_empty_bytes(self) -> None:
        assert asyncio.run(read_capped(_RecordingUpload(b""), 1000)) == b""

    def test_oversized_raises_before_full_body_is_read(self) -> None:
        body = b"x" * 200_000
        upload = _RecordingUpload(body)
        with pytest.raises(UploadTooLarge):
            asyncio.run(read_capped(upload, 100_000))
        # Reads stay bounded at 64 KB each; a single-read rewrite would ask
        # for -1 and this assertion would fail.
        assert upload.requested_sizes == [_CHUNK, _CHUNK]
        assert -1 not in upload.requested_sizes
        # The whole body was never consumed.
        assert upload.served < len(body)


class TestReadCappedToTempfile:
    """Tests for read_capped_to_tempfile()."""

    def test_body_under_limit_round_trips(self) -> None:
        body = b"hello world" * 100
        spooled = asyncio.run(read_capped_to_tempfile(_RecordingUpload(body), 100_000))
        try:
            assert spooled.read() == body
        finally:
            spooled.close()

    def test_body_exceeding_spool_max_size_spills_to_disk_and_round_trips(self) -> None:
        # spool_max_size is tiny relative to the body, forcing
        # SpooledTemporaryFile to roll over to a real file on disk — this is
        # the path that a purely in-memory test would never exercise.
        body = b"x" * 10_000
        spooled = asyncio.run(
            read_capped_to_tempfile(_RecordingUpload(body), 100_000, spool_max_size=100)
        )
        try:
            assert spooled.read() == body
        finally:
            spooled.close()

    def test_oversized_raises_and_closes_the_spooled_file(self, monkeypatch) -> None:
        created: list[tempfile.SpooledTemporaryFile] = []
        real_cls = tempfile.SpooledTemporaryFile

        class _Tracking(real_cls):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                created.append(self)

        monkeypatch.setattr(uploads_module.tempfile, "SpooledTemporaryFile", _Tracking)

        body = b"x" * 200_000
        upload = _RecordingUpload(body)
        with pytest.raises(UploadTooLarge):
            asyncio.run(read_capped_to_tempfile(upload, 100_000))
        # The reads stay bounded at 64 KB each, exactly like read_capped.
        assert upload.requested_sizes == [_CHUNK, _CHUNK]
        assert -1 not in upload.requested_sizes
        # No leaked open file handle: the spooled file was closed before the
        # exception propagated.
        assert len(created) == 1
        assert created[0].closed

    def test_oversized_with_small_spool_also_closes_the_spilled_file(self, monkeypatch) -> None:
        # Same as above but forces the disk-spill path first, so the file
        # being closed on error is a real filesystem handle, not just an
        # in-memory buffer.
        created: list[tempfile.SpooledTemporaryFile] = []
        real_cls = tempfile.SpooledTemporaryFile

        class _Tracking(real_cls):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                created.append(self)

        monkeypatch.setattr(uploads_module.tempfile, "SpooledTemporaryFile", _Tracking)

        body = b"x" * 200_000
        upload = _RecordingUpload(body)
        with pytest.raises(UploadTooLarge):
            asyncio.run(read_capped_to_tempfile(upload, 100_000, spool_max_size=100))
        assert len(created) == 1
        assert created[0].closed


class TestUploadTooLarge:
    """Tests for UploadTooLarge."""

    def test_public_message_reports_limit_in_mb(self) -> None:
        err = UploadTooLarge(50 * 1024 * 1024)
        assert err.limit_bytes == 50 * 1024 * 1024
        assert err.public_message == "File exceeds 50 MB upload limit"
