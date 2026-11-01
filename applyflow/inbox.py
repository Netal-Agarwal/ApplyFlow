"""Phase 3: reading Gmail (read-only) - check-inbox and reconcile.

check-inbox looks at every message since the last check (not sent by you) and sorts it:
  hard_bounce    a DSN with a 5.x.x status (or "address not found" wording) -> bounced + stopped (mark-bounced --stop)
  soft_bounce    a DSN with a 4.x.x status or a delay notice -> the original is sent once more the next working day
  bounce_unclear a DSN with no readable status -> listed in the digest, nothing recorded
  auto_reply     Auto-Submitted / X-Autoreply / Precedence headers, no-reply senders, automated wording -> auto_ack;
                 "a person will get back to you" (and no ATS signs) keeps the follow-up
  human_reply    anyone else, in one of our threads or from a contacted company's domain -> needs_action (follow-ups
                 stop for the whole company; the digest lists it)
  drive_request  a Google Drive access request (the resume link) -> listed in the digest
  other          not about outreach (newsletters ...): ignored
Nothing is ever replied to automatically.

reconcile finds the batches you sent by hand in your Sent folder (by recipient and subject), records each Gmail
message / thread id and real send time, marks unmarked rows sent and lists the rows it can't find.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone

from applyflow.contacts import FREEMAIL
from applyflow.gmail import GmailError, Mail, header, parse_mail, thread_link

IST = timezone(timedelta(hours=5, minutes=30))

_DSN_FROM = re.compile(r"^(?:mailer-daemon|postmaster|mail-daemon|mailerdaemon|mdaemon)@", re.I)
_DSN_SUBJECT = re.compile(r"delivery status notification|undeliver|delivery (?:has )?failed|mail delivery|failure "
                          r"notice|returned mail|delivery failure|non-?delivery|could not be delivered|"
                          r"message not delivered", re.I)
_HARD_WORDS = re.compile(r"address not found|does ?n[o']t exist|user unknown|no such user|unknown user|mailbox "
                         r"(?:unavailable|not found|does not exist)|recipient (?:address )?rejected|"
                         r"(?:wasn't|was not|couldn't|could not) be delivered|failed permanently|permanent(?:ly)? "
                         r"fail|invalid recipient|account (?:is )?disabled", re.I)
_SOFT_WORDS = re.compile(r"\bdelay(?:ed)?\b|temporar(?:y|ily)|will (?:keep )?(?:retry|trying)|not (?:been )?delivered "
                         r"yet|mailbox (?:is )?full|over quota", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_NOREPLY = re.compile(r"^(?:no-?reply|do-?not-?reply|donotreply|notifications?|auto-?reply|autoresponder|"
                      r"mailer|bounce[s]?|careers-noreply|jobs-noreply|talent-noreply)\b", re.I)
_AUTO_SUBJECT = re.compile(r"^(?:automatic reply|auto(?:matic)?[- ]?(?:reply|response)|autoreply|out of (?:the )?office|"
                           r"ooo\b|away from|thank(?:s| you) for (?:your|applying|contacting|reaching|writing)|"
                           r"we(?:'ve| have) received|received:|acknowledg|application (?:received|submitted))", re.I)
_AUTO_BODY = re.compile(r"this is an? auto(?:mated|matic)|auto-?generated|automated (?:response|reply|message)|"
                        r"(?:please )?do not reply to this|this (?:mail ?box|inbox|address) is not monitored|"
                        r"we have received your (?:application|resume|cv|email|message|mail)|"
                        r"your application (?:has been|was) (?:received|submitted)|out of (?:the )?office", re.I)
_PERSON_WILL = re.compile(r"\b(?:will|shall|'ll)\s+(?:get back|respond|reply|revert|reach out|be in touch|contact you|"
                          r"connect with you|look into)|one of (?:our|the) (?:team|recruiters|hr|colleagues)|"
                          r"someone from (?:our|the) team", re.I)
_ATS_SIGNS = re.compile(r"\bif (?:your|the) (?:profile|skills|experience|background|application|candidature|"
                        r"qualifications)\b[^.]{0,80}\b(?:match|suit|fit|meet|align|shortlist)|only shortlisted|"
                        r"applicant tracking|candidate portal|careers portal|track (?:the status of )?your application|"
                        r"application (?:id|number|reference)|do not reply|not monitored", re.I)
_DRIVE_FROM = re.compile(r"^drive-shares-(?:dm-)?noreply@google\.com$", re.I)
_DRIVE_SUBJECT = re.compile(r"share request|requests? (?:to )?access|requesting access|access request", re.I)


@dataclass
class Classified:
    category: str
    recipients: list[str] = field(default_factory=list)      # bounced addresses (DSNs)
    status: str = ""                                          # DSN status code (5.1.1 ...)
    person_will_respond: bool = False
    detail: str = ""


def dsn_status(text: str, subject: str = "") -> tuple[str, str]:
    """('hard' | 'soft' | '', status code) from a DSN's text."""
    m = re.search(r"^\s*Status:\s*([245]\.\d{1,3}\.\d{1,3})", text, re.M | re.I)
    action = re.search(r"^\s*Action:\s*(\w+)", text, re.M | re.I)
    if re.search(r"\(delay\)|delayed", subject, re.I) or (action and action.group(1).lower() == "delayed"):
        return "soft", m.group(1) if m else ""
    if m is None:
        m = re.search(r"\b([45]\.\d{1,3}\.\d{1,3})\b", text)
    if m:
        return ("hard" if m.group(1).startswith("5") else "soft" if m.group(1).startswith("4") else ""), m.group(1)
    smtp = re.search(r"\b([45])\d\d[ -]", text)
    if _HARD_WORDS.search(text):
        return "hard", smtp.group(0).strip() if smtp else ""
    if _SOFT_WORDS.search(text) or (smtp and smtp.group(1) == "4"):
        return "soft", smtp.group(0).strip() if smtp else ""
    if smtp and smtp.group(1) == "5":
        return "hard", smtp.group(0).strip()
    return "", ""


