"""Filtering and scoring: decide whether an opening fits, how well, and what to do about it.

Score (0-100) = family_weight x (skills 0-50 + location 0-20 + track 0-20 + experience 0-10
                                 + startup bonus), capped at 100

Every decision appends a human-readable reason, so `applyflow explain <job>` can
show exactly why something was kept, tiered or rejected. That's what makes the
filters tunable on real data.
"""

from __future__ import annotations

import re
from datetime import date

from applyflow.config import Profile, RoleFamily
from applyflow.models import Job, ScoreResult
from applyflow.text import contains_phrase, matching_phrases

# ---------------------------------------------------------------------------
# Role family and title filters
# ---------------------------------------------------------------------------

_ROLE_SPLIT = re.compile(r"\s*(?:;|,|/|&|\band\b|\+)\s*", re.I)
# "Product Engineer (Product Manager role, not a Full-stack role)": the parenthetical
# says what the role really is, and "not a ... role" must not count as a role.
_CLARIFIED_ROLE = re.compile(r"[^,;/|()]*\(\s*([^(),;]+?)\s+(?:role|position)\b[^)]*\)", re.I)
_NEGATED_ROLE = re.compile(r"\bnot\s+(?:an?\s+)?[^,;()]*?\b(?:role|position)\b", re.I)


def hn_role_text(title: str) -> str:
    """HN header roles with clarifications applied and negated roles removed."""
    return _NEGATED_ROLE.sub("", _CLARIFIED_ROLE.sub(r"\1", title))


def _excluded_by(title: str, profile: Profile) -> str | None:
    for phrase in profile.title_exclude:
        if contains_phrase(title, phrase):
            return phrase
    return None


def title_exclusion(job: Job, profile: Profile) -> str | None:
    """Return the excluded phrase that rules this job out, or None.

    HN titles often list several roles ("Senior Backend Engineer, Interns"), so
    an HN post is only excluded when *every* listed role is excluded.
    """
    if job.source == "hn":
        roles = [r for r in _ROLE_SPLIT.split(hn_role_text(job.title)) if r.strip()]
        if not roles:
            return None
        hits = [_excluded_by(r, profile) for r in roles]
        return hits[0] if all(hits) else None
    return _excluded_by(job.title, profile)


def classify_family(job: Job, profile: Profile) -> RoleFamily | None:
    title = hn_role_text(job.title) if job.source == "hn" else job.title
    for family in profile.role_families:
        if any(contains_phrase(title, p) for p in family.title_patterns):
            return family
    # HN headers are free-form; fall back to the opening of the post.
    if job.source == "hn":
        head = job.description[:400]
        for family in profile.role_families:
            if any(contains_phrase(head, p) for p in family.title_patterns):
                return family
    return None


# ---------------------------------------------------------------------------
# Track: internship / new grad / unspecified
# ---------------------------------------------------------------------------

_INTERN_RE = re.compile(r"\b(intern|interns|internship|internships|co-?op)\b", re.I)
_NEW_GRAD_TITLE_RE = re.compile(
    r"\b(new grad(uate)?s?|graduate|university|campus|fresher|freshers|entry[\s-]level|early[\s-]career|"
    r"junior|jr|associate software engineer|trainee|apprentice|"
    r"sde[\s-]?(1|i)|engineer[\s-]?(1|i)|software engineer[\s-]?(1|i)|(class of )?20(26|27))\b",
    re.I,
)
_NEW_GRAD_DESC_RE = re.compile(
    r"\b(new grad(uate)?s?|recent (college )?graduates?|fresh graduates?|freshers?|entry[\s-]level|"
    r"early[\s-]career|graduating (in )?(20)?2[67]|20(26|27) (batch|graduates?|grads?|pass[\s-]?outs?)|"
    r"0\s*(-|–|to)\s*[12]\s*years?)\b",
    re.I,
)


def detect_track(job: Job) -> str:
    head = f"{job.title} | {job.employment_type}"
    if _INTERN_RE.search(head) or job.employment_type.lower() == "intern":
        return "internship"
    if _NEW_GRAD_TITLE_RE.search(job.title):
        return "new_grad"
    if job.source == "hn" and _INTERN_RE.search(job.description[:600]):
        return "internship"
    if _NEW_GRAD_DESC_RE.search(job.description):
        return "new_grad"
    return "unspecified"


# ---------------------------------------------------------------------------
# Experience required
# ---------------------------------------------------------------------------

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
_YEARS_RE = re.compile(
    rf"\b{_NUM}\s*\+?\s*(?:(?:-|–|to)\s*{_NUM}\s*\+?\s*)?(?:years?|yrs?)\b(?:\s+of)?",
    re.I,
)


def _to_int(token: str) -> int:
    return int(token) if token.isdigit() else _WORD_NUMBERS[token.lower()]


