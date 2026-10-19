"""Phase 2a: find the best person to email about hiring at each target company.

Targets are companies with a tier A/B opening (never recruiting marketplaces) plus
India-located startups whose board we can't read ("general outreach"). For each one we
look, in order of trust, and stop early once a good contact for the right role turns up:

  1. emails the posting (or HN post) publishes for applicants      -> verified
  2. the company's own site: homepage + a few likely pages         -> verified (personal email)
                                                                      fallback (careers@, jobs@, ...)
  3. the company's YC page (founder names and titles), robots permitting
  4. its GitHub organisation, if the site links one: public members whose
     public email is on the company domain                          -> verified
  5. an email pattern confirmed by a published address (first.last, flast, ...)
     applied to a named founder / CTO / EM                          -> strong;
     with no confirmed pattern, first@domain for them               -> guessed

Mail servers are never probed. Nothing here sends anything; Phase 2b drafts, a human
approves.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from applyflow.config import Profile
from applyflow.detect import normalize_website
from applyflow.discovery import board_identity, name_key, same_site, site_domain
from applyflow.http import FetchError
from applyflow.models import Job
from applyflow.scoring import MARKETPLACE_HINT, hint_for_size
from applyflow.text import extract_emails, html_to_text

Log = Callable[[str], None]

CONFIDENCE = ["verified", "strong", "guessed", "fallback"]
SOURCE_ORDER = ["posting", "website", "github", "yc", "pattern"]

# How well each kind of contact fits who we're told to look for (0 = best).
ROLE_FIT = {
    "founder or CTO": {"posting": 0, "cto": 0, "founder": 0, "eng_lead": 1, "recruiting": 2,
                       "hiring_inbox": 3, "other": 4, "general_inbox": 5},
    "engineering manager or tech lead": {"posting": 0, "eng_lead": 0, "cto": 0, "founder": 1, "recruiting": 2,
                                         "hiring_inbox": 3, "other": 4, "general_inbox": 5},
    "recruiter or engineering manager": {"posting": 0, "recruiting": 0, "eng_lead": 1, "cto": 2, "founder": 2,
                                         "hiring_inbox": 3, "other": 4, "general_inbox": 5},
}
ROLE_FIT["the email in the post"] = ROLE_FIT["founder or CTO"]
GUESSABLE_GROUPS = {"founder", "cto", "eng_lead"}           # people we generate first@domain for


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Contact:
    name: str
    role: str                 # title as published, or what the address is ("careers inbox")
    email: str
    confidence: str           # verified | strong | guessed | fallback
    source: str               # posting | website | yc | github | pattern
    source_url: str
    group: str = "other"      # posting | founder | cto | eng_lead | recruiting | hiring_inbox | general_inbox | other
    fit: int = 9
    note: str = ""

    def label(self) -> str:
        who = f"{self.name} ({self.role})" if self.name else self.role
        return f"{who} <{self.email}> [{self.confidence}, {self.source}]"


@dataclass
class Person:
    """A named person at the company, found without an email address."""
    name: str
    role: str
    source: str
    source_url: str

    @property
    def group(self) -> str:
        return role_group(self.role)


@dataclass
class Target:
    key: str                  # discovered company key ("yc:slug") or "co:<name>"
    company: str
    kind: str                 # role | general_outreach
    hint: str                 # who to look for (scoring.contact_hint)
    website: str = ""
    domain: str = ""
    domain_source: str = ""
    team_size: int | None = None
    roles: list[str] = field(default_factory=list)      # "Title (score)" for role targets
    best_score: int | None = None
    jobs: list[Job] = field(default_factory=list)
    yc_slug: str = ""


@dataclass
class ContactResult:
    target: Target
    status: str                                         # found | none | no_domain
    contacts: list[Contact] = field(default_factory=list)      # top 3, primary first
    considered: list[Contact] = field(default_factory=list)    # every candidate, ranked
    excluded: list[tuple[str, str]] = field(default_factory=list)   # (email, why)
    people: list[Person] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)          # what each source found
    why_primary: str = ""


# ---------------------------------------------------------------------------
# Roles and addresses
# ---------------------------------------------------------------------------

def role_group(title: str) -> str:
    t = title.lower()
    if re.search(r"\bcto\b|chief tech", t):
        return "cto"
    if re.search(r"head of (engineering|technology|tech)|vp,? (of )?engineering|vice president,? (of )?engineering|"
                 r"director of engineering|engineering (manager|lead)|tech(nical)? lead", t):
        return "eng_lead"
    if re.search(r"founder|\bceo\b|chief executive", t):
        return "founder"
    if re.search(r"recruit|talent|\bhr\b|human resources|people (ops|operations|partner)|head of people", t):
        return "recruiting"
    return "other"


HIRING_INBOXES = {"careers", "career", "jobs", "job", "hiring", "hr", "talent", "recruiting", "recruitment",
                  "recruit", "join", "joinus", "work", "workwithus"}
GENERAL_INBOXES = {"hello", "hi", "info", "contact", "team", "admin", "office", "mail", "enquiries", "enquiry",
                   "inquiries", "connect", "people"}
FOUNDER_INBOXES = {"founders", "founder"}
SKIP_LOCALS = {"noreply", "no-reply", "donotreply", "privacy", "security", "abuse", "legal", "billing", "sales",
               "press", "media", "pr", "support", "help", "helpdesk", "dpo", "gdpr", "accessibility", "compliance",
               "investors", "investor", "ir", "partners", "partnerships", "marketing", "feedback", "grievance",
               "grievances", "nodal", "cs", "customercare", "care", "service", "orders", "accounts", "finance",
               "webmaster", "postmaster", "notifications", "bounce", "example", "name", "email", "you", "user"}


def classify_address(email: str) -> tuple[str, str, str] | None:
    """(group, role description, confidence) for a published address, or None to ignore it."""
    local = email.split("@", 1)[0].lower()
    base = re.sub(r"[._-]?\d+$", "", local)
    if base in SKIP_LOCALS or any(part in SKIP_LOCALS for part in re.split(r"[._+-]", base)):
        return None                       # also "help-events@", "support.team@"
    if base in HIRING_INBOXES:
        return "hiring_inbox", f"{local}@ inbox", "fallback"
    if base in FOUNDER_INBOXES:
        return "founder", "founders' inbox", "fallback"
    if base in GENERAL_INBOXES:
        return "general_inbox", f"{local}@ inbox", "fallback"
    return "other", "", "verified"


# ---------------------------------------------------------------------------
# Names and email patterns
# ---------------------------------------------------------------------------

def name_parts(name: str) -> tuple[str, str]:
    """('priya', 'sharma') for 'Dr. Priya K. Sharma', ('j', 'richards') for 'J Richards'; last '' for one name."""
    words = [w for w in re.findall(r"[a-z]+", name.lower()) if w not in {"dr", "mr", "ms", "mrs", "prof"}]
    if not words:
        return "", ""
    middle_initials = [w for w in words[1:-1] if len(w) == 1]
    words = [w for w in words if w not in middle_initials]
    return words[0], (words[-1] if len(words) > 1 else "")


# Words that can make up a job title; anything else capitalised in a "title" is usually
# another organisation ("Co-founder & Tech Lead, Good Tape", "Founder - Heroku"): a
# testimonial, investor or advisor, not someone at this company.
_TITLE_VOCAB = {
    "co", "founder", "cofounder", "founders", "ceo", "cto", "coo", "cfo", "cpo", "chief", "executive", "technology",
    "technical", "operating", "product", "officer", "head", "of", "engineering", "tech", "lead", "leader", "vp",
    "vice", "president", "director", "senior", "sr", "principal", "manager", "engineer", "recruiter", "talent",
    "acquisition", "partner", "hr", "human", "resources", "people", "operations", "ops", "and", "the", "business",
    "general", "managing", "board", "member", "chairman", "chairperson", "md", "x", "s",
}


def title_names_other_org(title: str, company: str) -> bool:
    own = set(re.findall(r"[a-z0-9]+", company.lower()))
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", title):
        w = word.lower()
        if word[0].isupper() and w not in _TITLE_VOCAB and w not in own:
            return True
    return False


PATTERNS = ["first.last", "firstlast", "first_last", "flast", "f.last", "first"]   # specific before generic


def apply_pattern(pattern: str, first: str, last: str) -> str:
    if not first or (pattern != "first" and not last):
        return ""
    return {"first": first, "first.last": f"{first}.{last}", "firstlast": f"{first}{last}",
            "first_last": f"{first}_{last}", "flast": f"{first[0]}{last}", "f.last": f"{first[0]}.{last}"}[pattern]


def pattern_of(local: str, name: str) -> str:
    first, last = name_parts(name)
    for pattern in PATTERNS:
        if apply_pattern(pattern, first, last) == local.lower():
            return pattern
    return ""


_TITLE_RE = re.compile(
    r"\b(co-?founder|founder|ceo|cto|chief (?:executive|technology|technical|operating|product) officer|"
    r"head of (?:engineering|technology|tech|talent|people|hr)|vp,? (?:of )?engineering|"
    r"vice president,? (?:of )?engineering|director of engineering|engineering manager|engineering lead|"
    r"tech(?:nical)? lead|recruiter|talent (?:acquisition|partner|lead)|hr (?:manager|lead|business partner)|"
    r"human resources|people (?:operations|partner))\b", re.I)
_TOKEN = r"[A-Z][a-z]+(?:['-][A-Z]?[a-z]+)?"
_NAME = rf"{_TOKEN}(?: {_TOKEN}){{1,3}}"
_NAME_LINE = re.compile(rf"^(?:Dr\.? )?({_NAME})$")
_NAME_THEN_TITLE = re.compile(rf"^(?:Dr\.? )?({_NAME})\s*(?:,|\||-|–|—|\(|:)\s*(.{{2,70}}?)\)?$")
_NAME_STOP = {
    "our", "team", "the", "meet", "contact", "join", "about", "careers", "engineering", "head", "chief", "founder",
    "founders", "co", "we", "us", "read", "more", "view", "learn", "get", "in", "touch", "home", "company", "people",
    "leadership", "board", "advisors", "investors", "backed", "by", "and", "of", "for", "with", "at", "linkedin",
    "twitter", "email", "privacy", "policy", "terms", "all", "rights", "reserved", "product", "platform", "blog",
    "news", "jobs", "open", "roles", "apply", "now", "sign", "log", "book", "demo", "request", "pricing",
    "customers", "solutions", "resources", "technology", "officer", "executive", "manager", "lead", "director",
    "president", "vice", "talent", "recruiter", "operations", "human", "story", "mission", "vision", "values",
    "office", "offices", "india", "bangalore", "bengaluru", "hyderabad", "delhi", "mumbai", "pune", "san",
    "francisco", "new", "york", "ai", "inc", "labs", "private", "limited", "careers", "culture", "life",
    # job words: "Robotics Engineer" is a title, not a person
    "engineer", "engineers", "developer", "designer", "scientist", "analyst", "robotics", "software", "data",
    "intern", "associate", "specialist", "consultant", "marketing", "sales", "design", "research", "growth",
    "acquisition", "partner", "senior", "junior", "staff", "principal", "backend", "frontend", "full", "stack",
    "guide", "survival", "letter", "note", "message", "podcast", "webinar", "event", "events",
}


def _is_name(text: str) -> bool:
    return all(w.lower() not in _NAME_STOP for w in text.split())


def _name_line(line: str) -> str:
    """The person's name if the whole line is one ('Priya Sharma'), else ''."""
    m = _NAME_LINE.match(line)
    return m.group(1) if m and _is_name(m.group(1)) and not _TITLE_RE.search(line) else ""