def failed_recipients(mail: Mail) -> list[str]:
    out = [a.strip().lower() for a in mail.headers.get("x-failed-recipients", "").split(",") if "@" in a]
    for rx in (r"^\s*Final-Recipient:\s*rfc822;\s*<?([^\s>]+@[^\s>]+)>?", r"^\s*Original-Recipient:\s*rfc822;\s*<?([^\s>]+@[^\s>]+)>?"):
        out += [m.lower() for m in re.findall(rx, mail.text, re.M | re.I)]
    return list(dict.fromkeys(out))


def is_dsn(mail: Mail) -> bool:
    return bool(_DSN_FROM.match(mail.from_email) or "message/delivery-status" in mail.content_types
                or (_DSN_SUBJECT.search(mail.subject) and re.search(r"mail delivery|postmaster|daemon", mail.sender, re.I)))


def auto_signals(mail: Mail) -> list[str]:
    h = mail.headers
    out = []
    auto = h.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        out.append(f"Auto-Submitted: {auto}")
    for name in ("x-autoreply", "x-autorespond", "x-auto-response"):
        if name in h:
            out.append(f"{name}: {h[name]}")
    if h.get("precedence", "").strip().lower() in ("auto_reply", "bulk", "junk"):
        out.append(f"Precedence: {h['precedence']}")
    if _NOREPLY.match(mail.from_email):
        out.append(f"no-reply sender ({mail.from_email})")
    if _AUTO_SUBJECT.search(mail.subject.strip()):
        out.append(f"subject: {mail.subject.strip()[:60]}")
    m = _AUTO_BODY.search(mail.text)
    if m:
        out.append(f"wording: {m.group(0)}")
    return out


def _mailing(mail: Mail) -> bool:
    """A newsletter or mailing-list message (a company's product update after we wrote to it): not an answer."""
    return "list-unsubscribe" in mail.headers or "list-id" in mail.headers


