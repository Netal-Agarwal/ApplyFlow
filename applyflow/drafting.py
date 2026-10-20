"""Phase 2b: an email draft for every outreach target, and the checks every draft must pass.

Two kinds of email:
  * role email (the target has an A/B opening): name the role, one line about the company,
    the best-matching project with one concrete detail, availability, a low-pressure ask.
  * general outreach (no opening, and manual targets): what stood out about the company,
    what I could contribute, and whether they're open to a January 2027 intern / 2027 new
    grad, or a short chat.

Every target gets a template draft that reads naturally even with almost no company
information. `draft-pack` writes a packet per target (every input plus a style guide); a
draft written from it (output/drafts/written/<id>.md) replaces the template once it passes
`validate`. Drafts may only use the candidate facts in profile.yaml (`candidate.facts`).
Nothing here sends anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from applyflow.config import Profile
from applyflow.text import contains_phrase

SIGN_OFF = "Best regards,"
SIGN_OFFS = ("Best regards,", "Best,", "Kind regards,", "Warm regards,", "Regards,", "Thanks,", "Thank you,",
             "Many thanks,")

# Who gets the resume attached rather than linked (Phase 2b hybrid rule).
HR_GROUPS = {"recruiting", "hiring_inbox"}
_RESUME_BY_EMAIL = re.compile(
    r"\b(?:send|email|e-mail|mail|forward|share|drop)\b[^.\n]{0,60}\b(?:resume|résumé|cv)s?\b"
    r"|\b(?:resume|résumé|cv)s?\b[^.\n]{0,40}\bto\s+[\w.+-]+(?:@|\s+(?:\[at\]|\(at\)|at)\s+)", re.I)

BUZZWORDS = [
    "passionate", "synergy", "synergies", "rockstar", "rock star", "ninja", "guru", "cutting-edge", "cutting edge",
    "game-changer", "game changer", "game-changing", "revolutionary", "revolutionize", "world-class",
    "best-in-class", "disrupt", "disrupting", "disruptive", "leverage", "thought leader", "go-getter",
    "hard-working", "hardworking", "self-starter", "team player", "outside the box", "finds you well",
    "i am writing to express", "dream company", "huge fan", "big fan", "amazing", "incredible", "awesome",
    "visionary", "groundbreaking", "unparalleled", "esteemed", "humbly", "seamless", "next-gen",
]
_CLICKBAIT = re.compile(r"!|\burgent\b|\bquick question\b|^\s*(?:re|fwd?):|\bdon'?t miss\b|\blast chance\b|"
                        r"\bact now\b|\byou won'?t believe\b|\bfree\b", re.I)
_PLACEHOLDER = re.compile(r"[\[\]{}<>]|\b(?:TODO|TBD|XXX+|FIXME|lorem ipsum|first_?name|company_?name|"
                          r"your name here|insert)\b", re.I)
_TIMEZONE = re.compile(r"time[- ]?zones?", re.I)
# A sentence about the company's own fundraise (Phase 2c): only allowed with a sourced funding signal.
# "raises the bar" isn't about money: a raise counts only next to an amount or round word.
_FUNDING_TALK = re.compile(r"\b(?:raised|raising|raises?|raise)\b[^.!?]{0,40}?(?:\$|₹|\b(?:rs|inr|usd|mn|million|"
                           r"crore|cr|round|series|seed|funding|capital|investors?)\b)|\bfund-?raise\b|"
                           r"\bfundraising\b|\bfunding round\b|\bseed round\b|\bseries [a-f]\b|\bpre-?seed\b|"
                           r"\bpre-series\b|\bcongratulations on (?:the|your) (?:recent )?(?:raise|round|funding|series|"
                           r"seed|pre)", re.I)
_ROUND_WORDS = re.compile(r"\b(pre-?series [a-f]|series [a-f]|pre-?seed|seed|angel|bridge)\b", re.I)
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]
_MONTH_YEAR = re.compile(r"\b(" + "|".join(_MONTHS) + r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+"
                         r"(20\d\d)\b", re.I)
# Words that mark a sentence as being about the company / the posting, not about me.
_ABOUT_THEM = re.compile(r"\b(?:you|your|yours|the role|this role|the team|the posting|the job|the stack|"
                         r"you're|you've)\b", re.I)

# Named technologies the fact check looks for, on top of the profile's skill list. Generic words
# ("backend", "api", "cloud") are left out: they're not claims about a specific tool.
EXTRA_TECH = ["go", "golang", "rust", "kubernetes", "k8s", "kafka", "redis", "docker", "terraform", "graphql",
              "grpc", "django", "spring", "spring boot", "typescript", "next.js", "vue", "angular", "flutter",
              "kotlin", "swift", "pytorch", "tensorflow", "langchain", "spark", "airflow", "snowflake",
              "elasticsearch", "gcp", "azure", "llm", "llms", "rag", "ffmpeg", "c#", ".net", "ruby",
              "elixir", "scala", "php", "laravel", "celery", "rabbitmq", "machine learning", "deep learning",
              "distributed systems", "linux", "ci/cd", "mcp"]
GENERIC_TECH = {"api", "apis", "backend", "cloud", "rest api", "rest apis", "restful", "sql", "git",
                "algorithms", "data structures", "microservices", "serverless", "go"}

# Scoring vocabulary -> how the skill is written in the facts.
SKILL_ALIASES = {"postgres": "postgresql", "nodejs": "node.js", "express": "express.js", "lambda": "aws lambda",
                 "rest api": "rest apis", "restful": "rest apis", "apis": "rest apis", "api": "rest apis"}


# ---------------------------------------------------------------------------
# Candidate facts
# ---------------------------------------------------------------------------

@dataclass
class Project:
    name: str
    stack: str = ""
    detail: str = ""
    keywords: list[str] = field(default_factory=list)


@dataclass
class Facts:
    """The only things a draft may say about the candidate (profile.yaml `candidate.facts`)."""
    name: str
    first_name: str
    degree_short: str
    college_short: str
    signature_title: str
    graduation: str
    internship: str
    full_time: str
    focus: str
    skills: list[str]
    projects: list[Project]
    experience: list[dict[str, str]]
    patent: dict[str, str]
    phone: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    resume_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def project(self, name: str) -> Project | None:
        return next((p for p in self.projects if p.name.lower() == (name or "").lower()), None)

    def text(self) -> str:
        """Every fact as one string, for the fact check."""
        parts = [self.name]
        _flatten(self.raw, parts)
        return "\n".join(p for p in parts if p)

    def credentials(self) -> str:
        """The one-sentence credentials line: the internship and the patent (template wording)."""
        lab = self.experience[0].get("short", "") if self.experience else ""
        patent = self.patent.get("short", "") if self.patent.get("title") else ""
        if lab and patent:
            return f"Before this, I interned at {lab} and co-invented a published patent on {patent}."
        if lab:
            return f"Before this, I interned at {lab}."
        if patent:
            return f"I also co-invented a published patent on {patent}."
        return ""

    def credential_marks(self) -> list[tuple[str, str]]:
        """(label, word) pairs a credentials line must contain: the lab's organisation and the patent."""
        marks = []
        if self.experience and self.experience[0].get("short"):
            org = self.experience[0].get("org_short") or self.experience[0]["short"]
            marks.append(("internship", org))
        if self.patent.get("title") and self.patent.get("short"):
            marks.append(("patent", "patent"))
        return marks

    def signature(self) -> list[str]:
        lines = [self.name, self.signature_title, self.phone,
                 f"LinkedIn: {self.linkedin_url}" if self.linkedin_url else "",
                 f"GitHub: {self.github_url}" if self.github_url else "",
                 f"Resume: {self.resume_url}" if self.resume_url else ""]
        return [ln for ln in lines if ln]

    def listing(self) -> list[str]:
        """The facts as bullet lines (packets, report)."""
        lines = [f"Name: {self.name}", f"Education: {self.raw.get('education', '')}",
                 f"Graduation: {self.graduation}", f"Availability: {self.internship}; {self.full_time}",
                 f"Focus: {self.focus}", f"Skills: {', '.join(self.skills)}"]
        if self.raw.get("coursework"):
            lines.append(f"Coursework: {self.raw['coursework']}")
        for e in self.experience:
            lines.append(f"Experience: {e.get('name', '')} at {e.get('org', '')} ({e.get('when', '')}): "
                         f"{e.get('detail', '')}")
        if self.patent.get("title"):
            lines.append(f"Patent: {self.patent['title']} (published {self.patent.get('published', '')}): "
                         f"{self.patent.get('detail', '')}")
        for p in self.projects:
            lines.append(f"Project: {p.name} ({p.stack}): {p.detail}")
        return [ln for ln in lines if not ln.endswith(": ")]