_POSSESSIVE_TITLE = re.compile(r"\b(?:co-?founder|founder|ceo|cto)['’]s\b", re.I)   # "Founder's story"


def _title_line(line: str) -> bool:
    """A short line that is only a title ('Co-founder & CEO'), not 'Name, Title'."""
    return len(line) <= 70 and bool(_TITLE_RE.search(line)) and not _name_line(line) \
        and not _NAME_THEN_TITLE.match(line) and not _POSSESSIVE_TITLE.search(line)


def people_in_text(text: str, url: str, source: str = "website") -> list[Person]:
    """Named people with a hiring-relevant title: 'Priya Sharma, CTO', or a name line next to a title line."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    found: dict[str, Person] = {}

    def add(name: str, title: str) -> None:
        if _is_name(name) and name not in found:
            found[name] = Person(name=name, role=" ".join(title.split()), source=source, source_url=url)

    for i, line in enumerate(lines):
        m = _NAME_THEN_TITLE.match(line)
        if m and _TITLE_RE.search(m.group(2)) and _is_name(m.group(1))                 and not _POSSESSIVE_TITLE.search(m.group(2)):
            add(m.group(1), m.group(2))
            continue
        name = _name_line(line)
        if not name:
            continue
        # Card layouts put the title under the name; some put it above - but only take the
        # line above when it isn't the title of the card before (a name right above it).
        if i + 1 < len(lines) and _title_line(lines[i + 1]):
            add(name, lines[i + 1])
        elif i >= 1 and _title_line(lines[i - 1]) and not (i >= 2 and _name_line(lines[i - 2])):
            add(name, lines[i - 1])
    return list(found.values())


# ---------------------------------------------------------------------------
# Do-not-contact list
# ---------------------------------------------------------------------------

@dataclass
class DoNotContact:
    emails: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> "DoNotContact":
        dnc = cls()
        if not path.exists():
            return dnc
        for raw in path.read_text(encoding="utf-8").splitlines():
            entry = raw.split("#", 1)[0].strip().lower()
            if entry.startswith("@") and len(entry) > 1:
                dnc.domains.add(entry[1:])
            elif "@" in entry:
                dnc.emails.add(entry)
        return dnc

    def blocks(self, email: str) -> str:
        """The list entry that blocks this address, or ''."""
        email = email.lower()
        if email in self.emails:
            return email
        domain = email.rsplit("@", 1)[-1]
        for d in self.domains:
            if domain == d or domain.endswith("." + d):
                return f"@{d}"
        return ""


# ---------------------------------------------------------------------------
# GitHub (public REST API, unauthenticated: 60 requests/hour per IP)
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
_GITHUB_ORG = re.compile(r"""href=["']https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/?["'#?]""", re.I)
_GITHUB_RESERVED = {"features", "about", "pricing", "login", "join", "sponsors", "topics", "orgs", "enterprise",
                    "marketplace", "explore", "site", "security", "apps", "settings", "contact", "team", "home"}


class GitHubLimit(Exception):
    pass


def _header(headers: Any, name: str) -> str:
    if not headers:
        return ""
    for k, v in dict(headers).items():
        if k.lower() == name:
            return str(v)
    return ""


@dataclass
class GitHubBudget:
    """Tracks GitHub's rate limit across a run; once it's exhausted, GitHub is skipped until the reset."""
    clock: Callable[[], float] = time.time
    calls: int = 0
    remaining: int | None = None
    reset_at: float | None = None
    exhausted: bool = False

    def get(self, client, path: str, params: dict | None = None) -> Any:
        if self.exhausted and (self.reset_at is None or self.clock() < self.reset_at):
            raise GitHubLimit(self.status())
        self.exhausted = False
        self.calls += 1
        try:
            data, headers = client.get_api(f"{GITHUB_API}{path}", params, GITHUB_HEADERS)
        except FetchError as exc:
            self._update(exc.headers)
            if exc.status == 429 or (exc.status == 403 and self.remaining == 0):
                self.exhausted = True
                raise GitHubLimit(self.status()) from exc
            if exc.status == 404:
                return None
            raise
        self._update(headers)
        if self.remaining == 0:
            self.exhausted = True
        return data

    def _update(self, headers: Any) -> None:
        remaining, reset = _header(headers, "x-ratelimit-remaining"), _header(headers, "x-ratelimit-reset")
        if remaining.isdigit():
            self.remaining = int(remaining)
        if reset.isdigit():
            self.reset_at = float(reset)

    def status(self) -> str:
        if self.exhausted:
            when = time.strftime("%H:%M", time.localtime(self.reset_at)) if self.reset_at else "later"
            return f"GitHub rate limit exhausted after {self.calls} call(s); resets at {when}"
        left = "?" if self.remaining is None else self.remaining
        return f"GitHub: {self.calls} call(s) made, {left} left this hour"


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

_LIKELY_PAGE = re.compile(r"about|team|people|leadership|founders?|contact|careers?|jobs|company|who-we-are|"
                          r"our-story", re.I)
_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
_MAILTO = re.compile(r"""href\s*=\s*["']mailto:([^"'?]+)""", re.I)
CONVENTIONAL_PATHS = ["/about", "/team", "/contact", "/careers", "/company"]


def likely_pages(html: str, root: str, limit: int) -> list[str]:
    """Same-site links that look like about/team/contact/careers pages, then conventional paths."""
    host = urlparse(root).netloc.lower().removeprefix("www.")
    pages: list[str] = []
    for href in _HREF.findall(html):
        url = urljoin(root + "/", href.strip()).split("?")[0].rstrip("/")
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not _LIKELY_PAGE.search(parts.path):
            continue
        h = parts.netloc.lower().removeprefix("www.")
        if (h == host or h.endswith("." + host)) and url != root and url not in pages:
            pages.append(url)
    for path in CONVENTIONAL_PATHS:
        if root + path not in pages:
            pages.append(root + path)
    return pages[:limit]


@dataclass
class SiteFindings:
    emails: list[tuple[str, str]] = field(default_factory=list)       # (email, page url)
    people: list[Person] = field(default_factory=list)
    github_orgs: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)                  # robots.txt said no
    failed: list[str] = field(default_factory=list)