def classify(mail: Mail) -> Classified:
    """What a message is (see the module docstring). Human vs other is decided by the caller (thread / domain)."""
    if _DRIVE_FROM.match(mail.from_email) or (_DRIVE_SUBJECT.search(mail.subject) and "google.com" in mail.from_email):
        who = mail.headers.get("reply-to", "") or mail.snippet[:120]
        return Classified("drive_request", detail=f"{mail.subject.strip()} ({who})")
    if is_dsn(mail):
        kind, code = dsn_status(mail.text, mail.subject)
        cat = {"hard": "hard_bounce", "soft": "soft_bounce"}.get(kind, "bounce_unclear")
        return Classified(cat, failed_recipients(mail), code, detail=code or "no status code")
    signals = auto_signals(mail)
    # a reply that only thanks you ("Thanks for reaching out, we have no openings") is a person, not an auto-reply:
    # a subject alone needs another sign, unless it says Automatic reply / Out of office
    strong = [s for s in signals if not s.startswith("subject:")]
    subject_only = signals and not strong and not re.match(r"(?i)(?:automatic reply|auto|out of|ooo)", mail.subject.strip())
    if signals and not subject_only:
        person = bool(_PERSON_WILL.search(mail.text)) and not _ATS_SIGNS.search(mail.text) \
            and not _NOREPLY.match(mail.from_email)
        return Classified("auto_reply", person_will_respond=person,
                          detail=("a person will respond; " if person else "") + "; ".join(signals))
    return Classified("human_reply", detail=" ".join(mail.text.split())[:200])


# ---------------------------------------------------------------------------
# check-inbox
# ---------------------------------------------------------------------------

@dataclass
class InboxResult:
    looked_at: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)
    needs_action: list[str] = field(default_factory=list)
    brake: str = ""


def ist_day(when: datetime) -> date:
    return when.astimezone(IST).date()


class _Index:
    """Which target a message belongs to: by thread, by a sent address, or by the sent address's domain."""

    def __init__(self, store):
        self.thread = {r["thread_id"]: (r["target_id"], r["email"]) for r in store.gmail_messages()}
        self.sent: dict[str, tuple[str, str]] = {}
        self.domains: dict[str, set[str]] = {}
        for e in store.events():
            if e["event"] == "sent":
                self.sent[e["email"]] = (e["target_id"], e["company_key"] or "")
                self.domains.setdefault(e["email"].split("@")[-1], set()).add(e["target_id"])
        self.keys = {t: k for t, k in self.sent.values()}

    def by_thread(self, thread_id: str) -> tuple[str, str] | None:
        return self.thread.get(thread_id)

    def by_domain(self, email: str) -> str:
        """The one target sent to on this domain (or a parent domain: careers.robo.example -> robo.example)."""
        domain = email.split("@")[-1].lower()
        if domain in FREEMAIL:
            return ""
        labels = domain.split(".")
        for i in range(len(labels) - 1):
            hits = self.domains.get(".".join(labels[i:]), set())
            if hits:
                return next(iter(hits)) if len(hits) == 1 else ""
        return ""


