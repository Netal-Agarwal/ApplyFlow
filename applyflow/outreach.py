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

Phase 2f: `queue` uses a per-type bounce gate (`type_gates`) instead of the overall 5% tier-3 gate: evidence-backed
guesses (10 a day) and hello@ (5 a day) pause for a day after 2+ bounces in one day; hiring inboxes pause while 3+
of their last 20 sends bounced; verified named people and founders@ are never paused by other types' bounces; pure
pattern guesses are never sent. Other published addresses are classified (`classify_published`): hiring ones are
sendable, support / sales / ops / billing ones aren't. A company over 300 people is emailed only at its own
published hiring inbox (or a posting's address).
"""

from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from applyflow.config import Profile
from applyflow.contacts import (FOUNDER_INBOXES, FREEMAIL, HIRING_INBOXES, ROLE_LOCAL, DoNotContact, _local_base,
                                classify_address, inbox_preference, manual_result, manual_target, name_from_local,
                                needs_manual_check, role_group, search_links, shared_inbox)
from applyflow.detect import normalize_website
from applyflow.discovery import name_key, site_domain
from applyflow.drafting import (HR_GROUPS, Draft, DraftInputs, Facts, check_written, display_company, gmail_url,
                                load_facts, parse_written, render_packet, STYLE_GUIDE, tech_terms, template_draft)
from applyflow.http import FetchError
from applyflow.scoring import APPLY_ONLY_HINT, MARKETPLACE_HINT
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
# 2g: what a reply means. Every outcome stops follow-ups; auto_ack also confirms delivery; declined_warm targets come
# back in the report's Recheck list on their recheck date.
OUTCOMES = ("interested", "declined_warm", "declined", "auto_ack")
OUTCOME_LABELS = {"interested": "replied: interested", "declined_warm": "replied: not now, stay in touch",
                  "declined": "replied: declined", "auto_ack": "automatic acknowledgement (delivered)"}
# 2g: after `mark-bounced --stop`, only these can reopen a target - never another guess.
REOPEN_KINDS = ("verified_person", "hiring_inbox")


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
GENERAL_INBOXES = {"hello", "hi", "team", "hey"}    # 2e: capped per day (hey@: 2f)
SAFE_KINDS = ("verified_person", "founders", "hiring_inbox")
SENDABLE_KINDS = SAFE_KINDS + ("evidence_guess", "general_inbox")       # 2f: what `queue` may pick
KIND_LABELS = {"verified_person": "verified named person", "founders": "founders@", "hiring_inbox": "hiring inbox",
               "general_inbox": "hello@ / hi@ / hey@ / team@", "evidence_guess": "evidence-backed guess",
               "unsendable": "unsendable (info@ / contact@, legal page, support / sales / ops)",
               "guessed": "pure pattern guess (never sent)", "other": "other published address"}


_LEGAL_PAGE = re.compile(r"privacy|terms|legal|policy|polic(?:y|ies)|grievance|refund|cookie|disclaimer|"
                         r"account-deletion|compliance", re.I)


def legal_page_person(email: str, source_url: str, name: str = "", source: str = "website") -> bool:
    """2e: a personal address seen only on a privacy / terms / legal page, tied to no named person: usually a
    grievance or privacy officer, not someone who hires."""
    from urllib.parse import urlparse
    return (source == "website" and not name and not shared_inbox(email)
            and bool(_LEGAL_PAGE.search(urlparse(source_url or "").path)))


def contact_kind(email: str, confidence: str, source_url: str = "", name: str = "", source: str = "",
                 evidence: dict | str | None = None, role: str = "") -> str:
    """verified_person | founders | hiring_inbox (the safe kinds) | general_inbox | evidence_guess (2f) | unsendable |
    guessed (a pure pattern guess, never sent) | other (2e; 2f classifies these with classify_published)."""
    base = _local_base(email)
    if base in UNSENDABLE_INBOXES or (source_url and legal_page_person(email, source_url, name, source)):
        return "unsendable"
    if confidence in ("guessed", "strong"):        # 2f: an inferred address (pattern) is a guess, seen on no page
        return "evidence_guess" if evidence else "guessed"
    if base in FOUNDER_INBOXES:
        return "founders"
    if base in HIRING_INBOXES:
        return "hiring_inbox"
    if base in GENERAL_INBOXES:
        return "general_inbox"
    if confidence == "verified" and not shared_inbox(email) and not ROLE_LOCAL.search(email.split("@", 1)[0]):
        # 2f: an address on a website is a verified person only with a name and a hiring role (founder, CTO,
        # engineering lead, recruiter); an unnamed "person at the company" may be sales or support.
        if source == "website" and (not name or role_group(role or "") not in HIRING_PEOPLE):
            return "other"
        return "verified_person"
    return "other"


HIRING_PEOPLE = {"founder", "cto", "eng_lead", "recruiting"}


_ROLE_LOCAL = ROLE_LOCAL          # 2e name, kept for scripts that import it

# 2f: what an "other" published address is. Hiring words make it a hiring inbox (sendable); support, sales, ops,
# billing, legal, press and general contact words make it unsendable; a lone first name with no named role is
# not confirmed as someone who hires.
_PUBLISHED_UNSENDABLE = [
    ("support", r"support|help|care|success|service"),
    ("sales / business", r"sales|business|bd|crm|partner|partners|partnership|enquir|inquir|begin|demo|institute"),
    ("operations / admin", r"admin|ops|operations|office|accounts?|finance|billing|payments?"),
    ("legal / compliance", r"legal|compliance|grievance|privacy|nodal"),
    ("press / media / social", r"press|media|news|newsroom|social|marketing|pr"),
    ("general contact inbox (like info@ / contact@)", r"information|connect|reachus|reach|mail|company|enquiry|"
                                                      r"query|queries|humans|contactus|write|talk|letstalk"),
]


def classify_published(email: str, role: str = "", source_url: str = "", company: str = "") -> tuple[str, str]:
    """2f: (kind, reason) for a published address contact_kind calls 'other': hiring_inbox | general_inbox
    (hey@-like) | unsendable."""
    local = email.split("@", 1)[0].lower().removeprefix("mailto:")
    words = [w for w in re.split(r"[._+-]", local) if w]
    if _local_base(email) in HIRING_INBOXES or re.search(r"^(?:hr|careers?|jobs?|talent|recruit\w*|hiring|people)"
                                                         r"(?:$|[._-]|india|team|desk)", local):
        return "hiring_inbox", f"hiring inbox ({local}@)"
    if local in GENERAL_INBOXES:
        return "general_inbox", f"general greeting inbox ({local}@), capped like hello@"
    for label, rx in _PUBLISHED_UNSENDABLE:
        if any(re.fullmatch(rf"(?:{rx})\w*", w) for w in words):
            return "unsendable", f"{label} address ({local}@), not someone who hires"
    squashed = re.sub(r"[^a-z0-9]", "", company.lower())
    if squashed and re.sub(r"[^a-z0-9]", "", local) in (squashed, squashed[:len(local)]):
        return "unsendable", f"the company's own name as an inbox ({local}@): a general contact address"
    if re.fullmatch(r"[a-z]{2,12}", local):
        return "unsendable", (f"a personal address ({local}@) tied to no named founder, CTO or recruiter on the "
                              f"site: role not confirmed")
    return "unsendable", f"unrecognised address ({local}@), role not confirmed"


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
    evidence: dict = field(default_factory=dict)      # 2f: the proof behind a sendable guess
    company: str = ""              # for classify_published

    @property
    def tier(self) -> int | None:
        return send_tier(self.confidence, self.group)

    @property
    def kind(self) -> str:
        kind = contact_kind(self.email, self.confidence, self.source_url, self.name, self.source, self.evidence,
                            self.role)
        return classify_published(self.email, self.role, self.source_url, self.company)[0] if kind == "other" else kind

    @property
    def kind_reason(self) -> str:
        """Why an address is (un)sendable, for the report and explain output."""
        kind = contact_kind(self.email, self.confidence, self.source_url, self.name, self.source, self.evidence,
                            self.role)
        if kind == "other":
            return classify_published(self.email, self.role, self.source_url, self.company)[1]
        return KIND_LABELS.get(kind, kind)

    def label(self) -> str:
        who = f"{self.name} ({self.role})" if self.name else self.role
        extra = f", {self.note}" if self.note else ""
        return f"{self.email} - {who} [{self.confidence}{extra}]"


def address_sequence(contacts) -> list[Address]:
    """Every address to try, in order: each stored contact, followed by its alternates (guesses)."""
    seq: list[Address] = []
    for i, c in enumerate(contacts):
        group = contact_group(c["name"] or "", c["role"] or "", c["email"], c["source"] or "")
        keys = c.keys()
        evidence = json.loads(c["evidence"]) if "evidence" in keys and c["evidence"] else {}
        base = Address(email=c["email"], name=c["name"] or "", role=c["role"] or "", confidence=c["confidence"],
                       group=group, source=c["source"] or "", source_url=c["source_url"] or "", mx=c["mx"] or "",
                       note="" if i == 0 else "backup contact",
                       proof=(c["proof"] if "proof" in keys else "") or "", evidence=evidence,
                       company=c["company"] or "")
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
    sector: str = ""                           # 2f: fintech | consultancy | startup | other
    large: bool = False                        # 2f: over 300 people (apply-only): hiring inbox only
    outcome: str = ""                          # 2g: interested | declined_warm | declined | auto_ack (mark-replied)
    outcome_date: str = ""
    recheck: str = ""                          # 2g: YYYY-MM-DD when a declined_warm target is looked at again
    replied_by: str = ""                       # 2g: "Name <email>" of the person who replied, if any

    @property
    def followups_stopped(self) -> bool:
        """2g: a recorded reply (any outcome, auto-acknowledgements included) ends the follow-up sequence."""
        return bool(self.outcome)

    @property
    def guess_rank(self) -> int:
        """2g: evidence-backed guesses whose evidence is a first-name address (founder-style, rahul@) before those
        backed only by a staff first.last address; 2 for anything else."""
        a = self.address
        if a is None or a.kind != "evidence_guess":
            return 2
        return 0 if evidence_kind(a.evidence) == "first_name" else 1

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
    # 2f: a target queued for a day but not yet sent keeps the address it was queued with; it's otherwise treated like
    # any unsent target (its role email, the unsendable filter), so the report and drafts match the stored queue.
    sent_ids = {e["target_id"] for e in store.events() if e["event"] in ("sent", "bounced", "stopped")}
    queued_at: dict[str, str] = {}
    for r in store.conn.execute("SELECT target_id, email FROM send_queue ORDER BY batch_date"):
        queued_at[r[0]] = r[1]
    locked_ids = set(locked)
    pinned = {t: e for t, e in queued_at.items() if t not in sent_ids and t not in locked_ids}
    hard_protected = protected - set(pinned)
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
        if kind != "role" and tid not in hard_protected and (found_2e or (match and match["title"])):
            kind = "role"                  # 2e: an opening found since the lookup (HN archive, YC jobs, careers page)
            role_rows = found_2e
        inputs = DraftInputs(target_id=tid, company_key=key, company=display_company(check["company"]), kind=kind,
                             team_size=check["team_size"], rules=locked.get(tid, "2f"))
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
        large = check["hint"] == APPLY_ONLY_HINT
        if tid not in hard_protected:      # 2e: info@ / contact@, legal-page and reviewed-out addresses are never sent to
            remaining = [a for a in remaining if a.kind != "unsendable" and not a.proof.startswith("excluded:")
                         and a.email not in excluded]
            if large:                      # 2f: a large company only at its own hiring inbox (or a posting's address)
                remaining = [a for a in remaining if a.source == "posting"
                             or (a.kind == "hiring_inbox" and a.proof == "company site")]
            keep = next((a for a in seq if a.email == pinned.get(tid) and a.email not in bounced), None)
            if keep is not None and keep not in remaining:          # queued before a rule changed: kept (2e)
                remaining.insert(0, keep)
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
            # 2g: a stopped target reopens only through a verified named person or a published hiring inbox found
            # since (never another guess, never the stopped address's alternates)
            reopen = [a for a in remaining if a.kind in REOPEN_KINDS and a.email not in bounced
                      and (a.kind != "hiring_inbox" or a.proof in ("company site", "reply", "posting"))]
            if reopen and not blocked:
                item.address, item.backups = reopen[0], reopen[1:]
                item.status_note = f"reopened: {reopen[0].email} ({reopen[0].kind}) after the " \
                                   f"{stopped[-1]['event_date']} stop"
            else:
                item.status, item.status_note = "stopped", f"bounced {stopped[-1]['event_date']}; not moved on to " \
                    "its alternates or backups (2g: only a verified person or published hiring inbox can reopen it)"
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
            queued_email = pinned.get(tid)
            if queued_email and item.address.email != queued_email:      # 2f: the address it was queued with
                pick = next((a for a in seq if a.email == queued_email), None)
                if pick is not None:
                    item.address, item.backups = pick, [a for a in remaining if a.email != queued_email]
        if item.address:
            a = item.address
            name = a.name or (name_from_local(a.email) if inputs.rules != "2b" else "")
            inputs.to_email, inputs.contact_name, inputs.contact_role = a.email, name, a.role
            inputs.confidence, inputs.contact_group = a.confidence, a.group
            item.send_tier = a.tier
        replies = [e for e in evs if e["event"] == "replied"]
        if replies:                        # 2g: mark-replied
            r = replies[-1]
            item.outcome, item.outcome_date = r["outcome"] or "", r["event_date"]
            item.recheck, item.replied_by = r["recheck_date"] or "", r["note"] or ""
            item.status_note = f"{OUTCOME_LABELS.get(item.outcome, item.outcome)} {item.outcome_date}" + \
                (f"; recheck {item.recheck}" if item.recheck else "") + "; no follow-ups"
        item.why = _why(check, inputs, kind)
        item.manual_links = _links(item)
        from applyflow.supply import sector_for
        item.large = large
        item.sector = inputs.sector = sector_for(check["company"], prow, inputs, check["team_size"], large)
        item.draft = _draft(inputs, facts, written.get(tid), cfg, terms)
        item.protected = tid in protected
        items.append(item)

    def priority(it: OutreachItem) -> tuple:
        """Tiers 1-2 together, lowest bounce risk first (bounce_rank, 2d); tier 3 after them. Then (2f) companies
        with a hiring signal - an open role that fits, a funding round in the last 6 months, or more than 20
        people - then role targets, hiring likelihood and inbox preference (2c)."""
        size = it.team_size
        low_risk = it.send_tier in (1, 2)
        signal = it.kind == "role" or bool(it.inputs.funding.get("recent")) or (size or 0) > 20
        # 2f: capped hello@ / hi@ / team@ come after the evidence-backed guesses (tier 3)
        general = bool(it.address) and it.address.kind == "general_inbox"
        # 2g: within tiers 1-2, named hiring people and founders@ first, then hiring inboxes - established companies
        # first (50+ people, or a consultancy / fintech / SaaS: these delivered best) - then the rest by bounce risk
        akind = it.address.kind if it.address else ""
        group = 0 if akind in ("verified_person", "founders") else 1 if akind == "hiring_inbox" else 2
        established = it.sector in ("consultancy", "fintech", "saas") or (size or 0) >= 50
        return (STATUS_ORDER.get(it.status, 9), 3.5 if general else (0 if low_risk else it.send_tier or 9),
                group if low_risk else 0, (0 if established else 1) if low_risk else 0,
                it.bounce_rank if low_risk else it.guess_rank, 0 if signal else 1, KIND_ORDER.get(it.kind, 3),
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
    gates: dict = field(default_factory=dict)          # 2f: kind -> TypeGate
    fresh: list = field(default_factory=list)          # 2f: FreshCheck per re-checked address
    held_by_kind: dict = field(default_factory=dict)   # 2f: kind -> targets its gate left out

    @property
    def tier3_open(self) -> bool:
        return self.gate.open


# ---------------------------------------------------------------------------
# Phase 2f: per-type bounce gate
# ---------------------------------------------------------------------------

@dataclass
class TypeGate:
    """How many addresses of one contact kind a day's batch may hold, and why."""
    kind: str
    state: str                        # open | capped | paused | closed
    limit: int | None                 # None = no daily limit
    reason: str


@dataclass
class SendRecord:
    day: str                          # the send's date
    kind: str
    bounced_on: str = ""              # the bounce's date ('' = not bounced)


def type_gates(sends: list[SendRecord], day: date, cfg: dict) -> dict[str, TypeGate]:
    """2f, per contact kind: verified named people and founders@ always open (no other type's bounces count);
    evidence-backed guesses (`evidence_guess_per_day`) and hello@ (`general_inbox_per_day`) capped, and paused on
    the day after one with `day_bounces_pause`+ of their bounces (and that day itself, once recorded); hiring
    inboxes paused while `hiring_max_bounces`+ of their last `hiring_window` sends bounced; pure guesses and
    unsendable addresses closed."""
    g = cfg["type_gates"]
    pause_at = int(g["day_bounces_pause"])
    today, yesterday = day.isoformat(), (day - timedelta(days=1)).isoformat()
    gates = {k: TypeGate(k, "open", None, "open: never paused by other types' bounces")
             for k in ("verified_person", "founders")}
    for kind, cap in (("evidence_guess", int(g["evidence_guess_per_day"])),
                      ("general_inbox", int(cfg.get("general_inbox_per_day") or 5))):
        mine = [s for s in sends if s.kind == kind]
        recent = {d: sum(1 for s in mine if s.bounced_on == d) for d in (yesterday, today)}
        hit = next((d for d in (today, yesterday) if recent[d] >= pause_at), "")
        if hit:
            gates[kind] = TypeGate(kind, "paused", 0, f"paused: {recent[hit]} bounced on {hit} "
                                                      f"({pause_at}+ in one day pause it the next day)")
        else:
            gates[kind] = TypeGate(kind, "capped", cap, f"up to {cap}/day; {sum(1 for s in mine if s.bounced_on)} "
                                                       f"bounce(s) in {len(mine)} send(s) so far")
    window, most = int(g["hiring_window"]), int(g["hiring_max_bounces"])
    last = sorted((s for s in sends if s.kind == "hiring_inbox" and s.day <= today), key=lambda s: s.day)[-window:]
    bad = sum(1 for s in last if s.bounced_on)
    gates["hiring_inbox"] = (TypeGate("hiring_inbox", "paused", 0, f"paused: {bad} of the last {len(last)} hiring-inbox "
                                                                  f"sends bounced ({most}+ of {window})")
                             if bad >= most else
                             TypeGate("hiring_inbox", "open", None, f"open: {bad} of the last {len(last)} hiring-inbox "
                                                                    f"sends bounced (pauses at {most} of {window})"))
    gates["guessed"] = TypeGate("guessed", "closed", 0, "closed: a pure pattern guess is never sent (2f)")
    gates["unsendable"] = TypeGate("unsendable", "closed", 0, "closed: support / sales / ops / info@ / contact@")
    gates["other"] = TypeGate("other", "closed", 0, "closed: unclassified address")
    return gates


@dataclass
class BatchRules:
    """2f: rows a batch never takes - a target without a validated Claude draft (`require_written_draft`), and
    hello@ / hi@ / team@ anywhere but a small startup (`general_inbox_max_team`, sector startup)."""
    require_written: bool = False
    general_max_team: int | None = None
    state: str = "rules"
    limit: int | None = None
    reason: str = ""

    def holds(self, it: "OutreachItem") -> str:
        if self.require_written and it.draft is not None and it.draft.source != "claude":
            return "no validated Claude draft"
        kind = it.address.kind if it.address else ""
        if kind == "general_inbox" and self.general_max_team is not None and (
                it.sector != "startup" or (it.team_size or 0) > self.general_max_team):
            return "hello@ at a company that isn't a small startup"
        return ""


def batch_rules(cfg: dict) -> BatchRules:
    g = cfg.get("type_gates") or {}
    cap = g.get("general_inbox_max_team")
    return BatchRules(bool(cfg.get("require_written_draft")), None if cap in (None, "") else int(cap))


def send_records(store) -> list[SendRecord]:
    """Every send with its contact kind (recorded at mark-sent since 2f; earlier sends are classified from the
    stored contact, tier 3 counting as a guess) and the date it bounced, if it did."""
    contacts = {r["email"]: r for r in store.conn.execute("SELECT * FROM contacts")}
    events = store.events()
    bounced = {}
    for e in events:
        if e["event"] == "bounced":
            bounced.setdefault((e["target_id"], e["email"]), e["event_date"])
    out = []
    for e in events:
        if e["event"] != "sent":
            continue
        kind = e["kind"] if "kind" in e.keys() and e["kind"] else ""
        if not kind:
            c = contacts.get(e["email"])
            conf = "guessed" if e["send_tier"] == 3 else (c["confidence"] if c else "published_inbox")
            kind = contact_kind(e["email"], conf, evidence=(c["evidence"] if c is not None and "evidence" in c.keys()
                                                             else None))
            if kind == "other" and c is not None and c["name"] and role_group(c["role"] or "") in HIRING_PEOPLE:
                kind = "verified_person"          # a named hiring person sent before 2f
            if kind == "other":
                kind = classify_published(e["email"])[0]
        out.append(SendRecord(e["event_date"], kind, bounced.get((e["target_id"], e["email"]), "")))
    return out


def pick_batch(items: list[OutreachItem], day: date, size: int, history: dict, tier3_cfg: dict,
               already: list[OutreachItem] | None = None, general_cap: int | None = None,
               gates: dict[str, TypeGate] | None = None) -> Batch:
    """The day's emails: ready targets in priority order, one per company. With `gates` (2f) each contact kind
    is held to its own gate (type_gates); without, the 2e rules: tier 1, 2, then 3 within the tier-3 gate, at
    most `general_cap` to hello@ / hi@ / team@."""
    already = already or []
    general = sum(1 for it in already if it.address and it.address.kind == "general_inbox")
    gate = tier3_gate(history, day, tier3_cfg)
    batch = Batch(day=day.isoformat(), items=list(already), gate=gate, first_sent=history.get("first_sent"),
                  gates=gates or {})
    seen = {it.company_id for it in already}
    taken = {it.target_id for it in already}
    tier3 = sum(it.send_tier == 3 for it in already)
    per_kind: dict[str, int] = {}
    for it in already:
        if it.address:
            per_kind[it.address.kind] = per_kind.get(it.address.kind, 0) + 1
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
        if gates is not None:
            kind = it.address.kind if it.address else "other"
            g = gates.get(kind) or gates["other"]
            rules = gates.get("_rules")
            why = rules.holds(it) if rules is not None else ""
            if why:                            # 2f: no validated Claude draft, or hello@ at a company that isn't small
                batch.held_by_kind[why] = batch.held_by_kind.get(why, 0) + 1
                continue
            if g.limit is not None and per_kind.get(kind, 0) >= g.limit:
                batch.held_by_kind[kind] = batch.held_by_kind.get(kind, 0) + 1
                batch.general_held += kind == "general_inbox"
                batch.tier3_held += it.send_tier == 3
                continue
            per_kind[kind] = per_kind.get(kind, 0) + 1
            seen.add(it.company_id)
            batch.items.append(it)
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


# ---------------------------------------------------------------------------
# Phase 2f: pre-send freshness check
# ---------------------------------------------------------------------------

FRESH_KINDS = ("hiring_inbox", "general_inbox", "evidence_guess", "verified_person", "founders")


@dataclass
class FreshCheck:
    target_id: str
    email: str
    url: str
    status: str                    # ok | gone (dropped) | unchecked (kept: the page couldn't be read now)
    note: str = ""


class Freshness:
    """2f: before a batch is built, re-fetch the page each picked address was found on (polite client, robots.txt)
    and drop the address if it's no longer there - for hiring inboxes, hello@, founders@, named people and the
    evidence behind a guess (the published same-format address, and the person's name on their page). A dropped
    address goes to excluded_contacts with the reason, so it's never queued again. A page that answers 404 / 410
    counts as gone; any other failure (network, robots.txt now disallowing it) keeps the address, marked unchecked.
    Posting, manual and GitHub addresses have no page to re-check. Pages are fetched once per run."""

    def __init__(self, client, store, day: date, log: Log = lambda _: None):
        self.client, self.store, self.day, self.log = client, store, day, log
        self.pages: dict[str, str | None | Exception] = {}
        self.checks: list[FreshCheck] = []

    def _page(self, url: str):
        if url not in self.pages:
            try:
                self.pages[url] = self.client.get_page(url)
            except FetchError as exc:
                self.pages[url] = exc
        return self.pages[url]

    def _has(self, url: str, needle: str, is_email: bool = True):
        """True / False, or None when the page couldn't be read (except 404 / 410: gone, so False)."""
        page = self._page(url)
        if isinstance(page, FetchError):
            return False if page.status in (404, 410) else None
        if is_email:
            from applyflow.contacts import page_has_email
            return page_has_email(page, needle)
        from applyflow.text import html_to_text
        name = needle.lower()                 # a YC page carries founders in its embedded JSON, not its text
        return name in " ".join(html_to_text(page).split()).lower() or name in htmllib.unescape(page).lower()

    def check(self, it: "OutreachItem") -> bool:
        """Is the item's address still where it was found? False = dropped (and recorded)."""
        a = it.address
        if a is None or a.kind not in FRESH_KINDS or a.source not in ("website", "pattern", "yc"):
            return True
        if a.kind == "evidence_guess":
            ev = a.evidence or {}
            probes = [(ev.get("published_url") or "", ev.get("published") or "", True),
                      (ev.get("person_url") or a.source_url, a.name, False)]
        else:
            probes = [(a.source_url, a.email, True)]
        unread = []
        for url, needle, is_email in probes:
            if not url or not needle:
                continue
            found = self._has(url, needle, is_email)
            what = needle if is_email else f"{needle}'s name"
            if found is None:
                unread.append(url)
                continue
            if not found:
                why = f"2f freshness check {self.day.isoformat()}: {what} no longer on {url}"
                self.store.exclude_contact(a.email, why)
                self.checks.append(FreshCheck(it.target_id, a.email, url, "gone", why))
                self.log(f"  dropped {a.email}: {why}")
                return False
        if unread:
            self.checks.append(FreshCheck(it.target_id, a.email, unread[0], "unchecked",
                                          f"couldn't read {unread[0]} now ({self.pages[unread[0]]}); kept"))
            return True
        self.checks.append(FreshCheck(it.target_id, a.email, probes[0][0], "ok"))
        return True


def make_queue(store, profile: Profile, day: date, size: int | None = None, dnc: DoNotContact | None = None,
               save: bool = True, fresh: Freshness | None = None) -> Batch:
    """Pick (and store) a day's batch. Entries of that day already marked sent stay in it; targets waiting in
    an earlier day's stored batch are left for that day. With `fresh` (2f), every picked address is re-checked on
    its source page first; a dropped one is replaced by the next pick, until the batch is stable."""
    items = refresh_drafts(store, profile, dnc) if save else build_items(store, profile, dnc, day=day)
    mark_queued(store, items)
    size = int(size or profile.outreach["daily_batch"])
    by_id = {it.target_id: it for it in items}
    sent_today = [by_id[r["target_id"]] for r in store.queue(day.isoformat())
                  if r["target_id"] in by_id and by_id[r["target_id"]].status == "sent"
                  and by_id[r["target_id"]].sent_date == day.isoformat()]
    gates = type_gates(send_records(store), day, profile.outreach)
    gates["_rules"] = batch_rules(profile.outreach)
    checked: set[str] = set()
    while True:
        batch = pick_batch(items, day, size, store.send_history(day.isoformat()), profile.outreach["tier3"],
                           sent_today, general_cap=_general_cap(profile), gates=gates)
        if fresh is None:
            break
        stale = [it for it in batch.items if it.status == "ready" and it.target_id not in checked
                 and not fresh.check(it)]
        checked |= {it.target_id for it in batch.items}
        if not stale:
            break
        items = refresh_drafts(store, profile, dnc) if save else build_items(store, profile, dnc, day=day)
        mark_queued(store, items)
        by_id = {it.target_id: it for it in items}
        sent_today = [by_id[t.target_id] for t in sent_today if t.target_id in by_id]
        checked -= {it.target_id for it in stale}
    batch.fresh = fresh.checks if fresh is not None else []
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
                       profile.outreach["tier3"], general_cap=_general_cap(profile),
                       gates=type_gates(send_records(store), day, profile.outreach))
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
                    day.isoformat(), day.isoformat() if it.target_id in queued else "", it.send_tier, it.address.kind)
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
        kind = it.address.kind if it.address and it.address.email == r["email"] else ""
        store.add_event(it.target_id, it.company_key, r["email"], "sent", batch_day.isoformat(), batch_day.isoformat(),
                        r["send_tier"], kind)
        marked.append(f"{it.target_id} -> {r['email']}")
    refresh_drafts(store, profile, dnc)
    return marked