def read_site(client, website: str, domain: str, max_pages: int) -> SiteFindings:
    out = SiteFindings()
    root = normalize_website(website)
    queue = [root]
    seen: set[str] = set()
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            page = client.get_page(url)
        except FetchError as exc:
            (out.blocked if "robots" in str(exc) else out.failed).append(url)
            if url == root:
                queue.extend(root + p for p in CONVENTIONAL_PATHS[:max_pages])
            continue
        out.read.append(url)
        if url == root:
            queue.extend(likely_pages(page, root, max_pages))
        text = html_to_text(page)
        for email in [m.strip().lower() for m in _MAILTO.findall(page)] + extract_emails(text):
            if same_site(email.rsplit("@", 1)[-1], domain) and email not in [e for e, _ in out.emails]:
                out.emails.append((email, url))
        for person in people_in_text(text, url):
            if person.name not in [p.name for p in out.people]:
                out.people.append(person)
        for org in _GITHUB_ORG.findall(page):
            if org.lower() not in _GITHUB_RESERVED and org not in out.github_orgs:
                out.github_orgs.append(org)
    return out


_DATA_PAGE = re.compile(r'data-page="([^"]+)"')


def yc_people(client, slug: str) -> tuple[list[Person], str]:
    """Founders from the company's YC page, and a note on what happened."""
    url = f"https://www.ycombinator.com/companies/{slug}"
    try:
        page = client.get_page(url)
    except FetchError as exc:
        return [], ("YC page disallowed by robots.txt" if "robots" in str(exc) else f"YC page failed ({exc})")
    m = _DATA_PAGE.search(page)
    try:
        founders = json.loads(htmllib.unescape(m.group(1)))["props"]["company"]["founders"] if m else []
    except (ValueError, KeyError, TypeError):
        founders = []
    people = [Person(name=str(f.get("full_name") or "").strip(), role=str(f.get("title") or "Founder").strip(),
                     source="yc", source_url=url) for f in founders or [] if f.get("full_name")]
    return people, f"YC page: {len(people)} founder(s)"