def check_inbox(store, profile, gmail, now: datetime, since: datetime | None = None, dnc=None, log=print,
                me: str = "") -> InboxResult:
    """Read every message since the last check (or `since`) and apply what it means (module docstring)."""
    from applyflow.outreach import NEEDS_ACTION, refresh_drafts
    res = InboxResult()
    if since is None:
        last = store.get_state("inbox_last_check")
        since = datetime.fromisoformat(last) if last else now - timedelta(days=3)
    me = (me or gmail.address() or "").lower()
    query = f"after:{int(since.timestamp())} -in:sent -in:drafts -in:chats"
    ids = [m["id"] for m in gmail.list_ids(query, limit=1000)]
    seen = store.seen_inbox_ids()
    idx = _Index(store)
    events = store.events()
    bounced = {(e["target_id"], e["email"]) for e in events if e["event"] == "bounced"}
    replied: dict[str, list[str]] = {}
    for e in events:
        if e["event"] == "replied":
            replied.setdefault(e["target_id"], []).append(e["outcome"] or "")
    changed = False
    for gid in ids:
        if gid in seen:
            continue
        mail = parse_mail(gmail.get(gid, "full"))
        if mail.from_email == me or "SENT" in mail.labels:
            continue
        res.looked_at += 1
        c = classify(mail)
        day = ist_day(mail.received_at).isoformat()
        row = {"gmail_id": mail.gmail_id, "thread_id": mail.thread_id, "received_at": mail.received_at.isoformat(),
               "sender": mail.sender[:200], "subject": mail.subject[:200], "category": c.category, "target_id": "",
               "email": "", "detail": c.detail[:500], "applied": ""}
        hit = idx.by_thread(mail.thread_id)
        if c.category in ("hard_bounce", "soft_bounce", "bounce_unclear"):
            addrs = [a for a in c.recipients if a in idx.sent] or [a for a in _EMAIL.findall(mail.text.lower())
                                                                  if a in idx.sent]
            if not addrs and hit:
                addrs = [hit[1]]
            if addrs:
                tid, key = idx.sent.get(addrs[0], (hit[0] if hit else "", ""))
                row["target_id"], row["email"] = tid, addrs[0]
                if c.category == "hard_bounce":
                    if (tid, addrs[0]) in bounced:
                        row["applied"] = "bounce already recorded"
                    else:
                        store.add_event(tid, key or idx.keys.get(tid, ""), addrs[0], "bounced", day)
                        store.add_event(tid, key or idx.keys.get(tid, ""), addrs[0], "stopped", day)
                        bounced.add((tid, addrs[0]))
                        row["applied"] = f"bounced + stopped ({c.status or 'hard'})"
                        changed = True
                elif c.category == "soft_bounce":
                    row["applied"] = "retry once on the next working day"
            else:
                row["category"] = "bounce_unclear" if c.category != "soft_bounce" else c.category
                row["applied"] = "no sent address of ours in it"
        elif c.category == "drive_request":
            row["applied"] = "listed in the digest"
        else:
            tid = hit[0] if hit else idx.by_domain(mail.from_email)
            if tid and not hit and _mailing(mail):
                tid = ""                   # the company's newsletter or a notification, not an answer to us
            if not tid:
                row["category"] = "other"
            else:
                row["target_id"] = tid
                row["email"] = next((e for e, (t, _) in idx.sent.items() if t == tid), "")
                key = idx.keys.get(tid, "")
                earlier = replied.get(tid, [])
                if c.category == "auto_reply":
                    if earlier:
                        row["applied"] = f"reply already recorded ({earlier[-1]})"
                    else:
                        store.add_event(tid, key, row["email"], "replied", day, outcome="auto_ack",
                                        keep_followup=c.person_will_respond)
                        replied.setdefault(tid, []).append("auto_ack")
                        row["applied"] = "auto_ack" + (" (a person will respond: follow-up kept)"
                                                       if c.person_will_respond else " (no follow-up)")
                        changed = True
                else:
                    who = f"{mail.from_name or mail.from_email.split('@')[0]} <{mail.from_email}>"
                    if earlier and earlier[-1] in ("interested", "declined_warm", "declined"):
                        row["applied"] = f"outcome already recorded ({earlier[-1]})"
                    elif earlier and earlier[-1] == NEEDS_ACTION:
                        row["applied"] = "needs action (another message)"
                    else:
                        store.add_event(tid, key, row["email"], "replied", day, outcome=NEEDS_ACTION, note=who)
                        replied.setdefault(tid, []).append(NEEDS_ACTION)
                        sent_domain = row["email"].split("@")[-1]
                        if mail.from_email.split("@")[-1] == sent_domain and sent_domain not in FREEMAIL \
                                and mail.from_email != row["email"]:
                            company = store.conn.execute("SELECT company FROM contact_checks WHERE company_key = ?",
                                                         (key,)).fetchone()
                            store.add_reply_contact(key, company[0] if company else "", mail.from_name or "", "",
                                                    mail.from_email, f"reply to {row['email']}")
                        row["applied"] = "needs action: follow-ups stopped for the company"
                        changed = True
                    res.needs_action.append(f"{who} re {mail.subject[:60]} - {thread_link(mail.thread_id)}")
        store.save_inbox_message(row)
        res.by_category[row["category"]] = res.by_category.get(row["category"], 0) + 1
        if row["applied"]:
            res.applied.append(f"{row['category']}: {row['target_id'] or '-'} {row['email']} - {row['applied']}")
    if changed:
        refresh_drafts(store, profile, dnc)
    today = ist_day(now)
    limit = int(profile.outreach["sending"]["bounce_brake"])
    hard_today = [r for r in store.inbox_messages(category="hard_bounce")
                  if r["received_at"] and ist_day(datetime.fromisoformat(r["received_at"])) == today]
    if len(hard_today) >= limit:
        res.brake = f"{len(hard_today)} hard bounces today (brake at {limit}): sending stopped for {today.isoformat()}"
        store.add_brake(today.isoformat(), "bounces", res.brake)
    store.set_state("inbox_last_check", now.isoformat())
    return res


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

