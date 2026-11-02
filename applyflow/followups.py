"""Phase 3: follow-ups - one per contact, 5 working days after the original, sent as a reply in the same thread.

Eligible only if nobody at the company has replied as a person, the email didn't bounce, the target isn't stopped
or on the do-not-contact list, and its outcome isn't declined / declined_warm (or interested / needs_action: a
person is already talking to you). An ATS-style auto-acknowledgement ends the sequence; a small company's
auto-reply saying a person will respond (auto_ack with keep_followup) doesn't.

Three template variants rotate by target id; each names the person (or the team), the company, the role (when
the original was a role email) and the company line from the original draft, in 50-70 words, with one polite ask.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from datetime import date, timedelta

from applyflow.drafting import SIGN_OFF, Facts, split_body, word_count

GENERIC_ROLES = "a January 2027 internship or a 2027 new-grad role"

# {line} is the company line from the original draft (or a shorter form of it, or nothing) followed by a space.
VARIANTS = [
    "I'm following up on {earlier} about {role} at {company}. {line}I'd still love to contribute, "
    "and I'm happy to share more about my projects. Would you be open to a short call in the coming weeks?",
    "A short follow-up on my note about {role} at {company}. {line}I know inboxes get busy, so no pressure at all. "
    "If it helps, I can send anything else you need. Would a 15-minute chat sometime this month suit you?",
    "I wanted to check back on my earlier email about {role} at {company}. {line}I remain very interested and "
    "would be glad to talk through my backend work. Is there any chance this could fit {company}'s plans for 2027?",
]
PADDING = ["Thank you for your time either way."]
_PUSHY = re.compile(r"\b(?:just bumping|bump(?:ing)? this|circling back again|asap|urgent(?:ly)?|per my last|"
                    r"kindly revert|following up again|as soon as possible|haven'?t heard back|did you get)\b", re.I)
_PLACEHOLDER = re.compile(r"[\[\]{}<>]")


# ---------------------------------------------------------------------------
# Working days (Mon-Fri)
# ---------------------------------------------------------------------------

def is_working_day(day: date) -> bool:
    return day.weekday() < 5


def add_working_days(day: date, n: int) -> date:
    out = day
    while n > 0:
        out += timedelta(days=1)
        if is_working_day(out):
            n -= 1
    return out


def next_working_day(day: date) -> date:
    return add_working_days(day, 1)


# ---------------------------------------------------------------------------
# The text
# ---------------------------------------------------------------------------

def follow_subject(subject: str) -> str:
    subject = subject.strip()
    return subject if re.match(r"(?i)re:\s", subject) else f"Re: {subject}"


def company_line(original_body: str, company: str = "") -> str:
    """The original's observation about the company: the first sentence of its first paragraph after the 'I'm Netal'
    introduction (one naming the company first), never a question or a funding congratulation."""
    _, message, _, _ = split_body(original_body)
    first = message.split("\n\n")[0] if message else ""
    found = [s.strip() for s in re.split(r"(?<=[.!])\s+", " ".join(first.split()))
             if s.strip() and "?" not in s and not re.match(r"(?i)(?:i(?:'m| am|'d| would)\b|congratulations)", s.strip())]
    named = [s for s in found if company and company.lower() in s.lower()]
    return (named or found or [""])[0]


def line_options(line: str) -> list[str]:
    """The company line in full, cut at its first clause, and not at all."""
    out = [line] if line else []
    if line:
        short = re.split(r",\s+(?:and|but|which|so|while)\s+|\s+[-–—]\s+|;\s+", line)[0].rstrip(".,;") + "."
        if short != line and word_count(short) >= 5:
            out.append(short)
    return out + [""]


def variant_for(target_id: str) -> int:
    return zlib.crc32(target_id.encode()) % len(VARIANTS)


LAST_WEEK_DAYS = (5, 9)          # "from last week" only when the original went out 5-9 calendar days before
_LAST_WEEK = re.compile(r"\blast week\b", re.I)


def earlier_phrase(sent: date | None, day: date | None) -> str:
    """'my email from last week' when the original went out 5-9 days before the follow-up, else 'my earlier email'."""
    if sent is not None and day is not None and LAST_WEEK_DAYS[0] <= (day - sent).days <= LAST_WEEK_DAYS[1]:
        return "my email from last week"
    return "my earlier email"


def build_followup(target_id: str, greeting: str, company: str, role_title: str, original_body: str, facts: Facts,
                   lo: int = 50, hi: int = 70, variant: int | None = None, sent: date | None = None,
                   day: date | None = None) -> tuple[str, int]:
    """(body, variant). The longest company-line form that keeps the message within lo-hi words. `sent` / `day`
    (the original's date, the follow-up's date) decide whether it may say 'last week'."""
    v = variant_for(target_id) if variant is None else variant
    earlier = earlier_phrase(sent, day)
    role = f"the {role_title} role" if role_title else GENERIC_ROLES
    line = company_line(original_body, company)
    signature = "\n".join(facts.signature(resume=True))
    message = ""
    for option in line_options(line):
        for pad in ["", *PADDING]:
            text = VARIANTS[v].format(role=role, company=company, line=f"{option} " if option else "", earlier=earlier)
            text = f"{text} {pad}".strip()
            if lo <= word_count(text) <= hi:
                message = text
                break
            message = message or text
        else:
            continue
        break
    return f"{greeting}\n\n{message}\n\n{SIGN_OFF}\n{signature}", v


def validate_followup(subject: str, body: str, original_subject: str, greeting: str, facts: Facts,
                      attachment: bool = False, lo: int = 50, hi: int = 70, sent: date | None = None,
                      day: date | None = None) -> list[str]:
    """Why a follow-up can't go out ([] = fine). 'last week' needs `sent` and `day` 5-9 days apart."""
    out = []
    if _LAST_WEEK.search(body):
        gap = (day - sent).days if sent is not None and day is not None else None
        if gap is None or not LAST_WEEK_DAYS[0] <= gap <= LAST_WEEK_DAYS[1]:
            out.append("says 'last week' but the original went out "
                       + (f"{gap} days earlier" if gap is not None else "on an unknown date")
                       + f" (allowed only {LAST_WEEK_DAYS[0]}-{LAST_WEEK_DAYS[1]} days)")
    if attachment:
        out.append("a follow-up never carries an attachment")
    if subject != follow_subject(original_subject):
        out.append(f"subject must be 'Re: <original subject>' ({follow_subject(original_subject)!r})")
    first, message, signature, signoff = split_body(body)
    if first != greeting:
        out.append(f"greeting must be {greeting!r}")
    if not signoff:
        out.append(f"no sign-off line ({SIGN_OFF})")
    n = word_count(message)
    if not lo <= n <= hi:
        out.append(f"{n} words between greeting and sign-off (needs {lo}-{hi})")
    if message.count("?") != 1:
        out.append(f"one polite ask: exactly one question (found {message.count('?')})")
    if _PUSHY.search(message):
        out.append(f"pushy wording: {_PUSHY.search(message).group(0)!r}")
    if _PLACEHOLDER.search(message) or _PLACEHOLDER.search(subject):
        out.append("brackets or a placeholder left in")
    sig = "\n".join(signature)
    for label, needle in (("phone", facts.phone), ("LinkedIn", f"LinkedIn: {facts.linkedin_url}"),
                          ("GitHub", f"GitHub: {facts.github_url}")):
        if not needle or needle not in sig:
            out.append(f"signature lacks {label}")
    return out


