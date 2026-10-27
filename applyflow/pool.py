"""Phase 2c: the outreach pool - every company from every source, scored by how likely it is to
hire a backend / cloud / AI intern or 2027 new grad, and the top ~400 selected as targets.

Hiring-likelihood score (0-100), weights in profile.yaml `pool.weights`, every factor a reason:
  recent funding (6 months > 12 months), team size (10-150 best; 2-9 and 151-500 lower), a careers
  page or job board, tech fit from the one-liner / tags / site text, a preferred city, a recent YC
  batch or founding year, and India vs remote_global.

Existing role targets (A/B openings), manual targets and companies already worked on (a contact
lookup, a written draft, a queue entry) always stay in. The rest of the ~400 slots go to the best
scores, with at most `remote_global_cap` remote-global companies and none above `max_team_size`.

Phase 2f: a large company (over 300 people, or a late / big round with no headcount) is selected as a
"hiring inbox only" target unless it's on `never_cold_email` or a huge IT-services firm (`pool.it_services`);
YC companies in India that YC lists as hiring, fintechs (India, or a remote YC fintech with a posting open
to India) and product-engineering consultancies (`sector: consultancy`, exempt from the consulting / services
words but held to a tech-fit floor) are selected even past `target_size`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from applyflow.config import Profile, load_sources
from applyflow.discovery import name_key, site_domain
from applyflow.scoring import MARKETPLACE_HINT
from applyflow.sources import (YC_ALL_URL, FundingRound, PoolCompany, attach_funding, collect_funding, collect_list,
                               collect_portfolio, latest_funding, merge, months_ago, pool_sector, yc_pool)
from applyflow.text import matching_phrases

Log = Callable[[str], None]


def _age(day: str, today: date) -> float:
    """Months since `day`; 99 when there's no usable date."""
    age = months_ago(day, today)
    return 99.0 if age is None else age


@dataclass
class Scored:
    company: PoolCompany
    score: int
    reasons: list[str]
    selected: bool = False
    rank: int = 0
    why_selected: str = ""


_SERIES = re.compile(r"(?<![a-z])(pre[- ]?)?series[- ]?([a-z])\b", re.I)
_LATE_WORDS = re.compile(r"pre-?ipo|\bipo\b|growth|late[- ]stage", re.I)


def _amount_mn(amount: str) -> float:
    """'$12 Mn' -> 12.0, '$1.2 Bn' -> 1200.0, '$250K' -> 0.25; 0 when unreadable (INR amounts aren't converted)."""
    m = re.search(r"\$\s*([\d.,]+)\s*(mn|million|m|bn|billion|b|k|thousand)?\b", amount or "", re.I)
    if not m:
        return 0.0
    digits = m.group(1).replace(",", "")
    value = float(digits) if digits.replace(".", "", 1).isdigit() else 0.0
    unit = (m.group(2) or "").lower()
    if unit.startswith("b"):
        return value * 1000
    if unit in ("k", "thousand"):
        return value / 1000
    return value


def late_round(round_name: str, min_series: str = "C") -> bool:
    """'Series C' / 'Series E' / 'pre-IPO' are late for min_series C; 'Pre-Series C' and 'Series B' aren't."""
    m = _SERIES.search(round_name or "")
    if m:
        return not m.group(1) and m.group(2).lower() >= min_series.strip().lower()[:1]
    return bool(_LATE_WORDS.search(round_name or ""))


def never_cold_email(name: str, domain: str, profile: Profile) -> str:
    """The `never_cold_email` entry (a name or a domain) this company matches - or a huge IT-services firm from
    `pool.it_services` (2f) - or ''."""
    for entry in list(profile.never_cold_email) + list(profile.pool.get("it_services") or []):
        e = entry.strip().lower()
        if not e:
            continue
        if ("." in e and domain and (domain == e or domain.endswith("." + e))) or name_key(e) == name_key(name):
            return entry
    return ""


