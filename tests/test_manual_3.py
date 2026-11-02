"""Manual sending (2026-11-02): follow-ups without Gmail records, stopped / info@ targets left out, and the `daily`
evening routine (mark the day's batch and follow-up checklist sent, the all-type brake, the next batch, the files)."""

import json
from datetime import date, timedelta

from applyflow.contacts import ContactResult, Target
from applyflow.manual import daily, followup_checklist
from applyflow.outreach import build_items, make_queue, mark_bounced, mark_sent
from applyflow.store import Store
from tests.test_drafting import add_company, contact
from tests.test_sending_3 import MONDAY, Env, world

TUE = MONDAY + timedelta(days=1)


class ManualFollowUpTests(Env):
    def test_due_without_gmail_records_only_in_manual_mode(self):
        store = world()
        mark_sent(store, self.profile, "pool-nuv-example", date(2026, 10, 22))
        due, why = followup_checklist(store, self.profile, TUE, 10)
        self.assertEqual([f.item.target_id for f in due], ["pool-nuv-example"])
        it = due[0].item
        self.assertEqual(due[0].original["subject"], it.draft.subject)           # find it in Sent by this subject
        self.assertEqual(due[0].subject, f"Re: {it.draft.subject}")
        self.assertNotIn("last week", due[0].body)                             # 22 Oct -> 3 Nov: 12 days
        from applyflow.drafting import load_facts
        from applyflow.followups import due_followups
        engine, why = due_followups(store, build_items(store, self.profile), TUE, self.profile.outreach["sending"],
                                    load_facts(self.profile))
        self.assertEqual(engine, [])                                           # the engine needs a Gmail record

    def test_stopped_by_hand_and_info_inboxes_get_none(self):
        store = world()
        add_company(store, "pool:inf.example", "Inf", website="https://inf.example",
                    contacts=[contact("careers@inf.example", "published_inbox", role="careers inbox")])
        for tid in ("pool-nuv-example", "pool-hydra-example", "pool-inf-example"):
            mark_sent(store, self.profile, tid, date(2026, 10, 22))
        it = next(i for i in build_items(store, self.profile) if i.target_id == "pool-hydra-example")
        store.add_event(it.target_id, it.company_key, it.sent_email, "stopped", "2026-10-23")   # no bounce
        store.conn.execute("UPDATE outreach_events SET email = 'info@inf.example' WHERE target_id = 'pool-inf-example'")
        store.conn.commit()
        due, why = followup_checklist(store, self.profile, TUE, 10)
        self.assertEqual([f.item.target_id for f in due], ["pool-nuv-example"])
        self.assertEqual(why.get("stopped"), 1)

    def test_oldest_first_and_cap(self):
        store = world(8)
        tids = [i.target_id for i in build_items(store, self.profile) if i.status == "ready"]
        for n, tid in enumerate(tids):
            mark_sent(store, self.profile, tid, date(2026, 10, 27) - timedelta(days=n % 3))
        due, why = followup_checklist(store, self.profile, TUE, 4)
        days = [f.item.sent_date for f in due]
        self.assertEqual(days, sorted(days))
        self.assertEqual(len(due), 4)
        self.assertEqual(why["over the daily follow-up cap (next day)"], len(tids) - 4)


class DailyTests(Env):
    def run_daily(self, store, sent, **kw):
        return daily(store, self.profile, sent, out_dir=self.tmp, backup_dir=self.tmp, log=lambda _: None, **kw)

    def test_evening_routine(self):
        store = world(12)
        for tid in ("pool-co10-example", "pool-co11-example"):               # sent 21 Oct: follow-ups due Tuesday
            mark_sent(store, self.profile, tid, date(2026, 10, 21))
        make_queue(store, self.profile, MONDAY, size=4)                       # Monday's batch, sent by hand
        mon = [r["target_id"] for r in store.queue(MONDAY.isoformat())]
        res = self.run_daily(store, MONDAY, skip=(mon[-1],))
        self.assertTrue(res.backup.exists())
        self.assertEqual(len(res.marked), 3)
        self.assertEqual(res.skipped, [mon[-1]])
        self.assertEqual(res.next_day, TUE.isoformat())
        tue = [r["target_id"] for r in store.queue(TUE.isoformat())]
        self.assertIn(mon[-1], tue)                                          # the unsent row is back in the pool
        self.assertFalse(set(mon[:-1]) & set(tue))
        self.assertTrue((self.tmp / f"batch_{TUE.isoformat()}.md").exists())
        text = (self.tmp / f"followups_{TUE.isoformat()}.md").read_text(encoding="utf-8")
        self.assertIn("reply in the original thread", text)
        self.assertEqual(json.loads(store.get_state(f"followups:{TUE.isoformat()}")),
                         [f.item.target_id for f in res.followups])
        self.assertEqual({f.item.target_id for f in res.followups}, {"pool-co10-example", "pool-co11-example"})
        other = [p for r in res.rows for p in r["problems"] if not p.startswith("no validated Claude draft")]
        self.assertEqual(other, [])                    # the test world has template drafts: only that is flagged
        # Tuesday evening: the batch and the checklist's follow-ups (one skipped) are recorded
        wed = self.run_daily(store, TUE, skip_followups=("pool-co11-example",))
        self.assertEqual(len(wed.marked), len(tue))
        self.assertEqual(wed.followups_marked, ["pool-co10-example -> careers@co10.example"])
        fu = [e for e in store.events() if e["event"] == "followup"]
        self.assertEqual([(e["target_id"], e["event_date"]) for e in fu], [("pool-co10-example", TUE.isoformat())])
        self.assertEqual([f.item.target_id for f in wed.followups], ["pool-co11-example"])   # one per contact
        again = self.run_daily(store, TUE, skip_followups=("pool-co11-example",))   # rerun: nothing marked twice
        self.assertEqual((again.marked, again.followups_marked), ([], []))

    def test_daily_brake_stops_the_next_batch(self):
        store = world(8)
        make_queue(store, self.profile, MONDAY, size=5)
        self.run_daily(store, MONDAY, next_day=MONDAY + timedelta(days=7))   # mark Monday only
        for r in store.queue(MONDAY.isoformat())[:4]:
            mark_bounced(store, self.profile, r["target_id"], MONDAY, stop=True)
        res = self.run_daily(store, MONDAY)
        self.assertIn("4 hard bounces", res.brake)
        self.assertEqual((res.batch_path, store.queue(TUE.isoformat())), (None, []))
        forced = self.run_daily(store, MONDAY, force=True)
        self.assertIsNotNone(forced.batch_path)