# ---------------------------------------------------------------------------
# Who is due
# ---------------------------------------------------------------------------

HUMAN_OUTCOMES = ("interested", "declined_warm", "declined", "needs_action")


@dataclass
class FollowUp:
    item: object                          # OutreachItem
    original: object                      # gmail_messages row (thread_id, rfc_id, subject, sent_at)
    due: date
    subject: str = ""
    body: str = ""
    variant: int = 0
    failures: list[str] = field(default_factory=list)


def original_message(store, it) -> object | None:
    """The message a follow-up replies to: the latest delivered one (a soft-bounce retry, else the original)."""
    rows = [r for r in store.gmail_messages(it.target_id) if r["row_type"] in ("new", "retry")
            and r["email"] == (it.sent_email or "").lower()]
    return rows[-1] if rows else None


def original_day(original, it) -> date | None:
    """The IST date the message a follow-up replies to went out."""
    from datetime import datetime
    from applyflow.inbox import ist_day
    if original is not None and original["sent_at"]:
        return ist_day(datetime.fromisoformat(original["sent_at"]))
    return date.fromisoformat(it.sent_date) if it.sent_date else None


def awaiting_retry(store) -> set[str]:
    """Targets with a soft bounce and no retry sent yet: the follow-up waits for the retry."""
    soft = {r["target_id"] for r in store.inbox_messages(category="soft_bounce") if r["target_id"]}
    return soft - {r["target_id"] for r in store.gmail_messages() if r["row_type"] == "retry"}


