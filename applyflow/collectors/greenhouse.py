"""Greenhouse public job board API.

Docs: https://developers.greenhouse.io/job-board.html
GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
"""

from __future__ import annotations

from typing import Any

from applyflow.http import PoliteClient
from applyflow.models import Company, Job
from applyflow.text import extract_contact_emails, html_to_text

BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseCollector:
    ats = "greenhouse"

    def __init__(self, client: PoliteClient):
        self.client = client

    @staticmethod
    def board_url(slug: str) -> str:
        return f"{BASE}/{slug}/jobs"

    def fetch(self, company: Company) -> list[Job]:
        data = self.client.get_json(self.board_url(company.slug), params={"content": "true"})
        return self.parse(data, company)

    @staticmethod
    def parse(data: dict[str, Any], company: Company) -> list[Job]:
        raw_jobs = data.get("jobs", []) or []
        board_size = len(raw_jobs)
        jobs = []
        for raw in raw_jobs:
            location = ((raw.get("location") or {}).get("name") or "").strip()
            description = html_to_text(raw.get("content"))
            departments = ", ".join(d.get("name", "") for d in raw.get("departments") or [] if d.get("name"))
            jobs.append(
                Job(
                    source="greenhouse",
                    external_id=str(raw["id"]),
                    company=company.name or raw.get("company_name") or "",   # "Rubrik Job Board" is a board title
                    title=(raw.get("title") or "").strip(),
                    url=raw.get("absolute_url") or "",
                    apply_url=raw.get("absolute_url") or "",
                    description=description,
                    locations=[location] if location else [],
                    remote=True if "remote" in location.lower() else None,
                    department=departments,
                    posted_at=raw.get("first_published") or raw.get("updated_at") or "",
                    emails=extract_contact_emails(description),
                    board_size=board_size,
                )
            )
        return jobs
