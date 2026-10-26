"""Phase 2e: refill the outreach pool with safe contacts.

Sources, each recording whether an open role that fits was found (`role_matches`):
  1. HN "Who is hiring?" archive (several monthly threads, HN Algolia API): top-level posts located in
     India, or remote and open to India / anywhere / worldwide / global / APAC, whose roles pass the title
     and role-family filters and that invite applicants to email someone. Posts saying the role is filled
     are skipped; companies already contacted, queued or on the do-not-contact list are skipped.
  2. YC company job pages (ycombinator.com/companies/<slug>/jobs, allowed by YC's robots.txt): engineering
     roles that fit (intern, new grad or no stated experience; India or remote-India) become role targets.
  3. Entrackr's weekly funding reports (prose, read by `parse_entrackr_deals`) next to Inc42's tables: each
     startup's website comes from a likely domain whose homepage names the company (with an India or sector
     signal) and that has MX records; unresolved ones are dropped.
  4. import-targets: companies you found yourself (config/private/manual_targets.txt).

"Safe" contacts are verified named people, founders@ and hiring inboxes (careers@, jobs@, talent@, hr@,
work@, join-us@ ...). hello@ / hi@ / team@ are counted apart (5 a day); info@ / contact@ are never sent to.
Nothing here sends email.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urljoin

from applyflow.config import Profile
from applyflow.discovery import name_key, same_site, site_domain
from applyflow.http import FetchError
from applyflow.models import Job
from applyflow.scoring import classify_family, score_job, title_exclusion
from applyflow.sources import FundingRound, company_website
from applyflow.text import extract_contact_emails, html_to_text, page_summary

Log = Callable[[str], None]

# Locations a 2e role may be in: an Indian city, or remote open to India / anywhere.
KEEP_LOCATIONS = ("preferred_india", "other_india", "remote_open")


# ---------------------------------------------------------------------------
# Who is already a target (dedupe)
# ---------------------------------------------------------------------------

@dataclass
class Existing:
    """Companies already worked on: domain / name -> what happened to them."""
    contacted: dict[str, str] = field(default_factory=dict)     # sent, queued, bounced or locked: never again
    targets: dict[str, str] = field(default_factory=dict)       # any looked-up target
    dnc: Any = None

    def status(self, name: str, domain: str = "", emails: list[str] = ()) -> str:
        """'contacted: <id>' | 'do_not_contact: <entry>' | 'existing: <id>' | ''."""
        keys = [k for k in (domain, name_key(name)) if k]
        for k in keys:
            if k in self.contacted:
                return f"contacted: {self.contacted[k]}"
        if self.dnc is not None:
            for e in list(emails) + ([f"x@{domain}"] if domain else []):
                blocked = self.dnc.blocks(e)
                if blocked:
                    return f"do_not_contact: {blocked}"
        for k in keys:
            if k in self.targets:
                return f"existing: {self.targets[k]}"
        return ""


def existing_targets(store, dnc=None) -> Existing:
    from applyflow.outreach import target_id
    protected = store.protected_target_ids()
    out = Existing(dnc=dnc)
    for c in store.contact_checks():
        tid = target_id(c["company_key"])
        for k in (c["domain"] or "", name_key(c["company"])):
            if not k:
                continue
            out.targets.setdefault(k, tid)
            if tid in protected:
                out.contacted.setdefault(k, tid)
    return out


# ---------------------------------------------------------------------------
# 1. HN "Who is hiring?" archive
# ---------------------------------------------------------------------------

HN_BASE = "https://hn.algolia.com/api/v1"
_THREAD_TITLE = re.compile(r"^ask hn: who is hiring\?\s*\((\w+)\s+(\d{4})\)", re.I)
_FILLED = re.compile(
    r"\[\s*(?:filled|closed|hired)\s*\]|\(\s*(?:filled|closed)\s*\)|^\s*(?:filled|closed)\b[\s:.!-]|"
    r"\b(?:position|positions|role|roles|job|jobs|this|it|opening)\s+(?:has|have|is|are)\s+(?:now\s+|been\s+)*filled\b|"
    r"\bno longer (?:hiring|accepting|available|open)\b|\bwe(?:'ve| have) (?:filled|hired for) (?:this|the|these|all)\b|"
    r"\b(?:edit|update)\s*:?\s*(?:filled|closed|hired|no longer)\b|\bhiring (?:is )?(?:now )?closed\b",
    re.I | re.M)


def is_filled(text: str) -> bool:
    """The post (or its author's edit) says the role is filled / closed."""
    return bool(_FILLED.search(text or ""))


def hiring_threads(client, start: date, end: date) -> list[dict[str, str]]:
    """'Ask HN: Who is hiring? (Month Year)' stories posted between start and end: [{id, title, date}]."""
    lo = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    hi = int(datetime(end.year, end.month, end.day, 23, 59, tzinfo=timezone.utc).timestamp())
    data = client.get_json(f"{HN_BASE}/search_by_date", params={
        "tags": "story,author_whoishiring", "hitsPerPage": 50,
        "numericFilters": f"created_at_i>={lo},created_at_i<={hi}"})
    out = []
    for hit in data.get("hits", []):
        if _THREAD_TITLE.match(hit.get("title") or ""):
            out.append({"id": str(hit["objectID"]), "title": hit["title"], "date": (hit.get("created_at") or "")[:10]})
    return sorted(out, key=lambda t: t["date"], reverse=True)


def best_header_role(job: Job, profile: Profile) -> str:
    """The first role in an HN header that passes the title filter and names a target role family
    ('Senior SRE; Backend Engineer (New Grad)' -> 'Backend Engineer (New Grad)'); '' if none."""
    from applyflow.scoring import _ROLE_SPLIT, hn_role_text
    title = re.sub(r"\(([^()]*)\)", lambda m: "(" + m.group(1).replace(",", "§") + ")", job.title)  # keep "(Rust, MySQL)"
    for role in (r.strip() for r in _ROLE_SPLIT.split(hn_role_text(title)) if r.strip()):
        role = re.sub(r"\s*\([^)]*$", "", role.replace("§", ",")).strip()          # a cut-off "(..." goes
        for form in (role, re.sub(r"\b(Engineer|Developer|Intern)s\b", r"\1", role)):   # "Software Engineers"
            probe = Job(source="ats", external_id="", company=job.company, title=form, url="")
            if title_exclusion(probe, profile) is None and classify_family(probe, profile) is not None:
                return form
    return ""


@dataclass
class Decision:
    job: Job
    keep: bool
    reason: str
    tier: str = ""
    score: int = 0
    location_class: str = ""
    dedupe: str = ""                 # '' (new) | existing: <id> | contacted: <id> | do_not_contact: ...
    company_key: str = ""
    story: str = ""                  # HN thread id


def hn_archive_decision(job: Job, profile: Profile, today: date | None = None) -> Decision:
    """Whether one archived HN post becomes a role target (before dedupe)."""
    if is_filled(job.description):
        return Decision(job, False, "the post says the role is filled")
    job.emails = extract_contact_emails(job.description)
    role = best_header_role(job, profile)
    if not role:
        return Decision(job, False, f"no header role passes the title / role-family filters ('{job.title}')")
    job.title = role
    result = score_job(job, profile, today)
    d = Decision(job, False, "", result.tier, result.score, result.location_class)
    if result.tier == "rejected":
        d.reason = result.reasons[0]
    elif result.location_class not in KEEP_LOCATIONS:
        d.reason = f"location {result.location_class}: not India or remote open to India / anywhere"
    elif not job.emails:
        d.reason = "no email the post invites applicants to use"
    else:
        d.keep, d.reason = True, f"{role}: tier {result.tier}, score {result.score}, {result.location_class}"
    return d


def role_target_key(store, company: str) -> str:
    """The key build_targets gives a role target for this company name."""
    row = store.conn.execute("SELECT key, name FROM companies").fetchall()
    found = next((r["key"] for r in row if name_key(r["name"]) == name_key(company)), None)
    return found or f"co:{name_key(company)}"


def post_domain(job: Job) -> str:
    from applyflow.contacts import hn_header_website, posting_email_domain
    return site_domain(hn_header_website(job)) or posting_email_domain([job])


def collect_hn_archive(store, client, profile: Profile, start: date, end: date, existing: Existing,
                       log: Log = lambda _: None, today: date | None = None) -> dict[str, Any]:
    """Read every monthly thread between start and end, keep the posts that fit, store them as jobs (board
    hn:<story>) with a role match each. Newest post per company wins."""
    from applyflow.collectors.hn import HNWhoIsHiringCollector
    collector = HNWhoIsHiringCollector(client)
    stats: dict[str, Any] = {"threads": [], "posts": 0, "kept": 0, "new": 0, "existing": 0, "skipped": {}}
    best: dict[str, Decision] = {}
    for thread in hiring_threads(client, start, end):
        jobs = collector.fetch(thread["id"])
        stats["threads"].append(f"{thread['title']}: {len(jobs)} posts")
        stats["posts"] += len(jobs)
        log(f"  {thread['title']}: {len(jobs)} top-level posts")
        for job in jobs:
            d = hn_archive_decision(job, profile, today)
            d.story = thread["id"]
            if not d.keep:
                bucket = re.sub(r"\s*\(.*$|'[^']*'|\d+", "", d.reason).strip()
                stats["skipped"][bucket] = stats["skipped"].get(bucket, 0) + 1
                continue
            k = post_domain(job) or name_key(job.company)
            if k not in best or (job.posted_at or "") > (best[k].job.posted_at or ""):
                best[k] = d                # newer posts rank higher: the latest one per company is kept
    for d in sorted(best.values(), key=lambda d: d.job.posted_at or "", reverse=True):
        job = d.job
        d.dedupe = existing.status(job.company, post_domain(job), job.emails)
        if d.dedupe.startswith(("contacted", "do_not_contact")):
            reason = d.dedupe.split(":", 1)[0]
            stats["skipped"][f"already {reason}"] = stats["skipped"].get(f"already {reason}", 0) + 1
            continue
        stats["kept"] += 1
        stats["existing" if d.dedupe else "new"] += 1
        store.upsert_jobs(f"hn:{d.story}", [job])
        d.company_key = role_target_key(store, job.company)
        store.save_role_match(d.company_key, "hn_archive", "yes", title=job.title, url=job.url, job_id=job.id,
                              location=job.location_text, posted_at=(job.posted_at or "")[:10],
                              note=f"{d.reason}; emails {', '.join(job.emails)}" + (f"; {d.dedupe}" if d.dedupe else ""))
        log(f"  kept {job.company:<28} {job.title[:40]:<40} {(job.posted_at or '')[:10]} {', '.join(job.emails)}")
    stats["decisions"] = list(best.values())
    return stats


# ---------------------------------------------------------------------------
# 2. YC company job pages
# ---------------------------------------------------------------------------

YC_JOBS_URL = "https://www.ycombinator.com/companies/{slug}/jobs"
_DATA_PAGE = re.compile(r'data-page="([^"]+)"')


def _data_page(page: str) -> dict[str, Any]:
    m = _DATA_PAGE.search(page or "")
    if not m:
        return {}
    try:
        return json.loads(htmllib.unescape(m.group(1)))
    except ValueError:
        return {}


def parse_yc_jobs_page(page: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(company, job postings) from a YC company's /jobs page (its embedded page JSON)."""
    props = _data_page(page).get("props") or {}
    return props.get("company") or {}, list(props.get("jobPostings") or [])


def parse_yc_job_detail(page: str) -> str:
    """The posting's description (markdown-ish text) from its YC job page."""
    job = (_data_page(page).get("props") or {}).get("job") or {}
    return str(job.get("description") or "")


def yc_locations(text: str) -> list[str]:
    """'Bengaluru, KA, IN / Remote (IN)' -> ['Bengaluru, KA, India', 'Remote (India)']: YC's country codes
    spelled out, so 'IN' reads as India and 'US' as a foreign place."""
    out = []
    for part in (p.strip() for p in (text or "").split(" / ") if p.strip()):
        out.append(re.sub(r"\bIN\b", "India", part))
    return out


_YEARS = re.compile(r"(\d+)\s*\+?\s*(?:years?|yrs?)", re.I)


def yc_experience_years(min_experience: str | None) -> int | None:
    m = _YEARS.search(min_experience or "")
    return int(m.group(1)) if m else None


def new_grads_ok(posting: dict[str, Any]) -> bool:
    return "new grad" in str(posting.get("minExperience") or "").lower()


def yc_posting_job(posting: dict[str, Any], company: dict[str, Any], description: str = "") -> Job:
    exp = posting.get("minExperience") or ""
    lines = [f"Type: {posting.get('type') or ''}", f"Experience: {exp or 'not stated'}",
             f"Skills: {', '.join(posting.get('skills') or [])}", f"Salary: {posting.get('salaryRange') or ''}",
             description]
    locs = yc_locations(str(posting.get("location") or ""))
    return Job(source="ycjobs", external_id=str(posting.get("id")), company=str(company.get("name") or ""),
               title=str(posting.get("title") or ""),
               url="https://www.ycombinator.com" + str(posting.get("url") or ""),
               description="\n".join(x for x in lines if x), locations=locs,
               remote=True if any("remote" in x.lower() for x in locs) else None,
               employment_type=str(posting.get("type") or ""), department=str(posting.get("roleSpecificType") or ""),
               team_size=company.get("team_size") if isinstance(company.get("team_size"), int) else None)


def yc_role_decision(posting: dict[str, Any], job: Job, profile: Profile) -> Decision:
    """Engineering roles only; intern, new grad or no stated experience; India or remote open to India."""
    if str(posting.get("role") or "") != "eng":
        return Decision(job, False, f"not an engineering role ({posting.get('prettyRole') or posting.get('role')})")
    years = yc_experience_years(posting.get("minExperience"))
    result = score_job(job, profile)
    d = Decision(job, False, "", result.tier, result.score, result.location_class)
    if result.tier == "rejected":
        d.reason = result.reasons[0]
    elif years is not None and years >= 1 and result.track not in ("internship", "new_grad"):
        d.reason = f"asks for {posting.get('minExperience')} (intern / new grad / no stated experience only)"
    elif result.location_class not in KEEP_LOCATIONS:
        d.reason = f"location {result.location_class} ({job.location_text})"
    else:
        d.keep = True
        d.reason = f"{result.track}, tier {result.tier}, score {result.score}, {result.location_class}"
    return d


def collect_yc_jobs(store, client, profile: Profile, slugs: list[tuple[str, str]], existing: Existing,
                    log: Log = lambda _: None) -> dict[str, Any]:
    """Read each YC company's jobs page; keep the roles that fit (job pages fetched for those), store them
    as jobs (board ycjobs:<slug>) and record a role match (yes / no) per company."""
    stats: dict[str, Any] = {"companies": 0, "with_postings": 0, "postings": 0, "kept": 0, "matched_companies": 0,
                             "new_companies": 0, "failed": 0, "skipped": {}, "new_grads_ok": []}
    for slug, name in slugs:
        try:
            company, postings = parse_yc_jobs_page(client.get_page(YC_JOBS_URL.format(slug=slug)))
        except FetchError as exc:
            stats["failed"] += 1
            log(f"  {name:<28} jobs page failed: {exc}")
            continue
        stats["companies"] += 1
        key = f"yc:{slug}"
        if not postings:
            store.save_role_match(key, "yc_jobs", "no", url=YC_JOBS_URL.format(slug=slug), note="no open postings")
            continue
        stats["with_postings"] += 1
        stats["postings"] += len(postings)
        kept: list[Decision] = []
        for posting in postings:
            job = yc_posting_job(posting, company)
            d = yc_role_decision(posting, job, profile)
            if d.keep:                      # the job page has the real description: score again with it
                try:
                    job = yc_posting_job(posting, company, parse_yc_job_detail(client.get_page(job.url)))
                    d = yc_role_decision(posting, job, profile)
                except FetchError:
                    pass
            if d.keep:
                kept.append(d)
            else:
                bucket = re.sub(r"\s*\(.*$|'[^']*'|\d+", "", d.reason).strip()
                stats["skipped"][bucket] = stats["skipped"].get(bucket, 0) + 1
        if not kept:
            store.save_role_match(key, "yc_jobs", "no", url=YC_JOBS_URL.format(slug=slug),
                                  note=f"{len(postings)} posting(s), none fit")
            continue
        kept.sort(key=lambda d: (-d.score, d.job.title))
        store.upsert_jobs(f"ycjobs:{slug}", [d.job for d in kept])
        best = kept[0]
        posting = next(p for p in postings if str(p.get("id")) == best.job.external_id)
        stats["kept"] += len(kept)
        stats["matched_companies"] += 1
        status = existing.status(name, site_domain(str(company.get("website") or "")))
        if not status.startswith("contacted"):
            stats["new_companies"] += 0 if status else 1
        for d in kept:
            p = next(p for p in postings if str(p.get("id")) == d.job.external_id)
            if new_grads_ok(p):
                stats["new_grads_ok"].append((name, d.job.title, str(p.get("salaryRange") or "")))
        store.save_role_match(key, "yc_jobs", "yes", title=best.job.title, url=best.job.url, job_id=best.job.id,
                              location=best.job.location_text, experience=str(posting.get("minExperience") or ""),
                              salary=str(posting.get("salaryRange") or ""), new_grads_ok=new_grads_ok(posting),
                              note=f"{best.reason}; {len(kept)} fitting of {len(postings)}" + (f"; {status}" if status else ""))
        log(f"  {name:<28} {len(kept)}/{len(postings)} fit: {best.job.title} ({best.job.location_text})")
    return stats


# ---------------------------------------------------------------------------
# 3. Entrackr weekly funding reports
# ---------------------------------------------------------------------------

ENTRACKR_SITEMAP = "https://entrackr.com/sitemap_{day}.xml"
_REPORT_URL = re.compile(r"<loc>(https://entrackr\.com/report/weekly-funding-report[^<]*funding-and-acquisitions[^<]*)</loc>")


def entrackr_report_urls(client, today: date, weeks: int = 12, log: Log = lambda _: None) -> list[tuple[str, str]]:
    """(url, YYYY-MM-DD) of the weekly funding reports from the last `weeks` weeks (they come out on Saturdays;
    Friday to Sunday daily sitemaps are read)."""
    out: list[tuple[str, str]] = []
    for i in range(weeks * 7):
        day = today - timedelta(days=i)
        if day.weekday() not in (4, 5, 6):
            continue
        try:
            xml = client.get_text(ENTRACKR_SITEMAP.format(day=day.isoformat()))
        except FetchError as exc:
            log(f"  sitemap {day}: {exc}")
            continue
        for url in _REPORT_URL.findall(xml):
            if url not in [u for u, _ in out]:
                out.append((url, day.isoformat()))
    return out


_SECTION_END = re.compile(r"\[\s*(?:City and segment|Series-wise|Week-on-week|Key Hirings|Fund Launches)", re.I)


def report_text(page: str) -> str:
    """The deal paragraphs of a weekly report: from 'This week,' to the city / series breakdown."""
    body = re.sub(r"<(script|style|noscript)\b.*?</\1\s*>", " ", page or "", flags=re.S | re.I)
    text = html_to_text(body)
    start = text.find("This week,")
    if start < 0:
        return ""
    starts = [m.start() for m in re.finditer(r"This week,", text)]
    start = starts[1] if len(starts) > 1 and starts[1] - starts[0] < 600 else start   # the dek repeats the lede
    end = _SECTION_END.search(text, start)
    return text[start: end.start() if end else start + 6000]


_NAME = r"[A-Z0-9][\w.&'’+-]*(?:\s+(?:[A-Z0-9][\w.&'’+-]*|of|for|and|&))*?"
_AMOUNT = r"(?:over |around |about |nearly |close to |an undisclosed amount|undisclosed)?(?:\$|Rs\.?\s?|INR\s?)?[\d.,]+\s*(?:million|mn|crore|cr|billion|bn|lakh)?(?:\s*\([^)]*\))?"
_VERB = r"(?:has\s+)?(?:raised|secured|bagged|received|is raising|picked up|mopped up|netted|closed|got)"
_ROUND = re.compile(r"\bin (?:a|an|its|their|the) ((?:pre-?)?(?:seed|series [a-f]|bridge|angel|strategic|debt|funding|"
                    r"growth|pre-series [a-f])[\w -]*?) round", re.I)
_DESCRIPTOR_END = re.compile(r"\b(startup|start-up|platform|brand|company|studio|marketplace|firm|provider|player|"
                             r"maker|manufacturer|app|school|venture)s?$", re.I)
_LEADING = re.compile(r"^(?:Meanwhile|However|Also|Additionally|Further|Similarly|In addition|While|And|"
                      r"Elsewhere),?\s+", re.I)
_SKIP_NAMES = re.compile(r"^(?:Growth-stage|Early-stage|Bengaluru|Mumbai|Delhi|The|This|It|They|Startups?|"
                         r"Homegrown|Indian|US-based)$", re.I)


def _split_name(chunk: str) -> tuple[str, str]:
    """'Bengaluru-based AI gaming studio Spill Games' -> ('Spill Games', 'Bengaluru-based AI gaming studio')."""
    chunk = _LEADING.sub("", chunk.strip(" ,;"))
    chunk = re.sub(r"^.*?\b(?:led by|including)\s+", "", chunk)
    m = re.match(r"^(?P<desc>(?:[\w&/-]+\s+){0,6}(?:startups?|start-ups?|platform|brand|company|studio|marketplace|"
                 r"firm|provider|player|maker|manufacturer|app|school|edtech|fintech|healthtech|agritech|"
                 r"spacetech|deeptech|saas|venture)s?)\s+(?P<name>[A-Z0-9].*)$", chunk)
    desc, name = (m.group("desc"), m.group("name")) if m else ("", chunk)
    name = re.sub(r"^(?:'s|’s)\s+parent\s+", "", name)
    name = re.sub(r"^\S+(?:'s|’s) parent\s+", "", name)
    name = re.split(r",\s*(?:a|an|which|the)\b|\s+\((?!.*\))", name)[0].strip(" ,")
    name = re.sub(r"^(?:the|its)\s+", "", name, flags=re.I)
    return name, desc.strip()


def _good_name(name: str) -> bool:
    words = name.split()
    return bool(words) and len(words) <= 5 and name[0].isupper() and not _SKIP_NAMES.match(name) \
        and not re.search(r"\b(?:startups?|deals?|rounds?|week|million|crore)\b", name, re.I)


def parse_entrackr_deals(text: str, source_url: str, day: str) -> list[FundingRound]:
    """Startups named in a weekly report's deal paragraphs: name, round, amount, sector descriptor. Deals named
    without an amount ('Other funded startups included edtech X, ...') are kept with an empty amount."""
    out: dict[str, FundingRound] = {}

    def add(name: str, desc: str, amount: str = "", round_name: str = "") -> None:
        name = name.strip(" .,")
        if not _good_name(name) or name_key(name) in {name_key(k) for k in out}:
            return
        out[name] = FundingRound(company=name, round=round_name.strip().title().replace("Pre-Series", "Pre-Series")
                                 if round_name else "", amount=amount.strip(), date=day,
                                 sector=re.sub(r"^(?:[\w-]+-based|homegrown)\s+", "", desc, flags=re.I).strip(),
                                 source="entrackr", source_url=source_url)

    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z])|\n+", text or ""):
        sent = sent.strip()
        round_m = _ROUND.search(sent)
        round_name = round_m.group(1) if round_m else ""
        # "X and Y raised $A and $B, respectively, in seed rounds"
        m = re.search(rf"^(?P<head>.*?)\s+(?P<a>{_NAME})\s+and\s+(?P<b>{_NAME})\s+{_VERB}\s+(?P<amt1>{_AMOUNT})\s+and\s+"
                      rf"(?P<amt2>{_AMOUNT}),?\s+respectively", sent)
        if m:
            desc = _split_name(m.group("head") + " x")[1]
            rnd = re.search(r"in ((?:pre-?)?seed|series [a-f]|pre-series [a-f])\w* rounds", sent, re.I)
            add(_split_name(m.group("head") + " " + m.group("a"))[0], desc, m.group("amt1"), rnd.group(1) if rnd else "")
            add(m.group("b"), desc, m.group("amt2"), rnd.group(1) if rnd else "")
            continue
        # "Other funded startups included edtech A, B startup C, among others."
        m = re.search(r"(?:other funded startups|other startups that raised[^,]*|also raised funding)[^:]*?"
                      r"(?:included|include|were|are)\s+(?P<list>.+?)(?:,?\s+among others)?\.?$", sent, re.I)
        if m:
            for part in re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group("list")):
                name, desc = _split_name(part)
                add(name, desc)
            continue
        # "..., led by X, which raised $A" / "X raised $A in a Series A round"
        for m in re.finditer(rf"(?P<chunk>(?:[\w&/'’.-]+\s+){{0,9}}?{_NAME})(?:,\s*(?:a|an)\s+[^,]+,)?\s*,?\s*"
                             rf"(?:which\s+)?{_VERB}\s+(?P<amt>{_AMOUNT})", sent):
            chunk = m.group("chunk")
            if re.search(r"\bstartups?\s+raised$|\bstage startups$", chunk + " raised", re.I):
                continue
            name, desc = _split_name(chunk)
            add(name, desc, m.group("amt"), round_name)
    return list(out.values())


