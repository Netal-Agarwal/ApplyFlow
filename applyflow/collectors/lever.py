"""Lever public postings API.

Docs: https://github.com/lever/postings-api
GET https://api.lever.co/v0/postings/{slug}?mode=json
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from applyflow.http import PoliteClient
from applyflow.models import Company, Job
from applyflow.text import extract_contact_emails, html_to_text

BASE = "https://api.lever.co/v0/postings"


class LeverCollector:
    ats = "lever"

    def __init__(self, client: PoliteClient):
        self.client = client

    @staticmethod
    def board_url(slug: str) -> str:
        return f"{BASE}/{slug}"

    def fetch(self, company: Company) -> list[Job]:
        data = self.client.get_json(self.board_url(company.slug), params={"mode": "json"})
        return self.parse(data, company)

    @staticmethod
    def parse(data: list[dict[str, Any]], company: Company) -> list[Job]:
        board_size = len(data or [])
        jobs = []
        for raw in data or []:
            cats = raw.get("categories") or {}
            locations = [loc for loc in (cats.get("allLocations") or []) if loc]
            if not locations and cats.get("location"):
                locations = [cats["location"]]

            parts = [raw.get("descriptionPlain") or ""]
            for item in raw.get("lists") or []:
                parts.append(item.get("text") or "")
                parts.append(html_to_text(item.get("content")))
            parts.append(raw.get("additionalPlain") or "")
            description = "\n".join(p.strip() for p in parts if p and p.strip())

            workplace = (raw.get("workplaceType") or "").lower()
            remote = True if workplace == "remote" else (False if workplace in ("on-site", "onsite") else None)
            if remote is None and any("remote" in loc.lower() for loc in locations):
                remote = True

            created = raw.get("createdAt")
            posted_at = (
                datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
                if isinstance(created, (int, float))
                else ""
            )
            department = " / ".join(x for x in (cats.get("department"), cats.get("team")) if x)

            jobs.append(
                Job(
                    source="lever",
                    external_id=str(raw["id"]),
                    company=company.name,
                    title=(raw.get("text") or "").strip(),
                    url=raw.get("hostedUrl") or "",
                    apply_url=raw.get("applyUrl") or raw.get("hostedUrl") or "",
                    description=description,
                    locations=locations,
                    remote=remote,
                    employment_type=cats.get("commitment") or "",
                    department=department,
                    posted_at=posted_at,
                    emails=extract_contact_emails(description),
                    board_size=board_size,
                )
            )
        return jobs
