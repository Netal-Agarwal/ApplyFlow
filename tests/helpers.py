"""Shared test helpers: fixtures and a fake HTTP client."""

from __future__ import annotations

import json
from pathlib import Path

from applyflow.config import PROFILE_EXAMPLE_PATH, load_profile
from applyflow.http import FetchError

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def example_profile():
    return load_profile(PROFILE_EXAMPLE_PATH)


class FakeClient:
    """Stands in for PoliteClient: maps URL endings to fixture payloads."""

    def __init__(self, routes: dict[str, object], pages: dict[str, object] | None = None):
        self.routes = routes
        self.pages = pages or {}           # exact URL -> HTML (or an exception to raise)
        self.calls: list[tuple[str, dict | None]] = []
        self.page_calls: list[str] = []

    def get_page(self, url: str) -> str:
        self.page_calls.append(url)
        page = self.pages.get(url)
        if page is None:
            raise FetchError(url, "HTTP 404", 404)
        if isinstance(page, Exception):
            raise page
        return page

    def get_json(self, url: str, params: dict | None = None):
        self.calls.append((url, params))
        for fragment, payload in self.routes.items():
            if url.split("?")[0].endswith(fragment):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise FetchError(url, "HTTP 404", 404)


def default_routes() -> dict[str, object]:
    return {
        "boards-api.greenhouse.io/v1/boards/acme/jobs": fixture("greenhouse_acme.json"),
        "api.lever.co/v0/postings/zeta": fixture("lever_zeta.json"),
        "api.ashbyhq.com/posting-api/job-board/nimbus": fixture("ashby_nimbus.json"),
        "hn.algolia.com/api/v1/search_by_date": fixture("hn_stories.json"),
        "hn.algolia.com/api/v1/search": fixture("hn_comments.json"),
    }