def large_reason(c: PoolCompany, profile: Profile) -> str:
    """Why an unknown-size company counts as large (apply-only, never cold-emailed), or ''. The
    `never_cold_email` list applies whatever the size."""
    listed = never_cold_email(c.name, c.domain, profile)
    if listed:
        return f"on the never_cold_email list ({listed})"
    if c.team_size is not None:
        return ""
    cfg = profile.pool["large_company"]
    for f in c.funding:
        if late_round(f.round, str(cfg["min_series"])):
            return f"{f.round} round with no headcount (Series {cfg['min_series']}+), likely 300+ people"
        if _amount_mn(f.amount) >= float(cfg["min_amount_mn"]):
            return f"raised {f.amount} in one round (${cfg['min_amount_mn']} Mn+), likely 300+ people"
    return ""


def funding_only(c: PoolCompany) -> bool:
    return bool(c.sources) and all(s.startswith("funding:") for s in c.sources)


def product_fit(c: PoolCompany, profile: Profile, site_text: str = "") -> tuple[str, list[str]]:
    """Phase 2c.1: is this a software-product company? ('', notes) when it passes, else (why it fails, notes).

    Applies to pool companies not from YC. A name like '... Capital' with investing words, or a non-software
    business (biofuel, solar, hardware, logistics operators, wholesale, lenders, services, ...) in the name,
    sector, one-liner or homepage fails unless its homepage (`site_text`) or one-liner names a software product
    (at least `software_product_min` product phrases). A company found only in funding news must also reach
    `funding_only_min_tech` tech-fit points (an Inc42 software subsector counts as one more product word)."""
    cfg = profile.pool
    notes: list[str] = []
    if any(s == "yc" for s in c.sources):
        return "", notes
    sectors = " ".join(f"{r.sector} {r.subsector}" for r in c.funding)
    own = " ".join([c.name, c.description, " ".join(c.tags), sectors, site_text])
    product = matching_phrases(f"{c.description} {site_text}", [w.lower() for w in cfg["software_product"]])
    shows_product = len(product) >= int(cfg["software_product_min"])
    if product:
        notes.append(f"product words: {', '.join(product[:5])}")
    invest_name = matching_phrases(c.name, [w.lower() for w in cfg["investment_name"]])
    invest_words = matching_phrases(f"{c.description} {site_text}", [w.lower() for w in cfg["investment_words"]])
    if invest_name and invest_words and not shows_product:
        return (f"investment firm, not a software product ('{invest_name[0]}' in the name; site: "
                f"{', '.join(invest_words[:3])})"), notes
    non_sw = matching_phrases(own, [w.lower() for w in cfg["non_software"]])
    if non_sw and not shows_product:
        return (f"not a software-product business ({', '.join(non_sw[:3])}); neither its homepage nor its "
                f"one-liner names a software product"), notes
    if non_sw:
        notes.append(f"{', '.join(non_sw[:2])}, but it names a software product")
    if funding_only(c):
        fit, words = tech_fit(own, profile)
        sw = sorted({r.subsector for r in c.funding
                     if r.subsector.strip().lower() in [x.lower() for x in cfg["software_sectors"]]})
        if sw and fit in ("mid", "unclear"):       # the sector is one more product word
            fit = "high" if fit == "mid" else "mid"
            notes.append(f"Inc42 software sector: {', '.join(sw)}")
        w = cfg["weights"]
        points = {"high": int(w["tech_high"]), "mid": int(w["tech_mid"]), "low": int(w["tech_low"])}.get(fit, 0)
        floor = int(cfg["funding_only_min_tech"])
        if points < floor:
            return (f"funding news only, tech fit {points:+d} below the floor ({floor})"
                    + (f": {', '.join(words[:3])}" if words else ": no product words")), notes
    return "", notes


def exclusion(c: PoolCompany, profile: Profile, site_text: str = "") -> tuple[str, str]:
    """('large' | 'tech_floor' | '', why): why a pool company can't be a cold-email target."""
    big = large_reason(c, profile)
    if big:
        return "large", big
    if c.sector == "consultancy":                 # 2f: services is the point; hold it to a tech-fit floor instead
        why = consultancy_fit(c, profile, site_text)
        return ("tech_floor", why) if why else ("", "")
    why, _ = product_fit(c, profile, site_text)
    return ("tech_floor", why) if why else ("", "")


