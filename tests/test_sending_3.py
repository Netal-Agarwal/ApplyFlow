"""Phase 3: the Gmail sending engine - MIME (attach vs link), threading headers, idempotency and the lock, the
send window (late start, 13:00, hard stop), follow-up eligibility / cap / weekends and the three variants, inbox
classification (DSNs, auto-reply headers and wording, human replies, Drive requests), brakes, reconcile matching,
plan shortfall and the digest. Offline: a fake Gmail client and Gmail API fixtures in tests/fixtures/gmail/."""

import base64
import copy
import email
import json
import random
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta
from email import policy
from pathlib import Path
from unittest import mock

from applyflow import sending
from applyflow.drafting import load_facts, split_body, word_count
from applyflow.followups import (VARIANTS, add_working_days, build_followup, company_line, due_followups,
                                 follow_subject, validate_followup)
from applyflow.gmail import (GmailAuthError, GmailBrakeError, GmailError, GmailTransientError, Outgoing,
                             classify_error, parse_mail)
from applyflow.inbox import IST, check_inbox, classify, reconcile
from applyflow.outreach import build_items, mark_replied, mark_sent
from applyflow.sending import (SendLock, LockBusy, SendWindow, build_digest, drop, hold, plan_day, send_day,
                               summary)
from applyflow.store import Store
from tests.helpers import FIXTURES, example_profile
from tests.test_drafting import add_company, contact

MONDAY = date(2026, 11, 2)
ME = "me@example.com"


def gmail_fixture(name: str) -> dict:
    return json.loads((FIXTURES / "gmail" / f"{name}.json").read_text(encoding="utf-8"))


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


class Clock:
    def __init__(self, start: datetime):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class FakeGmail:
    """Stands in for GmailClient: keeps what was sent (Sent folder) and serves inbox fixtures."""

    def __init__(self, inbox=(), clock: Clock | None = None):
        self.sent: list[dict] = []
        self.inbox = {m["id"]: m for m in inbox}
        self.fail: list[Exception | None] = []
        self.clock = clock
        self.queries: list[str] = []

    def address(self) -> str:
        return ME

    def add_sent(self, to: str, subject: str, when: datetime, thread: str = "") -> dict:
        n = len(self.sent) + 1
        rec = {"id": f"s{n}", "threadId": thread or f"t{n}", "to": to, "subject": subject,
               "ms": int(when.timestamp() * 1000), "msgid": f"<msg{n}@mail.gmail.com>", "mime": None}
        self.sent.append(rec)
        return rec

    def send(self, raw: str, thread_id: str = "") -> dict:
        if self.fail:
            exc = self.fail.pop(0)
            if exc is not None:
                raise exc
        mime = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
        when = self.clock() if self.clock else datetime.now(IST)
        rec = self.add_sent(mime["To"], mime["Subject"], when, thread_id)
        rec["mime"], rec["msgid"] = mime, mime["Message-ID"]
        return {"id": rec["id"], "threadId": rec["threadId"], "labelIds": ["SENT"]}

    def list_ids(self, query: str, limit: int = 500) -> list[dict]:
        self.queries.append(query)
        if query.startswith("in:sent"):
            to = re.search(r"to:(\S+)", query).group(1).lower()
            return [{"id": r["id"], "threadId": r["threadId"]} for r in self.sent if to in r["to"].lower()]
        return [{"id": i} for i in self.inbox]

    def get(self, gid: str, fmt: str = "full", headers=()) -> dict:
        if gid in self.inbox:
            return self.inbox[gid]
        r = next(r for r in self.sent if r["id"] == gid)
        return {"id": r["id"], "threadId": r["threadId"], "internalDate": str(r["ms"]), "labelIds": ["SENT"],
                "payload": {"headers": [{"name": "Subject", "value": r["subject"]}, {"name": "To", "value": r["to"]},
                                        {"name": "Message-ID", "value": r["msgid"]}]}}


