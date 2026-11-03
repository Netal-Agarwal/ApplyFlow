"""Phase 2h: the direct-apply list - roles to apply for by hand at companies ApplyFlow doesn't cold-email.

Who: large companies (over 300 people) with no published hiring inbox and form-only companies (status apply_only),
companies whose hiring inbox bounced or was stopped (Hevo, Pyramidion ...), and stored A/B openings whose contact
hint is "apply via posting". Companies failing the India / tech checks (fit_hold) are left out.

Where the roles come from, all on the company's own pages (robots.txt, the polite client):
  * its ATS board linked from its site (Greenhouse / Lever / Ashby / Workable APIs): every posting, scored;
  * else its careers page: the role titles that fit (`refill.careers_page_roles`), each title's own detail page read
    when the careers page links it (location, years, posted date); a title with no detail page is kept only when it
    says intern / fresher / graduate / trainee / SDE I;
  * stored openings (jobs table) at those companies.

A role fits when its family is backend, cloud, full-stack or a generic SDE title; it's an internship or new-grad
role, or states no experience (score_job rejects anything over profile.max_years_required); and it's in India or
remote and open to India (`refill.KEEP_LOCATIONS`; a careers-page role with no location counts only at an
India-based company and says so). Sorted by fit, then freshness. Nothing here applies or sends anything.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

from applyflow.config import Profile
from applyflow.http import FetchError
from applyflow.models import Job
from applyflow.text import html_to_text

Log = Callable[[str], None]

FIT_FAMILIES = {"backend": 0, "cloud": 0, "sde_generic": 1, "fullstack": 2}
FRESHER_TITLE = re.compile(r"\bintern(?:ship)?\b|\bfresher|\bgraduate\b|\btrainee\b|\bentry[- ]level\b|"
                           r"\bsde[ -]?(?:i|1)\b|\bjunior\b|\bjr\.?\b|\bassociate software\b|\bcampus\b", re.I)
# The page read as a careers page must be one ("/companies-where-our-students-got-jobs" lists alumni, not openings).
CAREERS_PATH = re.compile(r"careers?(?:[/._-]|$)|/jobs?(?:[/._-]|$)|join|openings?|vacanc|work-with|hiring|/positions",
                          re.I)
FIELDS = ["company", "role_title", "location", "apply_link", "posted", "fit_notes", "source", "why_direct"]


@dataclass
class Row:
    company: str
    role_title: str
    location: str
    apply_link: str
    posted: str
    fit_notes: str
    source: str
    why_direct: str
    rank: tuple = ()

    def as_dict(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in FIELDS}


def role_fit(result, india_company: bool, title: str = "") -> str:
    """'' when a scored role fits the direct-apply rules, else why not."""
    if result.tier == "rejected":                # tier skip is fine: a role stating no experience scores below B
        return (result.reasons or ["rejected"])[-1]
    if result.family not in FIT_FAMILIES:
        return f"family {result.family} (not backend / full-stack / cloud)"
    if result.track not in ("internship", "new_grad") and result.experience_years not in (None, 0):
        return f"asks for {result.experience_years} year(s)"
    from applyflow.refill import KEEP_LOCATIONS
    if result.location_class not in KEEP_LOCATIONS and not (result.location_class == "unknown" and india_company):
        return f"location {result.location_class}"
    return ""


def fit_notes(result, extra: str = "") -> str:
    track = {"internship": "internship", "new_grad": "new grad"}.get(result.track, "no stated experience")
    skills = ", ".join(result.matched_skills[:5])
    parts = [f"{result.family} ({track})", f"tier {result.tier}, score {result.score}"]
    if skills:
        parts.append(f"skills: {skills}")
    if extra:
        parts.append(extra)
    return "; ".join(parts)


def rank_key(result, posted: str) -> tuple:
    """Fit first (tier, family, fresher track), then freshness (newest first; undated last)."""
    tier = {"A": 0, "B": 1}.get(result.tier, 2)
    track = 0 if result.track in ("internship", "new_grad") else 1
    days = -int(posted[:10].replace("-", "")) if re.match(r"\d{4}-\d{2}-\d{2}", posted or "") else 0
    return (tier, track, FIT_FAMILIES.get(result.family, 3), days == 0, days, -result.score)


_A = re.compile(r"""<a\b[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.S | re.I)
_POSTED = re.compile(r"(?:posted|published|date posted)\s*(?:on|:)?\s*([0-3]?\d[ -][A-Za-z]{3,9}[ -,]+20\d\d|"
                     r"[A-Za-z]{3,9} [0-3]?\d,? 20\d\d|20\d\d-\d\d-\d\d)", re.I)


