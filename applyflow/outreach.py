"""Phase 2b: which draft goes to whom, in what order, and what was sent.

ApplyFlow never sends anything. `queue` picks a day's batch; you send each email yourself
(the report's Open-in-Gmail button) and record it with `mark-sent`. `mark-bounced` moves a
target on to its next address: a guessed address's alternates first, then the backup contacts
(`--stop` records the bounce and puts the rest of the target's addresses on hold instead).

Send tiers, by how likely the address is to arrive:
  1 = a verified address, or a recruiter / HR contact (a published hiring inbox)
  2 = a published inbox (or a pattern-confirmed / fallback address)
  3 = a guessed address - gated by `outreach.tier3` (see tier3_gate): unlocked 2 days after the
      first mark-sent with zero bounces, 5 a day, then 10 a day, paused at a 5% bounce rate

Phase 2e: info@ / contact@ addresses are never sent to (status "unsendable" when nothing else is left),
hello@ / hi@ / team@ are capped at `outreach.general_inbox_per_day` a day, and two targets for one company
never both go out (the second is a "duplicate").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from applyflow.config import Profile
from applyflow.contacts import (FOUNDER_INBOXES, FREEMAIL, HIRING_INBOXES, DoNotContact, _local_base,
                                classify_address, inbox_preference, manual_result, manual_target, name_from_local,
                                needs_manual_check, role_group, search_links, shared_inbox)
from applyflow.detect import normalize_website
from applyflow.discovery import name_key, site_domain
from applyflow.drafting import (HR_GROUPS, Draft, DraftInputs, Facts, check_written, display_company, gmail_url,
                                load_facts, parse_written, render_packet, STYLE_GUIDE, tech_terms, template_draft)
from applyflow.http import FetchError
from applyflow.scoring import MARKETPLACE_HINT
from applyflow.text import page_summary

Log = Callable[[str], None]

STATUS_ORDER = {"ready": 0, "sent": 1, "exhausted": 2, "stopped": 2, "do_not_contact": 3, "no_contact": 4, "apply_only": 5,
                "no_domain": 6, "dropped": 7, "unsendable": 8, "duplicate": 9}
STATUS_LABELS = {"ready": "ready to send", "sent": "sent", "exhausted": "every address bounced",
                 "stopped": "bounced; its other addresses are on hold",
                 "do_not_contact": "on the do-not-contact list", "no_contact": "no contact found",
                 "apply_only": "apply via the posting", "no_domain": "no website domain",
                 "dropped": "dropped from the pool (2c.1 rules)",
                "unsendable": "only info@ / contact@ or legal-page addresses (not sent to, 2e)",
                "duplicate": "another target for the same company is sent, queued or ahead of it"}
KIND_ORDER = {"role": 0, "manual": 1, "general_outreach": 2}


def target_id(company_key: str) -> str:
    """'yc:clickpe' -> 'yc-clickpe' (used for packet and written-draft file names)."""
    return re.sub(r"[^a-z0-9]+", "-", company_key.lower()).strip("-")


def contact_group(name: str, role: str, email: str, source: str) -> str:
    if source == "posting":
        return "posting"
    if name:
        return role_group(role or "")
    kind = classify_address(email, small=True)
    return kind[0] if kind else "other"


UNSENDABLE_INBOXES = {"info", "contact"}            # 2e: never sent to
GENERAL_INBOXES = {"hello", "hi", "team"}           # 2e: capped per day
SAFE_KINDS = ("verified_person", "founders", "hiring_inbox")
KIND_LABELS = {"verified_person": "verified named person", "founders": "founders@", "hiring_inbox": "hiring inbox",
               "general_inbox": "hello@ / hi@ / team@", "unsendable": "info@ / contact@ / legal page (unsendable)",
               "guessed": "guessed", "other": "other published address"}


_LEGAL_PAGE = re.compile(r"privacy|terms|legal|policy|polic(?:y|ies)|grievance|refund|cookie|disclaimer|"
                         r"account-deletion|compliance", re.I)


def legal_page_person(email: str, source_url: str, name: str = "", source: str = "website") -> bool:
    """2e: a personal address seen only on a privacy / terms / legal page, tied to no named person: usually a
    grievance or privacy officer, not someone who hires."""
    from urllib.parse import urlparse
    return (source == "website" and not name and not shared_inbox(email)
            and bool(_LEGAL_PAGE.search(urlparse(source_url or "").path)))


def contact_kind(email: str, confidence: str, source_url: str = "", name: str = "", source: str = "") -> str:
    """2e: verified_person | founders | hiring_inbox (the safe kinds) | general_inbox | unsendable | guessed | other."""
    base = _local_base(email)
    if base in UNSENDABLE_INBOXES or (source_url and legal_page_person(email, source_url, name, source)):
        return "unsendable"
    if confidence == "guessed":
        return "guessed"
    if base in FOUNDER_INBOXES:
        return "founders"
    if base in HIRING_INBOXES:
        return "hiring_inbox"
    if base in GENERAL_INBOXES:
        return "general_inbox"
    if confidence == "verified" and not shared_inbox(email) and not _ROLE_LOCAL.search(email.split("@", 1)[0]):
        return "verified_person"
    return "other"


# A function, not a person: "customer-success@", "partner.institute@", "sales.team@" (2e).
_ROLE_LOCAL = re.compile(r"(?:^|[._-])(?:customer|success|partner|partners|institute|sales|support|admin|ops|"
                         r"operations|marketing|accounts?|enquir(?:y|ies)|business|bd|media|press|events?|"
                         r"service|services|office|desk|community)(?:$|[._-])", re.I)


def send_tier(confidence: str, group: str) -> int | None:
    if confidence == "verified" or (group in HR_GROUPS and confidence in ("strong", "published_inbox", "fallback")):
        return 1
    if confidence in ("strong", "published_inbox", "fallback"):
        return 2
    if confidence == "guessed":
        return 3
    return None


@dataclass
class Address:
    email: str
    name: str = ""
    role: str = ""
    confidence: str = ""
    group: str = "other"
    source: str = ""
    source_url: str = ""
    mx: str = ""
    note: str = ""                 # "alternate for <name>" / "backup contact"
    proof: str = ""                # company site | posting | manual | github profile | demoted: ...

    @property
    def tier(self) -> int | None:
        return send_tier(self.confidence, self.group)

    @property
    def kind(self) -> str:
        return contact_kind(self.email, self.confidence, self.source_url, self.name, self.source)

    def label(self) -> str:
        who = f"{self.name} ({self.role})" if self.name else self.role
        extra = f", {self.note}" if self.note else ""
        return f"{self.email} - {who} [{self.confidence}{extra}]"


def address_sequence(contacts) -> list[Address]:
    """Every address to try, in order: each stored contact, followed by its alternates (guesses)."""
    seq: list[Address] = []
    for i, c in enumerate(contacts):
        group = contact_group(c["name"] or "", c["role"] or "", c["email"], c["source"] or "")
        base = Address(email=c["email"], name=c["name"] or "", role=c["role"] or "", confidence=c["confidence"],
                       group=group, source=c["source"] or "", source_url=c["source_url"] or "", mx=c["mx"] or "",
                       note="" if i == 0 else "backup contact",
                       proof=(c["proof"] if "proof" in c.keys() else "") or "")
        seq.append(base)
        for alt in json.loads(c["alternates"] or "[]"):
            seq.append(Address(email=alt, name=base.name, role=base.role, confidence="guessed", group=group,
                               source="pattern", source_url=base.source_url, mx=base.mx,
                               note=f"alternate for {base.name or base.email}"))
    seen, unique = set(), []
    for a in seq:
        if a.email not in seen:
            seen.add(a.email)
            unique.append(a)
    return unique


@dataclass
class OutreachItem:
    target_id: str
    company_key: str
    company: str
    kind: str
    status: str
    inputs: DraftInputs
    draft: Draft
    address: Address | None = None
    backups: list[Address] = field(default_factory=list)
    bounced: list[str] = field(default_factory=list)
    sent_email: str = ""
    sent_date: str = ""
    send_tier: int | None = None
    why: str = ""
    status_note: str = ""
    best_score: int | None = None
    team_size: int | None = None
    domain: str = ""
    priority: int = 0
    manual_links: list[tuple[str, str]] = field(default_factory=list)
    hl_score: int | None = None                # hiring likelihood (Phase 2c pool), when the company is in it
    hl_reasons: list[str] = field(default_factory=list)
    queued_on: str = ""                        # an earlier stored batch holds it, not yet marked sent
    protected: bool = False                    # queued on any day, sent / bounced, or held to an older rule set

    @property
    def inbox_pref(self) -> int:
        return inbox_preference(self.address.email) if self.address else 9

    @property
    def bounce_rank(self) -> int:
        """Tiers 1-2, lowest bounce risk first (2d): 0 a verified named person (stored name or a first.last address), 1 a hiring inbox (careers@, jobs@,
        talent@, hr@, work@ ...), 2 founders@, 3 hello@ / hi@ / team@, 4 any other address, 5 info@ / contact@."""
        a = self.address
        if a is None:
            return 9
        base = _local_base(a.email)
        named = a.name or name_from_local(a.email)          # a first.last address names its owner
        if a.confidence == "verified" and named and not shared_inbox(a.email):
            return 0
        if base in HIRING_INBOXES:
            return 1
        if base in FOUNDER_INBOXES:
            return 2
        return {"hello": 3, "hi": 3, "team": 3, "info": 5, "contact": 5}.get(base, 4)

    @property
    def gmail(self) -> str:
        to = self.sent_email or (self.address.email if self.address else "")
        return gmail_url(to, self.draft.subject, self.draft.body) if to else ""

    @property
    def company_id(self) -> str:
        """One email per company: two targets for the same company (manual + discovered) collide here."""
        if self.domain and self.domain not in FREEMAIL:
            return self.domain
        return name_key(self.company) or self.company_key


# ---------------------------------------------------------------------------
# Building the items
# ---------------------------------------------------------------------------

def _role_rows(store) -> dict[str, list]:
    rows: dict[str, list] = {}
    for row in store.role_rows():
        if row["contact_hint"] == MARKETPLACE_HINT:
            continue
        rows.setdefault(name_key(row["company"]), []).append(row)
    return rows


def _remote_global(locations: str) -> bool:
    """A team that's remote or spans countries: say you can work across time zones."""
    parts = [p.strip().lower() for p in (locations or "").split(";") if p.strip()]
    return any(p == "remote" or "india" not in p for p in parts) if parts else False


