"""Workable public job widget API.

GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true

Workable is common among smaller startups, including many in India.
"""

from __future__ import annotations

from typing import Any

from applyflow.http import PoliteClient
from applyflow.models import Company, Job
from applyflow.text import extract_emails, html_to_text

BASE = "https://apply.workable.com/api/v1/widget/accounts"


def _location_string(loc: dict[str, Any]) -> str:
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    return ", ".join(p for p in parts if p)


class WorkableCollector:
    ats = "workable"

    def __init__(self, client: PoliteClient):
        self.client = client

    @staticmethod
    def board_url(slug: str) -> str:
        return f"{BASE}/{slug}"

    def fetch(self, company: Company) -> list[Job]:
        data = self.client.get_json(self.board_url(company.slug), params={"details": "true"})
        return self.parse(data, company)

    @staticmethod
    def parse(data: dict[str, Any], company: Company) -> list[Job]:
        raw_jobs = data.get("jobs") or []
        board_size = len(raw_jobs)
        jobs = []
        for raw in raw_jobs:
            locations = [_location_string(loc) for loc in raw.get("locations") or [] if not loc.get("hidden")]
            if not locations:
                single = _location_string({"city": raw.get("city"), "region": raw.get("state"), "country": raw.get("country")})
                locations = [single] if single else []
            remote = True if raw.get("telecommuting") else None
            if remote:
                locations = [f"Remote ({loc})" if loc else "Remote" for loc in locations] or ["Remote"]
            description = html_to_text(raw.get("description"))
            if raw.get("experience"):
                description = f"{description}\nExperience: {raw['experience']}"
            jobs.append(
                Job(
                    source="workable",
                    external_id=str(raw.get("shortcode") or raw.get("url")),
                    company=data.get("name") or company.name,
                    title=(raw.get("title") or "").strip(),
                    url=raw.get("url") or raw.get("shortlink") or "",
                    apply_url=raw.get("application_url") or raw.get("url") or "",
                    description=description,
                    locations=locations,
                    remote=remote,
                    employment_type=raw.get("employment_type") or "",
                    department=raw.get("department") or "",
                    posted_at=raw.get("published_on") or raw.get("created_at") or "",
                    emails=extract_emails(description),
                    board_size=board_size,
                    team_size=company.team_size,
                )
            )
        return jobs