def required_years(description: str) -> int | None:
    """Largest minimum-years-of-experience stated near the word 'experience'.

    "3+ years of experience" -> 3, "0-1 years experience" -> 0. Numbers that
    aren't about experience ("founded 10 years ago") are ignored because we
    require 'experience' within a short window.
    """
    found: list[int] = []
    for match in _YEARS_RE.finditer(description):
        window = description[max(0, match.start() - 60): match.end() + 60].lower()
        if "experience" not in window and "exp." not in window:
            continue
        low = _to_int(match.group(1))
        if low <= 15:
            found.append(low)
    return max(found) if found else None


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def posting_age_days(job: Job, today: date | None = None) -> int | None:
    """Days since the board (re)published the posting, or None when the date is unknown."""
    try:
        posted = date.fromisoformat(job.posted_at[:10])
    except ValueError:
        return None
    return ((today or date.today()) - posted).days


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

LOCATION_RANK = [
    "preferred_india",
    "remote_open",
    "other_india",
    "remote_unclear",
    "abroad_onsite",
    "remote_restricted",
    "unknown",
]


def _classify_location_string(text: str, profile: Profile) -> str:
    loc = profile.locations
    lowered = text.lower()
    if any(contains_phrase(lowered, m) for m in loc.get("preferred_india", [])):
        return "preferred_india"
    if re.search(r"\b(remote|anywhere|distributed|wfh|work from home)\b", lowered):
        if any(contains_phrase(lowered, m) for m in loc.get("remote_open_markers", [])):
            return "remote_open"
        if any(contains_phrase(lowered, m) for m in loc.get("remote_restricted_markers", [])):
            return "remote_restricted"
        return "remote_unclear"
    if any(contains_phrase(lowered, m) for m in loc.get("other_india", [])):
        return "other_india"
    return "abroad_onsite"


def classify_location(job: Job, profile: Profile) -> str:
    texts = list(job.locations)
    if job.source == "hn":
        # The HN header mixes locations with other notes; read the whole first line.
        texts = [job.description.split("\n", 1)[0]]
    classes = [_classify_location_string(t, profile) for t in texts if t.strip()]
    if job.remote and not any(c.startswith("remote") for c in classes):
        # A remote flag next to only foreign places ("Canada | London | United States")
        # means remote within those countries, not open to India.
        if classes and all(c == "abroad_onsite" for c in classes):
            return "remote_restricted"
        classes.append("remote_unclear")
    if not classes:
        return "unknown"
    return min(classes, key=LOCATION_RANK.index)


# ---------------------------------------------------------------------------
# Visa sponsorship
# ---------------------------------------------------------------------------

_SPONSOR_NEGATIVE = re.compile(
    r"(no|not|unable to|cannot|can't|can not|will not|won't|do not|don't|does not|doesn't)\s+"
    r"(be able to\s+)?(provide|offer|support)?\s*(visa\s+|immigration\s+)?sponsor"
    r"|without\s+(the\s+need\s+for\s+|requiring\s+)?(visa\s+)?sponsorship"
    r"|must\s+(be|have)\s+(legally\s+)?(authori[sz]ed|eligible|the right)\s+to\s+work"
    r"|work\s+authori[sz]ation\s+(is\s+)?required"
    r"|sponsorship\s+(is\s+)?not\s+(available|provided|offered)",
    re.I,
)
_SPONSOR_POSITIVE = re.compile(
    r"visa\s+sponsorship\s+(is\s+)?(available|provided|offered)"
    r"|(we|will|can|happy to|able to)\s+(will\s+|can\s+)?sponsor"
    r"|sponsor(ing)?\s+(work\s+)?visas?"
    r"|relocation\s+(support|assistance|package|bonus|help)"
    r"|(we|will)\s+(help\s+you\s+)?relocate"
    r"|\bvisa\b[^.\n]{0,40}\b(support|sponsor)",
    re.I,
)


# Roles open only to citizens / cleared people can't sponsor anyone, whatever the
# benefits boilerplate says about relocation (Palantir's US-government roles).
_US_ONLY = re.compile(
    r"\bU\.?S\.?\s+(citizen(ship)?s?|persons?)\b"
    r"|\b(United States|American)\s+citizen(ship)?s?\b"
    r"|\bsecurity\s+clearances?\b"
    r"|\b(eligible|eligibility)\s+(for|to\s+obtain)\s+(a\s+)?(US\s+)?(security\s+)?clearance\b"
    r"|\b(active|top\s+secret|TS/SCI|secret)\s+clearance\b",
    re.I,
)
_ITAR = re.compile(r"\bITAR\b")   # case-sensitive: "military" contains "itar"