def _flatten(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k != "keywords":           # matching hints, not facts
                _flatten(v, out)
    elif isinstance(value, list):
        for v in value:
            _flatten(v, out)
    elif value not in (None, ""):
        out.append(str(value))


def load_facts(profile: Profile) -> Facts:
    cand = profile.candidate or {}
    raw = dict(cand.get("facts") or {})
    name = str(cand.get("name") or "")
    return Facts(
        name=name, first_name=str(raw.get("first_name") or name.split(" ")[0]),
        degree_short=str(raw.get("degree_short") or ""), college_short=str(raw.get("college_short") or ""),
        signature_title=str(raw.get("signature_title") or ""), graduation=str(raw.get("graduation") or ""),
        internship=str(raw.get("internship") or ""), full_time=str(raw.get("full_time") or ""),
        focus=str(raw.get("focus") or ""), skills=[str(s) for s in raw.get("skills") or []],
        projects=[Project(name=str(p.get("name") or ""), stack=str(p.get("stack") or ""),
                          detail=str(p.get("detail") or ""), keywords=[str(k).lower() for k in p.get("keywords") or []])
                  for p in raw.get("projects") or [] if p.get("name")],
        experience=[{k: str(v) for k, v in e.items()} for e in raw.get("experience") or []],
        patent={k: str(v or "") for k, v in (raw.get("patent") or {}).items()},
        phone=str(cand.get("phone") or ""), linkedin_url=str(cand.get("linkedin_url") or ""),
        github_url=str(cand.get("github_url") or ""), resume_url=str(cand.get("resume_url") or ""), raw=raw)


def tech_terms(profile: Profile) -> list[str]:
    terms = set(profile.skills) | set(EXTRA_TECH)
    return sorted(t for t in terms if t not in GENERIC_TECH)


# ---------------------------------------------------------------------------
# Inputs: everything a draft is built from
# ---------------------------------------------------------------------------

@dataclass
class DraftInputs:
    target_id: str
    company_key: str
    company: str                    # display name ("Wifi Dabba", not "Wifi Dabba, Inc.")
    kind: str                       # role | general_outreach | manual
    to_email: str = ""
    contact_name: str = ""
    contact_role: str = ""
    confidence: str = ""
    contact_group: str = ""         # founder | cto | eng_lead | recruiting | hiring_inbox | general_inbox | posting | other
    role_title: str = ""
    role_score: int | None = None
    role_tier: str = ""
    role_track: str = ""
    role_family: str = ""
    role_url: str = ""
    role_location: str = ""         # scoring location_class
    role_skills: list[str] = field(default_factory=list)
    role_text: str = ""             # the posting's text
    lead_project: str = ""
    one_liner: str = ""
    homepage_title: str = ""
    meta_description: str = ""
    homepage_snippet: str = ""
    website: str = ""
    team_size: int | None = None
    batch: str = ""
    locations: str = ""
    note: str = ""                  # add-target --note
    remote_global: bool = False
    rules: str = "2c"               # "2b": already scheduled under the pre-2c greeting / subject rules
    funding: dict[str, str] = field(default_factory=dict)   # sourced round: round, amount, date, source_url
    hl_score: int | None = None     # hiring likelihood (pool), for packets

    @property
    def first_name(self) -> str:
        words = [w for w in re.split(r"\s+", self.contact_name.strip())
                 if w.rstrip(".").lower() not in {"dr", "mr", "ms", "mrs", "prof"}]
        if not words:
            return ""
        first = words[0]
        return first.capitalize() if first.isupper() and len(first) > 2 else first

    @property
    def shared_inbox(self) -> bool:
        """The address is an inbox (founders@, careers@, hello@ ...), not one person's."""
        from applyflow.contacts import shared_inbox
        return bool(self.to_email) and shared_inbox(self.to_email)

    @property
    def routable_inbox(self) -> bool:
        """hello@ / hi@ / team@ / info@ / contact@: whoever triages it needs a subject that says 'application'."""
        from applyflow.contacts import routable_inbox
        return self.rules != "2b" and bool(self.to_email) and routable_inbox(self.to_email)

    @property
    def greeting(self) -> str:
        """'Hi <First>,' for a named person; 'Hi <Company> team,' for a shared inbox; 'Hi,' for a personal
        address whose owner's name isn't known (2c). Drafts locked to 2b keep 'Hi <Company> team,'."""
        if self.first_name:
            return f"Hi {self.first_name},"
        if self.rules == "2b" or not self.to_email or self.shared_inbox:
            return f"Hi {self.company} team,"
        return "Hi,"

    def source_text(self) -> str:
        """What the company itself published: the posting, its one-liner and site text (and a sourced funding
        round, so its amount and date may be quoted)."""
        funding = " ".join(self.funding.get(k, "") for k in ("round", "amount", "date")) if self.funding else ""
        return "\n".join([self.company, self.role_title, self.role_text, self.one_liner, self.homepage_title,
                          self.meta_description, self.homepage_snippet, self.note, self.batch, funding])

    def focus_word(self) -> str:
        """Backend / AI / Full-stack / Cloud: the role word for an application subject, by fit."""
        if self.role_family:
            fam = self.role_family.lower()
            if "ai" in fam or "ml" in fam or "data" in fam:
                return "AI"
            if "full" in fam:
                return "Full-stack"
            if "cloud" in fam or "platform" in fam or "devops" in fam:
                return "Cloud"
            return "Backend"
        text = " ".join([self.one_liner, self.meta_description, self.homepage_title, self.role_title]).lower()
        if re.search(r"\b(ai|llm|llms|genai|agents?|agentic|machine learning|ml|voice ai|computer vision)\b", text):
            return "AI"
        if re.search(r"\b(cloud|devops|kubernetes|observability|serverless|data centers?)\b", text):
            return "Cloud"
        if re.search(r"\b(marketplace|consumer app|mobile app|web app|e-?commerce|storefront)\b", text):
            return "Full-stack"
        return "Backend"


def display_company(name: str) -> str:
    """'Wifi Dabba, Inc.' -> 'Wifi Dabba'."""
    cleaned = re.sub(r",?\s+(?:inc|llc|ltd|pvt\.?\s*ltd|private limited|limited|corp)\.?$", "", name.strip(), flags=re.I)
    return cleaned or name


def resume_action(inputs: DraftInputs) -> tuple[str, str]:
    """('attach' | 'link', why): attach for recruiters / HR inboxes or a posting that asks for a resume by email."""
    if inputs.contact_group in HR_GROUPS:
        return "attach", "the contact is a recruiter / HR inbox"
    if inputs.role_text and _RESUME_BY_EMAIL.search(inputs.role_text):
        return "attach", "the posting asks for a resume by email"
    return "link", "founder / engineering contact or general inbox: resume link in the signature"


def _canonical(skill: str) -> str:
    return SKILL_ALIASES.get(skill.lower(), skill.lower())


def shared_skills(inputs: DraftInputs, facts: Facts) -> list[str]:
    """The posting's matched skills that are also in the facts, as the facts write them."""
    by_lower = {s.lower(): s for s in facts.skills}
    out: list[str] = []
    for skill in inputs.role_skills:
        name = by_lower.get(_canonical(skill))
        if name and name not in out and name.lower() not in GENERIC_TECH:
            out.append(name)
    return out


def choose_project(inputs: DraftInputs, facts: Facts) -> tuple[Project | None, str]:
    """Best project for this target: the role family's lead project and the posting's stack, or (no
    opening) the project whose keywords match what the company does."""
    if not facts.projects:
        return None, "no projects in the facts"
    if inputs.kind == "role":
        def score(p: Project) -> float:
            overlap = sum(contains_phrase(p.stack, _canonical(s)) for s in inputs.role_skills)
            return overlap + (2 if p.name == inputs.lead_project else 0)
        best = max(facts.projects, key=score)
        overlap = [s for s in inputs.role_skills if contains_phrase(best.stack, _canonical(s))]
        why = "lead project for the role family" if best.name == inputs.lead_project else "best stack overlap"
        return best, why + (f"; shares {', '.join(overlap)}" if overlap else "")
    about = " ".join([inputs.one_liner, inputs.meta_description, inputs.homepage_title, inputs.note])

    def hits(p: Project) -> list[str]:
        return [k for k in p.keywords if contains_phrase(about, k)]
    best = max(facts.projects, key=lambda p: len(hits(p)))    # ties keep the first (backend) project
    found = hits(best)
    return best, (f"company text mentions {', '.join(found[:4])}" if found else "default (backend) project")


def _clean_about(text: str) -> str:
    """A company one-liner usable inside a sentence, or '' if it's truncated, salesy or too long."""
    text = " ".join((text or "").split()).strip()
    if not text or text.endswith(("…", "...")) or any(contains_phrase(text, b) for b in BUZZWORDS):
        return ""
    first = re.split(r"(?<=[.!?])\s", text)[0].rstrip(".!? ")
    first = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]+", "", first).strip()     # emoji flags etc.
    if not first or len(first.split()) > 16 or _PLACEHOLDER.search(first):
        return ""
    return first