def domain_candidates(name: str) -> list[str]:
    """Likely domains for a startup name: squashed and hyphenated, with and without a trailing 'AI' /
    'Technologies' / 'Labs' word, on .com / .in / .ai / .io / .co."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    if not words:
        return []
    bases = ["".join(words)]
    trimmed = [w for w in words if w not in {"technologies", "technology", "tech", "labs", "lab", "ai", "india",
                                              "solutions", "systems", "private", "limited", "pvt", "ltd", "inc"}]
    if trimmed and trimmed != words:
        bases.append("".join(trimmed))
    if len(words) > 1:
        bases.append("-".join(words))
    out = []
    for base in bases:
        for tld in ("com", "in", "ai", "io", "co"):
            if words[-1] == "ai" and base == "".join(trimmed) and tld != "ai":
                continue
            d = f"{base}.{tld}"
            if d not in out:
                out.append(d)
    if words[-1] == "ai" and trimmed:
        out.insert(0, f"{''.join(trimmed)}.ai")
    return list(dict.fromkeys(out))


_INDIA_SIGNAL = re.compile(r"\bindia\b|\bindian\b|bengaluru|bangalore|mumbai|delhi|gurugram|gurgaon|noida|hyderabad|"
                           r"pune|chennai|kolkata|ahmedabad|jaipur|₹|\binr\b|\bcrore\b|\blakh\b|\bpvt\.? ltd\b|"
                           r"private limited|\+91", re.I)


def homepage_matches(page: str, name: str, sector: str = "", domain: str = "") -> tuple[bool, str]:
    """(accept, why): the homepage names the company (title, meta, or the first lines of text) and shows an India
    signal or shares a sector word - so 'Circle' doesn't resolve to a US company's circle.com."""
    summary = page_summary(page or "")
    head = f"{summary['title']} {summary['description']}"
    text = html_to_text(re.sub(r"<(script|style)\b.*?</\1\s*>", " ", page or "", flags=re.S | re.I))[:20000]
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    target = squash(name)
    if len(target) < 3:
        return False, "name too short to check"
    named = target in squash(head) or target in squash(text[:3000])
    if not named:
        return False, f"homepage doesn't name '{name}'"
    if domain.endswith(".in") or _INDIA_SIGNAL.search(f"{head} {text}"):
        return True, "names the company; India signal"
    words = [w for w in re.findall(r"[a-z]{4,}", (sector or "").lower())
             if w not in {"startup", "startups", "based", "platform", "company", "brand"}]
    hits = [w for w in words if re.search(rf"\b{w}", f"{head} {text}".lower())]
    if hits:
        return True, f"names the company; sector word '{hits[0]}'"
    return False, "names the company but shows no India or sector signal (may be a namesake)"


def resolve_domain(client, mx, name: str, sector: str = "", log: Log = lambda _: None) -> tuple[str, str]:
    """(website, why) for a startup named in funding news, or ('', why not)."""
    tried = []
    for domain in domain_candidates(name):
        try:
            page = client.get_page(f"https://{domain}")
        except FetchError as exc:
            tried.append(f"{domain}: {'robots.txt disallows' if 'disallowed by robots' in str(exc) else 'no site'}")
            continue
        ok, why = homepage_matches(page, name, sector, domain)
        if not ok:
            tried.append(f"{domain}: {why}")
            continue
        status = mx.check(domain) if mx is not None else "ok"
        if status != "ok":
            tried.append(f"{domain}: MX {status}")
            continue
        return f"https://{domain}", f"{domain}: {why}; MX ok"
    return "", "; ".join(tried[:6]) or "no candidate domain"


def load_entrackr_overrides(path=None) -> dict[str, list[dict[str, str]]]:
    """Reviewed deals per report URL (data/entrackr_deals.json, gitignored): {url: [{company, round, amount, sector}]}.
    The parser reads prose; where a report was read by hand, the reviewed list wins."""
    from applyflow.config import ROOT
    path = path or ROOT / "data" / "entrackr_deals.json"
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def collect_entrackr(client, today: date, weeks: int = 12, log: Log = lambda _: None,
                     overrides: dict[str, list[dict[str, str]]] | None = None) -> tuple[list[FundingRound], str]:
    """Every startup named in the last `weeks` weekly reports. `overrides` (reviewed deals per report URL,
    data/entrackr_deals.json) replaces the parser's reading of a report."""
    rounds: list[FundingRound] = []
    urls = entrackr_report_urls(client, today, weeks, log)
    for url, day in urls:
        if overrides and url in overrides:
            rounds += [FundingRound(**{**d, "source": "entrackr", "source_url": url, "date": d.get("date") or day})
                       for d in overrides[url]]
            continue
        try:
            rounds += parse_entrackr_deals(report_text(client.get_page(url)), url, day)
        except FetchError as exc:
            log(f"  {url}: {exc}")
    note = f"{len(rounds)} startups named in {len(urls)} weekly report(s)"
    log(f"  Entrackr weekly reports: {note}")
    return rounds, note