def _funding_inputs(pool_row, today: date) -> dict[str, str]:
    """The latest sourced round within 12 months, as draft inputs ('recent' = within 6 months)."""
    if pool_row is None:
        return {}
    from applyflow.pool import funding_signal
    from applyflow.sources import PoolCompany, months_ago
    c = PoolCompany.from_json(json.loads(pool_row["data"]))
    f = funding_signal(c, today, 12)
    if f is None:
        return {}
    return {"round": f.round, "amount": f.amount, "date": f.date, "source_url": f.source_url,
            "recent": "yes" if (months_ago(f.date, today) or 0) <= 6 else ""}


def build_items(store, profile: Profile, dnc: DoNotContact | None = None, facts: Facts | None = None,
                day: date | None = None) -> list[OutreachItem]:
    """Every outreach target with its current address and draft, in priority order."""
    day = day or today()
    facts = facts or load_facts(profile)
    dnc = dnc or DoNotContact()
    cfg = profile.outreach
    lo, hi = (profile.discovery.get("yc") or {}).get("preferred_team_size", [10, 300])
    terms = tech_terms(profile)
    roles = _role_rows(store)
    written = store.written_drafts()
    locked = store.draft_rules()
    pool_keys = {r["key"]: r for r in store.pool_rows()}
    matches = store.role_matches()
    excluded = store.excluded_contacts()
    match_jobs = {m["job_id"]: m["source"] for ms in matches.values() for m in ms if m["job_id"]}
    pool_domains = store.pool_by_domain()
    protected = store.protected_target_ids()
    from applyflow.pool import never_cold_email
    events: dict[str, list] = {}
    for e in store.events():
        events.setdefault(e["target_id"], []).append(e)
    items: list[OutreachItem] = []
    for check in store.contact_checks():
        key = check["company_key"]
        tid = target_id(key)
        disc = store.discovered_company(key)
        manual = store.get_manual_target(key) if check["kind"] == "manual" else None
        ctx = store.get_context(key)
        kind = check["kind"]
        role_rows = roles.get(name_key(check["company"]), [])
        match = next((m for m in matches.get(key, []) if m["match"] == "yes"), None)
        found_2e = [r for r in role_rows if r["id"] in match_jobs]
        if kind != "role" and tid not in protected and (found_2e or (match and match["title"])):
            kind = "role"                  # 2e: an opening found since the lookup (HN archive, YC jobs, careers page)
            role_rows = found_2e
        inputs = DraftInputs(target_id=tid, company_key=key, company=display_company(check["company"]), kind=kind,
                             team_size=check["team_size"], rules=locked.get(tid, "2d"))
        prow = pool_keys.get(key) or pool_domains.get(check["domain"] or "")
        if prow is not None and not disc:          # Phase 2c pool company (portfolio, funding, list)
            pdata = json.loads(prow["data"])
            inputs.one_liner, inputs.batch = pdata.get("description") or "", pdata.get("batch") or ""
            inputs.locations, inputs.website = pdata.get("locations") or "", pdata.get("website") or ""
            inputs.remote_global = prow["region"] == "remote_global"
        if prow is not None:
            if inputs.rules != "2b" and kind != "role":
                inputs.company = display_company(prow["name"])      # the pool's (merged) spelling
            inputs.funding = _funding_inputs(prow, day)
            inputs.hl_score = prow["score"]
        if disc:
            inputs.one_liner, inputs.batch = disc["description"] or "", disc["batch"] or ""
            inputs.locations, inputs.website = disc["locations"] or "", disc["website"] or ""
            inputs.remote_global = _remote_global(inputs.locations)
        if manual:
            inputs.note, inputs.website = manual["note"] or "", manual["website"] or ""
        if ctx and ctx["status"] == "ok":
            inputs.homepage_title, inputs.meta_description = ctx["homepage_title"] or "", ctx["meta_description"] or ""
            inputs.homepage_snippet = ctx["snippet"] or ""
            inputs.website = inputs.website or ctx["website"] or ""
        if not inputs.website and check["domain"]:
            inputs.website = f"https://{check['domain']}"
        role_rows = role_rows if kind == "role" else []
        if role_rows:
            r = role_rows[0]
            inputs.role_title, inputs.role_score, inputs.role_tier = r["title"], r["score"], r["tier"]
            inputs.role_track, inputs.role_family, inputs.role_url = r["track"] or "", r["family"] or "", r["url"] or ""
            inputs.role_location = r["location_class"] or ""
            inputs.role_skills = json.loads(r["matched_skills"] or "[]")
            inputs.role_text, inputs.lead_project = r["description"] or "", r["lead_project"] or ""
            inputs.remote_global = inputs.role_location == "remote_open"
            inputs.role_where = ROLE_WHERE.get(match_jobs.get(r["id"], ""), "")
        elif kind == "role" and match is not None and match["title"]:      # 2e: a careers page / board / manual role
            inputs.role_title, inputs.role_url = match["title"], match["url"] or ""
            inputs.role_where = ROLE_WHERE.get(match["source"], "")
        elif kind == "role":                     # the opening closed or dropped below B since the lookup
            roles_then = json.loads(check["roles"] or "[]")
            inputs.role_title = re.sub(r"\s*\(\d+\)$", "", roles_then[0]) if roles_then else "Software Engineer"

        seq = address_sequence(store.contacts_for(key))
        evs = events.get(tid, [])
        bounced = [e["email"] for e in evs if e["event"] == "bounced"]
        sent = [e for e in evs if e["event"] == "sent" and e["email"] not in bounced]
        stopped = [e for e in evs if e["event"] == "stopped"]
        remaining = [a for a in seq if a.email not in bounced]
        if tid not in protected:           # 2e: info@ / contact@, legal-page and reviewed-out addresses are never sent to
            remaining = [a for a in remaining if a.kind != "unsendable" and not a.proof.startswith("excluded:")
                         and a.email not in excluded]
        item = OutreachItem(target_id=tid, company_key=key, company=inputs.company, kind=kind, status="ready",
                            inputs=inputs, draft=None, bounced=bounced, best_score=check["best_score"],
                            team_size=check["team_size"], domain=check["domain"] or "",
                            hl_score=prow["score"] if prow is not None else None,
                            hl_reasons=json.loads(prow["reasons"] or "[]") if prow is not None else [])
        blocked = next((b for b in (dnc.blocks(a.email) for a in seq) if b), "")
        # 2c.1: a pool target the current rules dropped (large, below the tech-fit floor) or a never_cold_email
        # company is not emailed - unless it's already queued / sent / locked (protected).
        hold = ""
        if tid not in protected:
            listed = never_cold_email(check["company"], check["domain"] or "", profile)
            if listed:
                hold = f"apply_only:on the never_cold_email list ({listed})"
            elif prow is not None and not prow["selected"] and kind == "general_outreach":
                why = prow["why_selected"] or "not selected"
                hold = ("apply_only:" if "large" in why else "dropped:") + why
        if sent:
            item.status, item.sent_email, item.sent_date = "sent", sent[-1]["email"], sent[-1]["event_date"]
            item.address = next((a for a in seq if a.email == item.sent_email), Address(email=item.sent_email))
        elif stopped:
            item.status, item.status_note = "stopped", f"bounced {stopped[-1]['event_date']}; not moved on to "                                                        f"its alternates or backups"
        elif hold:
            item.status, item.status_note = hold.split(":", 1)
        elif not seq:
            item.status = check["status"] if check["status"] in ("apply_only", "no_domain") else "no_contact"
        elif blocked:
            item.status, item.status_note = "do_not_contact", f"matches {blocked}"
        elif not remaining and not bounced:
            item.status, item.status_note = "unsendable", ", ".join(a.email for a in seq)
        elif not remaining:
            item.status = "exhausted"
        else:
            item.address, item.backups = remaining[0], remaining[1:]
        if item.address:
            a = item.address
            name = a.name or (name_from_local(a.email) if inputs.rules != "2b" else "")
            inputs.to_email, inputs.contact_name, inputs.contact_role = a.email, name, a.role
            inputs.confidence, inputs.contact_group = a.confidence, a.group
            item.send_tier = a.tier
        item.why = _why(check, inputs, kind)
        item.manual_links = _links(item)
        item.draft = _draft(inputs, facts, written.get(tid), cfg, terms)
        item.protected = tid in protected
        items.append(item)

    def priority(it: OutreachItem) -> tuple:
        """Tiers 1-2 together, lowest bounce risk first (bounce_rank, 2d); tier 3 after them. Then role targets,
        hiring likelihood and inbox preference (2c)."""
        size = it.team_size
        low_risk = it.send_tier in (1, 2)
        return (STATUS_ORDER.get(it.status, 9), 0 if low_risk else it.send_tier or 9,
                it.bounce_rank if low_risk else 0, KIND_ORDER.get(it.kind, 3),
                -(it.hl_score if it.hl_score is not None else -1), it.inbox_pref, -(it.best_score or 0),
                0 if size is not None and lo <= size <= hi else 1, size if size is not None else 10**6,
                it.company.lower())
    items.sort(key=priority)
    mark_duplicates(items)
    items.sort(key=priority)
    for i, it in enumerate(items, start=1):
        it.priority = i
    return items


