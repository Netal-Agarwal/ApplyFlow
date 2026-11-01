"""Phase 3: the sending engine - plan, send, review, brakes, digest and summary.

  plan    the next working day's rows: `new_per_day` new emails in the queue order (caps, per-type gate, freshness
          check: `make_queue`), rows deferred from an earlier day first; follow-ups due (oldest first, up to
          `followups_per_day`); soft-bounce retries. Short of new rows -> "short by N". Writes the review page and
          the digest text.
  send    today's planned rows, one at a time with random 2-4 minute gaps from 09:00 IST, aiming to finish by 10:30;
          nothing after 10:45 on an on-time start. A late start (laptop asleep) may send until 13:00; what's left
          moves to the next working day. Each row is marked `sending` before the API call and `sent` (with its
          Gmail ids) right after it, under a lock file, so no row goes out twice - a row left `sending` by a crash
          is looked up in the Sent folder before anything else happens.
  brakes  config/private/PAUSE (pause / resume); auth errors (until gmail-login), 403 / abuse / quota errors and
          `bounce_brake` hard bounces in a day (that day). A hard ceiling of `daily_ceiling` sends a day.
"""

from __future__ import annotations

import html as htmllib
import os
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

from applyflow import config
from applyflow.followups import FollowUp, due_followups, follow_subject, is_working_day, next_working_day
from applyflow.gmail import (GmailAuthError, GmailBrakeError, GmailError, GmailTransientError, Outgoing, header,
                             thread_link)
from applyflow.inbox import IST, ist_day

Log = Callable[[str], None]
PAUSE_PATH = config.PRIVATE_DIR / "PAUSE"
LOCK_PATH = config.ROOT / "data" / "send.lock"
REVIEW_DIR = config.OUTPUT_DIR / "review"
DIGEST_DIR = config.OUTPUT_DIR / "digest"


def now_ist() -> datetime:
    return datetime.now(IST)


def _hm(text: str) -> dtime:
    h, m = str(text).split(":")
    return dtime(int(h), int(m))


# ---------------------------------------------------------------------------
# The sending window
# ---------------------------------------------------------------------------

@dataclass
class SendWindow:
    start: dtime
    end: dtime                 # gaps aim to finish by here
    hard_stop: dtime           # on-time start: nothing after this
    on_time_until: dtime       # a run starting later is a late start
    late_until: dtime          # late start: nothing after this
    gap: tuple[float, float]   # minutes

    @classmethod
    def from_cfg(cls, cfg: dict) -> "SendWindow":
        lo, hi = cfg["gap_minutes"]
        return cls(_hm(cfg["window_start"]), _hm(cfg["window_end"]), _hm(cfg["hard_stop"]), _hm(cfg["on_time_until"]),
                   _hm(cfg["late_until"]), (float(lo), float(hi)))

    def _at(self, ref: datetime, t: dtime) -> datetime:
        return datetime.combine(ref.date(), t, tzinfo=ref.tzinfo)

    def late(self, run_start: datetime) -> bool:
        return run_start > self._at(run_start, self.on_time_until)

    def deadline(self, run_start: datetime) -> datetime | None:
        """When sending must stop for a run that started at `run_start`; None = too late to send at all today."""
        if not self.late(run_start):
            return self._at(run_start, self.hard_stop)
        late = self._at(run_start, self.late_until)
        return late if run_start < late else None

    def opens(self, run_start: datetime) -> datetime:
        return max(run_start, self._at(run_start, self.start))

    def aim(self, run_start: datetime) -> datetime:
        if not self.late(run_start):
            return self._at(run_start, self.end)
        return self._at(run_start, self.late_until) - timedelta(minutes=15)

    def next_gap(self, now: datetime, remaining: int, run_start: datetime, rng: random.Random) -> float:
        """Seconds to wait before the next send: random within the gap range, shortened (never below its minimum)
        so the rest of the batch can finish by the aim time."""
        lo, hi = self.gap
        left = (self.aim(run_start) - now).total_seconds() / 60
        fit = left / remaining if remaining else hi
        top = max(lo, min(hi, fit))
        return rng.uniform(lo, top) * 60


# ---------------------------------------------------------------------------
# Brakes and the lock
# ---------------------------------------------------------------------------

def brake_reasons(store, day: date, pause_path: Path = PAUSE_PATH) -> list[str]:
    out = []
    if pause_path.exists():
        out.append(f"paused by hand ({pause_path.name} flag; `resume` to continue)")
    for b in store.brakes(day.isoformat()):
        if b["kind"] == "auth":
            out.append(f"Gmail login needed: run gmail-login ({b['reason']})")
        else:
            out.append(f"{b['kind']} brake for {b['day']}: {b['reason']}")
    return out