def evidence_kind(evidence: dict | str | None) -> str:
    """2g: what backs an evidence guess - 'first_name' (the published same-format address is a bare first name,
    usually a founder's: rahul@) or 'staff_first_last' (a staff member's first.last-style address), '' if none."""
    if isinstance(evidence, str):
        evidence = json.loads(evidence) if evidence else {}
    published = (evidence or {}).get("published", "")
    if not published:
        return ""
    return "staff_first_last" if re.search(r"[._-]", published.split("@", 1)[0]) else "first_name"


def guess_format(evidence: dict | str | None) -> str:
    """2g: the guessed address's format: 'first@', 'first.last@' or 'other'."""
    if isinstance(evidence, str):
        evidence = json.loads(evidence) if evidence else {}
    return {"first": "first@", "first.last": "first.last@"}.get((evidence or {}).get("format", ""), "other")


def guess_outcomes(store) -> dict[str, dict[str, dict[str, int]]]:
    """2g: evidence-guess sends by address format and by evidence kind: sent, bounced, replied (a human reply,
    any outcome but auto_ack)."""
    contacts = {r["email"]: r for r in store.conn.execute("SELECT email, evidence FROM contacts")}
    events = store.events()
    bounced = {(e["target_id"], e["email"]) for e in events if e["event"] == "bounced"}
    replied = {e["target_id"] for e in events if e["event"] == "replied" and e["outcome"] != "auto_ack"}
    out: dict[str, dict[str, dict[str, int]]] = {"format": {}, "evidence": {}}
    for e in events:
        if e["event"] != "sent" or (e["kind"] or "") != "evidence_guess":
            continue
        c = contacts.get(e["email"])
        ev = c["evidence"] if c is not None else ""
        for axis, key in (("format", guess_format(ev)), ("evidence", evidence_kind(ev) or "unknown")):
            row = out[axis].setdefault(key, {"sent": 0, "bounced": 0, "replied": 0})
            row["sent"] += 1
            row["bounced"] += (e["target_id"], e["email"]) in bounced
            row["replied"] += e["target_id"] in replied
    return out