def mark_duplicates(items: list[OutreachItem]) -> None:
    """2e: one company is emailed once. A ready target whose company (domain, else name) already has a sent,
    stopped, exhausted or protected target - or a ready one ahead of it in priority order - becomes a duplicate.
    Queued / sent / locked targets are never changed."""
    claimed: dict[str, str] = {}
    for it in items:
        if it.protected or it.status in ("sent", "stopped", "exhausted"):
            claimed.setdefault(it.company_id, it.target_id)
    for it in items:                       # already in priority order
        if it.status != "ready" or it.protected:
            continue
        other = claimed.get(it.company_id)
        if other and other != it.target_id:
            it.status, it.status_note = "duplicate", f"same company as {other}"
            continue
        claimed[it.company_id] = it.target_id


# Where a 2e opening was seen, as a role email says it ("the Backend Engineer role on your YC jobs page").
ROLE_WHERE = {"hn_archive": "in your Hacker News post", "yc_jobs": "on your YC jobs page",
              "careers": "on your careers page", "board": "on your careers page", "manual": ""}


def _draft(inputs: DraftInputs, facts: Facts, written, cfg: dict, terms: list[str]) -> Draft:
    lo, hi = int(cfg["min_words"]), int(cfg["max_words"])
    draft = template_draft(inputs, facts, lo, hi, terms)
    if written is None:
        return draft
    failures = check_written(written["subject"], written["body"], inputs, facts, lo, hi, terms)
    if failures:
        draft.failures = failures
        return draft
    return Draft(target_id=inputs.target_id, subject=written["subject"], body=written["body"],
                 resume_action=draft.resume_action, resume_reason=draft.resume_reason, source="claude",
                 project=draft.project, project_reason=draft.project_reason, skills=draft.skills,
                 skills_reason=draft.skills_reason)


