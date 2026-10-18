"""Startup discovery: find hiring companies, then find their job boards.

Source: YC's company directory, via the community-maintained export at
https://yc-oss.github.io/api/ (refreshed daily from YC's public directory).
`hiring.json` lists only companies currently marked as hiring.

For each company that passes the filters (active, team size, region), we look
for its job board on its own website (see detect.py), falling back to a few
slug guesses. A guessed board is only trusted when the board itself points back
to the company (its website or name); otherwise it is saved as "unverified" and
not collected. Results are stored, so a company is only re-checked after
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
from applyflow.models import Company, DiscoveredCompany, Job

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
    # "Remote" alone doesn't qualify: US startups use it to mean remote within the US.
    regions_any = cfg.get("regions_any", ["India"])
    low, high = (int(x) for x in cfg.get("preferred_team_size", [10, 300]))

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
                # YC's coarser regions stand in when a company lists no locations.
                locations=str(raw.get("all_locations") or "") or " ; ".join(str(r) for r in raw.get("regions") or []),
                batch=str(raw.get("batch") or ""),
            )
        )
    # India-located first; then teams in the preferred size range (big enough to take
    # interns, small enough that a cold email reaches a decision-maker), then other known
    # sizes, then unknown. Smallest first within each group.
    def priority(c: DiscoveredCompany) -> tuple[int, int, int]:
        size = c.team_size
        band = 2 if size is None else (0 if low <= size <= high else 1)
        return (0 if _matches_any(c.locations, ["India"]) else 1, band, size or 0)

    out.sort(key=priority)
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


def guess_boards(client, name: str, website: str) -> list[tuple[str, str, list[Job]]]:
    """Boards that exist under a few guessed slugs, biggest first: [(ats, slug, jobs)].

    Jobs are fetched without a company name, so Greenhouse and Workable report their own.
    """
    hits = []
    for slug in slug_guesses(name, website):
        for ats, collector_cls in ATS_COLLECTORS.items():
            try:
                jobs = collector_cls(client).fetch(Company(name="", ats=ats, slug=slug))
            except (FetchError, KeyError, TypeError, ValueError):
                continue
            # Ashby and Workable answer unknown boards with an empty list, so require jobs there.
            if not jobs and ats in ("ashby", "workable"):
                continue
            hits.append((ats, slug, jobs))
    return sorted(hits, key=lambda h: -len(h[2]))


# ---------------------------------------------------------------------------
# Verifying a guessed board: slugs like "bloom" or "asimov" are shared by many
# companies, so a board that exists under a guessed slug may be someone else's.
# ---------------------------------------------------------------------------

_LEVER_SITE = re.compile(
    r'class="main-(?:header-logo|footer-text)"[^>]*>\s*(?:<p>\s*)?<a[^>]+href="(https?://[^"]+)"'
    r'|<a[^>]+class="main-header-logo"[^>]+href="(https?://[^"]+)"', re.I)
_ASHBY_ORG = re.compile(r'"organization"\s*:\s*\{(.*?)\}', re.S)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
_ATS_HOSTS = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|linkedin\.com|twitter\.com|x\.com|"
                        r"facebook\.com|instagram\.com|youtube\.com|glassdoor\.", re.I)
_NAME_NOISE = {"inc", "ltd", "llc", "labs", "ai", "technologies", "technology", "tech", "the", "co", "hq", "job",
               "jobs", "board", "careers", "pvt", "private", "limited", "software", "formerly", "io"}


def site_domain(url: str) -> str:
    """'https://www.Acme.ai/about' -> 'acme.ai'; '' when there is no real domain."""
    if not url:
        return ""
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower().split(":")[0].removeprefix("www.")
    return host if "." in host else ""


def same_site(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def name_key(name: str) -> str:
    """'Weekday AI' and 'Weekday' -> 'weekday'; 'Brumby (Formerly GrazeMate)' -> 'brumby'."""
    name = re.sub(r"\(.*?\)", " ", name.lower())
    return "".join(w for w in re.findall(r"[a-z0-9]+", name) if w not in _NAME_NOISE)


def _first_external_url(text: str) -> str:
    return next((u for u in _URL.findall(text or "") if not _ATS_HOSTS.search(u)), "")


def board_identity(client, ats: str, slug: str) -> tuple[str, str]:
    """(company name, company website) as the board itself states them; '' when it doesn't say."""
    try:
        if ats == "lever":
            html = client.get_page(f"https://jobs.lever.co/{slug}")
            # The header logo often links back to the board itself; the footer has the home page.
            links = [g for m in _LEVER_SITE.finditer(html) for g in m.groups() if g]
            title = re.search(r"<title>([^<]*)</title>", html, re.I)
            return (title.group(1).strip() if title else ""), next((u for u in links if not _ATS_HOSTS.search(u)), "")
        if ats == "ashby":
            html = client.get_page(f"https://jobs.ashbyhq.com/{slug}")
            org = _ASHBY_ORG.search(html)
            if not org:
                return "", ""
            name = re.search(r'"name"\s*:\s*"([^"]*)"', org.group(1))
            site = re.search(r'"publicWebsite"\s*:\s*"([^"]*)"', org.group(1))
            return (name.group(1) if name else ""), (site.group(1) if site else "")
        if ats == "greenhouse":
            data = client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            return str(data.get("name") or ""), _first_external_url(str(data.get("content") or ""))
        if ats == "workable":
            data = client.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
            return str(data.get("name") or ""), _first_external_url(str(data.get("description") or ""))
    except (FetchError, AttributeError, TypeError, ValueError):
        pass
    return "", ""