def trigger_brake(store, day: date, kind: str, reason: str, log: Log = print) -> None:
    store.add_brake(day.isoformat(), kind, reason)
    log(f"BRAKE ({kind}): {reason}" + (" -> run gmail-login" if kind == "auth" else ""))


def pause(pause_path: Path = PAUSE_PATH) -> None:
    pause_path.parent.mkdir(parents=True, exist_ok=True)
    pause_path.write_text(f"paused {now_ist().isoformat(timespec='seconds')}\n", encoding="utf-8")


def resume(store, pause_path: Path = PAUSE_PATH) -> list[str]:
    """Remove the PAUSE flag and today's abuse / bounce brakes (an auth brake clears with gmail-login)."""
    done = []
    if pause_path.exists():
        pause_path.unlink()
        done.append("PAUSE flag removed")
    for kind in ("abuse", "bounces"):
        if any(b["kind"] == kind for b in store.brakes(now_ist().date().isoformat())):
            store.clear_brakes(kind)
            done.append(f"{kind} brake cleared")
    return done


class LockBusy(RuntimeError):
    pass


class SendLock:
    """A lock file (O_EXCL) so two `send` runs never overlap; one older than `stale_hours` is a crashed run's."""

    def __init__(self, path: Path = LOCK_PATH, stale_hours: float = 5.0):
        self.path, self.stale = path, stale_hours * 3600

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time():.0f}\n".encode())
                os.close(fd)
                return self
            except FileExistsError:
                age = time.time() - self.path.stat().st_mtime
                if age < self.stale:
                    raise LockBusy(f"another send run holds {self.path} (started {age / 60:.0f} min ago)")
                self.path.unlink(missing_ok=True)
        raise LockBusy(f"couldn't take {self.path}")

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def resume_pdf(cfg: dict) -> Path | None:
    if cfg.get("resume_pdf"):
        p = Path(cfg["resume_pdf"])
        return p if p.is_absolute() else config.ROOT / p
    found = sorted(config.PRIVATE_DIR.glob("*[Rr]esume*.pdf"))
    return found[0] if len(found) == 1 else None


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

@dataclass
class PlanResult:
    day: str
    new: list = field(default_factory=list)            # OutreachItem
    followups: list[FollowUp] = field(default_factory=list)
    retries: list = field(default_factory=list)        # OutreachItem
    short: int = 0
    held: bool = False
    carried: list[str] = field(default_factory=list)
    already_sent: int = 0
    followup_skips: dict[str, int] = field(default_factory=dict)
    batch: object = None
    review_path: Path | None = None
    digest_path: Path | None = None


def due_retries(store, items: list, day: date) -> list:
    """Soft-bounced originals to send once more: the bounce arrived before `day` (next working day or later), the
    target is still sent and unanswered, no hard bounce since, and no retry yet."""
    by_id = {it.target_id: it for it in items}
    retried = {r["target_id"] for r in store.gmail_messages() if r["row_type"] == "retry"}
    retried |= {r["target_id"] for r in store.plan_rows() if r["row_type"] == "retry" and r["status"] in
                ("sent", "sending", "dropped")}
    out = []
    for m in store.inbox_messages(category="soft_bounce"):
        it = by_id.get(m["target_id"] or "")
        if it is None or it.target_id in retried:
            continue
        when = ist_day(datetime.fromisoformat(m["received_at"]))
        if day < next_working_day(when) or it.status != "sent" or it.sent_email != (m["email"] or "") or it.outcome:
            continue
        retried.add(it.target_id)
        out.append(it)
    return out