# ---------------------------------------------------------------------------
# 4. Manual targets file (import-targets)
# ---------------------------------------------------------------------------

TARGETS_FILE_HEADER = """# ApplyFlow manual targets - companies you found yourself (e.g. browsing Wellfound).
# Import them with:  .venv\\Scripts\\python -m applyflow import-targets
#
# One company per line, fields separated by "|":
#   Company | website | role seen (optional) | note (optional)
#
# - website: the company's own domain (acme.com or https://acme.com), not a job-board link
# - role seen: the opening you saw (e.g. "Backend Engineer Intern"); the email then cites it
# - note: anything to remember (where you saw it, why it fits); shown in the drafting packet
# Lines starting with # are comments. Re-importing a company updates it.
#
# Acme Robotics | acmerobotics.com | Backend Engineer Intern | seen on Wellfound, Bengaluru, Python + FastAPI
# Nimbus Data | nimbusdata.io | | seed-stage data infra startup, no posting yet
"""


@dataclass
class TargetLine:
    line: int
    company: str
    website: str
    role_seen: str = ""
    note: str = ""


def parse_targets_file(text: str) -> tuple[list[TargetLine], list[str]]:
    """(targets, errors) from a manual targets file; comments and blank lines are skipped."""
    out, errors = [], []
    for n, raw in enumerate((text or "").lstrip("﻿").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            errors.append(f"line {n}: needs at least 'Company | website'")
            continue
        website = company_website(parts[1])
        if not website:
            errors.append(f"line {n}: '{parts[1]}' isn't a company website")
            continue
        out.append(TargetLine(n, parts[0], website, parts[2] if len(parts) > 2 else "",
                              " | ".join(p for p in parts[3:] if p)))
    return out, errors


# ---------------------------------------------------------------------------
# Role match from a company's own careers page or ATS board
# ---------------------------------------------------------------------------

_ROLE_LINE = re.compile(r"^[A-Z][\w/&+.,()' -]{4,80}$")


def careers_page_roles(text: str, profile: Profile) -> tuple[list[str], int]:
    """(titles that fit, how many job-like titles the page lists) from a careers page's text. A title fits when
    it names a target role family, passes the title filter and isn't marked senior by the title filter."""
    fits, seen = [], 0
    for line in (ln.strip() for ln in (text or "").splitlines()):
        if not _ROLE_LINE.match(line) or len(line.split()) > 9:
            continue
        probe = Job(source="careers", external_id="", company="", title=line, url="")
        if not re.search(r"\b(engineer|developer|intern|sde|programmer|scientist|architect|analyst|designer|"
                         r"manager|lead|executive|associate)\b", line, re.I):
            continue
        seen += 1
        if title_exclusion(probe, profile) is None and classify_family(probe, profile) is not None \
                and line not in fits:
            fits.append(line)
    return fits, seen


def board_role_match(client, website: str, profile: Profile, careers_url: str = "") -> dict[str, str]:
    """{match, source, title, url, note} from the company's ATS board (if its site links one) or its careers page."""
    from applyflow.collectors import ATS_COLLECTORS
    from applyflow.detect import detect_board
    from applyflow.models import Company
    try:
        board = detect_board(client, website, max_pages=3)
    except FetchError as exc:
        return {"match": "unknown", "source": "careers", "note": f"site unreachable ({exc})"}
    if board and board.supported:
        try:
            jobs = ATS_COLLECTORS[board.ats](client).fetch(Company(name="", ats=board.ats, slug=board.slug))
        except FetchError as exc:
            return {"match": "unknown", "source": "board", "note": f"{board.ats}:{board.slug} failed ({exc})"}
        best = None
        for job in jobs:
            r = score_job(job, profile)
            if r.tier in ("A", "B") and r.location_class in KEEP_LOCATIONS and (best is None or r.score > best[1].score):
                best = (job, r)
        if best:
            return {"match": "yes", "source": "board", "title": best[0].title, "url": best[0].url,
                    "note": f"{board.ats}:{board.slug}: tier {best[1].tier}, score {best[1].score}"}
        return {"match": "no", "source": "board", "url": board.found_on,
                "note": f"{board.ats}:{board.slug}: {len(jobs)} posting(s), none fit"}
    url = careers_url or (board.found_on if board else "")
    if not url:
        return {"match": "unknown", "source": "careers",
                "note": f"{board.ats} board (unsupported)" if board else "no careers page or board found"}
    try:
        text = html_to_text(re.sub(r"<(script|style)\b.*?</\1\s*>", " ", client.get_page(url), flags=re.S | re.I))
    except FetchError as exc:
        return {"match": "unknown", "source": "careers", "url": url, "note": f"careers page failed ({exc})"}
    fits, seen = careers_page_roles(text, profile)
    if fits:
        return {"match": "yes", "source": "careers", "title": fits[0], "url": url,
                "note": f"careers page lists {seen} role(s); fits: {', '.join(fits[:3])}"}
    if seen:
        return {"match": "no", "source": "careers", "url": url, "note": f"careers page lists {seen} role(s), none fit"}
    return {"match": "unknown", "source": "careers", "url": url,
            "note": "careers page lists no readable roles" + (f" ({board.ats} board)" if board else "")}


def check_roles(store, client, profile: Profile, keys: list[str], log: Log = lambda _: None) -> dict[str, int]:
    """Record a role match (board / careers page) for each target key that has none yet."""
    have = store.role_matches()
    counts: dict[str, int] = {}
    for key in keys:
        if have.get(key):
            continue
        check = store.conn.execute("SELECT * FROM contact_checks WHERE company_key = ?", (key,)).fetchone()
        if check is None or not check["domain"]:
            continue
        careers = json.loads(check["details"] or "{}").get("careers_page", "")
        res = board_role_match(client, f"https://{check['domain']}", profile, careers)
        store.save_role_match(key, res["source"], res["match"], title=res.get("title", ""), url=res.get("url", ""),
                              note=res.get("note", ""))
        counts[res["match"]] = counts.get(res["match"], 0) + 1
        log(f"  {check['company']:<28} role match {res['match']:<7} {res.get('title', '')[:40]} - {res.get('note', '')}")
    return counts


def yc_role_match(client, profile: Profile, slug: str) -> dict[str, str]:
    """{match, source, title, url, note} from one YC company's jobs page."""
    url = YC_JOBS_URL.format(slug=slug)
    try:
        company, postings = parse_yc_jobs_page(client.get_page(url))
    except FetchError as exc:
        return {"match": "unknown", "source": "yc_jobs", "url": url, "note": f"YC jobs page failed ({exc})"}
    kept = [d for d in (yc_role_decision(p, yc_posting_job(p, company), profile) for p in postings) if d.keep]
    if kept:
        best = max(kept, key=lambda d: d.score)
        return {"match": "yes", "source": "yc_jobs", "title": best.job.title, "url": best.job.url, "note": best.reason}
    return {"match": "no", "source": "yc_jobs", "url": url, "note": f"{len(postings)} posting(s), none fit"}


@dataclass
class ImportedTarget:
    line: TargetLine
    target_id: str = ""
    status: str = ""
    address: str = ""
    kind: str = ""
    tech_fit: str = ""
    role: dict[str, str] = field(default_factory=dict)
    error: str = ""


def import_targets(store, profile: Profile, client, path, dnc=None, log: Log = lambda _: None,
                   out_dir=None) -> tuple[list[ImportedTarget], list[str]]:
    """2e: every company in the manual targets file becomes a manual target: contact lookup, tech fit (from its
    homepage), a role match (the role you saw; else its YC jobs page, ATS board or careers page), a packet and
    a draft (a role email when a role was seen or found). Creates the file with its header if it's missing."""
    from pathlib import Path
    from applyflow.outreach import OutreachError, add_target, refresh_drafts, write_packets
    from applyflow.pool import tech_fit
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TARGETS_FILE_HEADER, encoding="utf-8")
        return [], [f"created {path} - add companies to it and run import-targets again"]
    lines, errors = parse_targets_file(path.read_text(encoding="utf-8"))
    yc_by_domain = {r["domain"]: r["key"][3:] for r in store.conn.execute(
        "SELECT key, domain FROM pool_companies WHERE key LIKE 'yc:%' AND domain != ''")}
    out = []
    for t in lines:
        rec = ImportedTarget(t)

        def check(key: str, t: TargetLine = t, rec: ImportedTarget = rec) -> None:
            if t.role_seen:
                rec.role = {"match": "yes", "source": "manual", "title": t.role_seen, "note": "the role you saw"}
                return
            slug = yc_by_domain.get(site_domain(t.website))
            res = yc_role_match(client, profile, slug) if slug and client is not None else {}
            if res.get("match") != "yes" and client is not None:
                details = store.conn.execute("SELECT details FROM contact_checks WHERE company_key = ?",
                                             (key,)).fetchone()
                careers = json.loads(details["details"] or "{}").get("careers_page", "") if details else ""
                res = board_role_match(client, t.website, profile, careers)
            if res:
                store.save_role_match(key, res["source"], res["match"], title=res.get("title", ""),
                                      url=res.get("url", ""), note=res.get("note", ""))
                rec.role = res
        try:
            it = add_target(store, profile, client, t.company, t.website, note=t.note, dnc=dnc, log=log,
                            role_seen=t.role_seen, before_drafts=check)
        except OutreachError as exc:
            rec.error = str(exc)
            out.append(rec)
            continue
        ctx = store.get_context(it.company_key)
        site = " ".join(x or "" for x in (ctx["homepage_title"], ctx["meta_description"], ctx["snippet"])) if ctx else ""
        fit, words = tech_fit(f"{t.company} {t.note} {site}", profile)
        rec.tech_fit = f"{fit}" + (f" ({', '.join(words[:3])})" if words else "")
        rec.target_id, rec.status = it.target_id, it.status
        rec.address, rec.kind = (it.address.email, it.address.kind) if it.address else ("", "")
        out.append(rec)
    if out and out_dir is not None:
        items = refresh_drafts(store, profile, dnc)
        write_packets(items, profile, Path(out_dir), only={r.target_id for r in out if r.target_id})
        by_id = {i.target_id: i for i in items}
        for r in out:
            if r.target_id in by_id:
                r.status = by_id[r.target_id].status
    return out, errors


