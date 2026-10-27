"""Phase 2f: genuine contact supply - fintech lists, product-engineering consultancies, more startups - plus the
sector tag every target carries, the classification of "other" published addresses and the manual referral list.

Sources (config/sources.yaml, all through the polite client: robots.txt, 1 request/second/host):
  * fintech_lists - RBI's public entity lists (kind: rbi; skipped while www.rbi.org.in's robots.txt can't be read)
                    and Sahamati's Account Aggregator / technology-service-provider pages (kind: sahamati: logos
                    linking each member's own website). YC companies tagged Fintech come from the pool's YC records.
  * consultancies - a hand-written seed list of product-engineering consultancies (India offices, engineers
                    building software for clients), each verified on its own website before it's added.
  * github_lists  - the pool's raw-file lists (2c), plus header tables like rshetye/AwesomeIndiaStartups.

Every company is checked on its own website before it joins the pool (`verify_company`: the homepage names it,
an India signal on the homepage or its contact / about page, MX records); what can't be verified is left out.
Banks, cooperative banks, card networks and PSUs are never added (`FINTECH_EXCLUDE`). Nothing here sends email.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from applyflow.config import Profile
from applyflow.discovery import name_key, site_domain
from applyflow.http import FetchError
from applyflow.sources import PoolCompany, clean_name, company_website, merge, pool_sector
from applyflow.text import html_to_text, page_summary

Log = Callable[[str], None]

# Never a target, whatever list names it: banks, cooperative banks, card networks, PSUs / government bodies.
FINTECH_EXCLUDE = re.compile(r"\bbank\b|\bbanking corporation\b|co-?operative|\bvisa\b|mastercard|american express|"
                             r"\brupay\b|\bnpci\b|national payments corporation|\bnsdl\b|\bnadl\b|\bcdsl\b|"
                             r"government|\bpsu\b|\bsbi\b|life insurance corporation|india post|\bidbi\b", re.I)


# ---------------------------------------------------------------------------
# Verifying a company on its own website
# ---------------------------------------------------------------------------

_INDIA = re.compile(r"\bindia\b|\bindian\b|bengaluru|bangalore|mumbai|\bdelhi\b|gurugram|gurgaon|noida|hyderabad|"
                    r"\bpune\b|chennai|kolkata|ahmedabad|jaipur|kochi|coimbatore|indore|chandigarh|\+91|"
                    r"pvt\.? ltd|private limited", re.I)
INDIA_PAGES = ["/contact", "/contact-us", "/about", "/about-us", "/locations", "/offices"]


@dataclass
class Verified:
    ok: bool
    why: str
    name: str = ""
    website: str = ""
    context: dict[str, str] = field(default_factory=dict)      # homepage title / description / snippet


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def site_name(page: str, domain: str) -> str:
    """The company's name as its homepage gives it: og:site_name, else the title segment naming the domain."""
    m = re.search(r"""<meta[^>]+property=["']og:site_name["'][^>]+content=["']([^"']{2,60})["']""", page or "", re.I) \
        or re.search(r"""<meta[^>]+content=["']([^"']{2,60})["'][^>]+property=["']og:site_name["']""", page or "", re.I)
    base = _squash(domain.split(".")[0])
    title = m.group(1) if m else page_summary(page or "")["title"]
    for part in re.split(r"\s+[|–—:-]\s+|\s*\|\s*", title):
        if base and (base in _squash(part) or _squash(part) in base) and len(part) <= 40:
            return clean_name(part)
    return base.capitalize()