def plan_day(store, profile, day: date, fresh=None, dnc=None, log: Log = print) -> PlanResult:
    from applyflow.drafting import load_facts
    from applyflow.outreach import build_items, make_queue
    cfg = profile.outreach["sending"]
    res = PlanResult(day=day.isoformat())
    if day.isoformat() in store.held_days():
        res.held = True
        store.save_plan(day.isoformat(), [])
        log(f"{day.isoformat()} is held: nothing planned.")
        _write_outputs(store, profile, res, dnc)
        return res
    earlier = [r for r in store.plan_rows() if r["row_type"] == "new" and r["batch_date"] < day.isoformat()
               and r["status"] in ("planned", "deferred", "held")]
    for r in earlier:
        store.remove_from_queue(r["batch_date"], r["target_id"])
    dropped = {r["target_id"] for r in store.plan_rows(day.isoformat()) if r["status"] == "dropped"}
    batch = make_queue(store, profile, day, int(cfg["new_per_day"]), dnc, fresh=fresh,
                       carry=[r["target_id"] for r in earlier], skip=dropped)
    res.batch = batch
    res.new = [it for it in batch.items if it.status == "ready"]
    res.already_sent = len(batch.items) - len(res.new)
    res.short = max(0, int(cfg["new_per_day"]) - len(batch.items))
    new_ids = {it.target_id for it in res.new}
    for r in earlier:
        moved = r["target_id"] in new_ids
        store.set_plan_status(r["batch_date"], r["target_id"], "new", "moved" if moved else "dropped",
                              error=f"moved to {day.isoformat()}" if moved else
                              f"not sendable when {day.isoformat()} was planned")
        if moved:
            res.carried.append(r["target_id"])
    items = build_items(store, profile, dnc, day=day)
    res.retries = due_retries(store, items, day)
    room = max(0, int(cfg["daily_ceiling"]) - len(batch.items) - len(res.retries))
    res.followups, res.followup_skips = due_followups(store, items, day, cfg, load_facts(profile), dnc,
                                                      cap=min(int(cfg["followups_per_day"]), room))
    rows = [(it.target_id, it.address.email, "new") for it in res.new]
    rows += [(it.target_id, it.sent_email, "retry") for it in res.retries]
    rows += [(f.item.target_id, f.item.sent_email, "followup") for f in res.followups]
    store.save_plan(day.isoformat(), rows)
    _write_outputs(store, profile, res, dnc)
    return res


# ---------------------------------------------------------------------------
# review: drop / hold
# ---------------------------------------------------------------------------

def upcoming_day(store, today: date, after: bool = False) -> str:
    """The next day (today included, unless `after`) with planned rows, else the next working day."""
    t = today.isoformat()
    days = sorted({r["batch_date"] for r in store.plan_rows(status="planned") if r["batch_date"] > t
                   or (r["batch_date"] == t and not after)})
    return days[0] if days else next_working_day(today).isoformat()


def drop(store, ref: str, day: str) -> str:
    """Drop one planned row (its position, address or target id) from a day's plan. A new row's address is
    excluded for good (excluded_contacts); a dropped follow-up / retry is never planned again."""
    rows = [r for r in store.plan_rows(day) if r["status"] in ("planned", "deferred")]
    ref_l = ref.strip().lower()
    hits = [r for r in rows if str(r["position"]) == ref_l or r["email"] == ref_l or r["target_id"] == ref_l]
    if len(hits) != 1:
        raise ValueError(f"{len(hits) or 'no'} planned row(s) on {day} match {ref!r}")
    r = hits[0]
    store.set_plan_status(day, r["target_id"], r["row_type"], "dropped", error="dropped in review")
    if r["row_type"] == "new":
        store.remove_from_queue(day, r["target_id"])
        store.exclude_contact(r["email"], f"dropped in review of the {day} plan")
    return f"dropped {r['row_type']} row {r['position']}: {r['target_id']} -> {r['email']}"


def hold(store, day: str, undo: bool = False) -> str:
    if undo:
        store.set_hold(day, False)
        return f"{day} is no longer held; run `plan --date {day}` to plan it."
    store.set_hold(day, True)
    n = 0
    for r in store.plan_rows(day):
        if r["status"] == "planned":
            store.set_plan_status(day, r["target_id"], r["row_type"], "held", error="day held")
            if r["row_type"] == "new":
                store.remove_from_queue(day, r["target_id"])
            n += 1
    return f"{day} held: nothing is sent that day ({n} planned row(s) released; new ones go into the next plan)."


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

@dataclass
class SendResult:
    sent: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    stopped: str = ""
    would: list[str] = field(default_factory=list)       # dry run


@dataclass
class Prepared:
    out: Outgoing | None
    why_not: str = ""


def _sends_on(store, day: date) -> int:
    return sum(1 for r in store.gmail_messages() if ist_day(datetime.fromisoformat(r["sent_at"])) == day)