def company_about(inputs: DraftInputs) -> str:
    for text in (inputs.one_liner, inputs.meta_description):
        about = _clean_about(text)
        if about and about.lower() != inputs.company.lower():
            return about
    return ""


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

@dataclass
class Draft:
    target_id: str
    subject: str
    body: str
    resume_action: str
    resume_reason: str
    source: str = "template"                      # template | claude
    project: str = ""
    project_reason: str = ""
    failures: list[str] = field(default_factory=list)            # why a written draft was rejected
    template_failures: list[str] = field(default_factory=list)   # the template's own check (should be empty)


@dataclass
class _Sentence:
    para: int
    text: str
    kind: str = "required"           # required | preferred | filler
    order: int = 0
    short: str = ""                  # shorter wording, used if the draft is still too long


def _availability(inputs: DraftInputs, facts: Facts) -> str:
    if inputs.role_track == "new_grad":
        return f"I can join for {facts.full_time}, and I'm also available for {facts.internship}."
    return f"I'm available for {facts.internship}, and for {facts.full_time}."


def _assemble(inputs: DraftInputs, facts: Facts, sentences: list[_Sentence], min_words: int, max_words: int) -> str:
    """Fit the message into the word range: preferred sentences go first, fillers only when short."""
    chosen = [s for s in sentences if s.kind in ("required", "preferred")]
    fillers = [s for s in sentences if s.kind == "filler"]

    def words(items: list[_Sentence]) -> int:
        return sum(len(s.text.split()) for s in items)
    for f in fillers:
        if words(chosen) >= min_words:
            break
        chosen.append(f)
    for s in sorted([s for s in chosen if s.kind != "required"], key=lambda s: (s.kind == "preferred", -s.order)):
        if words(chosen) <= max_words:
            break
        chosen.remove(s)
    for s in chosen:
        if words(chosen) > max_words and s.short:
            s.text = s.short
    paras: dict[int, list[str]] = {}
    for s in sorted(chosen, key=lambda s: (s.para, s.order)):
        paras.setdefault(s.para, []).append(s.text)
    message = "\n\n".join(" ".join(paras[k]) for k in sorted(paras))
    return "\n".join([inputs.greeting, "", message, "", SIGN_OFF, *facts.signature()])


