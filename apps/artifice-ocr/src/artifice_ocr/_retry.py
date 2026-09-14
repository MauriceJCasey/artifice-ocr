# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Retry decorator with exponential backoff for transient API failures."""

import time
import functools
from typing import Type

from artifice_ocr._logging import get_logger

log = get_logger("retry")

_DEFAULT_RETRYABLE: tuple[Type[Exception], ...] = (ConnectionError, TimeoutError)


def retry(
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple[Type[Exception], ...] = _DEFAULT_RETRYABLE,
    label: str = "",
):
    """Decorator that retries a function on transient failures.

    Uses exponential backoff: delay = min(base_delay * 2^attempt, max_delay).

    Args:
        max_attempts: Total number of attempts (1 = no retry, 4 = up to 3 retries).
        base_delay: Initial delay in seconds before first retry.
        max_delay: Cap on backoff delay.
        retryable_exceptions: Exception types that trigger a retry.
        label: Label for log messages (defaults to function name).
    """

    def decorator(fn):
        fn_label = label or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        log.error(
                            "%s failed after %d attempts: %s",
                            fn_label, max_attempts, exc,
                        )
                        raise
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    log.warning(
                        "%s attempt %d/%d failed (%s), retrying in %.1fs...",
                        fn_label, attempt, max_attempts, exc.__class__.__name__, delay,
                    )
                    time.sleep(delay)
            raise last_exc  # unreachable, but keeps type checkers happy

        return wrapper

    return decorator