def prepare(store, profile, row, items: dict, facts, me: str, dnc=None, to_self: bool = False,
            fresh=None) -> Prepared:
    """The email for a plan row, or why it can't go out now."""
    from applyflow.followups import (awaiting_retry, build_followup, followup_status, original_day, original_message,
                                     validate_followup)
    cfg = profile.outreach["sending"]
    it = items.get(row["target_id"])
    name = (profile.candidate or {}).get("name") or facts.name
    if it is None:
        return Prepared(None, "target no longer exists")
    to = me if to_self else row["email"]
    test = "[TEST] " if to_self else ""
    if row["row_type"] == "followup":
        original = original_message(store, it)
        blocked = {i.company_id for i in items.values() if i.outcome in ("interested", "declined_warm", "declined",
                                                                         "needs_action")}
        _, why = followup_status(store, it, date.fromisoformat(row["batch_date"]), cfg, dnc, blocked, original,
                                 waiting=awaiting_retry(store))
        if why:
            return Prepared(None, f"follow-up no longer due: {why}")
        lo, hi = cfg["followup_words"]
        sent, today = original_day(original, it), date.fromisoformat(row["batch_date"])
        body, _ = build_followup(it.target_id, it.inputs.greeting, it.inputs.company,
                                 it.inputs.role_title if it.kind == "role" else "", it.draft.body, facts, lo, hi,
                                 sent=sent, day=today)
        subject = follow_subject(original["subject"] or it.draft.subject)
        fails = validate_followup(subject, body, original["subject"] or "", it.inputs.greeting, facts, False, lo, hi,
                                  sent=sent, day=today)
        if fails:
            return Prepared(None, "follow-up fails validation: " + "; ".join(fails))
        rfc = original["rfc_id"] or ""
        return Prepared(Outgoing(to=to, subject=test + subject, body=body, from_name=name, from_email=me,
                                 in_reply_to=rfc, references=rfc, thread_id="" if to_self else original["thread_id"]))
    if row["row_type"] == "new":
        if it.status != "ready" or it.address is None:
            return Prepared(None, f"no longer ready ({it.status})")
        if it.address.email != row["email"]:
            return Prepared(None, f"its address changed to {it.address.email}")
    elif it.status != "sent" or it.sent_email != row["email"] or it.outcome:
        return Prepared(None, "retry no longer needed (replied, bounced or moved on)")
    if dnc is not None and dnc.blocks(row["email"]):
        return Prepared(None, "on the do-not-contact list")
    d = it.draft
    if d is None or d.failures or (profile.outreach.get("require_written_draft") and d.source != "claude"):
        return Prepared(None, "no validated draft")
    attach = d.resume_action == "attach"
    if attach and facts.resume_url and facts.resume_url in d.body:
        return Prepared(None, "draft has the resume link and would attach the PDF too (never both)")
    if not attach and "attached" in d.body.lower():
        return Prepared(None, "draft says 'attached' but its resume mode is link")
    pdf = resume_pdf(cfg) if attach else None
    if attach and (pdf is None or not pdf.exists()):
        return Prepared(None, "resume PDF not found in config/private/ (set outreach.sending.resume_pdf)")
    if fresh is not None and row["row_type"] == "new" and not to_self and not fresh.check(it):
        return Prepared(None, "address no longer on its source page (freshness check)")
    return Prepared(Outgoing(to=to, subject=test + d.subject, body=d.body, from_name=name, from_email=me,
                             attachment=pdf))


def _recover(store, gmail, row, subject_of: Callable[[object], str], log: Log) -> bool:
    """A row left 'sending' (crash mid-call): did it go out? Looks in the Sent folder; True = recorded as sent."""
    day = date.fromisoformat(row["batch_date"])
    q = f"in:sent to:{row['email']} after:{(day - timedelta(days=1)):%Y/%m/%d}"
    want = " ".join((subject_of(row) or "").split()).lower()
    for m in gmail.list_ids(q, limit=10):
        meta = gmail.get(m["id"], "metadata", ("Subject", "To", "Message-ID"))
        if " ".join(header(meta, "Subject").split()).lower() == want:
            when = datetime.fromtimestamp(int(meta.get("internalDate") or 0) / 1000, tz=IST)
            _record(store, row, {"id": meta["id"], "threadId": meta.get("threadId", "")}, header(meta, "Subject"),
                    header(meta, "Message-ID"), when, None)
            return True
    return False


def _record(store, row, resp: dict, subject: str, rfc_id: str, when: datetime, it) -> None:
    store.save_gmail_message(resp["id"], resp.get("threadId", ""), row["target_id"], row["email"], row["row_type"],
                             subject, when.astimezone(IST).isoformat(), row["batch_date"], "engine", rfc_id)
    store.set_plan_status(row["batch_date"], row["target_id"], row["row_type"], "sent", gmail_id=resp["id"],
                          thread_id=resp.get("threadId", ""), sent_at=when.isoformat())
    day = ist_day(when).isoformat()
    if row["row_type"] == "new":
        if not any(e["event"] == "sent" and e["target_id"] == row["target_id"] and e["email"] == row["email"]
                   for e in store.events()):
            tier = it.send_tier if it is not None else None
            kind = it.address.kind if it is not None and it.address else ""
            key = it.company_key if it is not None else ""
            store.add_event(row["target_id"], key, row["email"], "sent", day, row["batch_date"], tier, kind)
    else:
        key = it.company_key if it is not None else ""
        store.add_event(row["target_id"], key, row["email"], "followup" if row["row_type"] == "followup" else
                        "retried", day, row["batch_date"])