def verify_board(client, company: DiscoveredCompany, ats: str, slug: str, jobs: list[Job]) -> tuple[bool, str]:
    """Is this guessed board really this company's? Returns (verified, reason)."""
    own_site = site_domain(company.website)
    board_name, board_site = board_identity(client, ats, slug)
    board_site = site_domain(board_site)
    text = " ".join(f"{j.url} {j.apply_url} {j.description}" for j in jobs).lower()
    if own_site and board_site:          # the board says whose it is: that settles it
        if same_site(own_site, board_site):
            return True, f"board links to {board_site}"
        # ...unless the company renamed itself: "Fam (previously FamPay)" on famapp.in.
        bare = re.sub(r"\(.*?\)", "", company.name).strip().lower()
        if bare and re.search(rf"\b(previously|formerly)\s+(known\s+as\s+)?{re.escape(bare)}(?![a-z0-9])", text):
            return True, f"board links to {board_site}; postings say 'previously {company.name}' (renamed)"
        return False, f"{ats}:{slug} belongs to {board_site}, not {own_site}"
    key = name_key(company.name)
    for other in [board_name] + sorted({j.company for j in jobs if j.company}):
        if len(key) >= 3 and name_key(other) == key:
            return True, f"board name '{other}' matches"
    if own_site and own_site in text:
        return True, f"postings mention {own_site}"
    if len(key) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(company.name.lower())}(?![a-z0-9])", text):
        return True, f"postings mention '{company.name}'"
    if not jobs:
        return False, f"{ats}:{slug} has no postings and doesn't say whose board it is"
    return False, f"no evidence {ats}:{slug} is {company.name}'s (no matching website or name)"


@dataclass
class DiscoverySummary:
    candidates: int = 0
    checked: int = 0
    skipped_recent: int = 0
    found: list[DiscoveredCompany] = field(default_factory=list)
    unverified: list[DiscoveredCompany] = field(default_factory=list)
    unsupported: int = 0
    none: int = 0


def _set(company: DiscoveredCompany, ats: str, slug: str, status: str, detected_from: str = "",
         reason: str = "") -> DiscoveredCompany:
    company.ats, company.slug, company.board_status = ats, slug, status
    company.detected_from, company.status_reason = detected_from, reason
    return company


def check_company(client, company: DiscoveredCompany) -> DiscoveredCompany:
    """Fill in ats/slug/board_status for one company.

    A board linked from the company's own website is trusted. A board found by guessing a
    slug is only saved as found when verify_board finds evidence it's the same company.
    """
    match = detect_board(client, company.website)
    if match and match.supported:
        return _set(company, match.ats, match.slug, "found", "website", f"linked from {match.found_on}")
    rejected = None
    for ats, slug, jobs in guess_boards(client, company.name, company.website):
        ok, reason = verify_board(client, company, ats, slug, jobs)
        if ok:
            return _set(company, ats, slug, "found", "slug_guess", reason)
        rejected = rejected or (ats, slug, reason)
    if match:                             # e.g. Keka: a real board, just not one we can read
        return _set(company, match.ats, "", "unsupported", "website", f"linked from {match.found_on}")
    if rejected:
        return _set(company, rejected[0], rejected[1], "unverified", "slug_guess", rejected[2])
    return _set(company, "", "", "none")


def verify_saved_boards(store, client, log: Log = print) -> tuple[list[DiscoveredCompany], list[DiscoveredCompany]]:
    """Re-check every saved board; demote the ones that aren't the company's.

    Boards saved before verification existed don't record how they were found, so those
    are looked for on the company's website first. Unverified boards are re-checked too,
    and promoted when they now pass. Returns (demoted, promoted); demoted boards' jobs are
    deactivated so they leave the report, promoted ones are collected on the next `run`.
    """
    demoted, promoted = [], []
    for company in store.companies_with_board(("found", "unverified")):
        was = company.board_status
        if company.detected_from != "website" or was == "unverified":
            match = detect_board(client, company.website)
            if match and match.supported and (match.ats, match.slug.lower()) == (company.ats, company.slug.lower()):
                company.detected_from, company.status_reason = "website", f"linked from {match.found_on}"
            else:
                try:
                    jobs = ATS_COLLECTORS[company.ats](client).fetch(Company(name="", ats=company.ats, slug=company.slug))
                except (FetchError, KeyError, TypeError, ValueError):
                    jobs = []
                ok, reason = verify_board(client, company, company.ats, company.slug, jobs)
                company.detected_from, company.status_reason = "slug_guess", reason
                company.board_status = "found" if ok else "unverified"
        store.set_board_status(company)
        board = f"{company.ats}:{company.slug}"
        if company.board_status == "unverified":
            closed = store.deactivate_board(board)
            if was == "found":
                demoted.append(company)
                log(f"  {company.name:<30} {board:<30} DEMOTED ({closed} jobs hidden): {company.status_reason}")
            else:
                log(f"  {company.name:<30} {board:<30} still unverified: {company.status_reason}")
        elif was == "unverified":
            promoted.append(company)
            log(f"  {company.name:<30} {board:<30} PROMOTED: {company.status_reason}")
        else:
            log(f"  {company.name:<30} {board:<30} ok: {company.status_reason}")
    return demoted, promoted


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
            log(f"  {company.name:<30} {size:<12} board: {company.ats}:{company.slug} ({company.status_reason})")
        elif company.board_status == "unverified":
            summary.unverified.append(company)
            log(f"  {company.name:<30} {size:<12} unverified: {company.status_reason}")
        elif company.board_status == "unsupported":
            summary.unsupported += 1
            log(f"  {company.name:<30} {size:<12} uses {company.ats} (no public API - email candidate)")
        elif company.board_status == "none":
            summary.none += 1
            log(f"  {company.name:<30} {size:<12} no board found")
    return summary