def verify_company(client, mx, name: str, website: str, need_india: bool = True) -> Verified:
    """2f: is this the company, on its own site? The homepage names it (title, meta or the first lines), an India
    signal shows on the homepage or a contact / about / locations page (when `need_india`), and the domain has MX
    records. `name` may be '' (a list that gives only a website): the site's own name is used."""
    site = company_website(website)
    domain = site_domain(site)
    if not domain:
        return Verified(False, f"not a company website ({website})")
    try:
        page = client.get_page(site)
    except FetchError as exc:
        return Verified(False, f"homepage failed ({exc})", website=site)
    summary = page_summary(page)
    name = name or site_name(page, domain)
    text = html_to_text(re.sub(r"<(script|style)\b.*?</\1\s*>", " ", page, flags=re.S | re.I))[:20000]
    head = f"{summary['title']} {summary['description']}"
    # "Talentica Software" is "Talentica" on its own homepage: try the name without its generic last words too.
    short = re.sub(r"(?:\s+(?:software|technologies|technology|consulting|innovations|solutions|systems|labs|"
                   r"digital|services|private|limited|pvt|ltd|inc))+$", "", name, flags=re.I)
    named = [t for t in {_squash(name), _squash(short)} if len(t) >= 3]
    if not any(t in _squash(head) or t in _squash(text[:4000]) or t in _squash(domain) for t in named):
        return Verified(False, f"homepage doesn't name '{name}'", name, site)
    india = bool(_INDIA.search(f"{head} {text}")) or domain.endswith(".in")
    where = "homepage"
    if need_india and not india:
        for path in INDIA_PAGES:
            try:
                extra = client.get_page(site + path)
            except FetchError:
                continue
            if _INDIA.search(html_to_text(extra)[:30000]):
                india, where = True, path
                break
    if need_india and not india:
        return Verified(False, "no India office or signal on its homepage or contact / about pages", name, site)
    status = mx.check(domain) if mx is not None else "ok"
    if status != "ok":
        return Verified(False, f"MX {status}", name, site)
    ctx = {"website": site, "status": "ok", **summary}
    return Verified(True, f"names the company; India signal on {where}; MX ok" if need_india else
                    "names the company; MX ok", name, site, ctx)


# ---------------------------------------------------------------------------
# Fintech lists: RBI (entity tables) and Sahamati (member logos linking their sites)
# ---------------------------------------------------------------------------

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)


def parse_rbi_entities(page: str) -> list[str]:
    """Entity names from an RBI list page: every table whose header names a 'Name' column (Name of the entity /
    company / NBFC / payment aggregator ...). Serial numbers, blank and repeated rows are skipped."""
    out: list[str] = []
    for table in re.findall(r"<table\b.*?</table>", page or "", re.S | re.I):
        rows = [[" ".join(html_to_text(c).split()) for c in _CELL.findall(r)] for r in _ROW.findall(table)]
        header_at = next((i for i, r in enumerate(rows) if any(re.match(r"name\b", c, re.I) for c in r)), None)
        if header_at is None:
            continue
        col = next(i for i, c in enumerate(rows[header_at]) if re.match(r"name\b", c, re.I))
        for r in rows[header_at + 1:]:
            if col >= len(r):
                continue
            name = re.sub(r"\s*\((?:formerly|earlier)[^)]*\)", "", r[col], flags=re.I).strip(" .,*")
            if name and not re.fullmatch(r"[\d.\s]+", name) and name not in out:
                out.append(name)
    return out


def rbi_company_name(entity: str) -> str:
    """'Razorpay Software Private Limited' -> 'Razorpay': the brand a homepage would show."""
    name = re.sub(r"\b(?:private|pvt\.?|limited|ltd\.?|llp|technologies|technology|tech|solutions|services|"
                  r"software|fintech|payments?|india|financial|finance|digital|networks?|systems?)\b\.?", " ",
                  entity, flags=re.I)
    return " ".join(name.split()).strip(" .,&-") or entity


_SKIP_LINK = re.compile(r"sahamati|github\.com|youtube\.com|discord|rebit\.org|swagger|linkedin|twitter|x\.com|"
                        r"facebook|instagram|google\.com|apple\.com", re.I)


def parse_sahamati(page: str) -> list[str]:
    """Member websites from a Sahamati ecosystem page (AAs, TSPs): the logo links in the page's main content."""
    main = page[page.find("<main"):] if "<main" in (page or "") else (page or "")
    out = []
    for href in re.findall(r"""<a[^>]+href=["'](https?://[^"'#]+)""", main, re.I):
        site = company_website(href)
        if site and not _SKIP_LINK.search(href) and site not in out:
            out.append(site)
    return out