def send_day(store, profile, gmail, clock: Callable[[], datetime] = now_ist,
             sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None, fresh=None, dnc=None,
             dry_run: bool = False, to_self: bool = False, limit: int | None = None, log: Log = print,
             pause_path: Path = PAUSE_PATH, lock_path: Path = LOCK_PATH, me: str = "") -> SendResult:
    from applyflow.drafting import load_facts
    from applyflow.outreach import build_items, refresh_drafts
    cfg = profile.outreach["sending"]
    rng = rng or random.Random()
    res = SendResult()
    start = clock()
    day = start.date()
    if not to_self and not dry_run:
        reasons = brake_reasons(store, day, pause_path)
        if reasons:
            res.stopped = "; ".join(reasons)
            return res
        if day.isoformat() in store.held_days():
            res.stopped = f"{day.isoformat()} is held"
            return res
        if not is_working_day(day):
            res.stopped = f"{day.isoformat()} isn't a working day"
            return res
    facts = load_facts(profile)
    me = me or (profile.candidate or {}).get("email") or (gmail.address() if gmail is not None else "")
    with SendLock(lock_path):
        items = {it.target_id: it for it in build_items(store, profile, dnc, day=day)}
        plan_day_s = day.isoformat()
        if (to_self or dry_run) and not store.plan_rows(plan_day_s, "planned"):
            plan_day_s = upcoming_day(store, day)
        rows = store.plan_rows(plan_day_s)

        def subject_of(row) -> str:
            p = prepare(store, profile, row, items, facts, me, dnc)
            return p.out.subject if p.out else ""

        if not to_self and not dry_run:
            for row in [r for r in rows if r["status"] == "sending"]:
                if _recover(store, gmail, row, subject_of, log):
                    res.recovered.append(f"{row['target_id']} -> {row['email']} (found in Sent)")
                else:
                    store.set_plan_status(row["batch_date"], row["target_id"], row["row_type"], "planned",
                                          error="interrupted before Gmail accepted it; not in Sent")
            rows = store.plan_rows(plan_day_s)
        pending = [r for r in rows if r["status"] == "planned"]
        if limit is not None:
            pending = pending[:limit]
        deadline = None if (to_self or dry_run) else SendWindow.from_cfg(cfg).deadline(start)
        window = SendWindow.from_cfg(cfg)

        def defer(rest, why: str) -> None:
            nxt = next_working_day(day).isoformat()
            for r in rest:
                store.set_plan_status(r["batch_date"], r["target_id"], r["row_type"], "deferred", error=why)
                if r["row_type"] == "new":
                    store.remove_from_queue(r["batch_date"], r["target_id"])
                res.deferred.append(f"{r['target_id']} -> {r['email']} ({r['row_type']}) to {nxt}: {why}")

        if not to_self and not dry_run:
            if deadline is None:
                defer(pending, f"started {start:%H:%M} IST, after {cfg['late_until']}")
                return res
            opens = window.opens(start)
            if opens > clock():
                log(f"Waiting until {opens:%H:%M} IST.")
                sleep((opens - clock()).total_seconds())
        sent_any = gap_due = False
        for i, row in enumerate(pending):
            if gap_due and not dry_run:            # a gap after each email that went out, not after a dropped row
                sleep(2.0 if to_self else window.next_gap(clock(), len(pending) - i, start, rng))
                gap_due = False
            now = clock()
            if not to_self and not dry_run:
                if now >= deadline:
                    defer(pending[i:], f"window closed at {deadline:%H:%M} IST")
                    break
                reasons = brake_reasons(store, day, pause_path)
                if reasons:
                    res.stopped = "; ".join(reasons)
                    defer(pending[i:], res.stopped)
                    break
                if _sends_on(store, day) >= int(cfg["daily_ceiling"]):
                    res.stopped = f"daily ceiling of {cfg['daily_ceiling']} sends reached"
                    defer(pending[i:], res.stopped)
                    break
            p = prepare(store, profile, row, items, facts, me, dnc, to_self, None if dry_run else fresh)
            label = f"{row['row_type']:<8} {row['target_id']} -> {row['email']}"
            if p.out is None:
                if not to_self and not dry_run:
                    store.set_plan_status(row["batch_date"], row["target_id"], row["row_type"], "dropped",
                                          error=p.why_not)
                    if row["row_type"] == "new":
                        store.remove_from_queue(row["batch_date"], row["target_id"])
                res.dropped.append(f"{label}: {p.why_not}")
                continue
            if dry_run:
                res.would.append(f"{label} | {p.out.subject} | " + (f"attach {p.out.attachment.name}"
                                 if p.out.attachment else "no attachment") + (" | in thread" if p.out.thread_id else ""))
                continue
            if not to_self:
                store.set_plan_status(row["batch_date"], row["target_id"], row["row_type"], "sending", attempt=True)
            try:
                resp = gmail.send(p.out.raw(), p.out.thread_id)
            except (GmailAuthError, GmailBrakeError) as exc:
                kind = "auth" if isinstance(exc, GmailAuthError) else "abuse"
                trigger_brake(store, day, kind, str(exc), log)
                res.stopped = f"{kind} brake: {exc}"
                if not to_self:
                    store.set_plan_status(row["batch_date"], row["target_id"], row["row_type"], "planned", error=str(exc))
                    defer(pending[i:], res.stopped)
                break
            except GmailTransientError as exc:
                res.stopped = f"Gmail unavailable ({exc}); rerun `send` to continue"
                if not to_self:
                    store.set_plan_status(row["batch_date"], row["target_id"], row["row_type"], "planned", error=str(exc))
                break
            except GmailError as exc:
                res.failed.append(f"{label}: {exc}")
                if not to_self:
                    store.set_plan_status(row["batch_date"], row["target_id"], row["row_type"], "failed", error=str(exc))
                continue
            when = clock()
            gap_due = True
            if to_self:
                res.sent.append(f"{label} (to self: {me})")
                continue
            rfc = ""
            try:
                rfc = header(gmail.get(resp["id"], "metadata", ("Message-ID",)), "Message-ID")
            except GmailError:
                pass                     # filled in when a follow-up needs it
            _record(store, row, resp, p.out.subject, rfc, when, items.get(row["target_id"]))
            sent_any = True
            res.sent.append(f"{label} at {when:%H:%M}")
            log(f"  sent {label} at {when:%H:%M} IST")
        if sent_any:
            refresh_drafts(store, profile, dnc)
    return res