class Env(unittest.TestCase):
    """A temp dir for the review page, digest, lock and PAUSE flag; a profile whose resume PDF is a temp file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = self.tmp / "Test_Resume.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 test resume\n%%EOF\n")
        self.profile = copy.deepcopy(example_profile())
        self.profile.outreach["sending"]["resume_pdf"] = str(self.pdf)
        self.facts = load_facts(self.profile)
        for name, value in (("REVIEW_DIR", self.tmp / "review"), ("DIGEST_DIR", self.tmp / "digest")):
            p = mock.patch.object(sending, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.pause = self.tmp / "PAUSE"
        self.lock = self.tmp / "send.lock"

    def send(self, store, gmail, clock, **kw):
        return send_day(store, self.profile, gmail, clock=clock, sleep=clock.sleep, rng=random.Random(7),
                        pause_path=self.pause, lock_path=self.lock, me=ME, log=lambda _: None, **kw)


def world(n: int = 3) -> Store:
    """n ready targets: a founders@ inbox, a hiring inbox (attach), a named CTO (link), then more hiring inboxes."""
    s = Store(":memory:")
    add_company(s, "pool:hydra.example", "Hydra", website="https://hydra.example",
                contacts=[contact("founders@hydra.example", "published_inbox", role="founders@ inbox")])
    add_company(s, "pool:nuv.example", "Nuv", website="https://nuv.example",
                contacts=[contact("careers@nuv.example", "published_inbox", role="careers inbox")])
    add_company(s, "pool:nimbus.example", "Nimbus", website="https://nimbus.example",
                contacts=[contact("priya@nimbus.example", "verified", "Priya Sharma", "CTO")])
    for i in range(3, n):
        add_company(s, f"pool:co{i}.example", f"Company {i}", website=f"https://co{i}.example",
                    contacts=[contact(f"careers@co{i}.example", "published_inbox", role="careers inbox")])
    return s


def item(store, profile, tid):
    return next(i for i in build_items(store, profile) if i.target_id == tid)


def sent_original(store, profile, tid: str, day: date, thread: str = "", gid: str = "") -> None:
    """A target sent on `day` with its Gmail record (as `send` or `reconcile` leaves it)."""
    mark_sent(store, profile, tid, day)
    it = item(store, profile, tid)
    store.save_gmail_message(gid or f"g-{tid}", thread or f"thr-{tid}", tid, it.sent_email, "new", it.draft.subject,
                             at(day, 9, 5).isoformat(), day.isoformat(), "engine", f"<orig-{tid}@mail.gmail.com>")


# ---------------------------------------------------------------------------
# MIME and threading
# ---------------------------------------------------------------------------

class MimeTests(Env):
    def test_hiring_inbox_gets_the_pdf_and_a_link_draft_gets_none(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 9))
        gmail = FakeGmail(clock=clock)
        res = self.send(store, gmail, clock)
        self.assertEqual(len(res.sent), 3, res.dropped)
        by_to = {r["to"]: r["mime"] for r in gmail.sent}
        hr = by_to["careers@nuv.example"]
        self.assertTrue(hr.is_multipart())
        pdfs = [p for p in hr.iter_attachments() if p.get_content_type() == "application/pdf"]
        self.assertEqual([p.get_filename() for p in pdfs], ["Test_Resume.pdf"])
        self.assertNotIn(self.facts.resume_url, hr.get_body(("plain",)).get_content())
        cto = by_to["priya@nimbus.example"]
        self.assertFalse(cto.is_multipart())
        self.assertEqual(cto.get_content_type(), "text/plain")
        self.assertEqual(cto.get_content_charset(), "utf-8")
        self.assertIn(self.facts.resume_url, cto.get_content())
        self.assertNotIn("attached", cto.get_content().lower())
        self.assertIn("Your Name", cto["From"])
        self.assertIn(ME, cto["From"])
        self.assertNotIn("<img", cto.get_content())                 # no tracking pixel, no HTML

    def test_never_both_link_and_attachment(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        row = next(r for r in store.plan_rows(MONDAY.isoformat()) if r["email"] == "careers@nuv.example")
        items = {i.target_id: i for i in build_items(store, self.profile)}
        it = items[row["target_id"]]
        it.draft.body = it.draft.body.replace("I've attached my resume.", f"Resume: {self.facts.resume_url}")
        p = sending.prepare(store, self.profile, row, items, self.facts, ME)
        self.assertIsNone(p.out)
        self.assertIn("never both", p.why_not)

    def test_follow_up_is_a_threaded_reply_without_attachment(self):
        out = Outgoing(to="a@x.example", subject="Re: Hello", body="Hi,\n\nText\n", from_name="N", from_email=ME,
                       in_reply_to="<orig@mail.gmail.com>", thread_id="thr-1")
        m = email.message_from_bytes(base64.urlsafe_b64decode(out.raw()), policy=policy.default)
        self.assertEqual((m["In-Reply-To"], m["References"], m["Subject"]),
                         ("<orig@mail.gmail.com>", "<orig@mail.gmail.com>", "Re: Hello"))
        self.assertFalse(m.is_multipart())
        self.assertTrue(m["Message-ID"].endswith("@example.com>"))


# ---------------------------------------------------------------------------
# Window, idempotency, lock
# ---------------------------------------------------------------------------

class WindowTests(unittest.TestCase):
    W = SendWindow.from_cfg(example_profile().outreach["sending"])

    def test_deadlines(self):
        self.assertEqual(self.W.deadline(at(MONDAY, 8, 55)), at(MONDAY, 10, 45))     # waits for 09:00
        self.assertEqual(self.W.deadline(at(MONDAY, 9, 10)), at(MONDAY, 10, 45))
        self.assertEqual(self.W.deadline(at(MONDAY, 9, 40)), at(MONDAY, 13))         # late start (laptop asleep)
        self.assertEqual(self.W.deadline(at(MONDAY, 12, 30)), at(MONDAY, 13))
        self.assertIsNone(self.W.deadline(at(MONDAY, 13, 5)))                        # too late: all deferred
        self.assertEqual(self.W.opens(at(MONDAY, 8, 55)), at(MONDAY, 9))

    def test_gaps_stay_in_2_to_4_minutes_and_45_rows_finish_by_10_30(self):
        rng, start = random.Random(1), at(MONDAY, 9)
        now = start
        for i in range(44):
            g = self.W.next_gap(now, 45 - i - 1, start, rng)
            self.assertGreaterEqual(g, 120)
            self.assertLessEqual(g, 240)
            now += timedelta(seconds=g)
        self.assertLessEqual(now, at(MONDAY, 10, 30))


class IdempotencyTests(Env):
    def test_rerun_sends_nothing_twice(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 9))
        gmail = FakeGmail(clock=clock)
        self.assertEqual(len(self.send(store, gmail, clock).sent), 3)
        again = self.send(store, gmail, clock)
        self.assertEqual((again.sent, len(gmail.sent)), ([], 3))
        statuses = {r["status"] for r in store.plan_rows(MONDAY.isoformat())}
        self.assertEqual(statuses, {"sent"})
        ids = {r["gmail_id"] for r in store.plan_rows(MONDAY.isoformat())}
        self.assertEqual(len(ids), 3)
        events = [e for e in store.events() if e["event"] == "sent"]
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["event_date"] == MONDAY.isoformat() and e["kind"] for e in events))

    def test_crash_mid_send_is_resolved_from_the_sent_folder(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        rows = store.plan_rows(MONDAY.isoformat())
        clock = Clock(at(MONDAY, 9))
        gmail = FakeGmail(clock=clock)
        items = {i.target_id: i for i in build_items(store, self.profile)}
        # row 1 went out but the process died before recording it; row 2 died before Gmail accepted it
        done, lost = rows[0], rows[1]
        for r in (done, lost):
            store.set_plan_status(r["batch_date"], r["target_id"], r["row_type"], "sending", attempt=True)
        gmail.add_sent(done["email"], items[done["target_id"]].draft.subject, at(MONDAY, 9, 1))
        res = self.send(store, gmail, clock)
        self.assertEqual(len(res.recovered), 1)
        self.assertEqual(len(gmail.sent), 3)                  # the recovered one isn't sent again
        self.assertEqual({r["status"] for r in store.plan_rows(MONDAY.isoformat())}, {"sent"})
        attempts = {r["target_id"]: r["attempts"] for r in store.plan_rows(MONDAY.isoformat())}
        self.assertEqual(attempts[lost["target_id"]], 2)

    def test_lock_blocks_a_second_run_and_a_stale_lock_is_replaced(self):
        with SendLock(self.lock):
            with self.assertRaises(LockBusy):
                with SendLock(self.lock):
                    pass
        self.lock.write_text("1 0\n")
        import os
        os.utime(self.lock, (0, 0))                          # a crashed run's lock, hours old
        with SendLock(self.lock):
            pass
        self.assertFalse(self.lock.exists())


class TimingTests(Env):
    def test_on_time_run_waits_for_9_and_stops_at_10_45(self):
        store = world(40)
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 8, 50))
        self.profile.outreach["sending"]["gap_minutes"] = [4, 4]         # 30 rows x 4 min: past 10:45
        res = self.send(store, FakeGmail(clock=clock), clock)
        self.assertEqual(clock.sleeps[0], 600)                           # 08:50 -> 09:00
        self.assertTrue(res.deferred)
        last = max(datetime.fromisoformat(r["sent_at"]) for r in store.gmail_messages())
        self.assertLessEqual(last, at(MONDAY, 10, 45))
        self.assertEqual(len(res.sent) + len(res.deferred), 30)
        deferred = store.plan_rows(MONDAY.isoformat(), "deferred")
        self.assertTrue(all("10:45" in r["error"] for r in deferred))
        self.assertFalse({r["target_id"] for r in deferred} & {r["target_id"] for r in store.queue(MONDAY.isoformat())})
        # the next plan takes the deferred rows first
        tue = MONDAY + timedelta(days=1)
        plan = plan_day(store, self.profile, tue)
        self.assertEqual(plan.carried[:len(deferred)], [r["target_id"] for r in deferred])
        self.assertEqual({r["status"] for r in store.plan_rows(MONDAY.isoformat())} - {"sent"}, {"moved"})

    def test_late_start_sends_until_13_then_defers(self):
        store = world(40)
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 11, 30))
        self.profile.outreach["sending"]["gap_minutes"] = [4, 4]         # 30 rows x 4 min from 11:30: past 13:00
        res = self.send(store, FakeGmail(clock=clock), clock)
        last = max(datetime.fromisoformat(r["sent_at"]) for r in store.gmail_messages())
        self.assertGreater(last, at(MONDAY, 10, 45))
        self.assertLessEqual(last, at(MONDAY, 13))
        self.assertTrue(res.deferred)

    def test_start_after_13_defers_everything(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 13, 30))
        gmail = FakeGmail(clock=clock)
        res = self.send(store, gmail, clock)
        self.assertEqual((len(gmail.sent), len(res.deferred)), (0, 3))

    def test_weekend_and_held_days_send_nothing(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        sat = Clock(at(date(2026, 11, 7), 9))
        self.assertIn("working day", self.send(store, FakeGmail(clock=sat), sat).stopped)
        hold(store, MONDAY.isoformat())
        clock = Clock(at(MONDAY, 9))
        self.assertIn("held", self.send(store, FakeGmail(clock=clock), clock).stopped)


# ---------------------------------------------------------------------------
# Brakes
# ---------------------------------------------------------------------------

class BrakeTests(Env):
    def test_pause_flag(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        self.pause.write_text("paused")
        clock = Clock(at(MONDAY, 9))
        gmail = FakeGmail(clock=clock)
        res = self.send(store, gmail, clock)
        self.assertIn("paused", res.stopped)
        self.assertEqual(gmail.sent, [])

    def test_auth_error_brakes_until_gmail_login(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 9))
        gmail = FakeGmail(clock=clock)
        gmail.fail = [GmailAuthError("HTTP 401 invalid_grant: Token has been expired or revoked.", 401)]
        res = self.send(store, gmail, clock)
        self.assertIn("auth brake", res.stopped)
        self.assertEqual(len(res.deferred), 3)
        tomorrow = Clock(at(MONDAY + timedelta(days=1), 9))
        self.assertIn("run gmail-login", "; ".join(sending.brake_reasons(store, tomorrow().date(), self.pause)))
        subject, text = build_digest(store, self.profile, "2026-11-03", at(MONDAY, 20))
        self.assertIn("run gmail-login", text)
        self.assertIn("BRAKE ON", subject)
        store.clear_brakes("auth")                      # what gmail-login does
        self.assertEqual(sending.brake_reasons(store, tomorrow().date(), self.pause), [])

    def test_403_or_quota_brakes_the_day(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 9))
        gmail = FakeGmail(clock=clock)
        gmail.fail = [None, GmailBrakeError("HTTP 403 userRateLimitExceeded: User-rate limit exceeded", 403)]
        res = self.send(store, gmail, clock)
        self.assertEqual((len(res.sent), len(res.deferred)), (1, 2))
        self.assertIn("abuse", res.stopped)
        self.assertEqual(sending.brake_reasons(store, MONDAY + timedelta(days=1), self.pause), [])   # that day only

    def test_transient_error_leaves_rows_planned_for_a_rerun(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 9))
        gmail = FakeGmail(clock=clock)
        gmail.fail = [GmailTransientError("HTTP 503 backendError")]
        res = self.send(store, gmail, clock)
        self.assertIn("rerun", res.stopped)
        self.assertEqual(len(store.plan_rows(MONDAY.isoformat(), "planned")), 3)
        self.assertEqual(len(self.send(store, gmail, clock).sent), 3)

    def test_error_classification(self):
        self.assertIsInstance(classify_error(401, {"error": {"code": 401, "message": "Invalid Credentials"}}),
                              GmailAuthError)
        self.assertIsInstance(classify_error(400, {"error": "invalid_grant", "error_description": "Token has been "
                                                   "expired or revoked."}), GmailAuthError)
        self.assertIsInstance(classify_error(403, {"error": {"errors": [{"reason": "insufficientPermissions"}],
                                                             "message": "Insufficient Permission"}}), GmailBrakeError)
        self.assertIsInstance(classify_error(429, {"error": {"message": "Too many concurrent requests for user"}}),
                              GmailBrakeError)
        self.assertIsInstance(classify_error(400, {"error": {"errors": [{"reason": "failedPrecondition"}],
                                                             "message": "Daily user sending limit exceeded"}}),
                              GmailBrakeError)
        self.assertIsInstance(classify_error(500, {"error": {"message": "Backend Error"}}), GmailTransientError)
        err = classify_error(400, {"error": {"errors": [{"reason": "invalidArgument"}], "message": "Invalid To header"}})
        self.assertEqual(type(err), GmailError)

    def test_day_brake_four_bounces_or_over_twelve_percent(self):
        from applyflow.sending import day_brake
        cfg = self.profile.outreach["sending"]
        self.assertEqual(day_brake(3, 30, cfg), "")                    # 10%
        self.assertIn("4 hard bounces", day_brake(4, 45, cfg))
        self.assertIn("13%", day_brake(2, 15, cfg))                    # 2 of 15 > 12%
        self.assertEqual(day_brake(3, 25, cfg), "")                    # exactly 12%: not above
        self.assertEqual(day_brake(0, 0, cfg), "")

    def test_four_hard_bounces_in_a_day_stop_sending(self):
        store = world(4)
        tids = ["pool-hydra-example", "pool-nuv-example", "pool-nimbus-example", "pool-co3-example"]
        for tid in tids:
            mark_sent(store, self.profile, tid, date(2026, 10, 30))
        addrs = ["founders@hydra.example", "careers@nuv.example", "priya@nimbus.example", "careers@co3.example"]
        for n, addr in enumerate(addrs):
            m = gmail_fixture("dsn_postfix_hard")
            m["id"] = f"dsn{n}"
            m["payload"]["parts"][1]["body"]["data"] = base64.urlsafe_b64encode(
                f"Final-Recipient: rfc822; {addr}\nAction: failed\nStatus: 5.1.1\n".encode()).decode()
            m["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(b"User unknown").decode()
            check_inbox(store, self.profile, FakeGmail([m]), at(MONDAY, 11), since=at(MONDAY, 0), me=ME)
            braked = any(b["kind"] == "bounces" for b in store.brakes(MONDAY.isoformat()))
            self.assertEqual(braked, n == 3, n)                       # 3 bounces (no sends today) don't brake
        self.assertEqual(sum(e["event"] == "stopped" for e in store.events()), 4)
        add_company(store, "pool:zeta.example", "Zeta", website="https://zeta.example",
                    contacts=[contact("careers@zeta.example", "published_inbox", role="careers inbox")])
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 11, 30))
        res = self.send(store, FakeGmail(clock=clock), clock)
        self.assertIn("bounces brake", res.stopped)

    def test_daily_ceiling(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        self.profile.outreach["sending"]["daily_ceiling"] = 2
        clock = Clock(at(MONDAY, 9))
        res = self.send(store, FakeGmail(clock=clock), clock)
        self.assertEqual(len(res.sent), 2)
        self.assertIn("ceiling", res.stopped)


# ---------------------------------------------------------------------------
# Dry run and to-self
# ---------------------------------------------------------------------------

class TestModesTests(Env):
    def test_dry_run_changes_nothing(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 7))
        res = self.send(store, None, clock, dry_run=True)
        self.assertEqual(len(res.would), 3)
        self.assertTrue(any("attach Test_Resume.pdf" in w for w in res.would))
        self.assertEqual({r["status"] for r in store.plan_rows(MONDAY.isoformat())}, {"planned"})

    def test_to_self_marks_nothing(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(date(2026, 10, 31), 22))             # a Saturday night: test sends ignore the window
        gmail = FakeGmail(clock=clock)
        res = self.send(store, gmail, clock, to_self=True, limit=2)
        self.assertEqual(len(gmail.sent), 2)
        self.assertTrue(all(r["to"] == ME and r["subject"].startswith("[TEST] ") for r in gmail.sent))
        self.assertEqual({r["status"] for r in store.plan_rows(MONDAY.isoformat())}, {"planned"})
        self.assertEqual([e for e in store.events() if e["event"] == "sent"], [])
        self.assertEqual(store.gmail_messages(), [])
        self.assertEqual(len(res.sent), 2)


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

class FollowUpTests(Env):
    def cfg(self):
        return self.profile.outreach["sending"]

    def test_working_days_skip_the_weekend(self):
        self.assertEqual(add_working_days(date(2026, 10, 30), 5), date(2026, 11, 6))     # Fri -> Fri
        self.assertEqual(add_working_days(date(2026, 10, 28), 5), date(2026, 11, 4))     # Wed -> Wed
        self.assertEqual(add_working_days(date(2026, 10, 31), 1), date(2026, 11, 2))     # Sat -> Mon

    def test_due_dates_and_not_before_nov_2(self):
        store = world()
        sent_original(store, self.profile, "pool-hydra-example", date(2026, 10, 21))     # due 28 Oct -> 2 Nov
        sent_original(store, self.profile, "pool-nuv-example", date(2026, 10, 30))       # due 6 Nov
        items = build_items(store, self.profile)
        due, why = due_followups(store, items, MONDAY, self.cfg(), self.facts)
        self.assertEqual([f.item.target_id for f in due], ["pool-hydra-example"])
        self.assertEqual(why, {"not due yet": 1})
        due, _ = due_followups(store, items, date(2026, 10, 29), self.cfg(), self.facts)
        self.assertEqual(due, [])
        due, _ = due_followups(store, items, date(2026, 11, 6), self.cfg(), self.facts)
        self.assertEqual(len(due), 2)

    def test_who_never_gets_one(self):
        store = world(9)
        tids = ["pool-hydra-example", "pool-nuv-example", "pool-nimbus-example"] + \
               [f"pool-co{i}-example" for i in range(3, 9)]
        for tid in tids:
            sent_original(store, self.profile, tid, date(2026, 10, 22))
        p = self.profile
        mark_replied(store, p, "founders@hydra.example", "declined_warm", date(2026, 10, 28), "2026-12-01")
        mark_replied(store, p, "careers@nuv.example", "declined", date(2026, 10, 28))
        mark_replied(store, p, "careers@co3.example", "auto_ack", date(2026, 10, 28))                  # ATS
        mark_replied(store, p, "careers@co4.example", "auto_ack", date(2026, 10, 28), keep_followup=True)  # Codelattice
        from applyflow.outreach import mark_bounced
        mark_bounced(store, p, "pool-co5-example", date(2026, 10, 23), stop=True)
        mark_replied(store, p, "careers@co6.example", "needs_action", date(2026, 10, 29),
                     contact="Rahul M <rahul@co6.example>")
        from applyflow.contacts import DoNotContact
        dnc = DoNotContact(emails={"careers@co7.example"})
        items = build_items(store, p)
        due, why = due_followups(store, items, MONDAY, self.cfg(), self.facts, dnc)
        got = {f.item.target_id for f in due}
        self.assertEqual(got, {"pool-co4-example", "pool-nimbus-example", "pool-co8-example"})
        for tid in ("pool-hydra-example", "pool-nuv-example", "pool-co3-example", "pool-co5-example",
                    "pool-co6-example", "pool-co7-example"):
            self.assertNotIn(tid, got)
        self.assertEqual(why.get("auto-acknowledgement (ATS): no follow-up"), 1)

    def test_company_level_human_reply_blocks_another_target(self):
        store = world()
        sent_original(store, self.profile, "pool-hydra-example", date(2026, 10, 22))
        add_company(store, "manual:hydra", "Hydra", website="https://hydra.example",
                    contacts=[contact("rahul@hydra.example", "verified", "Rahul Mehta", "Founder")], kind="manual")
        items = build_items(store, self.profile)          # a second Hydra target, answered by a person
        other = next(i for i in items if i.target_id == "manual-hydra")
        other.status, other.sent_email, other.sent_date = "sent", "rahul@hydra.example", "2026-10-22"
        other.outcome = "needs_action"
        due, why = due_followups(store, items, MONDAY, self.cfg(), self.facts)
        self.assertNotIn("pool-hydra-example", [f.item.target_id for f in due])
        self.assertEqual(why.get("someone at the company has replied"), 1)

    def test_cap_oldest_first_and_one_per_contact(self):
        store = world(20)
        tids = [i.target_id for i in build_items(store, self.profile) if i.status == "ready"]
        for n, tid in enumerate(tids):
            sent_original(store, self.profile, tid, date(2026, 10, 21) + timedelta(days=n % 3))
        items = build_items(store, self.profile)
        due, why = due_followups(store, items, MONDAY, self.cfg(), self.facts, cap=15)
        self.assertEqual(len(due), 15)
        self.assertEqual(why["over the daily follow-up cap (next day)"], 5)
        self.assertEqual([f.original["sent_at"] for f in due], sorted(f.original["sent_at"] for f in due))
        first = due[0].item
        store.save_gmail_message("fu-1", f"thr-{first.target_id}", first.target_id, first.sent_email, "followup",
                                 due[0].subject, at(MONDAY, 9, 30).isoformat(), MONDAY.isoformat())
        again, why = due_followups(store, build_items(store, self.profile), MONDAY + timedelta(days=1), self.cfg(),
                                   self.facts)
        self.assertNotIn(first.target_id, [f.item.target_id for f in again])
        self.assertEqual(why.get("already followed up (one per contact)"), 1)

    def test_followup_send_is_threaded_with_no_attachment(self):
        store = world()
        sent_original(store, self.profile, "pool-nuv-example", date(2026, 10, 22), thread="thr-nuv")
        plan = plan_day(store, self.profile, MONDAY)
        self.assertEqual([f.item.target_id for f in plan.followups], ["pool-nuv-example"])
        clock = Clock(at(MONDAY, 9))
        gmail = FakeGmail(clock=clock)
        self.send(store, gmail, clock)
        fu = next(r for r in gmail.sent if r["subject"].startswith("Re: "))
        self.assertEqual(fu["threadId"], "thr-nuv")
        m = fu["mime"]
        self.assertEqual(m["In-Reply-To"], "<orig-pool-nuv-example@mail.gmail.com>")
        self.assertFalse(m.is_multipart())                     # careers@ got the PDF first time; never again
        self.assertNotIn("last week", m.get_content())         # 22 Oct -> 2 Nov is 11 days
        self.assertTrue(any(e["event"] == "followup" for e in store.events()))
        self.assertEqual(store.gmail_messages("pool-nuv-example")[-1]["row_type"], "followup")


class FollowUpTextTests(Env):
    ORIGINAL = ("Hi Acme team,\n\nI'm Sam, a final-year student. Acme helps clinics put their scheduling data to work, "
                "and data engineering for clients is a direction I'd like to grow in. I'd like to learn.\n\nMore.\n\n"
                "Best regards,\nYour Name")

    def test_company_line(self):
        self.assertTrue(company_line(self.ORIGINAL, "Acme").startswith("Acme helps clinics"))

    def test_three_variants_pass_the_validator(self):
        subject = "January 2027 intern or 2027 new grad at Acme?"
        for v in range(len(VARIANTS)):
            for greeting, role in (("Hi Acme team,", ""), ("Hi Priya,", "Backend Engineer Intern")):
                body, got = build_followup("t", greeting, "Acme", role, self.ORIGINAL, self.facts, variant=v)
                self.assertEqual(got, v)
                fails = validate_followup(follow_subject(subject), body, subject, greeting, self.facts)
                self.assertEqual(fails, [], f"variant {v + 1}: {fails}\n{body}")
                _, message, signature, _ = split_body(body)
                self.assertTrue(50 <= word_count(message) <= 70)
                self.assertIn("Acme", message)
                if role:
                    self.assertIn(f"the {role} role", message)

    def test_last_week_only_when_the_original_is_5_to_9_days_old(self):
        from applyflow.followups import earlier_phrase
        mon = MONDAY
        for days, phrase in ((4, "my earlier email"), (5, "my email from last week"), (9, "my email from last week"),
                             (10, "my earlier email"), (12, "my earlier email")):
            self.assertEqual(earlier_phrase(mon - timedelta(days=days), mon), phrase, days)
        self.assertEqual(earlier_phrase(None, mon), "my earlier email")
        subject = "January 2027 intern or 2027 new grad at Acme?"
        for sent, want in ((date(2026, 10, 21), "my earlier email"), (date(2026, 10, 27), "my email from last week")):
            body, _ = build_followup("t", "Hi,", "Acme", "", self.ORIGINAL, self.facts, variant=0, sent=sent, day=mon)
            self.assertIn(want, body)
            self.assertEqual(validate_followup(follow_subject(subject), body, subject, "Hi,", self.facts,
                                               sent=sent, day=mon), [])

    def test_validator_rejects_last_week_outside_5_to_9_days(self):
        subject = "Hello"
        body, _ = build_followup("t", "Hi,", "Acme", "", self.ORIGINAL, self.facts, variant=0,
                                 sent=date(2026, 10, 27), day=MONDAY)
        self.assertIn("last week", body)
        for sent in (date(2026, 10, 21), date(2026, 10, 29), None):
            fails = validate_followup("Re: Hello", body, subject, "Hi,", self.facts, sent=sent, day=MONDAY)
            self.assertTrue(any("last week" in f for f in fails), (sent, fails))

    def test_validator_catches_problems(self):
        subject = "Hello"
        body, _ = build_followup("t", "Hi,", "Acme", "", self.ORIGINAL, self.facts)
        self.assertIn("a follow-up never carries an attachment",
                      validate_followup("Re: Hello", body, subject, "Hi,", self.facts, attachment=True))
        two = body.replace(".", "?", 1)
        self.assertTrue(any("exactly one question" in f for f in validate_followup("Re: Hello", two, subject, "Hi,",
                                                                                  self.facts)))
        self.assertTrue(any("subject" in f for f in validate_followup("Hello", body, subject, "Hi,", self.facts)))
        no_sig = body.split("Best regards,")[0] + "Best regards,\nYour Name"
        fails = validate_followup("Re: Hello", no_sig, subject, "Hi,", self.facts)
        self.assertTrue({"signature lacks phone", "signature lacks LinkedIn", "signature lacks GitHub"} <= set(fails))
        long = body.replace("\n\nBest regards,", " I also wanted to add a few more words here to make this much "
                                                 "longer than it should be for a short note.\n\nBest regards,")
        self.assertTrue(any("words" in f for f in validate_followup("Re: Hello", long, subject, "Hi,", self.facts)))
        pushy = body.replace("\n\nBest regards,", " Just bumping this.\n\nBest regards,")
        self.assertTrue(any("pushy" in f for f in validate_followup("Re: Hello", pushy, subject, "Hi,", self.facts)))


# ---------------------------------------------------------------------------
# Inbox classification and check-inbox
# ---------------------------------------------------------------------------

class ClassifyTests(unittest.TestCase):
    def cat(self, name):
        return classify(parse_mail(gmail_fixture(name)))

    def test_dsns(self):
        c = self.cat("dsn_gmail_hard")
        self.assertEqual((c.category, c.status, c.recipients), ("hard_bounce", "5.1.1", ["careers@nowhere.example"]))
        c = self.cat("dsn_gmail_delay")
        self.assertEqual((c.category, c.status, c.recipients), ("soft_bounce", "4.4.1", ["jobs@slowmx.example"]))
        c = self.cat("dsn_postfix_hard")
        self.assertEqual((c.category, c.recipients), ("hard_bounce", ["hello@gone.example"]))

    def test_auto_replies(self):
        c = self.cat("auto_ats")
        self.assertEqual((c.category, c.person_will_respond), ("auto_reply", False))       # ATS: no follow-up
        c = self.cat("auto_person_will_respond")
        self.assertEqual((c.category, c.person_will_respond), ("auto_reply", True))        # Codelattice-style
        self.assertEqual(self.cat("auto_out_of_office").category, "auto_reply")

    def test_humans_and_drive(self):
        self.assertEqual(self.cat("human_reply").category, "human_reply")
        self.assertEqual(self.cat("human_thanks_no_openings").category, "human_reply")    # "thank you for reaching out"
        c = self.cat("drive_request")
        self.assertEqual(c.category, "drive_request")
        self.assertIn("arjun@zeta.example", c.detail)

    def test_parse_html_only_and_headers(self):
        m = parse_mail(gmail_fixture("human_reply"))
        self.assertEqual((m.from_email, m.from_name, m.thread_id), ("rahul@hydra.example", "Rahul Mehta",
                                                                    "thr-orig-7"))
        self.assertIn("internship slot", m.text)


class CheckInboxTests(Env):
    def setup_world(self):
        s = Store(":memory:")
        p = self.profile
        specs = [("pool:nowhere.example", "Nowhere", "careers@nowhere.example", "careers inbox"),
                 ("pool:slowmx.example", "Slow", "jobs@slowmx.example", "jobs inbox"),
                 ("pool:robo.example", "Robo", "careers@robo.example", "careers inbox"),
                 ("pool:codelab.example", "Codelab", "hr@codelab.example", "hr inbox"),
                 ("pool:hydra.example", "Hydra", "founders@hydra.example", "founders@ inbox")]
        for key, name, addr, role in specs:
            add_company(s, key, name, website=f"https://{key.split(':')[1]}",
                        contacts=[contact(addr, "published_inbox", role=role)])
        threads = {"pool-nowhere-example": "thr-orig-1", "pool-slowmx-example": "thr-orig-2",
                   "pool-codelab-example": "thr-orig-5", "pool-hydra-example": "thr-orig-7"}
        for key, *_ in specs:
            tid = key.replace(":", "-").replace(".", "-")
            sent_original(s, p, tid, date(2026, 10, 27), thread=threads.get(tid, ""))
        return s

    def test_check_inbox_applies_every_outcome(self):
        store = self.setup_world()
        names = ["dsn_gmail_hard", "dsn_gmail_delay", "auto_ats", "auto_person_will_respond", "human_reply",
                 "drive_request", "newsletter"]
        gmail = FakeGmail([gmail_fixture(n) for n in names])
        res = check_inbox(store, self.profile, gmail, at(MONDAY, 11), since=at(MONDAY, 0), me=ME)
        self.assertEqual(res.by_category, {"hard_bounce": 1, "soft_bounce": 1, "auto_reply": 2, "human_reply": 1,
                                           "drive_request": 1, "other": 1})
        items = {i.target_id: i for i in build_items(store, self.profile)}
        self.assertEqual(items["pool-nowhere-example"].status, "stopped")             # hard bounce -> --stop
        self.assertEqual(items["pool-robo-example"].outcome, "auto_ack")
        self.assertTrue(items["pool-robo-example"].followups_stopped)                  # ATS
        self.assertEqual(items["pool-codelab-example"].outcome, "auto_ack")
        self.assertFalse(items["pool-codelab-example"].followups_stopped)              # a person will respond
        self.assertEqual(items["pool-hydra-example"].outcome, "needs_action")
        self.assertEqual(items["pool-hydra-example"].replied_by, "Rahul Mehta <rahul@hydra.example>")
        self.assertIn("rahul@hydra.example", [c["email"] for c in store.contacts_for("pool:hydra.example")])
        self.assertEqual(len(res.needs_action), 1)
        self.assertIn("#all/thr-orig-7", res.needs_action[0])
        # the bounce counts in the per-type gate
        from applyflow.outreach import send_records
        self.assertEqual([r.bounced_on for r in send_records(store) if r.bounced_on], ["2026-11-02"])
        # idempotent: a second check applies nothing new
        again = check_inbox(store, self.profile, gmail, at(MONDAY, 15), since=at(MONDAY, 0), me=ME)
        self.assertEqual(again.looked_at, 0)
        # 3 Nov (follow-ups due): Codelab gets one; Slow (soft bounce) gets its retry, its follow-up waits
        tue = MONDAY + timedelta(days=1)
        plan = plan_day(store, self.profile, tue)
        self.assertEqual({f.item.target_id for f in plan.followups}, {"pool-codelab-example"})
        self.assertEqual([i.target_id for i in plan.retries], ["pool-slowmx-example"])
        self.assertEqual(plan.followup_skips.get("soft bounce: the retry goes first"), 1)
        clock = Clock(at(tue, 9))
        self.send(store, FakeGmail(clock=clock), clock)
        retry = [m for m in store.gmail_messages("pool-slowmx-example") if m["row_type"] == "retry"]
        self.assertEqual(len(retry), 1)
        self.assertEqual(plan_day(store, self.profile, tue + timedelta(days=1)).retries, [])     # once only
        due, _ = due_followups(store, build_items(store, self.profile), add_working_days(tue, 5),
                               self.profile.outreach["sending"], self.facts)
        slow = next(f for f in due if f.item.target_id == "pool-slowmx-example")
        self.assertEqual(slow.original["gmail_id"], retry[0]["gmail_id"])                        # replies to the retry
        plan_day(store, self.profile, tue)                    # the digest below reads the 3 Nov plan
        _, text = build_digest(store, self.profile, tue.isoformat(), at(MONDAY, 20))
        self.assertIn("Human replies needing your action: 1", text)
        self.assertIn("https://mail.google.com/mail/u/0/#all/thr-orig-7", text)
        self.assertIn("arjun@zeta.example", text)
        self.assertIn("hard_bounce: careers@nowhere.example (5.1.1)", text)

    def test_bounce_recorded_by_hand_is_not_doubled(self):
        store = self.setup_world()
        from applyflow.outreach import mark_bounced
        mark_bounced(store, self.profile, "pool-nowhere-example", MONDAY, stop=True)
        res = check_inbox(store, self.profile, FakeGmail([gmail_fixture("dsn_gmail_hard")]), at(MONDAY, 11),
                          since=at(MONDAY, 0), me=ME)
        self.assertIn("already recorded", res.applied[0])
        self.assertEqual(sum(e["event"] == "bounced" for e in store.events()), 1)


# ---------------------------------------------------------------------------
# reconcile, plan, review, digest, summary
# ---------------------------------------------------------------------------

class ReconcileTests(Env):
    def test_matches_by_recipient_and_subject(self):
        store = world()
        p = self.profile
        from applyflow.outreach import make_queue
        make_queue(store, p, date(2026, 10, 29))
        mark_sent(store, p, "pool-hydra-example", date(2026, 10, 29))          # marked by hand; nuv and nimbus not
        items = {i.target_id: i for i in build_items(store, p)}
        gmail = FakeGmail()
        gmail.add_sent("founders@hydra.example", items["pool-hydra-example"].draft.subject, at(date(2026, 10, 29), 9, 14))
        gmail.add_sent("Nuv <careers@nuv.example>", items["pool-nuv-example"].draft.subject + " ",
                       at(date(2026, 10, 30), 10, 2))                          # sent a day late
        gmail.add_sent("priya@nimbus.example", "A subject I edited by hand", at(date(2026, 10, 29), 9, 40))
        gmail.add_sent("someone@else.example", items["pool-nuv-example"].draft.subject, at(date(2026, 10, 29), 9))
        res = reconcile(store, p, gmail, date(2026, 10, 21), date(2026, 10, 29), scan_inbox=False)
        self.assertEqual(len(res.found), 3)
        self.assertEqual(len(res.marked), 2)                                   # nuv + nimbus newly marked
        self.assertIn("subject differs", " ".join(res.found))
        sent = {e["target_id"]: e for e in store.events() if e["event"] == "sent"}
        self.assertEqual(sent["pool-nuv-example"]["event_date"], "2026-10-30")   # the real send date
        self.assertEqual(sent["pool-nuv-example"]["batch_date"], "2026-10-29")
        msgs = {r["target_id"]: r for r in store.gmail_messages()}
        self.assertEqual(msgs["pool-hydra-example"]["thread_id"], "t1")
        self.assertEqual(msgs["pool-hydra-example"]["rfc_id"], "<msg1@mail.gmail.com>")
        self.assertEqual(msgs["pool-hydra-example"]["source"], "reconcile")
        again = reconcile(store, p, gmail, date(2026, 10, 21), date(2026, 10, 29), scan_inbox=False)
        self.assertEqual((again.found, again.marked), ([], []))

    def test_missing_rows_are_listed_and_released_on_request(self):
        store = world()
        from applyflow.outreach import make_queue
        make_queue(store, self.profile, date(2026, 10, 29))
        res = reconcile(store, self.profile, FakeGmail(), date(2026, 10, 29), date(2026, 10, 29), scan_inbox=False)
        self.assertEqual(len(res.missing), 3)
        self.assertEqual([e for e in store.events() if e["event"] == "sent"], [])
        reconcile(store, self.profile, FakeGmail(), date(2026, 10, 29), date(2026, 10, 29), release=True,
                  scan_inbox=False)
        self.assertEqual(store.queue("2026-10-29"), [])


class HandSentBatchTests(Env):
    def test_a_queue_batch_is_never_planned_or_sent_by_the_engine(self):
        from applyflow.outreach import make_queue
        from applyflow.sending import HandSentDay
        store = world(5)
        make_queue(store, self.profile, MONDAY, size=3)          # `queue --date` Monday: sent by hand from Gmail
        hand = {r["target_id"] for r in store.queue(MONDAY.isoformat())}
        with self.assertRaises(HandSentDay):
            plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 9))
        gmail = FakeGmail(clock=clock)
        self.assertIn("hand-sent", self.send(store, gmail, clock).stopped)
        tue = MONDAY + timedelta(days=1)
        plan = plan_day(store, self.profile, tue)                  # before reconcile: Monday's rows are held out
        self.assertFalse(hand & {it.target_id for it in plan.new})
        self.assertEqual(len(plan.new), 2)
        clock = Clock(at(tue, 9))
        self.send(store, gmail, clock)
        self.assertFalse(hand & {r["target_id"] for r in store.gmail_messages()})
        # Monday evening's reconcile finds them in Sent and marks them on their real date
        items = {i.target_id: i for i in build_items(store, self.profile)}
        for tid in hand:
            gmail.add_sent(items[tid].address.email, items[tid].draft.subject, at(MONDAY, 9, 30))
        res = reconcile(store, self.profile, gmail, date(2026, 10, 21), MONDAY, scan_inbox=False)
        self.assertEqual(len(res.marked), 3)
        self.assertEqual({e["event_date"] for e in store.events() if e["event"] == "sent" and e["target_id"] in hand},
                         {MONDAY.isoformat()})


class PlanTests(Env):
    def test_shortfall_is_flagged(self):
        store = world()
        res = plan_day(store, self.profile, MONDAY)
        self.assertEqual((len(res.new), res.short), (3, 27))
        self.assertTrue(res.review_path.exists())
        self.assertIn("SHORT BY 27", res.digest_path.read_text(encoding="utf-8"))

    def test_full_day_and_ceiling(self):
        store = world(40)
        res = plan_day(store, self.profile, MONDAY)
        self.assertEqual((len(res.new), res.short), (30, 0))
        self.assertEqual(len(store.plan_rows(MONDAY.isoformat())), 30)

    def test_drop_and_hold(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        msg = drop(store, "careers@nuv.example", MONDAY.isoformat())
        self.assertIn("dropped new row", msg)
        self.assertIn("careers@nuv.example", store.excluded_contacts())
        plan_day(store, self.profile, MONDAY)                           # a replan doesn't bring it back
        live = [r["email"] for r in store.plan_rows(MONDAY.isoformat()) if r["status"] == "planned"]
        self.assertNotIn("careers@nuv.example", live)
        with self.assertRaises(ValueError):
            drop(store, "nobody@x.example", MONDAY.isoformat())
        hold(store, MONDAY.isoformat())
        self.assertEqual(store.plan_rows(MONDAY.isoformat(), "planned"), [])
        tue = plan_day(store, self.profile, MONDAY + timedelta(days=1))
        self.assertEqual(len(tue.new), 2)                                # the held rows move on
        held = plan_day(store, self.profile, MONDAY)
        self.assertTrue(held.held)

    def test_digest_contents(self):
        store = world()
        sent_original(store, self.profile, "pool-hydra-example", date(2026, 10, 22))
        mark_replied(store, self.profile, "founders@hydra.example", "declined_warm", date(2026, 10, 28), "2026-11-01",
                     "Nishkarsh S <nishkarsh@hydra.example>")
        res = plan_day(store, self.profile, MONDAY)
        subject, text = build_digest(store, self.profile, MONDAY.isoformat(), at(date(2026, 11, 1), 20), res.short)
        self.assertIn("short by 28", subject)
        for needle in ("Plan for 2026-11-02: 2 row(s) - 2 new, 0 follow-up(s)", "Nuv", "careers@nuv.example",
                       "hiring inbox", "priya@nimbus.example", "Priya Sharma", "Brakes: none", "SHORT BY 28",
                       "Recheck list: 1", "Hydra: replied: not now", "Supply:", "Bounces since the last digest: 0",
                       "Google Drive access requests since the last digest: 0", "drop <row|email>"):
            self.assertIn(needle, text)

    def test_summary(self):
        store = world()
        plan_day(store, self.profile, MONDAY)
        clock = Clock(at(MONDAY, 9))
        self.send(store, FakeGmail(clock=clock), clock)
        text = summary(store, self.profile, MONDAY)
        self.assertIn("Sent today: 3 (new 3)", text)
        self.assertIn("Brake: off", text)
        self.assertIn("Follow-ups due:", text)
        self.assertIn("Replies by outcome: none", text)


if __name__ == "__main__":
    unittest.main()
