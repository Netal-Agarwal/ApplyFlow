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
from urllib.robotparser import RobotFileParser

import requests


class FetchError(RuntimeError):
    def __init__(self, url: str, message: str, status: int | None = None, headers: Any = None):
        super().__init__(f"{url}: {message}")
        self.url = url
        self.status = status
        self.headers = headers or {}


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
        self._robots: dict[str, RobotFileParser] = {}

    def _wait_for_host(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            remaining = self.min_interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = self._clock()

    def _get(self, url: str, params: dict[str, Any] | None, headers: dict[str, str] | None = None,
             max_retries: int | None = None) -> Any:
        host = urlparse(url).netloc
        retries = self.max_retries if max_retries is None else max_retries
        attempt = 0
        while True:
            self._wait_for_host(host)
            try:
                response = self.session.get(url, params=params, timeout=self.timeout, headers=headers)
            except requests.RequestException as exc:
                if attempt >= retries:
                    raise FetchError(url, f"request failed: {exc}") from exc
                attempt += 1
                self._sleep(2 ** attempt)
                continue

            status = response.status_code
            if status == 200:
                return response
            if status in (429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                self._sleep(min(delay, 60))
                continue
            raise FetchError(url, f"HTTP {status}", status, response.headers)

    def get_api(self, url: str, params: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None) -> tuple[Any, Any]:
        """One attempt at a JSON API that reports its own rate limit: returns (data, headers).

        No automatic retry: APIs like GitHub's ask clients not to retry before the reset time,
        so the caller reads the headers (or FetchError.headers) and decides.
        """
        response = self._get(url, params, headers, max_retries=0)
        try:
            return response.json(), response.headers
        except ValueError as exc:
            raise FetchError(url, "response was not JSON", response.status_code) from exc

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = self._get(url, params)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(url, "response was not JSON", response.status_code) from exc

    # -- web pages (company websites) -----------------------------------------
    # Job-board APIs are built for programmatic access. Company websites are not,
    # so page fetches check robots.txt first and use a single attempt.

    def allowed_by_robots(self, url: str) -> bool:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = RobotFileParser()
            try:
                response = self._get(f"{origin}/robots.txt", None, {"Accept": "text/plain"}, max_retries=0)
                parser.parse(response.text.splitlines())
            except FetchError as exc:
                # No robots.txt (404) means everything is allowed; other failures mean we can't tell.
                parser.parse([] if exc.status in (404, 410) else ["User-agent: *", "Disallow: /"])
            self._robots[origin] = parser
        return parser.can_fetch(self.session.headers.get("User-Agent", "*"), url)

    def get_page(self, url: str) -> str:
        """Fetch an HTML page, honouring robots.txt. Raises FetchError if disallowed."""
        if not self.allowed_by_robots(url):
            raise FetchError(url, "disallowed by robots.txt")
        response = self._get(url, None, {"Accept": "text/html"}, max_retries=0)
        return decode_page(response)[:2_000_000]


def decode_page(response: Any) -> str:
    """The page's text. Without a charset in Content-Type, requests assumes ISO-8859-1, which turns
    UTF-8 pages into mojibake ("PrimeVault â ..."): try UTF-8 first in that case."""
    content_type = str((response.headers or {}).get("Content-Type", "")).lower()
    raw = getattr(response, "content", None)
    if "charset" in content_type or not isinstance(raw, bytes):
        return response.text
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(getattr(response, "apparent_encoding", None) or "latin-1", errors="replace")