# ---------------------------------------------------------------------------
# Safe contacts: what is sendable, and for how many days
# ---------------------------------------------------------------------------

def safe_summary(items, daily: int = 30, general_cap: int = 5, guessed_cap: int = 7) -> dict[str, Any]:
    """Unsent ready targets by contact kind, and how many sending days they cover at `daily` a day with at most
    `general_cap` hello@ / hi@ / team@ and `guessed_cap` guessed addresses a day."""
    by_kind: dict[str, int] = {}
    queued = 0
    for it in items:
        if it.status == "ready" and it.address is not None:
            k = it.address.kind
            by_kind[k] = by_kind.get(k, 0) + 1
            queued += bool(it.queued_on)
    safe = sum(by_kind.get(k, 0) for k in ("verified_person", "founders", "hiring_inbox"))
    general, guessed = by_kind.get("general_inbox", 0), by_kind.get("guessed", 0)
    other = by_kind.get("other", 0)
    days, s, g, q, o = 0, safe + other, general, guessed, 0
    while s + g + q > 0 and days < 1000:
        room = daily
        take_g = min(g, general_cap, room)
        room -= take_g
        take_q = min(q, guessed_cap, room)
        room -= take_q
        take_s = min(s, room)
        if take_g + take_q + take_s == 0:
            break
        g, q, s = g - take_g, q - take_q, s - take_s
        days += 1
    full_days = (safe + other) // daily if daily else 0
    return {"by_kind": by_kind, "safe": safe, "general": general, "guessed": guessed, "other": other,
            "days": days, "safe_only_days": full_days, "queued": queued}