def template_draft(inputs: DraftInputs, facts: Facts, min_words: int = 120, max_words: int = 160,
                   terms: list[str] | None = None) -> Draft:
    project, project_reason = choose_project(inputs, facts)
    action, action_reason = resume_action(inputs)
    about = company_about(inputs)
    c = inputs.company
    intro = (f"I'm {facts.first_name}, a final-year {facts.degree_short} student at {facts.college_short} "
             f"graduating in {facts.graduation}")
    s: list[_Sentence] = []
    if inputs.kind == "role":
        s.append(_Sentence(0, f"{intro}, and I'd like to be considered for the {inputs.role_title} role at {c}.", order=0))
        if about:
            s.append(_Sentence(0, f"What {c} is building ({about}) is the kind of product I'd like to work on.",
                               "preferred", 1))
        else:
            s.append(_Sentence(0, f"The role lines up closely with the backend work I've been doing.", "preferred", 1))
    else:
        s.append(_Sentence(0, f"{intro}.", order=0))
        if about:
            s.append(_Sentence(0, f"{c} ({about}) caught my attention because it's the kind of product I'd like "
                                  f"to help build.", order=1))
        else:
            s.append(_Sentence(0, f"I'm writing because I'd like to work with the engineering team at {c}.", order=1))
        s.append(_Sentence(1, f"My main focus is {facts.focus}, and I could help {c} with API and service work on "
                              f"the product.", order=0, short=f"My main focus is {facts.focus}."))
        line = funding_line(inputs)
        if line:
            s.append(_Sentence(0, line, "preferred", 2))
    if project:
        lead = "The closest match from my work is" if inputs.kind == "role" else "Most recently I built"
        s.append(_Sentence(1, f"{lead} {project.name}, {project.detail}.", order=1,
                           short=f"{lead} {project.name} ({project.stack})." if project.stack else ""))
    shared = shared_skills(inputs, facts) if inputs.kind == "role" else []
    if shared:
        listed = ", ".join(shared[:3]) if len(shared[:3]) < 3 else f"{shared[0]}, {shared[1]} and {shared[2]}"
        if len(shared) == 2:
            listed = f"{shared[0]} and {shared[1]}"
        s.append(_Sentence(1, f"The posting's stack ({listed}) is what I've built these projects with.", "preferred", 2))
    credentials = facts.credentials()
    if credentials:
        s.append(_Sentence(1, credentials, order=8))
    if facts.experience and facts.experience[0].get("detail"):
        e = facts.experience[0]
        s.append(_Sentence(1, f"At {e.get('org_short') or e.get('org', '')}, I {e['detail']}.", "filler", 9))
    s.append(_Sentence(2, _availability(inputs, facts), order=0))
    if inputs.remote_global:
        s.append(_Sentence(2, "I'm comfortable working across time zones and keeping regular overlap with the team.",
                           order=1, short="I'm comfortable working across time zones."))
    resume_line = "I've attached my resume." if action == "attach" else "My resume is linked below."
    if inputs.kind == "role":
        s.append(_Sentence(3, f"{resume_line} If my background fits what you need, I'd be happy to talk "
                              f"whenever it suits you.", order=0))
    else:
        s.append(_Sentence(3, f"Would {c} be open to a January 2027 intern or a 2027 new grad? If not, I'd still "
                              f"welcome a short chat about where I could help. {resume_line}", order=0,
                           short=f"Would you be open to a January 2027 intern or a 2027 new grad? If not, I'd still "
                                 f"welcome a short chat. {resume_line}"))
    body = _assemble(inputs, facts, s, min_words, max_words)
    if inputs.routable_inbox:
        subject = application_subject(inputs, facts)
    elif inputs.kind == "role":
        subject = f"{inputs.role_title} - {facts.signature_title}"
    else:
        subject = f"January 2027 intern or 2027 new grad at {c}?"
    draft = Draft(target_id=inputs.target_id, subject=subject, body=body, resume_action=action,
                  resume_reason=action_reason, project=project.name if project else "", project_reason=project_reason)
    draft.template_failures = validate(subject, body, inputs, facts, action, min_words, max_words, terms or [])
    return draft


