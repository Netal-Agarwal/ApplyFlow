"""Phase 2c: the outreach pool - every company from every source, scored by how likely it is to
hire a backend / cloud / AI intern or 2027 new grad, and the top ~400 selected as targets.

Hiring-likelihood score (0-100), weights in profile.yaml `pool.weights`, every factor a reason:
  recent funding (6 months > 12 months), team size (10-150 best; 2-9 and 151-500 lower), a careers
  page or job board, tech fit from the one-liner / tags / site text, a preferred city, a recent YC
  batch or founding year, and India vs remote_global.

Existing role targets (A/B openings), manual targets and companies already worked on (a contact
lookup, a written draft, a queue entry) always stay in. The rest of the ~400 slots go to the best
scores, with at most `remote_global_cap` remote-global companies and none above `max_team_size`.
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
                               collect_portfolio, latest_funding, merge, months_ago, yc_pool)
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


_LATE_ROUND = re.compile(r"\bseries [c-z]\b|pre-?ipo|\bipo\b|growth|late stage", re.I)


def _amount_mn(amount: str) -> float:
    """'$12 Mn' -> 12.0, '$1.2 Bn' -> 1200.0; 0 when unreadable (INR amounts aren't converted)."""
    m = re.search(r"\$\s*([\d.]+)\s*(mn|m|million|bn|b|billion)?", amount or "", re.I)
    if not m:
        return 0.0
    value = float(m.group(1)) if m.group(1).replace(".", "", 1).isdigit() else 0.0
    return value * 1000 if (m.group(2) or "").lower().startswith("b") else value


def likely_big(c: PoolCompany) -> str:
    """With no headcount, a late-stage round (Series C+) or a $50 Mn+ raise means a company of hundreds:
    the reason, or ''."""
    if c.team_size is not None:
        return ""
    for f in c.funding:
        if _LATE_ROUND.search(f.round or ""):
            return f"late-stage round ({f.round}), likely 300+ people"
        if _amount_mn(f.amount) >= 50:
            return f"raised {f.amount} in one round, likely 300+ people"
    return ""


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
    big = likely_big(c)
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


def select(scored: list[Scored], fixed: dict[str, str], profile: Profile, outside: int) -> list[Scored]:
    """Rank everything; mark the fixed companies (domain -> why) and the best of the rest as selected,
    up to `target_size` targets in all. Fixed companies take slots too, and `outside` slots are already taken
    by targets that aren't in the pool (role / manual targets without a pool record)."""
    cfg = profile.pool
    size_cap = int(cfg["max_team_size"])
    remote_cap = int(cfg["remote_global_cap"])
    in_pool = {s.company.domain for s in scored}
    slots = int(cfg["target_size"]) - outside - sum(1 for d in fixed if d in in_pool)
    scored.sort(key=lambda s: (-s.score, 0 if s.company.region == "india" else 1, s.company.name.lower()))
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
        if c.team_size is not None and c.team_size > size_cap:
            s.why_selected = f"not selected: {c.team_size} people (apply via postings)"
            continue
        if likely_big(c):
            s.why_selected = f"not selected: {likely_big(c)} (apply via postings)"
            continue
        if slots <= 0:
            s.why_selected = "not selected: below the top ~400"
            continue
        if c.region != "india":
            if remote >= remote_cap:
                s.why_selected = f"not selected: remote-global cap ({remote_cap}) reached"
                continue
            remote += 1
        s.selected, s.why_selected = True, f"selected: rank {i} by hiring likelihood"
        slots -= 1
    return scored


@dataclass
class PoolSummary:
    per_source: dict[str, int] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    before_dedupe: int = 0
    after_dedupe: int = 0
    funding: dict[str, int] = field(default_factory=dict)
    scored: list[Scored] = field(default_factory=list)
    fixed_count: int = 0


def _fixed_targets(store, profile: Profile, pool_domains: set[str]) -> tuple[dict[str, str], int]:
    """Domains that must stay in (domain -> why) and how many targets sit outside the pool."""
    fixed: dict[str, str] = {}
    role_keys = set()
    for row in store.ranked(("A", "B")):
        if row["contact_hint"] != MARKETPLACE_HINT:
            role_keys.add(name_key(row["company"]))
    for check in store.contact_checks():
        if check["domain"]:
            fixed.setdefault(check["domain"], f"kept: already a {check['kind'].replace('_', ' ')} target")
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
        rounds, note = collect_funding(client, cfg, log)
        recent = [r for r in rounds if _age(r.date, today) <= 12]

        def wanted(r: FundingRound) -> bool:          # only look up startups that could fit
            fit, _ = tech_fit(f"{r.company} {r.sector} {r.subsector}", profile)
            return fit != "low" and _age(r.date, today) <= 12
        pool, stats = attach_funding(pool, recent, client, cfg, resolved, log, wanted)
        store.save_funding_lookups(resolved)
        summary.per_source[f"funding:{cfg['id']}"] = stats["matched"] + stats["resolved"]
        summary.notes[f"funding:{cfg['id']}"] = (f"{note}; {stats['matched']} matched a pool company, "
                                                 f"{stats['resolved']} new via their Inc42 profile, "
                                                 f"{stats['dropped']} dropped (no resolvable website)")
        summary.before_dedupe += stats["resolved"]
        log(f"  {summary.notes['funding:' + str(cfg['id'])]}")
    summary.after_dedupe = len(pool)

    boards = board_domains(store)
    contexts = store.context_texts()
    scored = []
    for c in pool:
        score, reasons = score_company(c, profile, today, c.domain in boards, contexts.get(c.key, ""))
        scored.append(Scored(c, score, reasons))
    fixed, fixed_count = _fixed_targets(store, profile, {c.domain for c in pool})
    summary.fixed_count = fixed_count
    summary.scored = select(scored, fixed, profile, fixed_count)
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
    out.sort(key=lambda s: (-s.score, 0 if s.company.region == "india" else 1, s.company.name.lower()))
    for i, s in enumerate(out, start=1):
        s.rank = i
    store.save_pool(out)
    return out


def pool_target_hint(c: PoolCompany, profile: Profile) -> str:
    """Who to look for: by team size when known; an unknown-size startup from a portfolio, funding
    roundup or list is treated as small (founder or CTO)."""
    from applyflow.scoring import hint_for_size
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