# ---------------------------------------------------------------------------
# Digest, review page, summary
# ---------------------------------------------------------------------------

KIND_SHORT = {"verified_person": "named person", "founders": "founders@", "hiring_inbox": "hiring inbox",
              "evidence_guess": "evidence guess", "general_inbox": "hello@"}


def _row_view(store, profile, day: str, dnc=None) -> list[dict]:
    from applyflow.outreach import build_items
    items = {it.target_id: it for it in build_items(store, profile, dnc)}
    out = []
    for r in store.plan_rows(day):
        it = items.get(r["target_id"])
        a = it.address if it is not None else None
        who = (a.name or "") if a is not None and a.email == r["email"] else ""
        if r["row_type"] != "new" and it is not None and it.replied_by:
            who = it.replied_by
        out.append({"pos": r["position"], "status": r["status"], "type": r["row_type"], "target_id": r["target_id"],
                    "company": it.company if it is not None else r["target_id"], "email": r["email"], "who": who,
                    "kind": KIND_SHORT.get(a.kind, a.kind) if a is not None else "", "item": it,
                    "error": r["error"] or ""})
    return out


def supply_line(store, profile, dnc=None) -> str:
    from applyflow.outreach import build_items, mark_queued
    from applyflow.supply import supply_summary
    items = build_items(store, profile, dnc)
    mark_queued(store, items)
    daily = int(profile.outreach["sending"]["new_per_day"])
    t = supply_summary(items, profile.outreach, daily)
    return (f"{t['sendable']} sendable targets not yet planned: {t['days']} sending day(s) at {daily}/day under the "
            f"caps, {t['full_days']} of them full {daily}/day")