def application_subject(inputs: DraftInputs, facts: Facts) -> str:
    """A subject a shared inbox (hello@ / info@ ...) routes as an application, e.g.
    'Internship / new-grad application – Netal Agarwal (Backend, Jan 2027)'."""
    if inputs.kind == "role" and inputs.role_title:
        subject = f"{inputs.role_title} application – {facts.name} (Jan 2027)"
        if len(subject) <= 90:
            return subject
    return f"Internship / new-grad application – {facts.name} ({inputs.focus_word()}, Jan 2027)"


def _month_year(day: str) -> str:
    try:
        y, m = int(day[:4]), int(day[5:7])
        return f"{_MONTHS[m - 1].capitalize()} {y}"
    except (ValueError, IndexError):
        return ""


def funding_line(inputs: DraftInputs) -> str:
    """A congratulations line for a sourced round within 6 months, worded only from the signal."""
    f = inputs.funding
    if not f or not f.get("source_url") or not f.get("recent"):
        return ""
    r = (f.get("round") or "").strip()
    named = f"the {r}" if re.search(r"series|round", r, re.I) else (f"the {r.lower()} round" if r else "the recent funding round")
    when = _month_year(f.get("date", ""))
    return f"Congratulations on {named}" + (f" announced in {when}." if when else ".")


