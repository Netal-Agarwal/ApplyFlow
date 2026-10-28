"""Phase 2c: company sources for the outreach pool.

Each collector turns one free, public source into `PoolCompany` records; `merge` then
dedupes them by website domain, keeping every source's data. Sources (config/sources.yaml):

  (a) yc        - yc-oss's full YC directory (`all.json`, any hiring status): active companies
                  located in India, plus a capped set of "Fully Remote" teams of 10-150 people
                  (`remote_global`, ranked below India)
  (b) portfolio - accelerator / VC portfolio pages, one generic regex collector per page
  (c) funding   - Inc42's weekly "Funding Galore" tables: a `funding` signal (round, amount, date,
                  source URL) matched to pool companies by domain or name; a startup that matches
                  nothing gets its website from its Inc42 profile page, or is dropped
  (d) lists     - curated GitHub lists of Indian startups, read from raw.githubusercontent.com

Everything goes through the polite client: robots.txt for web pages, 1 request/second/host.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urljoin

from applyflow.discovery import name_key, site_domain
from applyflow.http import FetchError
from applyflow.text import html_to_text

Log = Callable[[str], None]

YC_ALL_URL = "https://yc-oss.github.io/api/companies/all.json"
RAW_GITHUB = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"

# Hosts that are never a company's own website.
_NOT_A_COMPANY = re.compile(
    r"(^|\.)(linkedin\.com|twitter\.com|x\.com|facebook\.com|instagram\.com|youtube\.com|youtu\.be|medium\.com|"
    r"github\.com|play\.google\.com|apps\.apple\.com|google\.com|wa\.me|t\.me|crunchbase\.com|angel\.co|"
    r"wellfound\.com|tracxn\.com|inc42\.com|yourstory\.com|entrackr\.com|ycombinator\.com|hsforms\.com|"
    r"bit\.ly|linktr\.ee|notion\.so|substack\.com|economictimes\.indiatimes\.com|techcrunch\.com)$", re.I)

# whose data wins when sources disagree (2f: fintech lists and consultancy seeds after funding news)
SOURCE_PRIORITY = ["yc", "portfolio", "funding", "fintech", "consultancy", "saas", "list"]

# 2f: fintech words in a company's tags, sector or one-liner.
FINTECH_WORDS = re.compile(r"\b(?:fin-?tech|payments?|payment gateway|lending|lender|loans?|credit (?:card|line|score|"
                           r"bureau|infrastructure)|credit and lending|banking(?: and exchange)?|neo-?bank|upi|"
                           r"wealth-?tech|wealth management|insur-?tech|insurance|nbfc|remittances?|forex|treasury|"
                           r"invoic(?:e|es|ing)|account aggregator|kyc|card issuing|bnpl|brokerage|mutual funds?|"
                           r"consumer finance|asset management|financial services|stablecoins?|digital gold|"
                           r"accounts payable|accounts receivable|collections)\b", re.I)


@dataclass
class FundingRound:
    company: str
    round: str = ""              # "Series A", "Seed", "" when not stated
    amount: str = ""             # "$12 Mn", as reported
    date: str = ""               # YYYY-MM-DD the round was reported
    sector: str = ""
    subsector: str = ""
    source: str = ""             # inc42
    source_url: str = ""         # the article it came from
    website: str = ""            # resolved website, if any

    def describe(self) -> str:
        bits = [self.round or "funding round", self.amount, self.date]
        return " ".join(b for b in bits if b) + (f" ({self.source_url})" if self.source_url else "")


@dataclass
class PoolCompany:
    key: str                     # yc:<slug> for YC companies, pool:<domain> otherwise
    name: str
    website: str
    domain: str
    sources: list[str] = field(default_factory=list)          # "yc", "portfolio:axilor", "funding:inc42", ...
    source_urls: list[str] = field(default_factory=list)
    description: str = ""
    locations: str = ""
    team_size: int | None = None
    batch: str = ""
    founded_year: int | None = None
    tags: list[str] = field(default_factory=list)              # industries, tags, sectors
    hiring: bool | None = None                                 # YC's isHiring flag
    region: str = "india"                                      # india | remote_global
    yc_slug: str = ""
    funding: list[FundingRound] = field(default_factory=list)
    sector: str = ""                                           # 2f: fintech | consultancy | '' (startup / other)
    india_friendly: str = ""                                   # 2f: why a remote-global company hires in India
    context: dict[str, str] = field(default_factory=dict)      # 2f: verified homepage, stored as company context

    @property
    def kind(self) -> str:
        return self.sources[0].split(":", 1)[0] if self.sources else ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PoolCompany":
        data = dict(data)
        data["funding"] = [FundingRound(**f) for f in data.get("funding") or []]
        return cls(**data)


def _clean(text: str) -> str:
    return " ".join(html_to_text(htmllib.unescape(text or "")).split()).strip()


def clean_name(name: str) -> str:
    """'SQUADSTACK**' -> 'Squadstack'; 'Desible.ai' stays; 'IDFC' (short caps) stays."""
    name = re.sub(r"[*†‡]+$", "", _clean(name)).strip(" -|")
    if name.isupper() and len(name) > 4:
        name = name.title()
    return name


_HOST = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
                   r"\.[a-z]{2,})(?=[/:?#]|$)")


def company_website(url: str) -> str:
    """A normalised https URL for a company's own site, or '' (social, aggregator, no domain).

    Hand-made lists hold "a.com, www.b.com", "x.com (coming soon)" or an email: only the first real
    hostname counts, and an email address is not a website."""
    first = re.split(r"[\s,;|]+|\s\(", (url or "").strip().lower(), maxsplit=1)[0]
    if "@" in first.split("/", 3)[-1 if "://" in first else 0]:
        return ""
    m = _HOST.match(first)
    domain = m.group(1) if m else ""
    if not domain or _NOT_A_COMPANY.search(domain):
        return ""
    return f"https://{domain}"


def _pool_key(domain: str) -> str:
    return f"pool:{domain}"


# ---------------------------------------------------------------------------
# (a) YC: the whole directory
# ---------------------------------------------------------------------------

def batch_year(batch: str) -> int | None:
    """'Winter 2024' -> 2024, 'S21' / 'X25' -> 2021 / 2025."""
    m = re.search(r"(20\d\d)", batch or "")
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"[WSFX](\d\d)", (batch or "").strip(), re.I)
    return 2000 + int(m.group(1)) if m else None


def yc_pool(data: list[dict[str, Any]], remote_min: int = 10, remote_max: int = 150) -> list[PoolCompany]:
    """Active YC companies located in India (any hiring status), and remote-global ones of 10-150 people."""
    out = []
    for raw in data:
        if str(raw.get("status", "")).lower() != "active":
            continue
        website = company_website(str(raw.get("website") or ""))
        if not website:
            continue
        regions = [str(r) for r in raw.get("regions") or []]
        locations = str(raw.get("all_locations") or "")
        size = raw.get("team_size") if isinstance(raw.get("team_size"), int) else None
        india = bool(re.search(r"\bIndia\b", " ; ".join([locations] + regions)))
        remote = "Fully Remote" in regions and size is not None and remote_min <= size <= remote_max
        if not (india or remote):
            continue
        slug = str(raw.get("slug") or raw.get("id") or "")
        launched = raw.get("launched_at")
        year = batch_year(str(raw.get("batch") or ""))
        if year is None and isinstance(launched, (int, float)):
            year = datetime.fromtimestamp(launched, tz=timezone.utc).year
        out.append(PoolCompany(
            key=f"yc:{slug}", name=str(raw.get("name") or "").strip(), website=website, domain=site_domain(website),
            sources=["yc"], source_urls=[f"https://www.ycombinator.com/companies/{slug}"],
            description=str(raw.get("one_liner") or "").strip(),
            locations=locations or " ; ".join(regions), team_size=size, batch=str(raw.get("batch") or ""),
            founded_year=year,
            tags=[str(t) for t in (raw.get("industries") or []) + (raw.get("tags") or [])],
            hiring=raw.get("isHiring") if isinstance(raw.get("isHiring"), bool) else None,
            region="india" if india else "remote_global", yc_slug=slug))
    return out


# ---------------------------------------------------------------------------
# (b) Portfolio pages: one generic collector, configured per page
# ---------------------------------------------------------------------------

def parse_portfolio(page: str, cfg: dict[str, Any], page_url: str = "") -> list[PoolCompany]:
    """Companies on one portfolio page, as configured in sources.yaml (see the file's header)."""
    def grab(pattern: str | None, block: str) -> str:
        if not pattern:
            return ""
        m = re.search(pattern, block, re.S | re.I)
        return m.group(1) if m else ""

    sid = str(cfg["id"])
    out = []
    for m in re.finditer(cfg["item"], page, re.S | re.I):
        block = m.group(0)
        if cfg.get("keep") and not re.search(cfg["keep"], block, re.S | re.I):
            continue
        name = clean_name(grab(cfg["name"], block))
        website = company_website(urljoin(page_url, htmllib.unescape(grab(cfg["website"], block))))
        if not name or not website:
            continue
        year = grab(cfg.get("year"), block)
        location = _clean(grab(cfg.get("location"), block)) or str(cfg.get("locations") or "")
        out.append(PoolCompany(
            key=_pool_key(site_domain(website)), name=name, website=website, domain=site_domain(website),
            sources=[f"portfolio:{sid}"], source_urls=[page_url or str(cfg.get("url") or "")],
            description=_clean(grab(cfg.get("description"), block)), locations=location,
            founded_year=int(year) if year.isdigit() else None,
            region="india" if "india" in location.lower() else "remote_global"))
    return out


def collect_portfolio(client, cfg: dict[str, Any], log: Log = lambda _: None) -> tuple[list[PoolCompany], str]:
    """Every page of one portfolio (robots.txt checked by get_page). Returns (companies, note)."""
    url = str(cfg["url"])
    pages = cfg.get("pages") or {}
    found: dict[str, PoolCompany] = {}
    read = 0
    for n in range(1, int(pages.get("max", 1)) + 1):
        page_url = url if n == 1 else f"{url}?{pages['param']}={n}"
        try:
            page = client.get_page(page_url)
        except FetchError as exc:
            if n == 1:
                return [], f"skipped: {exc}"
            break
        read += 1
        if not re.search(cfg["item"], page, re.S | re.I):     # past the last page
            break
        for c in parse_portfolio(page, cfg, page_url):
            found.setdefault(c.domain, c)
    log(f"  {cfg.get('label', cfg['id'])}: {len(found)} companies from {read} page(s)")
    return list(found.values()), f"{len(found)} companies from {read} page(s)"


# ---------------------------------------------------------------------------
# (c) Funding news: Inc42 "Funding Galore" weekly tables
# ---------------------------------------------------------------------------

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)