def github_contacts(client, org: str, domain: str, budget: GitHubBudget, max_members: int,
                    people: list[Person]) -> tuple[list[Contact], str]:
    members = budget.get(client, f"/orgs/{org}/public_members", {"per_page": 100}) or []
    found = []
    for member in members[:max_members]:
        user = budget.get(client, f"/users/{member['login']}")
        email = str((user or {}).get("email") or "").lower()
        if not email or not same_site(email.rsplit("@", 1)[-1], domain):
            continue
        name = str(user.get("name") or member["login"])
        match = next((p for p in people if name_parts(p.name) == name_parts(name)), None)
        role = match.role if match else "engineer (GitHub org member)"
        found.append(Contact(name=name, role=role, email=email, confidence="verified", source="github",
                             source_url=str(user.get("html_url") or f"https://github.com/{member['login']}"),
                             group=role_group(role) if match else "other"))
    return found, f"GitHub org {org}: {len(members)} public member(s), {len(found)} with an email on {domain}"


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def _rank_key(c: Contact) -> tuple:
    return (c.fit, CONFIDENCE.index(c.confidence), SOURCE_ORDER.index(c.source), 0 if c.name else 1, c.email)


def _good_enough(contacts: list[Contact]) -> bool:
    return any(c.fit == 0 and c.confidence in ("verified", "strong") for c in contacts)