def funding_failures(message: str, inputs: DraftInputs) -> list[str]:
    """A draft may mention the company's fundraise only with a sourced signal, and must state it accurately.
    Funding words the company uses about its own product ("the fundraising platform for schools") don't count."""
    own = " ".join([inputs.one_liner, inputs.meta_description, inputs.homepage_title, inputs.homepage_snippet])
    if not any(not contains_phrase(own, m.group(0)) for m in _FUNDING_TALK.finditer(message)):
        return []
    f = inputs.funding
    if not f or not f.get("source_url"):
        return ["mentions funding, but there's no sourced funding signal for this company (never invent one)"]
    out = []
    signal_round = (f.get("round") or "").lower().replace("-", " ")
    # Only sentences about the raise are checked ("available from January 2027" isn't about it).
    message = " ".join(s for s in re.split(r"(?<=[.!?])\s+|\n+", message)
                       if _FUNDING_TALK.search(s) or re.search(r"\b(round|announced|investors?)\b", s, re.I))
    for m in _ROUND_WORDS.finditer(message):
        said = m.group(1).lower().replace("-", " ")
        if not signal_round or said not in signal_round:
            out.append(f"says '{m.group(1)}', but the funding signal's round is '{f.get('round') or 'not stated'}'")
    for m in _MONTH_YEAR.finditer(message):
        said_month = m.group(1).lower()[:3]
        if not f.get("date") or (said_month, m.group(2)) != (_MONTHS[int(f['date'][5:7]) - 1][:3], f["date"][:4]):
            out.append(f"says '{m.group(0)}', but the round was reported {f.get('date') or 'on an unknown date'}")
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def split_body(body: str) -> tuple[str, str, list[str], str]:
    """(greeting line, message text, signature lines, sign-off line) of a draft body."""
    lines = [ln.rstrip() for ln in body.strip().replace("\r\n", "\n").split("\n")]
    greeting = lines[0].strip() if lines else ""
    signoff_at = max((i for i, ln in enumerate(lines) if ln.strip() in SIGN_OFFS), default=-1)
    if signoff_at < 0:
        return greeting, "\n".join(lines[1:]).strip(), [], ""
    message = "\n".join(lines[1:signoff_at]).strip()
    signature = [ln.strip() for ln in lines[signoff_at + 1:] if ln.strip()]
    return greeting, message, signature, lines[signoff_at].strip()


def word_count(text: str) -> int:
    return len(text.split())


def _role_named(title: str, text: str) -> bool:
    core = re.split(r"\s+[-–—(|]\s*|\s*[(|]", title)[0].strip()
    low = " ".join(text.lower().split())
    return bool(title) and (title.lower() in low or (len(core) > 3 and core.lower() in low))


def validate(subject: str, body: str, inputs: DraftInputs, facts: Facts, action: str,
             min_words: int = 120, max_words: int = 160, terms: list[str] | None = None) -> list[str]:
    """Every reason this draft can't be used; [] when it passes."""
    failures: list[str] = []
    subject = (subject or "").strip()
    if not subject:
        failures.append("subject is empty")
    elif len(subject) > 90:
        failures.append(f"subject is {len(subject)} characters (max 90)")
    elif _CLICKBAIT.search(subject):
        failures.append(f"subject looks like clickbait ('{_CLICKBAIT.search(subject).group(0)}')")
    for label, text in (("subject", subject), ("body", body)):
        m = _PLACEHOLDER.search(text)
        if m:
            failures.append(f"leftover placeholder or bracket in the {label}: '{m.group(0)}'")

    greeting, message, signature, signoff = split_body(body)
    if greeting != inputs.greeting:
        if inputs.rules != "2b" and inputs.to_email and not inputs.shared_inbox and greeting.endswith(" team,"):
            failures.append(f"greeting is '{greeting}', but {inputs.to_email} is a person's address: "
                            f"'Hi <Company> team,' is only for shared inboxes (expected '{inputs.greeting}')")
        elif inputs.rules != "2b" and inputs.shared_inbox and not greeting.endswith(" team,"):
            failures.append(f"greeting is '{greeting}', but {inputs.to_email} is a shared inbox "
                            f"(expected '{inputs.greeting}')")
        else:
            failures.append(f"greeting is '{greeting}', expected '{inputs.greeting}'")
    if inputs.routable_inbox and subject and not ("application" in subject.lower() and facts.name.lower() in
                                                  subject.lower()):
        failures.append(f"{inputs.to_email} is a general inbox: the subject must read as an application and name "
                        f"the candidate, e.g. '{application_subject(inputs, facts)}'")
    if not signoff:
        failures.append(f"no sign-off line (e.g. '{SIGN_OFF}') before the signature")
    if signature != facts.signature():
        failures.append("signature missing or changed (must be: " + " / ".join(facts.signature()) + ")")
    n = word_count(message)
    if not min_words <= n <= max_words:
        failures.append(f"{n} words between greeting and sign-off (must be {min_words}-{max_words})")

    low = message.lower()
    if action == "attach":
        if not re.search(r"\battach", low) or "resume" not in low:
            failures.append("resume_action is attach, but the body doesn't say the resume is attached")
    else:
        if "resume" not in low:
            failures.append("no resume line (resume_action is link: say it's linked below)")
        elif re.search(r"\battach", low):
            failures.append("says the resume is attached, but resume_action is link")
    missing = [label for label, word in facts.credential_marks() if word.lower() not in low]
    if missing:
        failures.append(f"no credentials line (mention the {' and the '.join(missing)})")
    if inputs.remote_global and not _TIMEZONE.search(message):
        failures.append("remote / multi-country target, but no line about working across time zones")
    if inputs.kind == "role" and not _role_named(inputs.role_title, f"{subject}\n{message}"):
        failures.append(f"doesn't name the role ('{inputs.role_title}')")
    for word in BUZZWORDS:
        if contains_phrase(f"{subject}\n{message}", word):
            failures.append(f"buzzword or filler: '{word}'")
    failures += funding_failures(f"{subject}\n{message}", inputs)

    # Facts: every number and named technology must come from the facts, or (for technologies)
    # from the company's own text in a sentence about them ("your stack uses Go").
    fact_text, source = facts.text(), inputs.source_text()
    allowed = _numbers(fact_text) | _numbers(source)
    for number in sorted(_numbers(f"{subject}\n{message}") - allowed):
        failures.append(f"number '{number}' isn't in the candidate facts or the company's text")
    sentences = re.split(r"(?<=[.!?])\s+|\n+", message)
    for term in terms or []:
        if not contains_phrase(message, term) or contains_phrase(fact_text, term):
            continue
        uses = [s for s in sentences if contains_phrase(s, term)]
        if not (contains_phrase(source, term) and all(_ABOUT_THEM.search(s) or inputs.company.lower() in s.lower()
                                                      for s in uses)):
            failures.append(f"mentions '{term}', which isn't in the candidate facts")
    return failures