def build_digest(store, profile, day: str, now: datetime, short: int = 0, dnc=None) -> tuple[str, str]:
    """(subject, plain text) of the evening digest for the plan of `day`."""
    from applyflow.outreach import OUTCOME_LABELS, build_items, recheck_due
    since = store.get_state("last_digest", "")
    rows = _row_view(store, profile, day, dnc)
    live = [r for r in rows if r["status"] in ("planned", "deferred")]
    n_new = sum(r["type"] == "new" for r in live)
    n_fu = sum(r["type"] == "followup" for r in live)
    n_re = sum(r["type"] == "retry" for r in live)
    lines = [f"ApplyFlow digest - {now:%a %d %b %Y %H:%M} IST", ""]
    brakes = brake_reasons(store, now.date())
    lines.append("BRAKE: " + "; ".join(brakes) if brakes else "Brakes: none (sending is on).")
    if day in store.held_days():
        lines.append(f"{day} is HELD: nothing will be sent that day.")
    if short:
        lines.append(f"SHORT BY {short}: only {int(profile.outreach['sending']['new_per_day']) - short} new rows "
                     "could be planned under the caps and gates.")
    lines += ["", f"Plan for {day}: {len(live)} row(s) - {n_new} new, {n_fu} follow-up(s), {n_re} retry(ies)"]
    for r in live:
        lines.append(f"  {r['pos']:>2}. {r['company'][:28]:<28} {r['email']:<36} {r['who'][:24]:<24} "
                     f"{r['kind']:<14} {r['type']}")
    lines.append(f"  Review: output/review/plan_{day}.html - `drop <row|email> --date {day}` or `hold {day}`. "
                 "Unreviewed plans send as planned.")
    items = build_items(store, profile, dnc)
    waiting = [it for it in items if it.outcome == "needs_action"]
    lines += ["", f"Human replies needing your action: {len(waiting)}"]
    humans = {r["target_id"]: r for r in store.inbox_messages(category="human_reply")}
    for it in waiting:
        m = humans.get(it.target_id)
        link = thread_link(m["thread_id"]) if m is not None else ""
        lines.append(f"  - {it.company}: {it.replied_by or it.sent_email} on {it.outcome_date} {link}")
        if m is not None and m["detail"]:
            lines.append(f"      \"{m['detail'][:160]}\"")
    if waiting:
        lines.append("  Record each with `mark-replied <email> --outcome interested|declined_warm|declined "
                     "[--recheck D]`.")
    bounces = [r for r in store.inbox_messages(since=since) if r["category"] in ("hard_bounce", "soft_bounce",
                                                                                  "bounce_unclear")]
    lines += ["", f"Bounces since the last digest: {len(bounces)}"]
    for r in bounces:
        lines.append(f"  - {r['category']}: {r['email'] or '?'} ({r['detail']}) - {r['applied']}")
    autos = [r for r in store.inbox_messages(since=since, category="auto_reply")]
    if autos:
        lines += ["", f"Auto-replies since the last digest: {len(autos)}"]
        lines += [f"  - {r['email'] or r['sender']}: {r['applied']}" for r in autos]
    drive = store.inbox_messages(since=since, category="drive_request")
    lines += ["", f"Google Drive access requests since the last digest: {len(drive)}"]
    lines += [f"  - {r['detail'][:150]}" for r in drive]
    if drive:
        lines.append("  (the resume link in your signature may not be shared with 'anyone with the link')")
    due = recheck_due(items, now.date())
    lines += ["", f"Recheck list: {len(due)}"]
    lines += [f"  - {it.company}: {OUTCOME_LABELS.get(it.outcome, it.outcome)} {it.outcome_date}, recheck "
              f"{it.recheck} ({it.replied_by or it.sent_email})" for it in due]
    lines += ["", "Supply: " + supply_line(store, profile, dnc)]
    subject = f"ApplyFlow digest: {day} - {n_new} new, {n_fu} follow-ups" + (" - BRAKE ON" if brakes else "") + \
              (f" - short by {short}" if short else "") + (f" - {len(waiting)} replies to answer" if waiting else "")
    return subject, "\n".join(lines) + "\n"