def _why(check, inputs: DraftInputs, kind: str) -> str:
    size = f"{inputs.team_size} people" if inputs.team_size is not None else "size unknown"
    if kind == "role":
        if inputs.role_score is not None:
            return f"opening: {inputs.role_title} (score {inputs.role_score}, tier {inputs.role_tier}) · {size}"
        return f"opening (no longer ranked): {inputs.role_title} · {size}"
    if kind == "manual":
        return "added by hand" + (f": {inputs.note}" if inputs.note else "")
    batch = f"YC {inputs.batch} · " if inputs.batch else ""
    return f"general outreach: {batch}{size}, no readable job board"


def _links(item: OutreachItem) -> list[tuple[str, str]]:
    links = []
    a = item.address
    if a and needs_manual_check(a.confidence, a.name):
        linkedin, google = search_links(a.name, item.company)
        links += [("LinkedIn", linkedin), ("Google", google)]
    if a and a.source_url:
        links.append(("where found", a.source_url))
    if item.inputs.website:
        links.append(("website", item.inputs.website))
    if item.inputs.role_url:
        links.append(("posting", item.inputs.role_url))
    return links


def refresh_drafts(store, profile: Profile, dnc: DoNotContact | None = None) -> list[OutreachItem]:
    items = build_items(store, profile, dnc)
    store.save_drafts(items)
    return items