def find_contacts(client, target: Target, profile: Profile, dnc: DoNotContact, github: GitHubBudget,
                  log: Log = lambda _: None) -> ContactResult:
    result = ContactResult(target=target, status="none")
    if not target.domain:
        result.status = "no_domain"
        result.reasons.append("no website domain from discovery, seeds.yaml, the HN header or the board")
        return result
    fit_table = ROLE_FIT.get(target.hint, ROLE_FIT["founder or CTO"])
    cands: list[Contact] = []

    def add(contact: Contact) -> None:
        contact.fit = fit_table.get(contact.group, 4)
        cands.append(contact)

    # 1. Emails the postings publish for applicants.
    for job in target.jobs:
        for email in job.emails:
            add(Contact(name="", role="named in the posting for applicants", email=email.lower(),
                        confidence="verified", source="posting", source_url=job.url, group="posting"))
    if target.kind == "role":
        result.reasons.append(f"postings: {len(cands)} applicant email(s)")

    people: list[Person] = []
    patterns: list[tuple[str, str, str]] = []           # (pattern, email, url)
    if not _good_enough(cands):
        # 2. The company's own site.
        site = read_site(client, target.website or f"https://{target.domain}", target.domain,
                         int(profile.contacts["max_site_pages"]))
        for p in site.people:
            if title_names_other_org(p.role, target.company):
                result.reasons.append(f"ignored {p.name} ({p.role}): title names another organisation "
                                      "(testimonial, investor or advisor)")
            else:
                people.append(p)
        for email, url in site.emails:
            kind = classify_address(email)
            if kind is None:
                continue
            group, role, confidence = kind
            local = email.split("@", 1)[0]
            owner = next((p for p in people if pattern_of(local, p.name)), None)
            if owner:
                patterns.append((pattern_of(local, owner.name), email, url))
                group, role = owner.group, owner.role
            elif confidence == "verified" and not re.fullmatch(r"[a-z]{2,}[._-][a-z]{2,}", local):
                # Not tied to a named person and not first.last-shaped: "jira@", "rahul@" - can't
                # tell a person from a tool or team inbox, so don't call it a verified personal address.
                confidence, role = "fallback", "unrecognised address"
            add(Contact(name=owner.name if owner else "", role=role or "person at the company", email=email,
                        confidence=confidence, source="website", source_url=url, group=group))
        note = f"website: read {len(site.read)} page(s), {len(site.emails)} email(s), {len(people)} named people"
        if site.blocked:
            note += f"; robots.txt disallowed {', '.join(urlparse(u).path or '/' for u in site.blocked)}"
        result.reasons.append(note)

        # 3. YC page (founders), robots.txt permitting.
        if target.yc_slug and not _good_enough(cands):
            yc, note = yc_people(client, target.yc_slug)
            result.reasons.append(note)
            for p in yc:
                if title_names_other_org(p.role, target.company):
                    result.reasons.append(f"ignored {p.name} ({p.role}): title names another organisation")
                elif name_parts(p.name) not in [name_parts(q.name) for q in people]:
                    people.append(p)

        # 4. GitHub org linked from the company's own site.
        if site.github_orgs and not _good_enough(cands):
            try:
                found, note = github_contacts(client, site.github_orgs[0], target.domain, github,
                                              int(profile.contacts["github_max_members"]), people)
                for c in found:
                    add(c)
                result.reasons.append(note)
            except GitHubLimit as exc:
                result.reasons.append(f"GitHub org {site.github_orgs[0]} skipped: {exc}")
            except FetchError as exc:
                result.reasons.append(f"GitHub org {site.github_orgs[0]} failed: {exc}")
        elif not site.github_orgs:
            result.reasons.append("GitHub: no organisation linked from the site")

    # 5. Email patterns for named people we have no address for.
    known = {c.email for c in cands}
    named_with_email = {name_parts(c.name) for c in cands if c.name}
    confirmed = max({p for p, _, _ in patterns}, key=[p for p, _, _ in patterns].count) if patterns else ""
    for person in people:
        if name_parts(person.name) in named_with_email:
            continue
        first, last = name_parts(person.name)
        if confirmed and person.group in GUESSABLE_GROUPS | {"recruiting"}:
            local = apply_pattern(confirmed, first, last)
            example = next(e for p, e, _ in patterns if p == confirmed)
            if local and f"{local}@{target.domain}" not in known:
                add(Contact(name=person.name, role=person.role, email=f"{local}@{target.domain}",
                            confidence="strong", source="pattern", source_url=person.source_url,
                            group=person.group, note=f"pattern {confirmed} confirmed by {example}"))
                continue
        # first@domain needs a real first name: "J Richards" or "S G" would give a wrong address.
        if person.group in GUESSABLE_GROUPS and len(first) >= 2 and f"{first}@{target.domain}" not in known:
            add(Contact(name=person.name, role=person.role, email=f"{first}@{target.domain}", confidence="guessed",
                        source="pattern", source_url=person.source_url, group=person.group,
                        note="first@domain, no pattern confirmed"))
    if people:
        result.reasons.append("named people: " + "; ".join(f"{p.name} ({p.role}, {p.source})" for p in people))
    result.people = people

    # Dedupe (best version of each address), drop the do-not-contact list, rank.
    best: dict[str, Contact] = {}
    for c in sorted(cands, key=_rank_key):
        best.setdefault(c.email, c)
    ranked = []
    for c in sorted(best.values(), key=_rank_key):
        blocked = dnc.blocks(c.email)
        if blocked:
            result.excluded.append((c.email, f"on the do-not-contact list ({blocked})"))
        else:
            ranked.append(c)
    result.considered = ranked
    result.contacts = ranked[:3]
    if result.excluded:
        result.reasons.append("do-not-contact: excluded " + ", ".join(e for e, _ in result.excluded))
    if result.contacts:
        result.status = "found"
        p = result.contacts[0]
        result.why_primary = (f"best role fit for '{target.hint}' ({p.group}, fit {p.fit}), "
                              f"then confidence ({p.confidence}), then source ({p.source})")
    else:
        result.reasons.append("nothing found")
    log(f"  {target.company:<32} {result.status:<9} " + (result.contacts[0].label() if result.contacts else
                                                         "; ".join(result.reasons[-2:])))
    return result


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

