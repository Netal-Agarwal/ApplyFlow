"""Startup discovery: find hiring companies, then find their job boards.

Source: YC's company directory, via the community-maintained export at
https://yc-oss.github.io/api/ (refreshed daily from YC's public directory).
`hiring.json` lists only companies currently marked as hiring.

For each company that passes the filters (active, team size, region), we look
for its job board on its own website (see detect.py), falling back to a few
slug guesses. Results are stored, so a company is only re-checked after
`recheck_days`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from applyflow.collectors import ATS_COLLECTORS
from applyflow.config import Profile
from applyflow.detect import detect_board
from applyflow.http import FetchError
from applyflow.models import Company, DiscoveredCompany

YC_HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"

Log = Callable[[str], None]


def _matches_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(n.lower())}(?![a-z])", lowered) for n in needles)


def yc_candidates(data: list[dict[str, Any]], profile: Profile) -> list[DiscoveredCompany]:
    """Filter the YC directory down to companies worth checking."""
    cfg = profile.discovery.get("yc", {})
    min_size = int(cfg.get("min_team_size", 2))
    max_size = int(cfg.get("max_team_size", 1000))
    statuses = {s.lower() for s in cfg.get("statuses", ["Active"])}
    regions_any = cfg.get("regions_any", ["India", "Remote"])

    out = []
    for raw in data:
        if raw.get("isHiring") is False:
            continue
        if str(raw.get("status", "")).lower() not in statuses:
            continue
        size = raw.get("team_size")
        if isinstance(size, int) and not (min_size <= size <= max_size):
            continue
        place = " ; ".join([str(raw.get("all_locations") or "")] + [str(r) for r in raw.get("regions") or []])
        if regions_any and not _matches_any(place, regions_any):
            continue
        if not raw.get("website"):
            continue
        out.append(
            DiscoveredCompany(
                key=f"yc:{raw.get('slug') or raw.get('id')}",
                name=str(raw.get("name") or "").strip(),
                website=str(raw["website"]).strip(),
                source="yc",
                team_size=size if isinstance(size, int) else None,
                description=str(raw.get("one_liner") or "").strip(),
                locations=str(raw.get("all_locations") or ""),
                batch=str(raw.get("batch") or ""),
            )
        )
    # India first, then smaller teams first: that's where cold outreach works best.
    out.sort(key=lambda c: (0 if "india" in c.locations.lower() else 1, c.team_size or 10_000))
    return out


def slug_guesses(name: str, website: str) -> list[str]:
    host = urlparse(website if "://" in website else f"https://{website}").netloc.lower()
    host = host.removeprefix("www.")
    stem = host.split(".")[0] if host else ""
    words = re.findall(r"[a-z0-9]+", name.lower())
    guesses = [stem, "".join(words), "-".join(words)]
    seen, ordered = set(), []
    for g in guesses:
        if g and g not in seen:
            seen.add(g)
            ordered.append(g)
    return ordered[:3]


def guess_board(client, name: str, website: str) -> tuple[str, str, int] | None:
    """Try a few slugs on each supported ATS. Returns (ats, slug, job_count) for the biggest hit."""
    best = None
    for slug in slug_guesses(name, website):
        for ats, collector_cls in ATS_COLLECTORS.items():
            try:
                jobs = collector_cls(client).fetch(Company(name=name, ats=ats, slug=slug))
            except (FetchError, KeyError, TypeError, ValueError):
                continue
            # Ashby and Workable answer unknown boards with an empty list, so require jobs there.
            if not jobs and ats in ("ashby", "workable"):
                continue
            if best is None or len(jobs) > best[2]:
                best = (ats, slug, len(jobs))
        if best:
            return best
    return None


@dataclass
class DiscoverySummary:
    candidates: int = 0
    checked: int = 0
    skipped_recent: int = 0
    found: list[DiscoveredCompany] = field(default_factory=list)
    unsupported: int = 0
    none: int = 0


def check_company(client, company: DiscoveredCompany) -> DiscoveredCompany:
    """Fill in ats/slug/board_status for one company."""
    match = detect_board(client, company.website)
    if match and match.supported:
        company.ats, company.slug, company.board_status = match.ats, match.slug, "found"
        return company
    guess = guess_board(client, company.name, company.website)
    if guess:
        company.ats, company.slug, company.board_status = guess[0], guess[1], "found"
        return company
    if match:
        company.ats, company.slug, company.board_status = match.ats, "", "unsupported"
    else:
        company.ats, company.slug, company.board_status = "", "", "none"
    return company


def discover_yc(store, client, profile: Profile, limit: int = 50, log: Log = print,
                data: list[dict[str, Any]] | None = None) -> DiscoverySummary:
    if data is None:
        data = client.get_json(YC_HIRING_URL)
    candidates = yc_candidates(data, profile)
    recheck_days = int(profile.discovery.get("recheck_days", 30))
    summary = DiscoverySummary(candidates=len(candidates))
    for company in candidates:
        if summary.checked >= limit:
            break
        if store.recently_checked(company.key, recheck_days):
            summary.skipped_recent += 1
            continue
        summary.checked += 1
        try:
            check_company(client, company)
        except FetchError as exc:
            company.board_status = "error"
            log(f"  {company.name:<30} error: {exc}")
        store.upsert_discovered(company)
        size = f"{company.team_size} people" if company.team_size else "size ?"
        if company.board_status == "found":
            summary.found.append(company)
            log(f"  {company.name:<30} {size:<12} board: {company.ats}:{company.slug}")
        elif company.board_status == "unsupported":
            summary.unsupported += 1
            log(f"  {company.name:<30} {size:<12} uses {company.ats} (no public API - email candidate)")
        elif company.board_status == "none":
            summary.none += 1
            log(f"  {company.name:<30} {size:<12} no board found")
    return summary