def sponsorship_signal(text: str) -> tuple[str, str]:
    """Return (positive | negative | unknown, the phrase that decided it)."""
    for pattern in (_US_ONLY, _ITAR, _SPONSOR_NEGATIVE):
        m = pattern.search(text)
        if m:
            return "negative", m.group(0)
    m = _SPONSOR_POSITIVE.search(text)
    if m:
        return "positive", m.group(0)
    return "unknown", ""


def detect_sponsorship(text: str) -> str:
    return sponsorship_signal(text)[0]


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def skill_match(job: Job, profile: Profile, ignore_negatives: bool = False) -> tuple[float, list[str], list[str]]:
    """Return (fraction 0-1, matched positive skills, matched negative skills)."""
    text = f"{job.title}\n{job.description}"
    matched = matching_phrases(text, list(profile.skills))
    positives = [s for s in matched if profile.skills[s] > 0]
    negatives = [s for s in matched if profile.skills[s] < 0]
    raw = sum(profile.skills[s] for s in (positives if ignore_negatives else matched))
    fraction = max(0.0, min(1.0, raw / max(profile.skill_score_cap, 1)))
    return fraction, positives, negatives


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

LOCATION_POINTS = {
    "preferred_india": 20,
    "remote_open": 18,
    "other_india": 12,
    "remote_unclear": 10,
    "abroad_onsite": 12,      # only reached when sponsorship is signalled
    "unknown": 8,
}


def _reject(job: Job, reason: str, **fields) -> ScoreResult:
    return ScoreResult(job_id=job.id, accepted=False, score=0, tier="rejected", reasons=[reason], **fields)


def is_startup(job: Job, profile: Profile) -> bool:
    cfg = profile.startup
    if job.team_size is not None:
        return job.team_size <= int(cfg["max_team_size"])
    return job.board_size is not None and job.board_size <= int(cfg["small_board_job_count"])


def contact_hint(job: Job, profile: Profile) -> str:
    """Who Phase 2 should look for at this company."""
    cfg = profile.startup
    if job.source == "hn" and job.emails:
        return "the email in the post"
    small = (
        job.team_size is not None and job.team_size <= int(cfg["founder_contact_max_team_size"])
    ) or (job.team_size is None and job.board_size is not None
          and job.board_size <= int(cfg["small_board_job_count"]))
    if small:
        return "founder or CTO"
    if job.team_size is not None and job.team_size <= int(cfg["max_team_size"]):
        return "engineering manager or tech lead"
    return "recruiter or engineering manager"


def decide_action(job: Job, location_class: str, profile: Profile) -> str:
    if job.source == "hn":
        return "email" if job.emails else "apply"
    if location_class == "abroad_onsite":
        return "apply"          # sponsored relocation runs through formal hiring
    if job.board_size is not None and job.board_size > profile.large_company_job_count:
        return "apply"
    return "apply_plus_email"


def normalized_title(job: Job) -> str:
    """Title with the posting's own city words removed: 'Backend Engineer - Pune' -> 'backend engineer'."""
    title = job.title.lower()
    for loc in job.locations:
        city = loc.split(",")[0].strip().lower()
        if city:
            title = re.sub(rf"(?<![a-z0-9]){re.escape(city)}(?![a-z0-9])", " ", title)
    return " ".join(re.findall(r"[a-z0-9+#]+", title))


def merge_duplicates(jobs: list[Job], results: list[ScoreResult]) -> None:
    """One row per (company, title): the best-scoring posting keeps every location.

    Big companies post the same role once per city. The others are marked rejected
    with a reason pointing at the posting that was kept.
    """
    groups: dict[tuple[str, str], list[tuple[Job, ScoreResult]]] = {}
    for job, result in zip(jobs, results):
        if result.tier != "rejected":
            groups.setdefault((job.company.lower(), normalized_title(job)), []).append((job, result))
    for pairs in groups.values():
        if len(pairs) < 2:
            continue
        pairs.sort(key=lambda p: (-p[1].score, LOCATION_RANK.index(p[1].location_class or "unknown"), p[0].id))
        best_job, best = pairs[0]
        for job, result in pairs[1:]:
            for loc in job.locations:
                if loc not in best.merged_locations:
                    best.merged_locations.append(loc)
            result.accepted, result.tier = False, "rejected"
            result.reasons.insert(0, f"duplicate posting (same company + title; kept {best_job.id})")
        best.reasons.append(f"merged {len(pairs) - 1} duplicate posting(s) for other locations")


