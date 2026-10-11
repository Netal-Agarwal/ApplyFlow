"""A small, polite HTTP client.

Rules every collector follows:
  * identify ourselves honestly in the User-Agent
  * at most one request per host per `min_interval` seconds
  * back off and retry on 429 / 5xx, honouring Retry-After
  * short timeouts, so one slow board can't stall a whole run
"""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests


class FetchError(RuntimeError):
    def __init__(self, url: str, message: str, status: int | None = None):
        super().__init__(f"{url}: {message}")
        self.url = url
        self.status = status


class PoliteClient:
    def __init__(
        self,
        user_agent: str,
        min_interval: float = 1.0,
        timeout: float = 20.0,
        max_retries: int = 3,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}

    def _wait_for_host(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            remaining = self.min_interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = self._clock()

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        host = urlparse(url).netloc
        attempt = 0
        while True:
            self._wait_for_host(host)
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise FetchError(url, f"request failed: {exc}") from exc
                attempt += 1
                self._sleep(2 ** attempt)
                continue

            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise FetchError(url, "response was not JSON", status) from exc
            if status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                attempt += 1
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                self._sleep(min(delay, 60))
                continue
            raise FetchError(url, f"HTTP {status}", status)