@dataclass
class ReconcileResult:
    found: list[str] = field(default_factory=list)
    marked: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    inbox: InboxResult | None = None


def _norm(subject: str) -> str:
    return " ".join(re.sub(r"(?i)^\s*(?:\[test\]\s*)?", "", subject or "").split()).lower()


def reconcile(store, profile, gmail, start: date, end: date, dnc=None, release: bool = False, log=print,
              now: datetime | None = None, scan_inbox: bool = True) -> ReconcileResult:
    """Match every row of the stored batches from `start` to `end` (sent by hand) to a message in your Sent
    folder, by recipient and subject; then scan the inbox once from `start` (check_inbox)."""
    from applyflow.outreach import build_items, refresh_drafts
    res = ReconcileResult()
    items = {it.target_id: it for it in build_items(store, profile, dnc)}
    drafts = {r["target_id"]: r for r in store.drafts()}
    sent_ev: dict[str, object] = {}
    for e in store.events():
        if e["event"] == "sent":
            sent_ev[e["target_id"]] = e
    known = {r["target_id"] for r in store.gmail_messages() if r["row_type"] == "new"}
    rows = store.conn.execute("SELECT * FROM send_queue WHERE batch_date BETWEEN ? AND ? ORDER BY batch_date, "
                              "position", (start.isoformat(), end.isoformat())).fetchall()
    changed = False
    for r in rows:
        tid, batch = r["target_id"], r["batch_date"]
        if tid in known:
            continue
        ev = sent_ev.get(tid)
        email = (ev["email"] if ev is not None else r["email"]).lower()
        subject = drafts[tid]["subject"] if tid in drafts else ""
        bd = date.fromisoformat(batch)
        q = f"in:sent to:{email} after:{(bd - timedelta(days=2)):%Y/%m/%d} before:{(bd + timedelta(days=12)):%Y/%m/%d}"
        cands = []
        for m in gmail.list_ids(q, limit=20):
            meta = gmail.get(m["id"], "metadata", ("Subject", "To", "Message-ID", "Date"))
            if email in header(meta, "To").lower():
                cands.append(meta)
        exact = [m for m in cands if _norm(header(m, "Subject")) == _norm(subject)]
        pick, note = (min(exact, key=lambda m: int(m.get("internalDate") or 0)), "") if exact else (None, "")
        if pick is None and len(cands) == 1 and not header(cands[0], "Subject").lower().startswith("re:"):
            pick, note = cands[0], f"subject differs: {header(cands[0], 'Subject')!r}"
        label = f"{batch} {tid} -> {email}"
        if pick is None:
            res.missing.append(label + (f" ({len(cands)} message(s) to it, none matching)" if cands else ""))
            if release and ev is None:
                store.remove_from_queue(batch, tid)
                res.released.append(label)
            continue
        when = datetime.fromtimestamp(int(pick.get("internalDate") or 0) / 1000, tz=timezone.utc)
        store.save_gmail_message(pick["id"], pick.get("threadId", ""), tid, email, "new", header(pick, "Subject"),
                                 when.isoformat(), batch, "reconcile", header(pick, "Message-ID"))
        res.found.append(label + f" at {when.astimezone(IST):%Y-%m-%d %H:%M} IST" + (f" ({note})" if note else ""))
        if ev is None:
            it = items.get(tid)
            kind = it.address.kind if it is not None and it.address and it.address.email == email else ""
            store.add_event(tid, it.company_key if it else "", email, "sent", ist_day(when).isoformat(), batch,
                            r["send_tier"], kind)
            res.marked.append(label + f" (sent {ist_day(when).isoformat()})")
            changed = True
        elif ev["event_date"] != ist_day(when).isoformat():
            res.notes.append(f"{label}: marked sent {ev['event_date']}, Gmail says {ist_day(when).isoformat()} "
                             "(kept as marked)")
    if changed:
        refresh_drafts(store, profile, dnc)
    if scan_inbox:
        since = datetime.combine(start, dtime(0, 0), tzinfo=IST)
        res.inbox = check_inbox(store, profile, gmail, now or datetime.now(IST), since=since, dnc=dnc, log=log)
    return res