def followup_status(store, it, day: date, cfg: dict, dnc=None, blocked_companies: set[str] | None = None,
                    original=None, done: set[str] | None = None,
                    waiting: set[str] | None = None,
                    stopped: set[str] | None = None) -> tuple[date | None, str]:
    """(due date, '') when the target gets a follow-up on `day`, else (due date or None, why not)."""
    if it.status != "sent" or not it.sent_email:
        return None, f"not a sent target ({it.status})"
    if it.sent_email in it.bounced:
        return None, "bounced"
    if stopped is None:
        stopped = stopped_targets(store)
    if it.target_id in stopped:
        return None, "stopped"
    if dnc is not None and dnc.blocks(it.sent_email):
        return None, "on the do-not-contact list"
    if it.sent_email.split("@")[0].lower() in ("info", "contact"):
        return None, "sent to info@ / contact@: never emailed again"
    if it.outcome in HUMAN_OUTCOMES:
        return None, f"replied ({it.outcome})"
    if it.followups_stopped:
        return None, "auto-acknowledgement (ATS): no follow-up"
    if blocked_companies and it.company_id in blocked_companies:
        return None, "someone at the company has replied"
    if done and it.target_id in done:
        return None, "already followed up (one per contact)"
    if original is None:
        return None, "the original isn't in the Gmail records (run reconcile)"
    if waiting and it.target_id in waiting:
        return None, "soft bounce: the retry goes first"
    sent = original_day(original, it)
    due = max(add_working_days(sent, int(cfg["followup_after_working_days"])),
              date.fromisoformat(str(cfg["followups_not_before"])))
    if day < due:
        return due, f"due {due.isoformat()}"
    return due, ""


def stopped_targets(store) -> set[str]:
    """Targets with a `stopped` event (a hard bounce with --stop, or stopped by hand without one)."""
    return {e["target_id"] for e in store.events() if e["event"] == "stopped"}


def manual_original(it) -> dict:
    """Sending is manual (2026-11-02): with no Gmail record, the original is the sent event and its draft's subject
    (what you sent by hand); you find it in Sent by recipient and subject."""
    return {"gmail_id": "", "thread_id": "", "rfc_id": "", "sent_at": "",
            "subject": it.draft.subject if it.draft else "", "email": it.sent_email}


def due_followups(store, items: list, day: date, cfg: dict, facts: Facts, dnc=None,
                  cap: int | None = None, manual: bool = False) -> tuple[list[FollowUp], dict[str, int]]:
    """Follow-ups to plan on `day`, oldest original first, at most `cap`; plus counts of why others aren't.
    `manual`: a sent email with no Gmail record still counts (manual_original)."""
    blocked = {it.company_id for it in items if it.outcome in HUMAN_OUTCOMES}
    done = {r["target_id"] for r in store.gmail_messages() if r["row_type"] == "followup"}
    done |= {e["target_id"] for e in store.events() if e["event"] == "followup"}
    done |= {r["target_id"] for r in store.plan_rows() if r["row_type"] == "followup"
             and r["status"] in ("sent", "sending", "dropped")}
    waiting = awaiting_retry(store)
    stopped = stopped_targets(store)
    due, why_not = [], {}
    lo, hi = cfg["followup_words"]
    for it in items:
        if it.status != "sent":
            continue
        original = original_message(store, it)
        if original is None and manual:
            original = manual_original(it)
        when, why = followup_status(store, it, day, cfg, dnc, blocked, original, done, waiting, stopped)
        if why:
            key = "not due yet" if why.startswith("due ") else why
            why_not[key] = why_not.get(key, 0) + 1
            continue
        fu = FollowUp(item=it, original=original, due=when)
        fu.body, fu.variant = build_followup(it.target_id, it.inputs.greeting, it.inputs.company,
                                             it.inputs.role_title if it.kind == "role" else "",
                                             it.draft.body if it.draft else "", facts, lo, hi,
                                             sent=original_day(original, it), day=day)
        fu.subject = follow_subject(original["subject"] or (it.draft.subject if it.draft else ""))
        fu.failures = validate_followup(fu.subject, fu.body, original["subject"] or "", it.inputs.greeting, facts,
                                        False, lo, hi, sent=original_day(original, it), day=day)
        if fu.failures:
            why_not["follow-up text fails validation"] = why_not.get("follow-up text fails validation", 0) + 1
            continue
        due.append(fu)
    due.sort(key=lambda f: (original_day(f.original, f.item) or day, f.original["sent_at"], f.item.priority,
                            f.item.company.lower()))
    if cap is not None and len(due) > cap:
        why_not["over the daily follow-up cap (next day)"] = len(due) - cap
        due = due[:cap]
    return due, why_not