def parse_feed(xml: str) -> list[dict[str, str]]:
    """[{url, published (YYYY-MM-DD), content}] from an RSS feed."""
    out = []
    for item in _ITEM.findall(xml or ""):
        link = re.search(r"<link>\s*([^<\s]+)\s*</link>", item)
        pub = re.search(r"<pubDate>([^<]+)</pubDate>", item)
        content = re.search(r"<content:encoded>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</content:encoded>", item, re.S)
        try:
            published = parsedate_to_datetime(pub.group(1).strip()).date().isoformat() if pub else ""
        except (TypeError, ValueError):
            published = ""
        out.append({"url": link.group(1) if link else "", "published": published,
                    "content": content.group(1) if content else ""})
    return out


def _date(text: str) -> str:
    """'8 Oct 2026' -> '2026-10-08'; '' when unreadable."""
    text = _clean(text)
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _shift(day: str, days: int) -> str:
    from datetime import timedelta
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _dash(text: str) -> str:
    text = _clean(text)
    return "" if text in ("-", "–", "—", "") else text


def parse_funding_table(page: str, source_url: str, published: str = "", source: str = "inc42") -> list[FundingRound]:
    """Rows of a 'Date | Name | Sector | Subsector | ... | Funding Round Size | Funding Round Type' table."""
    rounds = []
    for table in re.findall(r"<table\b.*?</table>", page or "", re.S | re.I):
        rows = [[_clean(c) for c in _CELL.findall(r)] for r in _ROW.findall(table)]
        if not rows:
            continue
        header = [h.lower() for h in rows[0]]
        if "name" not in header:
            continue

        def col(row: list[str], *names: str) -> str:
            for n in names:
                if n in header and header.index(n) < len(row):
                    return row[header.index(n)]
            return ""
        for row in rows[1:]:
            name = clean_name(col(row, "name", "startup name", "startup"))
            if not name:
                continue
            amount = re.sub(r"\s*\*+$", "", _dash(col(row, "funding round size", "amount")))
            day = _date(col(row, "date"))
            if not day or (published and not (_shift(published, -40) <= day <= published)):
                day = published              # a typo in the table ("2926-10-05"): the article's date instead
            rounds.append(FundingRound(
                company=name, round=_dash(col(row, "funding round type", "round")), amount=amount,
                date=day, sector=_dash(col(row, "sector")),
                subsector=_dash(col(row, "subsector")), source=source, source_url=source_url))
    return rounds


