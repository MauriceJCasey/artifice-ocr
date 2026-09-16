# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Retry-logic tests (planned to move to packages/model-harness in a later phase)."""


# ---------------------------------------------------------------------------
# P3: Retry logic tests
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_first_attempt():
    from artifice_ocr._retry import retry

    call_count = 0

    @retry(max_attempts=3, base_delay=0.01, label="test")
    def succeed():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = succeed()
    assert result == "ok"
    assert call_count == 1


def test_retry_retries_on_failure_then_succeeds():
    from artifice_ocr._retry import retry

    call_count = 0

    @retry(max_attempts=3, base_delay=0.01, label="test")
    def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("transient")
        return "recovered"

    result = flaky()
    assert result == "recovered"
    assert call_count == 3


def test_retry_raises_after_max_attempts():
    from artifice_ocr._retry import retry

    @retry(max_attempts=2, base_delay=0.01, label="test")
    def always_fail():
        raise ConnectionError("permanent")

    try:
        always_fail()
        assert False, "Should have raised"
    except ConnectionError:
        pass


def test_retry_ignores_non_retryable_exceptions():
    from artifice_ocr._retry import retry

    @retry(max_attempts=3, base_delay=0.01, label="test")
    def value_error():
        raise ValueError("not retryable")

    try:
        value_error()
        assert False, "Should have raised"
    except ValueError:
        pass