def find_sent_target(items: list[OutreachItem], email: str) -> OutreachItem:
    """2g: the sent target a reply belongs to - the address it was sent to, else the one sent target on that
    address's domain (a person replying on a founders@ / careers@ thread)."""
    email = email.lower().strip()
    hits = [it for it in items if it.sent_email and it.sent_email.lower() == email]
    if not hits:
        domain = email.split("@", 1)[-1]
        hits = [it for it in items if it.sent_email and it.sent_email.lower().split("@", 1)[-1] == domain]
    if len(hits) != 1:
        raise OutreachError(f"{len(hits) or 'no'} sent target(s) for {email}"
                            + (" - mark it sent first (`mark-sent <id> --date ...`)" if not hits else ""))
    return hits[0]


def parse_contact(text: str) -> tuple[str, str]:
    """'Naresh Kumar <nkumar@nuvizz.com>' -> ('Naresh Kumar', 'nkumar@nuvizz.com')."""
    m = re.fullmatch(r"\s*(.*?)\s*<\s*([^<>\s]+@[^<>\s]+)\s*>\s*", text or "")
    if not m or not m.group(1):
        raise OutreachError(f"--contact must look like \"Name <email>\", got {text!r}")
    return m.group(1), m.group(2).lower()


def mark_replied(store, profile: Profile, email: str, outcome: str, day: date, recheck: str = "", contact: str = "",
                 role: str = "", dnc: DoNotContact | None = None) -> OutreachItem:
    """2g: record a reply to a sent email. Every outcome stops follow-ups; auto_ack also counts as delivered;
    declined_warm needs a recheck date (the report lists it again then). `contact` ("Name <email>") is stored as a
    verified named person (source: the reply), kept across re-lookups."""
    if outcome not in OUTCOMES:
        raise OutreachError(f"outcome must be one of {', '.join(OUTCOMES)}")
    if recheck:
        recheck = parse_day(recheck).isoformat()
    if outcome == "declined_warm" and not recheck:
        raise OutreachError("declined_warm needs --recheck YYYY-MM-DD")
    it = find_sent_target(build_items(store, profile, dnc), email)
    who = ""
    if contact:
        name, addr = parse_contact(contact)
        if addr.split("@", 1)[1] != it.sent_email.split("@", 1)[1]:
            raise OutreachError(f"{addr} isn't on {it.company}'s domain ({it.sent_email})")
        store.add_reply_contact(it.company_key, it.company, name, role, addr, f"reply to {it.sent_email}")
        who = f"{name} <{addr}>"
    store.add_event(it.target_id, it.company_key, it.sent_email, "replied", day.isoformat(), outcome=outcome,
                    recheck_date=recheck, note=who)
    items = refresh_drafts(store, profile, dnc)
    return next(i for i in items if i.target_id == it.target_id)


