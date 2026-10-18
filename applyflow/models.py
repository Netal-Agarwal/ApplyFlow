"""Core data types shared across collectors, storage and scoring."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Company:
    name: str
    ats: str                        # greenhouse | lever | ashby | workable
    slug: str
    team_size: int | None = None    # headcount when known (YC directory, or set in seeds.yaml)
    website: str = ""
    source: str = "seed"            # seed | yc | manual

    @property
    def board_key(self) -> str:
        return f"{self.ats}:{self.slug}"


@dataclass
class DiscoveredCompany:
    """A hiring company found by discovery, whether or not it has a board we can read."""

    key: str                        # stable id, e.g. "yc:razorpay"
    name: str
    website: str = ""
    source: str = "yc"
    team_size: int | None = None
    description: str = ""           # one-liner, useful for Phase 2 personalization
    locations: str = ""
    batch: str = ""
    ats: str = ""                   # supported ATS, an unsupported one (e.g. "keka"), or "" if none found
    slug: str = ""
    board_status: str = "unchecked" # found | unsupported | none | error | unchecked


@dataclass
class Job:
    """One opening, normalized from whichever source it came from."""

    source: str                     # greenhouse | lever | ashby | hn
    external_id: str                # id within that source
    company: str
    title: str
    url: str
    description: str = ""           # plain text
    locations: list[str] = field(default_factory=list)
    remote: bool | None = None      # True/False when the source says so, None if unknown
    employment_type: str = ""       # e.g. "full time", "Intern", as the source reports it
    department: str = ""
    posted_at: str = ""             # ISO-8601 when available
    apply_url: str = ""
    emails: list[str] = field(default_factory=list)   # contact emails stated in the posting
    board_size: int | None = None   # open jobs on the company's board (size proxy)
    team_size: int | None = None    # company headcount when known

    @property
    def id(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def location_text(self) -> str:
        return " | ".join(loc for loc in self.locations if loc)


@dataclass
class ScoreResult:
    job_id: str
    accepted: bool
    score: int                      # 0-100
    tier: str                       # A | B | skip | rejected
    family: str = ""
    track: str = ""                 # internship | new_grad | unspecified
    location_class: str = ""
    experience_years: int | None = None
    sponsorship: str = "unknown"    # positive | negative | unknown
    action: str = ""                # apply | email | apply_plus_email
    contact_hint: str = ""          # who to reach out to, used by Phase 2
    lead_project: str = ""
    matched_skills: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    merged_locations: list[str] = field(default_factory=list)   # this posting's + its merged duplicates'
