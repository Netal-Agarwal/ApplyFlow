"""Loading the profile and seed list from config/."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from applyflow.models import Company

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROFILE_PATH = CONFIG_DIR / "profile.yaml"
PROFILE_EXAMPLE_PATH = CONFIG_DIR / "profile.example.yaml"
SEEDS_PATH = CONFIG_DIR / "seeds.yaml"
SOURCES_PATH = CONFIG_DIR / "sources.yaml"
DO_NOT_CONTACT_PATH = CONFIG_DIR / "do_not_contact.txt"
DRAFTS_DIR = ROOT / "output" / "drafts"
DEFAULT_DB_PATH = ROOT / "data" / "applyflow.db"
OUTPUT_DIR = ROOT / "output"

SUPPORTED_ATS = ("greenhouse", "lever", "ashby", "workable")
SKILL_WEIGHTS = {"heavy": 3, "medium": 2, "light": 1, "negative": -3}

DEFAULT_STARTUP = {
    "max_team_size": 300,              # at or below this headcount counts as a startup
    "score_bonus": 6,                  # points added for startups
    "founder_contact_max_team_size": 50,
    "small_board_job_count": 15,       # boards this small suggest a small company when headcount is unknown
}
DEFAULT_FRESHNESS = {
    "stale_days": 120,                 # postings older than this lose stale_penalty points
    "stale_penalty": 10,
    "max_age_days": 365,               # older than this: rejected as an evergreen/abandoned posting
}
DEFAULT_CONTACTS = {
    "recheck_days": 30,
    "max_site_pages": 5,
    "deep_site_pages": 12,           # 2e `contacts --deep`: pages read beyond the homepage
    "github_max_members": 10,
    "apply_only_team_size": 300,     # bigger companies: apply via the posting, no contact search
    "mx_cache_days": 30,
}
DEFAULT_OUTREACH = {
    "daily_batch": 30,                 # emails `queue` picks per day
    "tier3": {                         # guessed addresses (send tier 3)
        "unlock_after_days": 2,        # calendar days after the first mark-sent, with zero bounces so far
        "start_per_day": 5,            # tier-3 emails per day once unlocked (inside daily_batch)
        "ramp_after_days": 3,          # days after the first tier-3 send before the higher limit
        "ramp_per_day": 10,            # ... if the bounce rate stays under max_bounce_rate
        "max_bounce_rate": 0.05,       # at or above this (bounces / sends), tier 3 pauses
    },
    "general_inbox_per_day": 5,        # 2e: hello@ / hi@ / team@ emails per day (info@ / contact@: never)
    "min_words": 160,                  # draft length between the greeting and the sign-off (2d format)
    "max_words": 200,
}
DEFAULT_DISCOVERY = {
    "recheck_days": 30,
    "yc": {
        "statuses": ["Active"],
        "min_team_size": 2,
        "max_team_size": 1000,
        "regions_any": ["India"],          # "Remote" alone would admit US-only startups
        "preferred_team_size": [10, 300],  # checked first: big enough for interns, small enough to email
    },
}


DEFAULT_POOL = {                       # Phase 2c: the ranked pool of companies to email
    "target_size": 400,                # outreach targets selected (role + manual targets included)
    "recent_funding_weeks": 12,        # 2e: funded this recently -> selected even past target_size
    "remote_global_cap": 80,           # at most this many remote-global (non-India) companies
    "max_team_size": 300,              # bigger companies aren't picked for general outreach (apply-only)
    "weights": {
        "funding_6m": 25,              # a funding round announced in the last 6 months
        "funding_12m": 15,             # ... in the last 12 months
        "team_best": 20,               # 10-150 people
        "team_small": 10,              # 2-9
        "team_mid": 8,                 # 151-500
        "team_unknown": 6,
        "board": 12,                   # a careers page / job board / YC "hiring" flag
        "tech_high": 20,               # SaaS, APIs, developer tools, AI, fintech infra, data, cloud
        "tech_mid": 10,                # one tech word
        "tech_low": -10,               # D2C, food, fashion, services
        "city": 10,                    # Bangalore, Hyderabad, Delhi NCR
        "recent_3y": 10,               # YC batch / founded in the last 3 years
        "recent_6y": 5,
        "india": 8,
        "remote_global": 0,
    },
    "tech_high": ["saas", "b2b", "api", "apis", "developer", "developers", "devtools", "developer tools", "sdk",
                  "infrastructure", "infra", "platform", "ai", "artificial intelligence", "ml", "machine learning",
                  "llm", "llms", "genai", "generative ai", "agent", "agents", "agentic", "fintech", "payments",
                  "payment", "lending", "banking", "data", "analytics", "database", "cloud", "devops",
                  "automation", "workflow", "security", "cybersecurity", "enterprise", "open source",
                  "software", "deeptech", "deep tech", "computer vision", "voice ai", "observability"],
    "tech_low": ["d2c", "food", "beverage", "beverages", "coffee", "restaurant", "restaurants", "fashion", "apparel",
                 "clothing", "jewellery", "jewelry", "beauty", "skincare", "cosmetics", "personal care", "furniture",
                 "home decor", "consulting", "agency", "staffing", "recruitment agency", "real estate brokerage",
                 "travel agency", "wedding", "snacks", "dairy", "ecommerce brand", "consumer brand", "nutrition"],
    "cities": ["bangalore", "bengaluru", "hyderabad", "delhi", "new delhi", "gurgaon", "gurugram", "noida",
               "ncr", "delhi ncr"],
    # Phase 2c.1: an unknown-size company is large (apply-only, never cold-emailed) after a round of
    # `min_series` or later, or one round of `min_amount_mn` ($ Mn) or more.
    "large_company": {"min_series": "C", "min_amount_mn": 50},
    # Phase 2c.1: a company found only in funding news needs at least this many tech-fit points
    # (weights.tech_high = 20: two or more product words; tech_mid = 10: one).
    "funding_only_min_tech": 20,
    # ... where an Inc42 subsector naming a software product counts as one more product word.
    "software_sectors": ["application layer", "aplication layer", "ai infrastructure & development", "horizontal saas",
                         "vertical saas", "fintech saas", "logistic saas", "logistics saas", "ecommerce enablers",
                         "investment tech", "invest tech", "wealth tech", "wealthtech", "insurtech", "lending tech",
                         "digital learning platform", "social networking platform"],
    # Phase 2c.1: not software-product businesses. A non-YC pool company matching one of these (name, sector,
    # one-liner or its own homepage) is dropped unless its homepage names a software product
    # (`software_product`, at least `software_product_min` of them; the source's one-liner counts too).
    "non_software": ["biofuel", "biofuels", "solar", "renewable energy", "clean energy", "energy storage",
                     "battery manufacturing", "ev charging", "hardware", "manufacturer", "manufacturers",
                     "manufacturing", "inertial navigation", "defence", "aerospace", "drone", "drones",
                     "logistics", "fleet", "freight", "trucking", "warehousing", "last-mile", "wholesale",
                     "fmcg", "kirana", "d2c", "nbfc", "green finance", "wealth managers", "mutual fund distributor",
                     "investment advisory",
                     "family office", "asset management", "portfolio management", "services company",
                     "it services", "ai services", "consulting", "staffing", "real estate", "construction",
                     "agri inputs", "farm produce", "textiles", "hydrogen", "electrolyzer", "electrolyzers",
                     "synthetic biology", "fermentation", "digital content house", "digital entertainment company",
                     "deliver medicines"],
    "software_product": ["saas", "api", "apis", "sdk", "developers", "developer platform", "no code", "no-code",
                         "low-code", "open source", "dashboard", "ai agents", "build agents",
                         "software platform", "cloud platform", "analytics platform", "data platform",
                         "workflow automation", "self-serve", "free trial", "start for free", "book a demo",
                         "request a demo", "personalized demo", "integrate", "automate", "project management software"],
    "software_product_min": 1,
    # Investment firms: a name like this plus investing words on the site.
    "investment_name": ["capital", "ventures", "partners", "investments", "holdings", "asset", "wealth"],
    "investment_words": ["invest", "investing", "investment", "investors", "portfolio", "wealth", "fund",
                         "funds", "advisory", "assets under management"],
}


class ConfigError(ValueError):
    pass


@dataclass
class RoleFamily:
    name: str
    tier: str
    title_patterns: list[str]
    lead_project: str = ""


@dataclass
class Profile:
    candidate: dict[str, Any]
    tracks: dict[str, bool]
    role_families: list[RoleFamily]
    family_tier_weights: dict[str, float]
    title_exclude: list[str]
    max_years_required: int
    locations: dict[str, Any]
    skills: dict[str, int]                 # skill phrase -> weight
    skill_score_cap: int
    tiers: dict[str, int]
    large_company_job_count: int
    startup: dict[str, Any] = field(default_factory=dict)
    freshness: dict[str, Any] = field(default_factory=dict)
    a_tier_tracks: list[str] = field(default_factory=lambda: ["internship", "new_grad"])
    fresher_skill_floor: int = 25          # min skill points (of 50); 0 turns the floor off
    recruiting_marketplaces: list[str] = field(default_factory=lambda: ["Weekday"])
    never_cold_email: list[str] = field(default_factory=list)    # 2c.1: names / domains, apply-only always
    contacts: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONTACTS))
    discovery: dict[str, Any] = field(default_factory=dict)
    outreach: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_OUTREACH))
    pool: dict[str, Any] = field(default_factory=lambda: _pool({}))
    source_path: Path = field(default=PROFILE_PATH)

    @property
    def user_agent(self) -> str:
        contact = (self.candidate or {}).get("contact_for_user_agent") or ""
        suffix = f"; contact: {contact}" if contact else ""
        return f"ApplyFlow/0.1 (personal job-search tool{suffix})"


def _require(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ConfigError(f"profile is missing required section '{key}'")
    return data[key]


def parse_profile(data: dict[str, Any], source_path: Path = PROFILE_PATH) -> Profile:
    families = []
    for raw in _require(data, "role_families"):
        tier = raw.get("tier", "primary")
        families.append(
            RoleFamily(
                name=raw["name"],
                tier=tier,
                title_patterns=[p.lower() for p in raw.get("title_patterns", [])],
                lead_project=raw.get("lead_project", ""),
            )
        )

    tier_weights = data.get("family_tier_weights", {"primary": 1.0, "secondary": 0.85, "stretch": 0.6})
    for fam in families:
        if fam.tier not in tier_weights:
            raise ConfigError(f"role family '{fam.name}' has unknown tier '{fam.tier}'")

    raw_skills = _require(data, "skills")
    skills: dict[str, int] = {}
    for group, weight in SKILL_WEIGHTS.items():
        for phrase in raw_skills.get(group, []) or []:
            skills[str(phrase).lower()] = weight

    tiers = data.get("tiers", {"A": 70, "B": 45})
    if tiers["A"] <= tiers["B"]:
        raise ConfigError("tiers.A must be higher than tiers.B")

    return Profile(
        candidate=data.get("candidate", {}) or {},
        tracks=data.get("tracks", {"internship": True, "new_grad": True, "unspecified": True}),
        role_families=families,
        family_tier_weights={k: float(v) for k, v in tier_weights.items()},
        title_exclude=[t.lower() for t in data.get("title_exclude", [])],
        max_years_required=int(data.get("max_years_required", 1)),
        locations=_require(data, "locations"),
        skills=skills,
        skill_score_cap=int(raw_skills.get("skill_score_cap", 22)),
        tiers=tiers,
        large_company_job_count=int(data.get("large_company_job_count", 60)),
        startup={**DEFAULT_STARTUP, **(data.get("startup") or {})},
        freshness={**DEFAULT_FRESHNESS, **(data.get("freshness") or {})},
        a_tier_tracks=list(data.get("a_tier_tracks") or ["internship", "new_grad"]),
        fresher_skill_floor=int(data.get("fresher_skill_floor", 25)),
        recruiting_marketplaces=[str(x) for x in data.get("recruiting_marketplaces", ["Weekday"]) or []],
        never_cold_email=[str(x) for x in data.get("never_cold_email") or []],
        contacts={**DEFAULT_CONTACTS, **(data.get("contacts") or {})},
        discovery={**DEFAULT_DISCOVERY, **(data.get("discovery") or {})},
        outreach=_outreach(data.get("outreach") or {}),
        pool=_pool(data.get("pool") or {}),
        source_path=source_path,
    )


def _outreach(raw: dict[str, Any]) -> dict[str, Any]:
    out = {**DEFAULT_OUTREACH, **raw}
    out["tier3"] = {**DEFAULT_OUTREACH["tier3"], **(raw.get("tier3") or {})}
    return out


def _pool(raw: dict[str, Any]) -> dict[str, Any]:
    out = {**DEFAULT_POOL, **raw}
    out["weights"] = {**DEFAULT_POOL["weights"], **(raw.get("weights") or {})}
    out["large_company"] = {**DEFAULT_POOL["large_company"], **(raw.get("large_company") or {})}
    return out


def load_sources(path: Path = SOURCES_PATH) -> dict[str, Any]:
    """config/sources.yaml: portfolio pages, funding roundups and GitHub lists (Phase 2c)."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_profile(path: Path | None = None) -> Profile:
    """Load config/profile.yaml, falling back to the example with a warning."""
    if path is None:
        if PROFILE_PATH.exists():
            path = PROFILE_PATH
        else:
            print(
                "note: config/profile.yaml not found, using config/profile.example.yaml.\n"
                "      Copy it to config/profile.yaml and edit it to make it yours.",
                file=sys.stderr,
            )
            path = PROFILE_EXAMPLE_PATH
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_profile(data, source_path=path)


def load_seeds(path: Path = SEEDS_PATH) -> list[Company]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    companies = []
    seen: set[tuple[str, str]] = set()
    for raw in data.get("companies", []) or []:
        ats = str(raw.get("ats", "")).lower()
        slug = str(raw.get("slug", "")).strip()
        if ats not in SUPPORTED_ATS:
            raise ConfigError(f"seed '{raw.get('name')}' has unsupported ats '{ats}'")
        if not slug:
            raise ConfigError(f"seed '{raw.get('name')}' is missing a slug")
        if (ats, slug) in seen:
            continue
        seen.add((ats, slug))
        size = raw.get("team_size")
        companies.append(Company(
            name=str(raw.get("name") or slug), ats=ats, slug=slug,
            team_size=int(size) if isinstance(size, (int, float)) else None,
            website=str(raw.get("website") or ""),
        ))
    return companies


def append_seed(company: Company, path: Path = SEEDS_PATH) -> bool:
    """Append a company to seeds.yaml, keeping the file's comments. Returns False if present."""
    existing = load_seeds(path)
    if any(c.ats == company.ats and c.slug == company.slug for c in existing):
        return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"  - {{name: {json.dumps(company.name)}, ats: {company.ats}, slug: {company.slug}}}\n")
    return True