def recheck_due(items: list[OutreachItem], today: date) -> list[OutreachItem]:
    """2g: declined_warm targets whose recheck date has arrived, oldest first."""
    due = [it for it in items if it.outcome == "declined_warm" and it.recheck and it.recheck <= today.isoformat()]
    return sorted(due, key=lambda it: (it.recheck, it.company.lower()))


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
    large = large_company_reason(store, profile, target.domain, note)
    if large:                              # 2f: hiring inbox only
        target.hint = APPLY_ONLY_HINT
        log(f"  {company}: large company ({large}): hiring inbox only")
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


def large_company_reason(store, profile: Profile, domain: str, note: str = "") -> str:
    """2f: why a company counts as large (over 300 people: hiring inbox only), from your note ("large") or its pool
    record (team size, or a late / big round with no headcount); '' otherwise."""
    if re.search(r"\blarge\b", note or "", re.I):
        return "your note says large"
    row = store.pool_by_domain().get(domain) if domain else None
    if row is None:
        return ""
    from applyflow.pool import large_reason
    from applyflow.sources import PoolCompany
    c = PoolCompany.from_json(json.loads(row["data"]))
    if c.team_size is not None and c.team_size > int(profile.contacts["apply_only_team_size"]):
        return f"{c.team_size} people"
    return large_reason(c, profile)


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

