"""Hacker News "Ask HN: Who is hiring?" threads, via the free HN Algolia API.

Docs: https://hn.algolia.com/api

Each top-level comment is one company's post. By convention the first line is a
pipe-separated header, e.g.

    Acme | Backend Engineer, Interns | Remote (Worldwide) | Full-time | https://acme.dev

Many posts ask applicants to email someone directly, which is why this source
matters for outreach.
"""

from __future__ import annotations

import re
from typing import Any

from applyflow.http import PoliteClient
from applyflow.models import Job
from applyflow.text import extract_emails, html_to_text

BASE = "https://hn.algolia.com/api/v1"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"

_ROLE_WORDS = re.compile(
    r"\b(engineer|engineers|engineering|developer|developers|dev|swe|sde|intern|interns|internship|"
    r"programmer|architect|scientist|devops|sre|full[\s-]?stack|backend|back[\s-]end|frontend|"
    r"roles?|positions?|hiring|graduate|new grad|founding|mts|technical staff)\b",
    re.I,
)
_EMPLOYMENT_WORDS = re.compile(
    r"^\s*(full[\s-]?time|part[\s-]?time|ft|pt|contract|contractor|freelance|permanent|"
    r"ft\s*/\s*pt|full[\s-]?time\s*/\s*contract)\s*$",
    re.I,
)
_SALARY = re.compile(r"[$€£₹]\s?\d|\d+\s?k\b|\bequity\b|\bsalary\b|\blpa\b", re.I)
_URL = re.compile(r"https?://\S+|\bwww\.\S+|\b[\w-]+\.(?:com|io|ai|dev|co|org|net|app)\b", re.I)


def parse_header(first_line: str) -> dict[str, Any]:
    segments = [s.strip() for s in first_line.split("|") if s.strip()]
    if len(segments) < 2:
        return {}
    company = re.sub(r"\s*\(.*?\)\s*$", "", segments[0]).strip() or segments[0]
    roles, locations = [], []
    for seg in segments[1:]:
        if _URL.fullmatch(seg) or seg.lower().startswith("http"):
            continue
        if _EMPLOYMENT_WORDS.match(seg) or _SALARY.search(seg):
            continue
        if _ROLE_WORDS.search(seg) and not re.search(r"\b(remote|onsite|on-site|hybrid)\b", seg, re.I):
            roles.append(seg)
        else:
            locations.append(seg)
    return {"company": company, "roles": roles, "locations": locations}


class HNWhoIsHiringCollector:
    source = "hn"

    def __init__(self, client: PoliteClient, max_pages: int = 10):
        self.client = client
        self.max_pages = max_pages

    def latest_story(self) -> dict[str, Any] | None:
        data = self.client.get_json(
            f"{BASE}/search_by_date",
            params={"tags": "story,author_whoishiring", "hitsPerPage": 10},
        )
        for hit in data.get("hits", []):
            if (hit.get("title") or "").lower().startswith("ask hn: who is hiring"):
                return hit
        return None

    def fetch(self, story_id: str | None = None) -> list[Job]:
        story_title = ""
        if story_id is None:
            story = self.latest_story()
            if story is None:
                return []
            story_id = str(story["objectID"])
            story_title = story.get("title", "")

        hits: list[dict[str, Any]] = []
        page = 0
        while page < self.max_pages:
            data = self.client.get_json(
                f"{BASE}/search",
                params={"tags": f"comment,story_{story_id}", "hitsPerPage": 1000, "page": page},
            )
            hits.extend(data.get("hits", []))
            page += 1
            if page >= int(data.get("nbPages", 1) or 1):
                break
        return self.parse(hits, story_id, story_title)

    @staticmethod
    def parse(hits: list[dict[str, Any]], story_id: str, story_title: str = "") -> list[Job]:
        jobs = []
        for hit in hits:
            # Only top-level comments are job posts; replies are discussion.
            if str(hit.get("parent_id")) != str(story_id):
                continue
            text = html_to_text(hit.get("comment_text"))
            if not text:
                continue
            first_line = text.split("\n", 1)[0]
            header = parse_header(first_line)
            if not header:
                continue
            title = "; ".join(header["roles"]) or "(see post)"
            locations = header["locations"]
            jobs.append(
                Job(
                    source="hn",
                    external_id=str(hit["objectID"]),
                    company=header["company"],
                    title=title,
                    url=ITEM_URL.format(id=hit["objectID"]),
                    description=text,
                    locations=locations,
                    remote=True if re.search(r"\bremote\b", first_line, re.I) else None,
                    posted_at=hit.get("created_at") or "",
                    emails=extract_emails(text),
                    department=story_title,
                    poster=str(hit.get("author") or ""),
                )
            )
        return jobs