_HN_URL = re.compile(r"https?://[^\s|]+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|io|ai|dev|co|org|net|app|in|so|xyz|tech)\b", re.I)


def hn_header_website(job: Job) -> str:
    first_line = job.description.split("\n", 1)[0]
    for url in _HN_URL.findall(first_line):
        url = url if "://" in url else f"https://{url}"
        if site_domain(url) and "ycombinator.com" not in url:
            return url
    return ""


def build_targets(store, profile: Profile, seeds: list, client=None, log: Log = lambda _: None) -> list[Target]:
    """Role targets (companies with an A/B opening, minus marketplaces), then India general-outreach targets."""
    discovered = {name_key(r["name"]): r for r in store.conn.execute("SELECT * FROM companies")}
    seed_sites = {f"{s.ats}:{s.slug}": s.website for s in seeds if s.website}
    targets: dict[str, Target] = {}
    for row in store.ranked(("A", "B")):
        if row["contact_hint"] == MARKETPLACE_HINT:
            continue
        key_name = name_key(row["company"])
        disc = discovered.get(key_name)
        key = disc["key"] if disc else f"co:{key_name}"
        target = targets.get(key)
        if target is None:
            target = targets[key] = Target(
                key=key, company=row["company"], kind="role", hint=row["contact_hint"] or "founder or CTO",
                team_size=row["team_size"] if row["team_size"] is not None else (disc["team_size"] if disc else None),
                yc_slug=key[3:] if key.startswith("yc:") else "")
            sources = [("seeds.yaml", seed_sites.get(row["board_key"] or "", "")),
                       ("discovery", disc["website"] if disc else "")]
            job = store.get_job(row["id"])
            if job and job.source == "hn":
                sources.append(("HN header", hn_header_website(job)))
            for label, site in sources:
                if site_domain(site):
                    target.website, target.domain, target.domain_source = site, site_domain(site), label
                    break
            if not target.domain and client is not None and row["board_key"] and ":" in row["board_key"]:
                ats, slug = row["board_key"].split(":", 1)
                if ats in ("lever", "ashby", "greenhouse", "workable"):
                    site = board_identity(client, ats, slug)[1]
                    if site_domain(site):
                        target.website, target.domain, target.domain_source = site, site_domain(site), "board"
        job = store.get_job(row["id"])
        if job:
            target.jobs.append(job)
        target.roles.append(f"{row['title']} ({row['score']})")
        target.best_score = max(target.best_score or 0, row["score"])
    for row in store.companies_without_board():
        if row["key"] in targets or "india" not in (row["locations"] or "").lower():
            continue
        website = row["website"] or ""
        targets[row["key"]] = Target(
            key=row["key"], company=row["name"], kind="general_outreach",
            hint=hint_for_size(row["team_size"], None, profile), website=website, domain=site_domain(website),
            domain_source="discovery" if site_domain(website) else "", team_size=row["team_size"],
            yc_slug=row["key"][3:] if row["key"].startswith("yc:") else "")
    return list(targets.values())


def result_to_json(result: ContactResult) -> dict[str, Any]:
    return {
        "considered": [asdict(c) for c in result.considered],
        "excluded": [list(x) for x in result.excluded],
        "people": [asdict(p) for p in result.people],
        "reasons": result.reasons,
        "why_primary": result.why_primary,
    }