def consultancy_fit(c: PoolCompany, profile: Profile, site_text: str = "") -> str:
    """2f: a product-engineering consultancy passes when its own words (homepage, one-liner) name software work
    (`consultancy_min_tech`: 'mid' = one tech word); consulting / services words don't count against it."""
    text = " ".join([c.description, site_text])
    high = matching_phrases(text, [w.lower() for w in profile.pool["tech_high"]])
    need = 2 if str(profile.pool.get("consultancy_min_tech", "mid")) == "high" else 1
    if len(high) >= need:
        return ""
    return f"consultancy whose homepage names no software work ({', '.join(high) or 'no tech words'})"


def is_large(c: PoolCompany, profile: Profile) -> str:
    """2f: over `max_team_size` people, or large by large_reason - '' otherwise."""
    if c.team_size is not None and c.team_size > int(profile.pool["max_team_size"]):
        return f"{c.team_size} people"
    return large_reason(c, profile)


def tech_fit(text: str, profile: Profile) -> tuple[str, list[str]]:
    """('high' | 'mid' | 'low' | 'unclear', matched words)."""
    cfg = profile.pool
    high = matching_phrases(text, [w.lower() for w in cfg["tech_high"]])
    low = matching_phrases(text, [w.lower() for w in cfg["tech_low"]])
    if low and len(high) < 2:
        return "low", low
    if len(high) >= 2:
        return "high", high
    if high:
        return "mid", high
    return "unclear", []


def score_company(c: PoolCompany, profile: Profile, today: date, has_board: bool = False,
                  context_text: str = "") -> tuple[int, list[str]]:
    w = profile.pool["weights"]
    total, reasons = 0, []

    def add(points: int, why: str) -> None:
        nonlocal total
        total += points
        reasons.append(f"{points:+d} {why}")

    f = latest_funding(c)
    age = months_ago(f.date, today) if f else None
    if f and age is not None and age <= 6:
        add(int(w["funding_6m"]), f"funding within 6 months: {f.describe()}")
    elif f and age is not None and age <= 12:
        add(int(w["funding_12m"]), f"funding within 12 months: {f.describe()}")
    else:
        reasons.append("+0 no sourced funding round in the last 12 months")

    size = c.team_size
    big = large_reason(c, profile)
    if big:
        reasons.append(f"+0 team size unknown, but {big}")
    elif size is None:
        add(int(w["team_unknown"]), "team size unknown")
    elif 10 <= size <= 150:
        add(int(w["team_best"]), f"team of {size} (10-150: best for interns and cold email)")
    elif 2 <= size <= 9:
        add(int(w["team_small"]), f"team of {size} (very small)")
    elif 151 <= size <= 500:
        add(int(w["team_mid"]), f"team of {size} (151-500)")
    else:
        reasons.append(f"+0 team of {size}")

    if has_board or c.hiring:
        add(int(w["board"]), "careers page / job board" + (" (YC lists it as hiring)" if c.hiring else ""))
    else:
        reasons.append("+0 no careers page or job board known")

    text = " ".join([c.name, c.description, " ".join(c.tags), context_text,
                     " ".join(f"{r.sector} {r.subsector}" for r in c.funding)])
    fit, words = tech_fit(text, profile)
    if fit == "high":
        add(int(w["tech_high"]), f"tech fit: {', '.join(words[:4])}")
    elif fit == "mid":
        add(int(w["tech_mid"]), f"some tech fit: {', '.join(words[:3])}")
    elif fit == "low":
        add(int(w["tech_low"]), f"weak tech fit: {', '.join(words[:3])}")
    else:
        reasons.append("+0 tech fit unclear (no product words)")

    cities = matching_phrases(c.locations, [x.lower() for x in profile.pool["cities"]])
    if cities:
        add(int(w["city"]), f"preferred city: {cities[0]}")

    year = c.founded_year
    if year:
        label = f"YC {c.batch}" if c.batch else f"founded/cohort {year}"
        if today.year - year <= 3:
            add(int(w["recent_3y"]), f"recent: {label}")
        elif today.year - year <= 6:
            add(int(w["recent_6y"]), f"fairly recent: {label}")
        else:
            reasons.append(f"+0 older company ({label})")

    if c.region == "india":
        add(int(w["india"]), "India-based")
    else:
        add(int(w["remote_global"]), "remote-global (hires remotely; ranked below India)")
    return max(0, min(100, total)), reasons