# ---------------------------------------------------------------------------
# Company context
# ---------------------------------------------------------------------------

def ensure_context(store, client, items: list[OutreachItem], log: Log = lambda _: None, force: bool = False) -> int:
    """Fetch the homepage of every target with no stored context (one page each, robots.txt respected)."""
    fetched = 0
    for it in items:
        site = normalize_website(it.inputs.website)
        if not site or (not force and store.get_context(it.company_key) is not None):
            continue
        try:
            ctx = {"website": site, "status": "ok", **page_summary(client.get_page(site))}
            log(f"  {it.company:<32} context: {ctx['title'][:60] or '(no title)'}")
        except FetchError as exc:
            status = "blocked" if "disallowed by robots" in str(exc) else "failed"
            ctx = {"website": site, "status": status, "note": str(exc)[:200]}
            log(f"  {it.company:<32} context {status}: {exc}")
        store.save_context(it.company_key, ctx)
        fetched += 1
    return fetched


# ---------------------------------------------------------------------------
# Packets and written drafts
# ---------------------------------------------------------------------------

def _meta(it: OutreachItem, profile: Profile) -> dict:
    return {"priority": it.priority, "send_tier": it.send_tier, "status": STATUS_LABELS.get(it.status, it.status),
            "why": it.why, "backups": [b.label() for b in it.backups],
            "min_words": profile.outreach["min_words"], "max_words": profile.outreach["max_words"]}