def collect_fintech(client, mx, cfg: dict[str, Any], profile: Profile, log: Log = lambda _: None,
                    resolve=None) -> tuple[list[PoolCompany], dict[str, Any]]:
    """One fintech list -> verified PoolCompany records (sector fintech, source fintech:<id>)."""
    from applyflow.pool import never_cold_email
    sid, url = str(cfg["id"]), str(cfg["url"])
    stats: dict[str, Any] = {"found": 0, "kept": 0, "dropped": []}
    if not client.allowed_by_robots(url):
        stats["note"] = "skipped: robots.txt disallows it or can't be read"
        log(f"  {cfg.get('label', sid)}: {stats['note']}")
        return [], stats
    try:
        page = client.get_page(url)
    except FetchError as exc:
        stats["note"] = f"skipped: {exc}"
        return [], stats
    if cfg.get("kind") == "rbi":
        entries = [(rbi_company_name(n), "") for n in parse_rbi_entities(page)]
    else:
        entries = [("", site) for site in parse_sahamati(page)]
    stats["found"] = len(entries)
    out = []
    for name, site in entries:
        if FINTECH_EXCLUDE.search(f"{name} {site}"):
            stats["dropped"].append(f"{name or site}: bank / card network / PSU")
            continue
        if not site and resolve is not None:
            site = resolve(name)
        if not site:
            stats["dropped"].append(f"{name}: no website resolved")
            continue
        listed = never_cold_email(name, site_domain(site), profile)
        if listed:
            stats["dropped"].append(f"{name or site}: never_cold_email / IT services ({listed})")
            continue
        v = verify_company(client, mx, name, site)
        if not v.ok or FINTECH_EXCLUDE.search(v.name):
            stats["dropped"].append(f"{name or site}: {v.why if not v.ok else 'bank / PSU'}")
            log(f"    drop {name or site}: {v.why}")
            continue
        domain = site_domain(v.website)
        out.append(PoolCompany(key=f"pool:{domain}", name=v.name, website=v.website, domain=domain,
                               sources=[f"fintech:{sid}"], source_urls=[url],
                               description=(v.context.get("description") or "")[:200], locations="India",
                               tags=["Fintech", str(cfg.get("label", sid))], sector="fintech", region="india",
                               context=v.context))
        log(f"    keep {v.name:<28} {v.website} ({v.why})")
    stats["kept"] = len(out)
    stats["note"] = f"{stats['found']} listed, {stats['kept']} verified"
    log(f"  {cfg.get('label', sid)}: {stats['note']}")
    return out, stats


def india_friendly(client, profile: Profile, c: PoolCompany) -> str:
    """2f: a remote-global YC fintech counts as India-friendly when its YC jobs page has a posting in India or remote
    and open to India / anywhere: '<title> (<location>)', else ''."""
    from applyflow.refill import KEEP_LOCATIONS, YC_JOBS_URL, parse_yc_jobs_page, yc_posting_job
    from applyflow.scoring import score_job
    if not c.yc_slug:
        return ""
    try:
        company, postings = parse_yc_jobs_page(client.get_page(YC_JOBS_URL.format(slug=c.yc_slug)))
    except FetchError:
        return ""
    for p in postings:
        job = yc_posting_job(p, company)
        if score_job(job, profile).location_class in KEEP_LOCATIONS:
            return f"YC posting '{job.title}' ({job.location_text})"
    return ""


# ---------------------------------------------------------------------------
# Consultancies
# ---------------------------------------------------------------------------

def collect_consultancies(client, mx, seeds: list[dict[str, str]], profile: Profile,
                          log: Log = lambda _: None) -> tuple[list[PoolCompany], dict[str, Any]]:
    """The seed list's consultancies that verify on their own site (named, India office, MX) - sector consultancy."""
    from applyflow.pool import never_cold_email
    out, stats = [], {"found": len(seeds), "kept": 0, "dropped": []}
    for seed in seeds:
        name, site = str(seed["name"]), str(seed["website"])
        listed = never_cold_email(name, site_domain(company_website(site)), profile)
        if listed:
            stats["dropped"].append(f"{name}: never_cold_email / IT services ({listed})")
            continue
        v = verify_company(client, mx, name, site)
        if not v.ok:
            stats["dropped"].append(f"{name}: {v.why}")
            log(f"    drop {name}: {v.why}")
            continue
        domain = site_domain(v.website)
        size = seed.get("team_size")
        out.append(PoolCompany(key=f"pool:{domain}", name=name, website=v.website, domain=domain,
                               sources=["consultancy:seed"], source_urls=[f"config/sources.yaml (consultancies)"],
                               description=(v.context.get("description") or "")[:200],
                               locations=str(seed.get("locations") or "India"),
                               team_size=int(size) if isinstance(size, int) else None,
                               tags=["Product engineering consultancy"], sector="consultancy", region="india",
                               context=v.context))
        log(f"    keep {name:<28} {v.website} ({v.why})")
    stats["kept"] = len(out)
    stats["note"] = f"{stats['found']} seeds, {stats['kept']} verified"
    return out, stats


# ---------------------------------------------------------------------------
# GitHub header tables (rshetye/AwesomeIndiaStartups: Company Name | Product | Brief | ... | Website | ...)
# ---------------------------------------------------------------------------