def rank_key(s: Scored) -> tuple:
    """Best score first; India before remote-global; then by source (YC > portfolio > funding > list), so ties
    don't go to the old hand-made lists by alphabet."""
    order = {"yc": 0, "portfolio": 1, "funding": 2, "list": 3}
    best = min((order.get(x.split(":", 1)[0], 4) for x in s.company.sources), default=4)
    return (-s.score, 0 if s.company.region == "india" else 1, best, s.company.name.lower())


def select(scored: list[Scored], fixed: dict[str, str], profile: Profile, outside: int,
           site_texts: dict[str, str] | None = None, today: date | None = None) -> list[Scored]:
    """Rank everything; mark the fixed companies (domain -> why) and the best of the rest as selected,
    up to `target_size` targets in all. Fixed companies take slots too, and `outside` slots are already taken
    by targets that aren't in the pool (role / manual targets without a pool record)."""
    cfg = profile.pool
    size_cap = int(cfg["max_team_size"])
    remote_cap = int(cfg["remote_global_cap"])
    in_pool = {s.company.domain for s in scored}
    slots = int(cfg["target_size"]) - outside - sum(1 for d in fixed if d in in_pool)
    scored.sort(key=rank_key)
    remote = sum(1 for s in scored if s.company.domain in fixed and s.company.region != "india")
    marketplaces = {name_key(m) for m in profile.recruiting_marketplaces}
    for i, s in enumerate(scored, start=1):
        s.rank = i
        c = s.company
        if c.domain in fixed:
            s.selected, s.why_selected = True, fixed[c.domain]
            continue
        if name_key(c.name) in marketplaces:
            s.why_selected = "not selected: recruiting marketplace (hires for clients)"
            continue
        site_text = (site_texts or {}).get(c.key, "")
        big = is_large(c, profile)
        if big:                            # 2f: hiring inbox only, unless never cold-emailed / IT services
            listed = never_cold_email(c.name, c.domain, profile)
            why, _ = ("", []) if c.sector == "consultancy" else product_fit(c, profile, site_text)
            if c.sector == "consultancy":
                why = consultancy_fit(c, profile, site_text)
            if listed:
                s.why_selected = f"not selected: large - on the never_cold_email / IT-services list ({listed})"
            elif why:
                s.why_selected = f"not selected: large and tech-fit floor - {why}"
            else:
                s.selected, s.why_selected = True, f"selected: large ({big}) - its own hiring inbox only (2f)"
            continue
        kind, why = exclusion(c, profile, site_text)
        if kind:
            s.why_selected = f"not selected: tech-fit floor - {why}"
            continue
        recent = recent_funding(c, today, int(cfg.get("recent_funding_weeks", 12))) if today else None
        priority = priority_2f(c, profile)
        if priority:                       # past target_size and the remote-global cap
            s.selected, s.why_selected = True, f"selected: {priority} (2f)"
            continue
        if slots <= 0 and recent is None:
            s.why_selected = "not selected: below the top ~400"
            continue
        if c.region != "india":
            if remote >= remote_cap:
                s.why_selected = f"not selected: remote-global cap ({remote_cap}) reached"
                continue
            remote += 1
        if slots <= 0:
            s.selected, s.why_selected = True, f"selected: funded in the last {cfg.get('recent_funding_weeks', 12)} " \
                                               f"weeks ({recent.round or 'round'} {recent.date}, 2e)"
            continue
        s.selected, s.why_selected = True, f"selected: rank {i} by hiring likelihood"
        slots -= 1
    return scored