class CompanyFitTests(Env):
    """2026-11-03: every row needs an India team (or remote-India hiring) and a software / tech company."""

    def inputs(self, **kw):
        from applyflow.drafting import DraftInputs
        return DraftInputs(target_id="t", company_key="k", company=kw.pop("company", "Acme"), kind="general_outreach",
                           **kw)

    def test_india_team(self):
        from applyflow.outreach import india_gap
        p = self.profile
        self.assertIn("no India team", india_gap(self.inputs(locations="San Francisco, CA, USA; Remote",
                                                             remote_global=True), p))       # Ready
        self.assertIn("Paris", india_gap(self.inputs(locations="Paris, France; Remote", remote_global=True), p))
        self.assertEqual(india_gap(self.inputs(locations="Bengaluru; Remote", remote_global=True), p), "")
        self.assertEqual(india_gap(self.inputs(locations="Bengaluru, KA, India"), p), "")
        self.assertEqual(india_gap(self.inputs(locations="Remote", remote_global=True,
                                               role_location="remote_open"), p), "")         # remote, open to India
        self.assertEqual(india_gap(self.inputs(), p), "")                                  # India pool region / manual

    def test_tech_company(self):
        from applyflow.outreach import tech_gap
        p = self.profile
        binks = self.inputs(company="Binks", one_liner="A zero inventory, zero returns factory-to-consumer apparel company")
        self.assertIn("apparel", tech_gap("Binks", binks, None, "startup", p))
        df = self.inputs(one_liner="Intelligent data processing with OCR and NLP",
                         meta_description="AI for life sciences, pharma, cosmetics and medical devices")
        self.assertEqual(tech_gap("Datafoundry", df, None, "startup", p), "")            # serves cosmetics, is software
        october = self.inputs(meta_description="Orchestrate a fleet of local and cloud coding agents.")
        self.assertEqual(tech_gap("October", october, None, "startup", p), "")
        shut = self.inputs(homepage_snippet="as of October 2024, Neuphony will not be operational anymore")
        self.assertIn("shut down", tech_gap("Neuphony", shut, None, "startup", p))
        self.assertIn("shut down", tech_gap("X", shut, None, "consultancy", p))          # applies to every sector
        lender = self.inputs(one_liner="An NBFC for small businesses")
        self.assertEqual(tech_gap("Lender", lender, None, "fintech", p), "")             # vetted fintech seed

    def test_batch_skips_them(self):
        store = world(4)
        add_company(store, "yc:binks", "Binks", website="https://getbinks.example", locations="Bengaluru, KA, India",
                    description="A factory-to-consumer apparel company",
                    contacts=[contact("careers@getbinks.example", "published_inbox", role="careers inbox")])
        add_company(store, "yc:ready", "Ready", website="https://ready.example",
                    locations="San Francisco, CA, USA; Remote", description="Broadband software for public agencies",
                    contacts=[contact("careers@ready.example", "published_inbox", role="careers inbox")])
        batch = make_queue(store, self.profile, MONDAY)
        picked = {it.target_id for it in batch.items}
        self.assertFalse({"yc-binks", "yc-ready"} & picked)
        self.assertEqual(batch.held_by_kind.get("no India team or not a tech company"), 2)
        self.assertEqual(len(picked), 4)