def parse_header_table(text: str, cfg: dict[str, Any]) -> list[PoolCompany]:
    """Rows of a markdown table whose header names a company column; the website is the first cell that's a
    company's own site (not LinkedIn & co.), the description the 'brief' / 'description' column."""
    from applyflow.sources import RAW_GITHUB
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in (text or "").splitlines()
            if ln.strip().startswith("|")]
    if not rows:
        return []
    header = [h.lower() for h in rows[0]]
    name_col = next((i for i, h in enumerate(header) if "company" in h or h == "name"), 0)
    desc_col = next((i for i, h in enumerate(header) if "brief" in h or "description" in h), None)
    url = RAW_GITHUB.format(**{k: cfg[k] for k in ("repo", "branch", "path")})
    loc = str(cfg.get("locations") or "India")
    out = []
    for r in rows[1:]:
        if not r or set("".join(r)) <= set("-: "):
            continue
        name = clean_name(re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", r[name_col] if name_col < len(r) else ""))
        site = ""
        for cell in r:
            for link in re.findall(r"\((https?://[^)\s]+)\)", cell) + [cell]:
                site = company_website(link) if re.search(r"\.[a-z]{2,}", link) else ""
                if site:
                    break
            if site:
                break
        if not name or not site:
            continue
        desc = re.sub(r"<br\s*/?>|\\", " ", r[desc_col]) if desc_col is not None and desc_col < len(r) else ""
        out.append(PoolCompany(key=f"pool:{site_domain(site)}", name=name, website=site, domain=site_domain(site),
                               sources=[f"list:{cfg['id']}"], source_urls=[url], description=" ".join(desc.split())[:240],
                               locations=loc, region="india" if "india" in loc.lower() else "remote_global"))
    return out


# ---------------------------------------------------------------------------
# Adding to the stored pool
# ---------------------------------------------------------------------------

def extend_pool(store, profile: Profile, companies: list[PoolCompany], today: date, log: Log = lambda _: None):
    """Merge new companies into the stored pool (dedupe by domain), store their verified homepages as context, and
    redo the selection offline (pool.reselect). Returns (scored, changes, new keys)."""
    from applyflow.pool import Scored, reselect
    rows = store.pool_rows()
    before = {r["key"]: bool(r["selected"]) for r in rows}
    existing = [PoolCompany.from_json(json.loads(r["data"])) for r in rows]
    have = {c.domain for c in existing}
    merged = merge(existing + companies)
    for c in companies:
        if c.context:
            key = next((m.key for m in merged if m.domain == c.domain), c.key)
            if store.get_context(key) is None or c.domain not in have:
                store.save_context(key, c.context)
    for c in merged:
        c.context = {}
        c.sector = c.sector or pool_sector(c)
    store.save_pool([Scored(c, 0, [], before.get(c.key, False)) for c in merged])
    scored, changes = reselect(store, profile, today)
    new = [c.key for c in merged if c.domain not in have]
    log(f"  pool: {len(existing)} -> {len(merged)} companies ({len(new)} new); "
        f"{sum(1 for ch in changes if ch.now.selected)} newly selected")
    return scored, changes, new


# ---------------------------------------------------------------------------
# Sector of a target
# ---------------------------------------------------------------------------

def sector_for(name: str, prow, inputs, team_size: int | None = None, large: bool = False) -> str:
    """2f: fintech | consultancy | startup | other. A pool record's sector wins (its source list or its tags);
    else fintech words in the company's own text; else startup (up to 300 people or unknown), other (large)."""
    if prow is not None:
        c = PoolCompany.from_json(json.loads(prow["data"]))
        sector = c.sector or pool_sector(c)
        if sector:
            return sector
    from applyflow.sources import FINTECH_WORDS
    text = " ".join([name, inputs.one_liner, inputs.meta_description, inputs.homepage_title, inputs.note])
    if FINTECH_WORDS.search(text):
        return "fintech"
    big = large or (team_size is not None and team_size > 300)
    return "other" if big else "startup"


# ---------------------------------------------------------------------------
# Classifying "other" published addresses
# ---------------------------------------------------------------------------

@dataclass
class Classified:
    target_id: str
    company: str
    email: str
    kind: str          # hiring_inbox | general_inbox | unsendable
    reason: str
    source_url: str = ""


def classify_other_addresses(store, items=None, apply: bool = False, primary_only: bool = True) -> list[Classified]:
    """2f: every stored address of an unprotected target that contact_kind calls 'other' (the primary one by
    default, i.e. what 2e counted as "other published address") - what it is now. With `apply`, the unsendable ones
    are recorded in excluded_contacts (with the reason), so they stay out after re-lookups."""
    from applyflow.outreach import classify_published, contact_kind, target_id
    protected = store.protected_target_ids()
    excluded = store.excluded_contacts()
    out = []
    for c in store.conn.execute("SELECT * FROM contacts ORDER BY company, rank").fetchall():
        tid = target_id(c["company_key"])
        if tid in protected or (primary_only and not c["is_primary"]) or c["email"] in excluded:
            continue
        evidence = json.loads(c["evidence"]) if c["evidence"] else None
        if contact_kind(c["email"], c["confidence"], c["source_url"] or "", c["name"] or "", c["source"] or "",
                        evidence) != "other":
            continue
        kind, reason = classify_published(c["email"], c["role"] or "", c["source_url"] or "", c["company"] or "")
        out.append(Classified(tid, c["company"] or "", c["email"], kind, reason, c["source_url"] or ""))
    if apply:
        for c in out:
            if c.kind == "unsendable":
                store.exclude_contact(c.email, f"2f: {c.reason}")
    return out


def backfill_evidence(store, protected: set[str] | None = None, log: Log = lambda _: None) -> dict[str, int]:
    """2f, offline: record evidence on every stored guess of an unprotected target from its lookup's candidates
    (contact_checks.details), moving the guess to the address format the evidence shows; clear it where it fails."""
    from applyflow.contacts import Contact, evidence_for_guess
    from applyflow.outreach import target_id
    protected = store.protected_target_ids() if protected is None else protected
    checks = {c["company_key"]: c for c in store.contact_checks()}
    names = store.known_first_names()
    counts = {"guesses": 0, "evidence": 0, "moved": 0}
    for row in store.conn.execute("SELECT * FROM contacts WHERE confidence = 'guessed' ORDER BY company_key, rank"
                                  ).fetchall():
        if target_id(row["company_key"]) in protected or (row["source"] or "") != "pattern":
            continue
        check = checks.get(row["company_key"])
        if check is None:
            continue
        counts["guesses"] += 1
        details = json.loads(check["details"] or "{}")
        considered = details.get("considered", [])
        me = next((c for c in considered if c.get("email") == row["email"]), {})
        from applyflow.contacts import role_group
        guess = Contact(name=row["name"] or "", role=row["role"] or "", email=row["email"], confidence="guessed",
                        source="pattern", source_url=row["source_url"] or me.get("source_url", ""),
                        group=me.get("group") or role_group(row["role"] or ""), mx=row["mx"] or "",
                        alternates=json.loads(row["alternates"] or "[]"))
        evidence, why = evidence_for_guess(guess, considered, check["domain"] or "", check["company"] or "",
                                           guess.mx, names)
        taken = {c["email"] for c in store.contacts_for(row["company_key"]) if c["id"] != row["id"]}
        if evidence and evidence["address"] not in taken:
            alternates = [a for a in [row["email"]] + guess.alternates if a != evidence["address"]]
            store.set_contact_evidence(row["id"], evidence["address"], alternates, evidence)
            counts["evidence"] += 1
            counts["moved"] += evidence["address"] != row["email"]
            log(f"  {check['company']:<28} {evidence['address']:<32} <- {evidence['published']} ({evidence['format']})")
        elif row["evidence"]:
            store.set_contact_evidence(row["id"], row["email"], guess.alternates, None)
    return counts


# ---------------------------------------------------------------------------
# Manual referral list (large / apply-only companies with an open role that fits)
# ---------------------------------------------------------------------------

REFERRAL_FIELDS = ["company", "role_title", "job_link", "linkedin_note", "why"]


def referral_note(company: str, role: str, project_line: str) -> str:
    """A LinkedIn connection note (<= 300 characters): the role, one line about my projects, the referral ask."""
    role = re.sub(r"\s+", " ", role).strip()
    for r in (role, re.split(r"\s+[-–(|,]\s*", role)[0]):
        note = (f"Hi! I'm Netal, a final-year B.Tech IT student at VIT Vellore. I'm applying for the {r} role at "
                f"{company}. {project_line} Would you be open to referring me if my profile fits?")
        if len(note) <= 300:
            return note
    return (f"Hi! I'm Netal, a final-year B.Tech IT student (VIT Vellore) applying to {company}. {project_line} "
            f"Would you be open to referring me?")[:300]


PROJECT_LINES = {
    "backend": "I'm building CareerForge AI, a FastAPI + PostgreSQL platform with 15 JWT-secured REST endpoints.",
    "ai": "I'm building an NLP system over 1,500+ court judgments with a Neo4j knowledge graph.",
    "cloud": "I built a serverless file service on 8 AWS Lambda functions with sub-300 ms retrieval.",
}


def referral_rows(store, profile: Profile, items) -> list[dict[str, str]]:
    """Large or apply-only targets with an open role that fits me (A/B openings, or a role match), one row per
    company: company, role title, job link and a LinkedIn note. No emails, no automation: you send these."""
    from applyflow.scoring import APPLY_ONLY_HINT
    matches = store.role_matches()
    rows, seen = [], set()
    roles_by_company: dict[str, list] = {}
    for r in store.role_rows():
        roles_by_company.setdefault(name_key(r["company"]), []).append(r)
    for it in items:
        large = it.large or it.status == "apply_only" or (it.team_size or 0) > 300
        if not large or it.company_id in seen:
            continue
        role = next((r for r in roles_by_company.get(name_key(it.company), [])), None)
        title, link, family = "", "", ""
        if role is not None:
            title, link, family = role["title"], role["url"] or role["apply_url"] or "", role["family"] or ""
        else:
            m = next((m for m in matches.get(it.company_key, []) if m["match"] in ("yes", "declined") and m["title"]), None)
            if m is not None:
                title, link = m["title"], m["url"] or ""
        if not title or not link:
            continue
        kind = "ai" if re.search(r"\b(ai|ml|machine learning|data|nlp|llm)\b", f"{family} {title}", re.I) else \
            "cloud" if re.search(r"cloud|devops|platform|sre|infra", f"{family} {title}", re.I) else "backend"
        seen.add(it.company_id)
        why = "apply-only (large company)" if it.status == "apply_only" or it.large else "over 300 people"
        rows.append({"company": it.company, "role_title": title, "job_link": link,
                     "linkedin_note": referral_note(it.company, title, PROJECT_LINES[kind]), "why": why})
    for r in store.role_rows():                       # A/B openings at apply-only companies with no target row
        if r["contact_hint"] != APPLY_ONLY_HINT or name_key(r["company"]) in {name_key(x["company"]) for x in rows}:
            continue
        kind = "ai" if re.search(r"ai|ml|data", r["family"] or "") else \
            "cloud" if re.search(r"cloud|platform", r["family"] or "") else "backend"
        rows.append({"company": r["company"], "role_title": r["title"], "job_link": r["url"] or r["apply_url"] or "",
                     "linkedin_note": referral_note(r["company"], r["title"], PROJECT_LINES[kind]),
                     "why": f"apply via posting (tier {r['tier']}, score {r['score']})"})
    return rows


def write_referrals(rows: list[dict[str, str]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=REFERRAL_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


def supply_summary(items, gates_cfg: dict, daily: int = 30) -> dict[str, Any]:
    """2f: ready, unsent, sendable targets by kind and by sector, and the sending days they cover at `daily` a day
    with hello@ and evidence guesses held to their caps (pure guesses and unsendable addresses never count)."""
    from applyflow.outreach import SENDABLE_KINDS
    by_kind: dict[str, int] = {}
    by_sector: dict[str, int] = {}
    pending = []
    for it in items:
        if it.status != "ready" or it.address is None or it.queued_on:
            continue
        k = it.address.kind
        by_kind[k] = by_kind.get(k, 0) + 1
        if k in SENDABLE_KINDS:
            by_sector[it.sector] = by_sector.get(it.sector, 0) + 1
            pending.append(k)
    caps = {"general_inbox": int(gates_cfg.get("general_inbox_per_day") or 5),
            "evidence_guess": int(gates_cfg["type_gates"]["evidence_guess_per_day"])}
    left = {k: pending.count(k) for k in set(pending)}
    days, full_days = 0, 0
    while sum(left.values()) and days < 1000:
        room = daily
        for k in ("verified_person", "founders", "hiring_inbox", "evidence_guess", "general_inbox"):
            take = min(left.get(k, 0), room, caps.get(k, room))
            left[k] = left.get(k, 0) - take
            room -= take
        if room == daily:
            break
        days += 1
        full_days += room == 0
    return {"by_kind": by_kind, "by_sector": by_sector, "sendable": len(pending), "days": days, "full_days": full_days}