def priority_2f(c: PoolCompany, profile: Profile) -> str:
    """2f: why a company is selected past target_size - a YC company in India that YC lists as hiring, a fintech,
    a product-engineering consultancy - or ''. A remote-global fintech only with a YC posting open to India."""
    if c.sector == "consultancy":
        return "product-engineering consultancy"
    if c.region == "india" and "yc" in c.sources and c.hiring:
        return "YC company in India that YC lists as hiring"
    if c.region == "india" and any(s in (profile.pool.get("priority_lists") or []) for s in c.sources):
        return "Indian startup from a 2f list (" + ", ".join(s for s in c.sources if s.startswith("list:")) + ")"
    if c.sector == "fintech":
        if c.region == "india":
            return "fintech in India"
        if c.india_friendly:
            return f"remote fintech, {c.india_friendly} (India-friendly)"
    return ""


def recent_funding(c: PoolCompany, today: date, weeks: int = 12) -> FundingRound | None:
    """2e: a sourced round reported in the last `weeks` weeks (such companies are always selected if they pass)."""
    f = latest_funding(c)
    if f and f.source_url and (today - date.fromisoformat(f.date)).days <= weeks * 7:
        return f
    return None


@dataclass
class PoolSummary:
    per_source: dict[str, int] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    before_dedupe: int = 0
    after_dedupe: int = 0
    funding: dict[str, int] = field(default_factory=dict)
    scored: list[Scored] = field(default_factory=list)
    fixed_count: int = 0