def inc42_slug(name: str) -> str:
    """'Desible.ai' -> 'desible-ai', 'Sunfox Technologies' -> 'sunfox-technologies'."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def website_from_profile(page: str) -> str:
    """The first link on a startup's Inc42 profile page that's a company website."""
    for url in re.findall(r'href="(https?://[^"]+)"', page or ""):
        site = company_website(url)
        if site:
            return site
    return ""


def collect_funding(client, cfg: dict[str, Any], log: Log = lambda _: None) -> tuple[list[FundingRound], str]:
    """Every round in the feed's recent Funding Galore articles (the feed carries each article's table)."""
    try:
        xml = client.get_text(str(cfg["feed"]))
    except FetchError as exc:
        return [], f"skipped: {exc}"
    articles = parse_feed(xml)[: int(cfg.get("max_articles", 30))]
    rounds: list[FundingRound] = []
    for a in articles:
        rows = parse_funding_table(a["content"], a["url"], a["published"], str(cfg["id"]))
        if not rows and a["url"]:
            try:
                rows = parse_funding_table(client.get_page(a["url"]), a["url"], a["published"], str(cfg["id"]))
            except FetchError:
                rows = []
        rounds += rows
    dates = sorted(r.date for r in rounds if r.date)
    note = f"{len(rounds)} rounds in {len(articles)} weekly article(s)" + (
        f", {dates[0]} to {dates[-1]}" if dates else "")
    log(f"  {cfg.get('label', cfg['id'])}: {note}")
    return rounds, note


# ---------------------------------------------------------------------------
# (d) Curated GitHub lists
# ---------------------------------------------------------------------------

_MD_LINK_ROW = re.compile(r"^\|\s*\[([^\]]+)\]\((https?://[^)\s]+)\)\s*\|(.*)$")


def parse_name_url_json(data: dict[str, str], cfg: dict[str, Any]) -> list[PoolCompany]:
    """{'Name': 'http://site'} (hemanth/bangalore-startups)."""
    out = []
    for name, url in (data or {}).items():
        website = company_website(str(url or ""))
        if website and clean_name(str(name)):
            out.append(_list_company(clean_name(str(name)), website, "", cfg))
    return out


def parse_markdown_table(text: str, cfg: dict[str, Any]) -> list[PoolCompany]:
    """'| [Name](https://site) | Sector | Stage | What it does |' rows."""
    out = []
    for line in (text or "").splitlines():
        m = _MD_LINK_ROW.match(line.strip())
        if not m:
            continue
        website = company_website(m.group(2))
        cells = [c.strip() for c in m.group(3).split("|") if c.strip()]
        if website:
            desc = cells[-1] if cells else ""
            c = _list_company(clean_name(m.group(1)), website, desc, cfg)
            c.tags = cells[:1]
            out.append(c)
    return out


def _list_company(name: str, website: str, description: str, cfg: dict[str, Any]) -> PoolCompany:
    url = RAW_GITHUB.format(**{k: cfg[k] for k in ("repo", "branch", "path")})
    loc = str(cfg.get("locations") or "India")
    return PoolCompany(key=_pool_key(site_domain(website)), name=name, website=website, domain=site_domain(website),
                       sources=[f"list:{cfg['id']}"], source_urls=[url], description=description, locations=loc,
                       region="india" if "india" in loc.lower() else "remote_global")


def collect_list(client, cfg: dict[str, Any], log: Log = lambda _: None) -> tuple[list[PoolCompany], str]:
    url = RAW_GITHUB.format(**{k: cfg[k] for k in ("repo", "branch", "path")})
    try:
        if cfg.get("format") == "name_url_json":
            companies = parse_name_url_json(json.loads(client.get_text(url)), cfg)
        elif cfg.get("format") == "header_table":            # 2f: | Company Name | ... | website | ...
            from applyflow.supply import parse_header_table
            companies = parse_header_table(client.get_text(url), cfg)
        else:
            companies = parse_markdown_table(client.get_text(url), cfg)
    except (FetchError, ValueError) as exc:
        return [], f"skipped: {exc}"
    note = f"{len(companies)} companies with a website ({cfg['repo']})"
    log(f"  {cfg['id']}: {note}")
    return companies, note


# ---------------------------------------------------------------------------
# Dedupe and merge
# ---------------------------------------------------------------------------

def _priority(c: PoolCompany) -> int:
    kind = c.kind
    return SOURCE_PRIORITY.index(kind) if kind in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def merge(companies: list[PoolCompany]) -> list[PoolCompany]:
    """One record per website domain. The highest-priority source (yc > portfolio > funding > list)
    gives the key and name; empty fields are filled from the others; sources, tags and funding are
    unioned. India wins over remote_global when any source places the company in India."""
    by_domain: dict[str, PoolCompany] = {}
    for c in sorted(companies, key=_priority):
        if not c.domain:
            continue
        have = by_domain.get(c.domain)
        if have is None:
            by_domain[c.domain] = PoolCompany.from_json(c.to_json())
            continue
        for src in c.sources:
            if src not in have.sources:
                have.sources.append(src)
        for url in c.source_urls:
            if url not in have.source_urls:
                have.source_urls.append(url)
        for attr in ("description", "locations", "batch", "yc_slug", "sector", "india_friendly"):
            if not getattr(have, attr) and getattr(c, attr):
                setattr(have, attr, getattr(c, attr))
        for attr in ("team_size", "founded_year", "hiring"):
            if getattr(have, attr) is None and getattr(c, attr) is not None:
                setattr(have, attr, getattr(c, attr))
        have.tags += [t for t in c.tags if t not in have.tags]
        for f in c.funding:
            if (f.date, f.round, f.source_url) not in {(g.date, g.round, g.source_url) for g in have.funding}:
                have.funding.append(f)
        if c.region == "india" and have.region != "india" and have.kind != "yc":
            have.region = "india"
    return list(by_domain.values())


def attach_funding(pool: list[PoolCompany], rounds: list[FundingRound], client=None, cfg: dict | None = None,
                   resolved: dict[str, str] | None = None, log: Log = lambda _: None,
                   wanted: Callable[[FundingRound], bool] = lambda _: True,
                   resolve: Callable[[FundingRound], str] | None = None) -> tuple[list[PoolCompany], dict]:
    """Attach each round to the pool company it names (by website domain, else by name). A round
    matching nothing gets the startup's website from its Inc42 profile (when `client` is given and
    `wanted(round)`) - or from `resolve(round)` (2e: Entrackr, likely domains) - becoming a new pool
    company; still unresolved, it's dropped.

    `resolved` caches name_key -> website ('' = looked up, none) across runs. Returns (pool, stats)."""
    resolved = resolved if resolved is not None else {}
    by_domain = {c.domain: c for c in pool}
    by_name: dict[str, PoolCompany] = {}
    for c in pool:
        k = name_key(c.name)
        if len(k) >= 3:
            by_name.setdefault(k, c)
    stats = {"rounds": len(rounds), "matched": 0, "resolved": 0, "dropped": 0, "lookups": 0}
    new: dict[str, PoolCompany] = {}
    for r in rounds:
        key = name_key(r.company)
        target = (by_domain.get(site_domain(r.website)) if r.website else None) or by_name.get(key)
        if target is None and key in new:
            target = new[key]
        if target is None:
            site = resolved.get(key)
            if site is None and resolve is not None and wanted(r):
                stats["lookups"] += 1
                site = resolved[key] = resolve(r)
            elif site is None and client is not None and cfg and cfg.get("company_page") and wanted(r):
                stats["lookups"] += 1
                url = str(cfg["company_page"]).format(slug=inc42_slug(r.company))
                try:
                    site = website_from_profile(client.get_page(url))
                except FetchError:
                    site = ""
                resolved[key] = site
            if site:
                domain = site_domain(site)
                target = by_domain.get(domain)
                if target is None:
                    target = PoolCompany(key=_pool_key(domain), name=r.company, website=site, domain=domain,
                                         sources=[f"funding:{r.source}"], source_urls=[r.source_url],
                                         tags=[t for t in (r.sector, r.subsector) if t], locations="India",
                                         description=r.sector if r.source == "entrackr" else "")
                    new[key] = by_domain[domain] = target
                    stats["resolved"] += 1
        if target is None:
            stats["dropped"] += 1
            continue
        if target is not new.get(key):
            stats["matched"] += 1
        squash = lambda n: re.sub(r"[^a-z0-9]", "", n.lower())
        if squash(r.company) == squash(target.name) and r.company != target.name and \
                len(re.findall(r"[^A-Za-z0-9]|[A-Z]", r.company)) > len(re.findall(r"[^A-Za-z0-9]|[A-Z]", target.name)):
            target.name = r.company          # "Desibleai" (a logo's alt text) -> "Desible.ai"
        r.website = r.website or target.website
        if f"funding:{r.source}" not in target.sources:
            target.sources.append(f"funding:{r.source}")
        if r.source_url not in target.source_urls:
            target.source_urls.append(r.source_url)
        target.tags += [t for t in (r.sector, r.subsector) if t and t not in target.tags]
        if (r.date, r.round, r.source_url) not in {(f.date, f.round, f.source_url) for f in target.funding}:
            target.funding.append(r)
    return pool + list(new.values()), stats


def pool_sector(c: PoolCompany) -> str:
    """2f: 'consultancy' (from the consultancy seeds), 'saas' (2g: the product / SaaS seeds), 'fintech' (a fintech list, or fintech words in its tags,
    funding sector or one-liner), else ''."""
    if any(s.startswith("consultancy:") for s in c.sources):
        return "consultancy"
    if any(s.startswith("saas:") for s in c.sources):
        return "saas"
    if any(s.startswith("fintech:") for s in c.sources):
        return "fintech"
    text = " ".join(c.tags + [f"{r.sector} {r.subsector}" for r in c.funding] + [c.description])
    return "fintech" if FINTECH_WORDS.search(text) else ""


def months_ago(day: str, today: date) -> float | None:
    try:
        d = date.fromisoformat(day)
    except (TypeError, ValueError):
        return None
    return (today - d).days / 30.44


def latest_funding(c: PoolCompany) -> FundingRound | None:
    dated = [f for f in c.funding if f.date and f.source_url]
    return max(dated, key=lambda f: f.date) if dated else None


def dumps(companies: list[PoolCompany]) -> str:
    return json.dumps([c.to_json() for c in companies])