def posted_date(text: str) -> str:
    """An ISO date from 'Posted on 12 Oct 2026' / 'Posted: October 12, 2026' / '2026-10-12', else ''."""
    from datetime import datetime
    m = _POSTED.search(text or "")
    if not m:
        return ""
    raw = re.sub(r"[ ,-]+", " ", m.group(1)).strip()
    for fmt in ("%Y %m %d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def detail_links(html: str, base: str, titles: list[str]) -> dict[str, str]:
    """Each careers-page title's own link on the same site (anchor text = the title)."""
    host = urlparse(base).netloc.lower().removeprefix("www.")
    out: dict[str, str] = {}
    for href, inner in _A.findall(html or ""):
        text = " ".join(html_to_text(inner).split())
        url = urljoin(base, href.strip())
        h = urlparse(url).netloc.lower().removeprefix("www.")
        for t in titles:
            if t not in out and t.lower() in text.lower() and (h == host or h.endswith("." + host)) \
                    and url.rstrip("/") != base.rstrip("/"):
                out[t] = url
    return out


def board_rows(client, profile: Profile, company: str, website: str, careers_url: str, india_company: bool,
               why: str, today: date, log: Log = lambda _: None) -> tuple[list[Row], str]:
    """Fitting roles at one company from its ATS board, else its careers page; (rows, what was read)."""
    from applyflow.collectors import ATS_COLLECTORS
    from applyflow.detect import detect_board
    from applyflow.models import Company
    from applyflow.refill import careers_page_roles
    from applyflow.scoring import score_job
    try:
        board = detect_board(client, website, max_pages=3)
    except FetchError as exc:
        return [], f"site unreachable ({exc})"
    rows: list[Row] = []
    if board and board.supported:
        try:
            jobs = ATS_COLLECTORS[board.ats](client).fetch(Company(name=company, ats=board.ats, slug=board.slug))
        except FetchError as exc:
            return [], f"{board.ats}:{board.slug} failed ({exc})"
        for job in jobs:
            r = score_job(job, profile, today)
            if role_fit(r, india_company):
                continue
            posted = (job.posted_at or "")[:10]
            rows.append(Row(company, job.title, job.location_text or "not stated", job.apply_url or job.url, posted,
                            fit_notes(r), f"{board.ats} board", why, rank_key(r, posted)))
        return rows, f"{board.ats}:{board.slug}: {len(jobs)} posting(s), {len(rows)} fit"
    url = careers_url or (board.found_on if board else "")
    if url and not CAREERS_PATH.search(urlparse(url).path):
        return [], f"'{url}' isn't a careers / jobs page"
    if not url:
        return [], f"{board.ats} board (unsupported), no careers page" if board else "no careers page or board"
    try:
        page = client.get_page(url)
    except FetchError as exc:
        return [], f"careers page failed ({exc})"
    text = html_to_text(re.sub(r"<(script|style)\b.*?</\1\s*>", " ", page, flags=re.S | re.I))
    titles, seen = careers_page_roles(text, profile)
    links = detail_links(page, url, titles)
    for title in titles[:6]:
        link = links.get(title, "")
        body, posted = "", ""
        if link:
            try:
                body = html_to_text(re.sub(r"<(script|style)\b.*?</\1\s*>", " ", client.get_page(link),
                                           flags=re.S | re.I))[:20000]
                posted = posted_date(body)
            except FetchError:
                link, body = "", ""
        if not body and not FRESHER_TITLE.search(title):
            continue                      # a bare title with no detail page: only fresher titles are kept
        loc = ""
        m = re.search(r"(?:location|based in|work location)\s*[:\-]?\s*([A-Za-z ,/()-]{3,60})", body, re.I)
        if m:
            loc = " ".join(m.group(1).split())
        job = Job(source="careers", external_id=title, company=company, title=title, url=link or url,
                  description=body, locations=[loc] if loc else [], posted_at=posted)
        r = score_job(job, profile, today)
        if role_fit(r, india_company):
            continue
        note = "" if body else "title only (no detail page): check years on the page"
        if r.location_class == "unknown":
            note = "; ".join(x for x in (note, "location not stated (company in India)") if x)
        rows.append(Row(company, title, loc or "not stated", link or url, posted, fit_notes(r, note),
                        "careers page", why, rank_key(r, posted)))
    return rows, f"careers page {url}: {seen} role(s) listed, {len(rows)} fit"


def candidates(store, profile: Profile, items) -> list[tuple[object, str]]:
    """(item, why) for every company that gets roles on the direct-apply list."""
    stopped_inbox = {r[0] for r in store.conn.execute(
        "SELECT DISTINCT target_id FROM outreach_events WHERE event IN ('bounced', 'stopped')")}
    out, seen = [], set()
    for it in items:
        if it.fit_hold or it.company_id in seen:
            continue
        why = ""
        if it.status == "apply_only":
            why = "large / form-only: apply via its careers page"
        elif it.large and it.status in ("no_contact", "unsendable", "dropped"):
            why = "large: no hiring inbox on its own pages"
        elif it.status in ("stopped", "exhausted") or (it.target_id in stopped_inbox and it.status != "sent"):
            why = "its inbox bounced or was stopped"
        if why:
            seen.add(it.company_id)
            out.append((it, why))
    return out


def stored_rows(store, profile: Profile, names: dict[str, str], today: date) -> list[Row]:
    """Stored openings (jobs table) at apply-only / large companies that fit, re-scored today."""
    from applyflow.discovery import name_key
    from applyflow.scoring import score_job
    out = []
    for r in store.conn.execute("SELECT * FROM jobs WHERE active = 1").fetchall():
        why = names.get(name_key(r["company"]))
        if not why:
            continue
        job = Job(source=r["source"], external_id=r["external_id"], company=r["company"], title=r["title"],
                  url=r["url"], description=r["description"] or "", locations=json.loads(r["locations"] or "[]"),
                  remote=None if r["remote"] is None else bool(r["remote"]), posted_at=r["posted_at"] or "",
                  apply_url=r["apply_url"] or "")
        res = score_job(job, profile, today)
        if role_fit(res, True):
            continue
        posted = (job.posted_at or "")[:10]
        out.append(Row(r["company"], job.title, job.location_text or "not stated", job.apply_url or job.url, posted,
                       fit_notes(res), f"stored {r['source']} posting", why, rank_key(res, posted)))
    return out


def build_direct_apply(store, client, profile: Profile, items, today: date | None = None,
                       log: Log = lambda _: None) -> tuple[list[Row], dict[str, int]]:
    from applyflow.discovery import name_key
    today = today or date.today()
    cands = candidates(store, profile, items)
    rows: list[Row] = []
    stats = {"companies": len(cands), "with_roles": 0}
    checks = {c["company_key"]: c for c in store.contact_checks()}
    for it, why in cands:
        check = checks.get(it.company_key)
        domain = (check["domain"] if check is not None else "") or ""
        if not domain:
            continue
        careers = json.loads(check["details"] or "{}").get("careers_page", "") if check is not None else ""
        got, note = board_rows(client, profile, it.company, f"https://{domain}", careers,
                               True, why, today, log)
        log(f"  {it.company:<30} {note}")
        stats["with_roles"] += bool(got)
        rows += got
    names = {name_key(it.company): why for it, why in cands}
    have = {(name_key(r.company), r.role_title.lower()) for r in rows}
    rows += [r for r in stored_rows(store, profile, names, today) if (name_key(r.company), r.role_title.lower()) not in have]
    best: dict[tuple[str, str], Row] = {}
    for r in rows:
        best.setdefault((name_key(r.company), r.role_title.lower()), r)
    rows = sorted(best.values(), key=lambda r: r.rank)
    stats["rows"] = len(rows)
    return rows, stats


def write_direct_apply(rows: list[Row], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(r.as_dict() for r in rows)
    return path