def check_written(subject: str, body: str, inputs: DraftInputs, facts: Facts, min_words: int = 120,
                  max_words: int = 160, terms: list[str] | None = None) -> list[str]:
    return validate(subject, body, inputs, facts, resume_action(inputs)[0], min_words, max_words, terms)


# ---------------------------------------------------------------------------
# Written drafts (output/drafts/written/<id>.md)
# ---------------------------------------------------------------------------

WRITTEN_FORMAT = """Subject: <one line>

Body:
Hi <FirstName>,            (or: Hi <Company> team, / Hi, - as the packet says)

<120-160 words>

Best regards,
<the signature exactly as given>"""


def parse_written(text: str) -> tuple[str, str]:
    """(subject, body) from a written draft file; ValueError if it isn't in the fixed format."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not lines[i].lower().startswith("subject:"):
        raise ValueError("first line must be 'Subject: ...'")
    subject = lines[i].split(":", 1)[1].strip()
    i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].strip().lower() == "body:":
        i += 1
    body = "\n".join(lines[i:]).strip()
    if not body:
        raise ValueError("no body after the subject")
    return subject, body


# ---------------------------------------------------------------------------
# Gmail compose link
# ---------------------------------------------------------------------------

def gmail_url(to: str, subject: str, body: str) -> str:
    """Opens a Gmail compose window with everything filled in; every value fully URL-encoded."""
    return ("https://mail.google.com/mail/?view=cm&to=" + quote(to, safe="@") + "&su=" + quote(subject, safe="")
            + "&body=" + quote(body, safe=""))


# ---------------------------------------------------------------------------
# Packets
# ---------------------------------------------------------------------------

STYLE_GUIDE = """# Drafting style guide (ApplyFlow Phase 2b)

Write one email per packet, to `output/drafts/written/<id>.md`, then run
`python -m applyflow import-drafts`. Drafts that fail a check stay on the template, with the reason.

**Voice.** Warm and professional: a final-year student writing to someone busy. Plain sentences,
no buzzwords ("passionate", "cutting-edge", "leverage", "synergy", "game-changer", "world-class"),
no flattery filler ("huge fan", "amazing", "dream company"), no "I hope this email finds you well".

**Greeting.** Exactly the greeting the packet gives: "Hi <FirstName>," for a named person (from the
post, page or an unambiguous handle), "Hi," for a person's address whose name isn't known, and
"Hi <Company> team," only for a shared inbox (founders@, careers@, hello@, info@ ...).

