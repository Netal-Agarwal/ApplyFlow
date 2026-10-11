"""Ashby public job posting API.

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
GET https://api.ashbyhq.com/posting-api/job-board/{slug}
"""

from __future__ import annotations

from typing import Any

from applyflow.http import PoliteClient
from applyflow.models import Company, Job
from applyflow.text import extract_emails, html_to_text

BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyCollector:
    ats = "ashby"

    def __init__(self, client: PoliteClient):
        self.client = client

    @staticmethod
    def board_url(slug: str) -> str:
        return f"{BASE}/{slug}"

    def fetch(self, company: Company) -> list[Job]:
        data = self.client.get_json(self.board_url(company.slug))
        return self.parse(data, company)

    @staticmethod
    def parse(data: dict[str, Any], company: Company) -> list[Job]:
        raw_jobs = [j for j in (data.get("jobs") or []) if j.get("isListed", True)]
        board_size = len(raw_jobs)
        jobs = []
        for raw in raw_jobs:
            locations = [raw.get("location") or ""]
            for sec in raw.get("secondaryLocations") or []:
                if sec.get("location"):
                    locations.append(sec["location"])
            locations = [loc.strip() for loc in locations if loc and loc.strip()]

            description = raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml"))
            workplace = (raw.get("workplaceType") or "").lower()
            if raw.get("isRemote") is True or workplace == "remote":
                remote: bool | None = True
            elif workplace == "onsite":
                remote = False
            else:
                remote = None

            department = " / ".join(x for x in (raw.get("department"), raw.get("team")) if x)
            jobs.append(
                Job(
                    source="ashby",
                    external_id=str(raw["id"]),
                    company=company.name,
                    title=(raw.get("title") or "").strip(),
                    url=raw.get("jobUrl") or "",
                    apply_url=raw.get("applyUrl") or raw.get("jobUrl") or "",
                    description=description.strip(),
                    locations=locations,
                    remote=remote,
                    employment_type=raw.get("employmentType") or "",
                    department=department,
                    posted_at=raw.get("publishedAt") or "",
                    emails=extract_emails(description),
                    board_size=board_size,
                )
            )
        return jobs