def _tid(company_key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", company_key.lower()).strip("-")       # outreach.target_id


def _fixed_targets(store, profile: Profile, pool: dict[str, PoolCompany] | set[str],
                   site_texts: dict[str, str] | None = None) -> tuple[dict[str, str], int]:
    """Domains that must stay in (domain -> why) and how many targets sit outside the pool.

    A general-outreach target already looked up stays in only while it passes the 2c.1 rules (large company,
    tech-fit floor); one that fails is left to `select`, which drops it - unless it's protected (queued, sent,
    bounced or held to the 2b draft rules)."""
    pool_by_domain = pool if isinstance(pool, dict) else {}
    pool_domains = set(pool)
    protected = store.protected_target_ids()
    fixed: dict[str, str] = {}
    role_keys = set()
    for row in store.role_rows():
        if row["contact_hint"] != MARKETPLACE_HINT:
            role_keys.add(name_key(row["company"]))
    for check in store.contact_checks():
        if not check["domain"]:
            continue
        c = pool_by_domain.get(check["domain"])
        if (check["kind"] == "general_outreach" and c is not None and _tid(check["company_key"]) not in protected
                and exclusion(c, profile, (site_texts or {}).get(c.key, "") or
                              (site_texts or {}).get(check["company_key"], ""))[0]):
            continue
        fixed.setdefault(check["domain"], f"kept: already a {check['kind'].replace('_', ' ')} target"
                         + (" (queued / locked)" if _tid(check["company_key"]) in protected else ""))
    for row in store.manual_targets():
        if site_domain(row["website"] or ""):
            fixed[site_domain(row["website"])] = "kept: manual target"
    checked = {name_key(c["company"]) for c in store.contact_checks()}
    unchecked_roles = len(role_keys - checked)          # role targets not looked up yet
    outside = sum(1 for d in fixed if d not in pool_domains)
    outside += sum(1 for c in store.contact_checks() if not c["domain"])      # no domain: can't be in the pool
    return fixed, outside + unchecked_roles


def board_domains(store) -> set[str]:
    """Domains of companies known to have a careers page or job board."""
    out = set()
    for r in store.conn.execute("SELECT website, board_status FROM companies "
                                "WHERE board_status IN ('found', 'unsupported')"):
        if site_domain(r["website"] or ""):
            out.add(site_domain(r["website"]))
    for r in store.conn.execute("SELECT domain, details FROM contact_checks"):
        if r["domain"] and json.loads(r["details"] or "{}").get("careers_page"):
            out.add(r["domain"])
    return out


def build_pool(store, client, profile: Profile, today: date, log: Log = print, sources: dict | None = None,
               yc_data: list | None = None) -> PoolSummary:
    """Run every collector, dedupe, attach funding, score, select, and store the pool."""
    sources = load_sources() if sources is None else sources
    summary = PoolSummary()
    everything: list[PoolCompany] = []

    log("(a) YC directory (all companies)...")
    try:
        data = yc_data if yc_data is not None else client.get_json(YC_ALL_URL)
        yc = yc_pool(data)
        india = [c for c in yc if c.region == "india"]
        summary.per_source["yc (India)"] = len(india)
        summary.per_source["yc (remote-global, 10-150)"] = len(yc) - len(india)
        summary.notes["yc"] = f"{len(data)} companies in all.json; {len(india)} active in India, " \
                              f"{len(yc) - len(india)} active Fully Remote with 10-150 people"
        everything += yc
        log(f"  {summary.notes['yc']}")
    except Exception as exc:                       # noqa: BLE001 - one source failing shouldn't stop the rest
        summary.notes["yc"] = f"skipped: {exc}"

    log("(b) portfolio pages...")
    for cfg in sources.get("portfolios") or []:
        found, note = collect_portfolio(client, cfg, log)
        summary.per_source[f"portfolio:{cfg['id']}"] = len(found)
        summary.notes[f"portfolio:{cfg['id']}"] = note
        everything += found

    log("(d) GitHub lists...")
    for cfg in sources.get("github_lists") or []:
        found, note = collect_list(client, cfg, log)
        summary.per_source[f"list:{cfg['id']}"] = len(found)
        summary.notes[f"list:{cfg['id']}"] = note
        everything += found

    summary.before_dedupe = len(everything)
    pool = merge(everything)

    log("(c) funding news...")
    resolved = store.funding_lookups()
    for cfg in sources.get("funding") or []:
        resolve = None
        if cfg.get("kind") == "entrackr":            # 2e: prose reports; websites from likely domains
            from applyflow.contacts import MXChecker
            from applyflow.refill import collect_entrackr, load_entrackr_overrides, resolve_domain
            rounds, note = collect_entrackr(client, today, int(cfg.get("weeks", 12)), log, load_entrackr_overrides())
            mx = MXChecker(client, store, int(profile.contacts["mx_cache_days"]))

            def resolve(r: FundingRound) -> str:
                site, why = resolve_domain(client, mx, r.company, r.sector)
                log(f"    {r.company:<28} {'-> ' + site if site else 'unresolved'} ({why})")
                return site
        else:
            rounds, note = collect_funding(client, cfg, log)
        recent = [r for r in rounds if _age(r.date, today) <= 12]

        def wanted(r: FundingRound) -> bool:          # only look up startups that could fit
            fit, _ = tech_fit(f"{r.company} {r.sector} {r.subsector}", profile)
            return fit != "low" and _age(r.date, today) <= 12
        pool, stats = attach_funding(pool, recent, client, cfg, resolved, log, wanted, resolve)
        store.save_funding_lookups(resolved)
        summary.per_source[f"funding:{cfg['id']}"] = stats["matched"] + stats["resolved"]
        how = "a likely domain" if resolve else "their Inc42 profile"
        summary.notes[f"funding:{cfg['id']}"] = (f"{note}; {stats['matched']} matched a pool company, "
                                                 f"{stats['resolved']} new via {how}, "
                                                 f"{stats['dropped']} dropped (no resolvable website)")
        summary.before_dedupe += stats["resolved"]
        log(f"  {summary.notes['funding:' + str(cfg['id'])]}")
    summary.after_dedupe = len(pool)

    boards = board_domains(store)
    contexts = store.context_texts()
    scored = []
    for c in pool:
        c.sector = c.sector or pool_sector(c)              # 2f
        score, reasons = score_company(c, profile, today, c.domain in boards, contexts.get(c.key, ""))
        scored.append(Scored(c, score, reasons))
    site_texts = store.site_texts()
    fixed, fixed_count = _fixed_targets(store, profile, {c.domain: c for c in pool}, site_texts)
    summary.fixed_count = fixed_count
    summary.scored = select(scored, fixed, profile, fixed_count, site_texts, today)
    summary.funding = {
        "6m": sum(1 for s in scored if (f := latest_funding(s.company)) and _age(f.date, today) <= 6),
        "12m": sum(1 for s in scored if (f := latest_funding(s.company)) and _age(f.date, today) <= 12),
    }
    store.save_pool(summary.scored)
    return summary


def rescore(store, profile: Profile, today: date) -> list[Scored]:
    """Score the stored pool again (e.g. after `contacts` found careers pages), keeping selections stable:
    a selected company stays selected."""
    boards, contexts = board_domains(store), store.context_texts()
    out = []
    for row in store.pool_rows():
        c = PoolCompany.from_json(json.loads(row["data"]))
        score, reasons = score_company(c, profile, today, c.domain in boards, contexts.get(c.key, ""))
        out.append(Scored(c, score, reasons, bool(row["selected"]), row["rank"], row["why_selected"] or ""))
    out.sort(key=rank_key)
    for i, s in enumerate(out, start=1):
        s.rank = i
    store.save_pool(out)
    return out


@dataclass
class Change:
    company: PoolCompany
    was_selected: bool
    now: Scored


def reselect(store, profile: Profile, today: date) -> tuple[list[Scored], list[Change]]:
    """2c.1: score the stored pool again and redo the selection under the current rules, offline. Companies
    already worked on stay in while they pass (large company, tech-fit floor); those that fail are dropped and
    their slots refilled with the next best by hiring likelihood (same size, same remote-global cap)."""
    boards, contexts, site_texts = board_domains(store), store.context_texts(), store.site_texts()
    before = {r["key"]: bool(r["selected"]) for r in store.pool_rows()}
    scored = []
    for row in store.pool_rows():
        c = PoolCompany.from_json(json.loads(row["data"]))
        c.sector = c.sector or pool_sector(c)              # 2f
        score, reasons = score_company(c, profile, today, c.domain in boards, contexts.get(c.key, ""))
        scored.append(Scored(c, score, reasons))
    fixed, outside = _fixed_targets(store, profile, {s.company.domain: s.company for s in scored}, site_texts)
    out = select(scored, fixed, profile, outside, site_texts, today)
    store.save_pool(out)
    changes = [Change(s.company, before.get(s.company.key, False), s) for s in out
               if s.selected != before.get(s.company.key, False)]
    return out, changes


def pool_target_hint(c: PoolCompany, profile: Profile) -> str:
    """Who to look for: by team size when known; an unknown-size startup from a portfolio, funding
    roundup or list is treated as small (founder or CTO)."""
    from applyflow.scoring import hint_for_size
    if large_reason(c, profile):
        from applyflow.scoring import APPLY_ONLY_HINT
        return APPLY_ONLY_HINT
    if c.team_size is not None:
        return hint_for_size(c.team_size, None, profile)
    return "founder or CTO"


def is_remote_global(region: str) -> bool:
    return region == "remote_global"


def funding_signal(c: PoolCompany, today: date, months: int = 12) -> FundingRound | None:
    """The latest sourced round within `months`, or None. Drafts may mention funding only from this."""
    f = latest_funding(c)
    if f and _age(f.date, today) <= months and f.source_url:
        return f
    return None


def describe_round(f: FundingRound) -> str:
    """'the Series A' / 'the seed round' / 'the recent funding round' - how a draft names it."""
    r = (f.round or "").strip()
    if not r:
        return "the recent funding round"
    if re.search(r"series|round", r, re.I):
        return f"the {r}"
    return f"the {r.lower()} round"