def write_packets(items: list[OutreachItem], profile: Profile, out_dir: Path, only: set[str] | None = None) -> list[Path]:
    """One packet per target in out_dir/packets, plus the style guide and a priority index."""
    facts = load_facts(profile)
    packets = out_dir / "packets"
    packets.mkdir(parents=True, exist_ok=True)
    (out_dir / "written").mkdir(parents=True, exist_ok=True)
    written = []
    for it in items:
        if only is not None and it.target_id not in only:
            continue
        path = packets / f"{it.target_id}.md"
        path.write_text(render_packet(it.inputs, it.draft, facts, _meta(it, profile)), encoding="utf-8")
        written.append(path)
    (packets / "STYLE_GUIDE.md").write_text(STYLE_GUIDE, encoding="utf-8")
    index = ["# Drafting packets, highest priority first", "",
             "| # | id | company | type | tier | status | to | draft |", "|---|---|---|---|---|---|---|---|"]
    for it in items:
        index.append(f"| {it.priority} | {it.target_id} | {it.company} | {it.kind} | {it.send_tier or '-'} | "
                     f"{STATUS_LABELS.get(it.status, it.status)} | {it.address.email if it.address else '-'} | "
                     f"{it.draft.source} |")
    (packets / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return written


@dataclass
class ImportResult:
    target_id: str
    file: str
    ok: bool
    failures: list[str] = field(default_factory=list)


def import_written(store, profile: Profile, written_dir: Path, dnc: DoNotContact | None = None) -> list[ImportResult]:
    """Load output/drafts/written/<id>.md files; a draft that fails validation stays on the template."""
    items = {it.target_id: it for it in build_items(store, profile, dnc)}
    facts, terms, cfg = load_facts(profile), tech_terms(profile), profile.outreach
    results = []
    for path in sorted(written_dir.glob("*.md")):
        tid = path.stem
        if tid not in items:
            results.append(ImportResult(tid, path.name, False, [f"no target with id '{tid}'"]))
            continue
        try:
            subject, body = parse_written(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            results.append(ImportResult(tid, path.name, False, [f"not in the fixed format: {exc}"]))
            continue
        store.save_written(tid, subject, body, str(path))
        failures = check_written(subject, body, items[tid].inputs, facts, int(cfg["min_words"]),
                                 int(cfg["max_words"]), terms)
        results.append(ImportResult(tid, path.name, not failures, failures))
    refresh_drafts(store, profile, dnc)
    return results


# ---------------------------------------------------------------------------
# Queue and send history
# ---------------------------------------------------------------------------

@dataclass
class Tier3Gate:
    """How many guessed (tier-3) addresses a day's batch may hold, and why."""
    state: str                        # locked | limited | full | paused
    limit: int
    reason: str

    @property
    def open(self) -> bool:
        return self.limit > 0


def tier3_gate(history: dict, day: date, cfg: dict) -> Tier3Gate:
    """Unlock `unlock_after_days` calendar days after the first send if no bounce has been recorded; then
    `start_per_day`, rising to `ramp_per_day` `ramp_after_days` after the first tier-3 send; paused whenever
    the overall bounce rate is at or above `max_bounce_rate`."""
    first, sent, bounced = history.get("first_sent"), int(history.get("sent") or 0), int(history.get("bounced") or 0)
    started = history.get("tier3_first_sent")
    max_rate = float(cfg["max_bounce_rate"])
    rate = bounced / sent if sent else 0.0
    stats = f"{bounced} bounce(s) in {sent} send(s), {rate:.0%}"
    if not first:
        return Tier3Gate("locked", 0, f"locked: nothing sent yet; it unlocks {cfg['unlock_after_days']} "
                                      "calendar days after the first mark-sent, if no email has bounced")
    if sent and rate >= max_rate:
        return Tier3Gate("paused", 0, f"paused: bounce rate {stats} is at or above {max_rate:.0%}; "
                                      "it resumes once more tier 1-2 sends bring it back under")
    unlock = date.fromisoformat(first) + timedelta(days=int(cfg["unlock_after_days"]))
    if day < unlock:
        return Tier3Gate("locked", 0, f"locked: first send was {first}; it unlocks on {unlock.isoformat()} "
                                      f"({cfg['unlock_after_days']} calendar days later) if nothing bounces")
    if bounced and not started:
        return Tier3Gate("locked", 0, f"locked: {stats} recorded before it unlocked; it needs zero bounces to start")
    if started:
        ramp = date.fromisoformat(started) + timedelta(days=int(cfg["ramp_after_days"]))
        if day >= ramp:
            return Tier3Gate("full", int(cfg["ramp_per_day"]),
                             f"open at {cfg['ramp_per_day']}/day: guessed sends began {started}; {stats}")
        return Tier3Gate("limited", int(cfg["start_per_day"]),
                         f"limited to {cfg['start_per_day']}/day until {ramp.isoformat()}, then "
                         f"{cfg['ramp_per_day']}/day if the bounce rate stays under {max_rate:.0%}; {stats}")
    return Tier3Gate("limited", int(cfg["start_per_day"]),
                     f"unlocked, limited to {cfg['start_per_day']}/day; {cfg['ramp_after_days']} days after the "
                     f"first guessed send it rises to {cfg['ramp_per_day']}/day if the bounce rate stays under "
                     f"{max_rate:.0%}; {stats}")


@dataclass
class Batch:
    day: str
    items: list[OutreachItem]
    gate: Tier3Gate
    tier3_held: int = 0               # tier-3 targets left out by the gate (locked, paused or over the limit)
    skipped_same_company: int = 0
    first_sent: str | None = None
    held_earlier: int = 0             # ready targets already in an earlier day's stored queue (not marked sent)
    general_held: int = 0             # hello@ / hi@ / team@ targets over the daily cap (2e)

    @property
    def tier3_open(self) -> bool:
        return self.gate.open


def pick_batch(items: list[OutreachItem], day: date, size: int, history: dict, tier3_cfg: dict,
               already: list[OutreachItem] | None = None, general_cap: int | None = None) -> Batch:
    """The day's emails: ready targets in priority order (tier 1, 2, then 3 within the gate), one per company,
    at most `general_cap` to hello@ / hi@ / team@ (2e)."""
    already = already or []
    general = sum(1 for it in already if it.address and it.address.kind == "general_inbox")
    gate = tier3_gate(history, day, tier3_cfg)
    batch = Batch(day=day.isoformat(), items=list(already), gate=gate, first_sent=history.get("first_sent"))
    seen = {it.company_id for it in already}
    taken = {it.target_id for it in already}
    tier3 = sum(it.send_tier == 3 for it in already)
    for it in items:
        if len(batch.items) >= size:
            break
        if it.status != "ready" or it.send_tier is None or it.target_id in taken:
            continue
        if it.queued_on and it.queued_on < batch.day:
            batch.held_earlier += 1           # waiting in an earlier day's stored batch
            continue
        if it.company_id in seen:
            batch.skipped_same_company += 1
            continue
        if general_cap is not None and it.address and it.address.kind == "general_inbox":
            if general >= general_cap:
                batch.general_held += 1
                continue
            general += 1
        if it.send_tier == 3:
            if tier3 >= gate.limit:
                batch.tier3_held += 1
                continue
            tier3 += 1
        seen.add(it.company_id)
        batch.items.append(it)
    return batch


def _general_cap(profile: Profile) -> int | None:
    cap = profile.outreach.get("general_inbox_per_day")
    return None if cap in (None, "") else int(cap)


def mark_queued(store, items: list[OutreachItem]) -> None:
    """Note, on each ready item, the earliest stored batch that already holds it."""
    by_id = {it.target_id: it for it in items}
    for row in store.conn.execute("SELECT target_id, MIN(batch_date) FROM send_queue GROUP BY target_id"):
        it = by_id.get(row[0])
        if it is not None and it.status == "ready":
            it.queued_on = row[1]


def make_queue(store, profile: Profile, day: date, size: int | None = None, dnc: DoNotContact | None = None,
               save: bool = True) -> Batch:
    """Pick (and store) a day's batch. Entries of that day already marked sent stay in it; targets waiting in
    an earlier day's stored batch are left for that day."""
    items = refresh_drafts(store, profile, dnc) if save else build_items(store, profile, dnc, day=day)
    mark_queued(store, items)
    size = int(size or profile.outreach["daily_batch"])
    by_id = {it.target_id: it for it in items}
    sent_today = [by_id[r["target_id"]] for r in store.queue(day.isoformat())
                  if r["target_id"] in by_id and by_id[r["target_id"]].status == "sent"
                  and by_id[r["target_id"]].sent_date == day.isoformat()]
    batch = pick_batch(items, day, size, store.send_history(day.isoformat()), profile.outreach["tier3"], sent_today,
                       general_cap=_general_cap(profile))
    if save:
        store.save_queue(day.isoformat(), [(it.target_id, it.address.email, it.send_tier)
                                           for it in batch.items if it.status == "ready"],
                         keep={it.target_id for it in sent_today})
    return batch


def stored_or_preview_batch(store, profile: Profile, items: list[OutreachItem],
                            day: date) -> tuple[list[OutreachItem], bool, Tier3Gate]:
    """The stored queue for `day` if `queue` ran, else a preview (not saved): (items, stored, tier-3 gate)."""
    by_id = {it.target_id: it for it in items}
    rows = store.queue(day.isoformat())
    mark_queued(store, items)
    batch = pick_batch(items, day, int(profile.outreach["daily_batch"]), store.send_history(day.isoformat()),
                       profile.outreach["tier3"], general_cap=_general_cap(profile))
    if rows:
        return [by_id[r["target_id"]] for r in rows if r["target_id"] in by_id], True, batch.gate
    return batch.items, False, batch.gate


class OutreachError(ValueError):
    pass


def _find(items: list[OutreachItem], tid: str) -> OutreachItem:
    for it in items:
        if it.target_id == tid or it.company_key == tid:
            return it
    matches = [it for it in items if tid.lower() in it.target_id or tid.lower() in it.company.lower()]
    if len(matches) == 1:
        return matches[0]
    raise OutreachError(f"no single target matches '{tid}'" + (
        f" (candidates: {', '.join(m.target_id for m in matches[:8])})" if matches else ""))


def mark_sent(store, profile: Profile, tid: str, day: date, dnc: DoNotContact | None = None) -> OutreachItem:
    items = build_items(store, profile, dnc)
    it = _find(items, tid)
    if it.status == "sent":
        raise OutreachError(f"{it.target_id} was already sent to {it.sent_email} on {it.sent_date}")
    if it.status != "ready" or it.address is None:
        raise OutreachError(f"{it.target_id} isn't ready to send ({STATUS_LABELS.get(it.status, it.status)})")
    queued = {r["target_id"]: r["email"] for r in store.queue(day.isoformat())}
    store.add_event(it.target_id, it.company_key, queued.get(it.target_id, it.address.email), "sent",
                    day.isoformat(), day.isoformat() if it.target_id in queued else "", it.send_tier)
    refresh_drafts(store, profile, dnc)
    return it


def mark_batch_sent(store, profile: Profile, batch_day: date, dnc: DoNotContact | None = None) -> list[str]:
    rows = store.queue(batch_day.isoformat())
    if not rows:
        raise OutreachError(f"no queue stored for {batch_day.isoformat()}; run `queue --date {batch_day.isoformat()}` first")
    items = {it.target_id: it for it in build_items(store, profile, dnc)}
    marked = []
    for r in rows:
        it = items.get(r["target_id"])
        if it is None or it.status != "ready":
            continue
        store.add_event(it.target_id, it.company_key, r["email"], "sent", batch_day.isoformat(), batch_day.isoformat(),
                        r["send_tier"])
        marked.append(f"{it.target_id} -> {r['email']}")
    refresh_drafts(store, profile, dnc)
    return marked


def mark_bounced(store, profile: Profile, tid: str, day: date, dnc: DoNotContact | None = None,
                 stop: bool = False) -> OutreachItem:
    """Record a bounce of the last address sent to; returns the target with its next address. With `stop`, the
    target isn't moved on: its alternates and backups are put on hold (status 'stopped', never queued)."""
    it = _find(build_items(store, profile, dnc), tid)
    if it.status != "sent":
        raise OutreachError(f"{it.target_id} has no sent email to bounce (status: {STATUS_LABELS.get(it.status, it.status)})")
    store.add_event(it.target_id, it.company_key, it.sent_email, "bounced", day.isoformat())
    if stop:
        store.add_event(it.target_id, it.company_key, it.sent_email, "stopped", day.isoformat())
    items = refresh_drafts(store, profile, dnc)
    return next(i for i in items if i.target_id == it.target_id)


# ---------------------------------------------------------------------------
# Manual targets
# ---------------------------------------------------------------------------

def add_target(store, profile: Profile, client, company: str, website: str, note: str = "", email: str = "",
               person: str = "", role: str = "", dnc: DoNotContact | None = None, log: Log = lambda _: None,
               out_dir: Path | None = None, role_seen: str = "", before_drafts: Callable[[str], None] | None = None
               ) -> OutreachItem:
    """Create a manual target, find a contact unless an email is given, and write its draft and packet.
    `role_seen` (2e import-targets) is an opening you saw: recorded as a role match, so the email cites it;
    `before_drafts(key)` runs after the contact lookup, before drafts are built (role checks)."""
    from applyflow.contacts import GitHubBudget, MXChecker, find_contacts
    dnc = dnc or DoNotContact()
    key = f"manual:{name_key(company) or re.sub(r'[^a-z0-9]+', '', company.lower())}"
    store.upsert_manual_target(key, company, normalize_website(website), note, email, person, role, role_seen)
    if role_seen:
        store.save_role_match(key, "manual", "yes", title=role_seen, note="the role you saw (import-targets)")
    row = store.get_manual_target(key)
    target = manual_target(row, profile)
    if email:
        blocked = dnc.blocks(email)
        if blocked:
            raise OutreachError(f"{email} is on the do-not-contact list ({blocked})")
        result = manual_result(target, row)
        store.save_contact_result(result)
        if client is not None and target.website:
            ensure_context(store, client, [_stub(key, company, target.website)], log)
    else:
        mx = MXChecker(client, store, int(profile.contacts["mx_cache_days"])) if client is not None else None
        result = find_contacts(client, target, profile, dnc, GitHubBudget(), log=log, mx=mx)
        store.save_contact_result(result)
    if before_drafts is not None:
        before_drafts(key)
    items = refresh_drafts(store, profile, dnc)
    it = next(i for i in items if i.company_key == key)
    if out_dir is not None:
        write_packets(items, profile, out_dir, only={it.target_id})
    return it


def _stub(key: str, company: str, website: str) -> OutreachItem:
    """Just enough of an item for ensure_context."""
    inputs = DraftInputs(target_id=target_id(key), company_key=key, company=company, kind="manual", website=website)
    return OutreachItem(target_id=inputs.target_id, company_key=key, company=company, kind="manual", status="",
                        inputs=inputs, draft=None)


def today() -> date:
    return date.today()


def parse_day(text: str | None) -> date:
    if not text:
        return today()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise OutreachError(f"'{text}' isn't a date in YYYY-MM-DD form") from exc