**Length.** 120-160 words between the greeting and the sign-off (the signature doesn't count).

**Role email** (the packet has an opening):
1. Name the role in the first two sentences.
2. One specific line about the company or product, taken from the packet's company context.
3. The packet's best-matching project with one concrete detail (a number, the stack, what it does).
4. Availability (internship from January 2027 and/or full-time from mid-2027).
5. A low-pressure ask to be considered.

**General outreach** (no opening):
1. What specifically stood out about the company - from its one-liner, site text or description.
   Say what they do; don't claim you use their product.
2. What I could contribute (backend / API / serverless work), tied to one project and one detail.
3. Ask whether they're open to a January 2027 intern or a 2027 new grad, or a short chat.

**Both.**
- A credentials line right after the project paragraph: one short sentence with the DRDO internship and
  the published patent, e.g. "Before this, I interned at DRDO's DEAL lab and co-invented a published
  patent on Android attention management." Vary the wording from email to email.
- Write the company line in your own words: an observation about the product, not a close paraphrase
  of their site's copy.
- Don't repeat a phrase between the opening and the closing (e.g. asking to be "considered" in both).
- Remote / multi-country targets (the packet says so) get one line about working across time zones.
- The resume line follows the packet's `resume_action`: `link` -> "My resume is linked below."
  (never "attached"); `attach` -> "I've attached my resume." (you attach the PDF in Gmail).
- Use only the candidate facts in the packet. Every number and named technology you write must be in
  those facts; a technology from the company's own text is fine only in a sentence about them
  ("your stack uses Go"), never as a skill claim.
- Subject: short and specific (role + who I am, or the intern / new-grad question). No "!",
  "urgent", "quick question", "Re:". To a general inbox (hello@ / hi@ / team@ / info@ / contact@) the
  subject must read as an application and name me, e.g. "Internship / new-grad application – Netal
  Agarwal (Backend, Jan 2027)", with the role word (Backend / AI / Full-stack / Cloud) fitting the company.
  founders@ and personal addresses keep the conversational subjects.
- Funding: mention a raise (e.g. congratulate them) only when the packet lists a sourced funding signal,
  and state its round and month exactly as given. Never infer or invent one.
- End with the sign-off and the signature exactly as the packet gives them.
- No brackets or placeholders left in the text.
"""


def render_packet(inputs: DraftInputs, draft: Draft, facts: Facts, meta: dict[str, Any]) -> str:
    """A self-contained drafting packet: target, recipient, company context, opening, rules, facts, template."""
    lines = [f"# Drafting packet: {inputs.company} ({inputs.target_id})", "",
             f"Write the draft to: `output/drafts/written/{inputs.target_id}.md` (format at the end).", "",
             f"- Priority: {meta.get('priority', '?')} · send tier: {meta.get('send_tier') or '-'} · "
             f"status: {meta.get('status', '')}",
             f"- Type: {'role email' if inputs.kind == 'role' else 'general outreach'}"
             + (" (manual target)" if inputs.kind == "manual" else ""),
             f"- Why a target: {meta.get('why', '')}", "", "## Recipient", ""]
    if inputs.to_email:
        who = f"{inputs.contact_name} ({inputs.contact_role})" if inputs.contact_name else inputs.contact_role
        lines += [f"- To: {inputs.to_email} - {who} [{inputs.confidence}]",
                  f"- Greeting to use: `{inputs.greeting}`"]
    else:
        lines.append(f"- No address yet ({meta.get('status', '')}); write it for `{inputs.greeting}`.")
    for b in meta.get("backups", []):
        lines.append(f"- If it bounces: {b}")
    lines += ["", "## Company context", "",
              f"- Website: {inputs.website or '-'}",
              f"- One-liner: {inputs.one_liner or '-'}",
              f"- Homepage title: {inputs.homepage_title or '-'}",
              f"- Meta description: {inputs.meta_description or '-'}",
              f"- Team size: {inputs.team_size if inputs.team_size is not None else 'unknown'}"
              + (f" · YC {inputs.batch}" if inputs.batch else "") + (f" · {inputs.locations}" if inputs.locations else ""),
              f"- Hiring likelihood: {inputs.hl_score if inputs.hl_score is not None else '-'}"]
    if inputs.note:
        lines.append(f"- Your note (add-target): {inputs.note}")
    lines += ["", "Homepage text (snippet):", "", "> " + (inputs.homepage_snippet or "(none fetched)").replace("\n", "\n> ")]
    if inputs.kind == "role":
        lines += ["", "## The opening", "",
                  f"- Title: {inputs.role_title}",
                  f"- Score {inputs.role_score} (tier {inputs.role_tier}) · track {inputs.role_track} · "
                  f"family {inputs.role_family} · location {inputs.role_location}",
                  f"- Posting: {inputs.role_url}",
                  f"- Skills it matched: {', '.join(inputs.role_skills) or '-'}", "",
                  "Posting text (first 2,500 characters):", "",
                  "> " + inputs.role_text[:2500].strip().replace("\n", "\n> ")]
    if inputs.funding:
        f = inputs.funding
        lines += ["", "## Funding signal (sourced)", "",
                  f"- {f.get('round') or 'round not stated'} · {f.get('amount') or 'amount not stated'} · reported "
                  f"{f.get('date')} · {f.get('source_url')}",
                  "- You may congratulate them on it" + (" (within 6 months)." if f.get("recent") else
                                                         " - it's 6-12 months old, so only if it reads naturally."),
                  "  State the round and month exactly as above."]
    else:
        lines += ["", "## Funding signal", "", "- None sourced: don't mention funding."]
    lines += ["", "## Rules for this draft", "",
              f"- Greeting exactly: `{inputs.greeting}`",
              f"- resume_action: **{draft.resume_action}** ({draft.resume_reason})",
              f"- Subject: " + (f"general inbox - must read as an application, e.g. `{application_subject(inputs, facts)}`"
                                if inputs.routable_inbox else "conversational (role + who I am, or the intern question)"),
              f"- Time-zone line: {'required (remote / multi-country)' if inputs.remote_global else 'not needed'}",
              f"- Best-matching project: {draft.project or '-'} ({draft.project_reason})",
              f"- {meta.get('min_words', 120)}-{meta.get('max_words', 160)} words between the greeting and the sign-off",
              "- End with exactly:", "", "```", SIGN_OFF, *facts.signature(), "```", "",
              "## Candidate facts (the only facts you may use)", ""]
    lines += [f"- {ln}" for ln in facts.listing()]
    lines += ["", "## Style guide", "", STYLE_GUIDE.split("\n", 2)[2].strip(), "",
              "## Template draft (reference - replace it)", "", "```", f"Subject: {draft.subject}", "", "Body:",
              draft.body, "```", "", "## Output format", "", "```", WRITTEN_FORMAT, "```", ""]
    return "\n".join(lines)
