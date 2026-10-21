"""Retries for transient source errors: timeouts, dropped connections, 429 and 5xx.

Three tries with exponential backoff and jitter. Anything else (404, bad data) fails at
once, so a broken source surfaces in sync_runs instead of being retried into silence.
"""

import random
import time
from collections.abc import Callable
from typing import Any

import httpx
import requests

TRIES = 3
BASE_DELAY = 1.0  # seconds; doubles each retry, plus up to 50% jitter


def transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError | requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        return status == 429 or status >= 500
    return isinstance(
        exc, httpx.TimeoutException | httpx.TransportError | requests.ConnectionError
        | requests.Timeout,
    )  # fmt: skip


def retry[T](fn: Callable[[], T], sleep: Callable[[float], None] = time.sleep) -> T:
    for attempt in range(TRIES):
        try:
            return fn()
        except Exception as exc:
            if attempt == TRIES - 1 or not transient(exc):
                raise
            sleep(BASE_DELAY * 2**attempt * (1 + random.random() / 2))
    raise AssertionError("unreachable")


class Retrying:
    """Wraps a loader module (nflreadpy) so every `load_*` call is retried."""

    def __init__(self, module: Any) -> None:
        self._module = module

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self._module, name)
        if not name.startswith("load_") or not callable(fn):
            return fn
        return lambda *a, **kw: retry(lambda: fn(*a, **kw))