def write_review(store, profile, day: str, path: Path, dnc=None) -> Path:
    from applyflow.followups import build_followup, original_day, original_message
    from applyflow.drafting import load_facts
    facts = load_facts(profile)
    lo, hi = profile.outreach["sending"]["followup_words"]
    e = htmllib.escape
    parts = [f"<!doctype html><meta charset='utf-8'><title>Plan {e(day)}</title>",
             "<style>body{font:14px system-ui,sans-serif;margin:24px;max-width:1100px}table{border-collapse:collapse;"
             "width:100%}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}"
             "pre{white-space:pre-wrap;background:#f6f6f6;padding:8px}.muted{color:#777}</style>",
             f"<h1>Send plan for {e(day)}</h1><p class='muted'>Drop a row: <code>python -m applyflow drop &lt;row|email&gt;"
             f" --date {e(day)}</code>. Skip the day: <code>python -m applyflow hold {e(day)}</code>. "
             "Unreviewed plans send as planned.</p><table><tr><th>#</th><th>Company</th><th>To</th><th>Type</th>"
             "<th>Status</th><th>Email</th></tr>"]
    for r in _row_view(store, profile, day, dnc):
        it = r["item"]
        subject, body, extra = "", "", ""
        if it is not None and it.draft is not None:
            if r["type"] == "followup":
                orig = original_message(store, it)
                subject = follow_subject(orig["subject"] if orig is not None else it.draft.subject)
                body, v = build_followup(it.target_id, it.inputs.greeting, it.inputs.company,
                                         it.inputs.role_title if it.kind == "role" else "", it.draft.body, facts, lo, hi,
                                         sent=original_day(orig, it), day=date.fromisoformat(day))
                extra = f"follow-up variant {v + 1}, in thread, no attachment"
            else:
                subject, body = it.draft.subject, it.draft.body
                extra = "attach resume PDF" if it.draft.resume_action == "attach" else "resume link, no attachment"
        parts.append(f"<tr><td>{r['pos']}</td><td>{e(r['company'])}</td><td>{e(r['email'])}<br><span class='muted'>"
                     f"{e(r['who'])} {e(r['kind'])}</span></td><td>{e(r['type'])}</td><td>{e(r['status'])}"
                     f"<br><span class='muted'>{e(r['error'])}</span></td><td><b>{e(subject)}</b><br>"
                     f"<span class='muted'>{e(extra)}</span><details><summary>body</summary><pre>{e(body)}</pre>"
                     "</details></td></tr>")
    parts.append("</table>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _write_outputs(store, profile, res: PlanResult, dnc=None) -> None:
    res.review_path = write_review(store, profile, res.day, REVIEW_DIR / f"plan_{res.day}.html", dnc)
    subject, text = build_digest(store, profile, res.day, now_ist(), res.short, dnc)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    res.digest_path = DIGEST_DIR / f"digest_{res.day}.txt"
    res.digest_path.write_text(f"Subject: {subject}\n\n{text}", encoding="utf-8")
    store.set_state(f"short:{res.day}", str(res.short))


def send_digest(store, profile, gmail, day: str, now: datetime, dnc=None, me: str = "") -> tuple[str, str]:
    """Email the digest to yourself through the API; returns (subject, text)."""
    short = int(store.get_state(f"short:{day}", "0") or 0)
    subject, text = build_digest(store, profile, day, now, short, dnc)
    me = me or gmail.address()
    name = (profile.candidate or {}).get("name") or ""
    gmail.send(Outgoing(to=me, subject=subject, body=text, from_name=name, from_email=me).raw())
    store.set_state("last_digest", now.isoformat())
    return subject, text


def summary(store, profile, day: date, dnc=None) -> str:
    from applyflow.outreach import OUTCOME_LABELS, build_items, send_records
    from applyflow.followups import due_followups
    from applyflow.drafting import load_facts
    cfg = profile.outreach["sending"]
    today = [r for r in store.gmail_messages() if ist_day(datetime.fromisoformat(r["sent_at"])) == day]
    by_type: dict[str, int] = {}
    for r in today:
        by_type[r["row_type"]] = by_type.get(r["row_type"], 0) + 1
    lines = [f"ApplyFlow summary for {day.isoformat()}",
             f"Sent today: {len(today)} (" + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())) +
             f") of the {cfg['daily_ceiling']} ceiling" if today else
             f"Sent today: 0 (ceiling {cfg['daily_ceiling']})"]
    plan = store.plan_rows(day.isoformat())
    if plan:
        counts: dict[str, int] = {}
        for r in plan:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        lines.append("Today's plan: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    recs = send_records(store)
    kinds: dict[str, list[int]] = {}
    for s in recs:
        k = kinds.setdefault(s.kind, [0, 0, 0])
        k[0] += 1
        k[1] += bool(s.bounced_on)
        k[2] += s.bounced_on == day.isoformat()
    lines.append("Bounces by type (all time, today): " + "; ".join(
        f"{KIND_SHORT.get(k, k)} {v[1]}/{v[0]}" + (f" ({v[2]} today)" if v[2] else "") for k, v in sorted(kinds.items())))
    items = build_items(store, profile, dnc)
    outcomes: dict[str, int] = {}
    for it in items:
        if it.outcome:
            outcomes[it.outcome] = outcomes.get(it.outcome, 0) + 1
    lines.append("Replies by outcome: " + (", ".join(f"{OUTCOME_LABELS.get(k, k)} {v}" for k, v in
                                                     sorted(outcomes.items())) or "none"))
    due, _ = due_followups(store, items, next_working_day(day), cfg, load_facts(profile), dnc)
    due_today, _ = due_followups(store, items, day, cfg, load_facts(profile), dnc)
    lines.append(f"Follow-ups due: {len(due_today)} today, {len(due)} by {next_working_day(day).isoformat()} "
                 f"(cap {cfg['followups_per_day']}/day)")
    brakes = brake_reasons(store, day)
    lines.append("Brake: " + ("; ".join(brakes) if brakes else "off"))
    lines.append("Supply: " + supply_line(store, profile, dnc))
    return "\n".join(lines)