def score_job(job: Job, profile: Profile, today: date | None = None) -> ScoreResult:
    excluded = title_exclusion(job, profile)
    if excluded:
        return _reject(job, f"title contains excluded term '{excluded}'")

    family = classify_family(job, profile)
    if family is None:
        return _reject(job, "title doesn't match any target role family")

    track = detect_track(job)
    if not profile.tracks.get(track, False):
        return _reject(job, f"track '{track}' is turned off in the profile", family=family.name, track=track)

    years = required_years(job.description)
    if years is not None and years > profile.max_years_required:
        return _reject(
            job,
            f"asks for {years}+ years of experience (max {profile.max_years_required})",
            family=family.name,
            track=track,
            experience_years=years,
        )

    fresh = profile.freshness
    age = posting_age_days(job, today)
    if age is not None and age > int(fresh["max_age_days"]):
        return _reject(
            job,
            f"posting is {age} days old (posted {job.posted_at[:10]}, max {fresh['max_age_days']})",
            family=family.name,
            track=track,
            experience_years=years,
        )

    location_class = classify_location(job, profile)
    sponsorship, sponsor_phrase = sponsorship_signal(job.description)
    sponsor_phrase = " ".join(sponsor_phrase.split())
    common = dict(family=family.name, track=track, experience_years=years,
                  location_class=location_class, sponsorship=sponsorship)
    if location_class == "remote_restricted":
        return _reject(job, f"remote but restricted to another region ({job.location_text})", **common)
    if location_class == "other_india" and not profile.locations.get("accept_other_india", True):
        return _reject(job, f"Indian city outside your preferred list ({job.location_text})", **common)
    if location_class == "abroad_onsite" and profile.locations.get("abroad_requires_sponsorship", True):
        if sponsorship != "positive":
            detail = (f"says no sponsorship ('{sponsor_phrase}')" if sponsorship == "negative"
                      else "no sponsorship signal")
            return _reject(job, f"onsite abroad ({job.location_text}) and {detail}", **common)

    reasons: list[str] = [f"role family: {family.name} ({family.tier})"]

    # Internships and new-grad roles train you; a stack listed in their JD isn't a gap.
    fresher = track in ("internship", "new_grad")
    fraction, positives, negatives = skill_match(job, profile, ignore_negatives=fresher)
    skill_points = 50 * fraction
    reasons.append(f"skills: {round(skill_points)}/50 from {', '.join(positives[:12]) or 'none'}")
    if negatives and fresher:
        reasons.append(f"skill gaps ignored for {track} roles: {', '.join(negatives)}")
    elif negatives:
        reasons.append(f"skill gaps pulling it down: {', '.join(negatives)}")

    location_points = LOCATION_POINTS[location_class]
    reasons.append(f"location: {location_class} -> {location_points}/20 ({job.location_text or 'not stated'})")

    track_points = 20 if track in ("internship", "new_grad") else 8
    reasons.append(f"track: {track} -> {track_points}/20")

    if years is None and fresher:
        experience_points = 10
        reasons.append(f"experience: not stated, expected for {track} roles -> 10/10")
    elif years is None:
        experience_points = 4
        reasons.append("experience: not stated -> 4/10 (could be secretly mid-level)")
    else:
        experience_points = 10
        reasons.append(f"experience: {years} year(s) asked -> 10/10")

    startup_points = 0
    if is_startup(job, profile):
        startup_points = int(profile.startup["score_bonus"])
        size = f"{job.team_size} people" if job.team_size is not None else f"{job.board_size} open roles"
        reasons.append(f"startup ({size}) -> +{startup_points}")

    stale_points = 0
    if age is not None and age > int(fresh["stale_days"]):
        stale_points = int(fresh["stale_penalty"])
        reasons.append(f"stale: posted {age} days ago ({job.posted_at[:10]}) -> -{stale_points}")

    weight = profile.family_tier_weights[family.tier]
    total = (skill_points + location_points + track_points + experience_points + startup_points
             - stale_points) * weight
    score = int(round(max(0.0, min(100.0, total))))
    if weight != 1.0:
        reasons.append(f"{family.tier} family weight x{weight}")
    if sponsorship == "positive":
        reasons.append(f"posting signals visa sponsorship / relocation support ('{sponsor_phrase}')")

    if score >= profile.tiers["A"] and track in profile.a_tier_tracks:
        tier = "A"
    elif score >= profile.tiers["A"]:
        tier = "B"
        reasons.append(f"capped at tier B: track '{track}' could be secretly mid-level "
                       f"(tier A is for {', '.join(profile.a_tier_tracks)})")
    elif score >= profile.tiers["B"]:
        tier = "B"
    else:
        tier = "skip"

    return ScoreResult(
        job_id=job.id,
        merged_locations=list(job.locations),
        accepted=tier in ("A", "B"),
        score=score,
        tier=tier,
        action=decide_action(job, location_class, profile),
        contact_hint=contact_hint(job, profile),
        lead_project=family.lead_project,
        matched_skills=positives,
        reasons=reasons,
        **common,
    )
