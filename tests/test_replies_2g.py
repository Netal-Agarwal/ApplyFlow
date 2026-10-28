"""Phase 2g: recording replies (mark-replied: interested / declined_warm / declined / auto_ack), the Recheck list,
stopped targets that reopen only through a verified person or a published hiring inbox, and evidence-guess outcomes
by address format and evidence kind (first-name evidence ranked above staff first.last evidence)."""

import unittest
from datetime import date

from applyflow.contacts import ContactResult, Target
from applyflow.outreach import (OutreachError, build_items, evidence_kind, guess_format, guess_outcomes, make_queue,
                                mark_bounced, mark_replied, mark_sent, parse_contact, recheck_due, send_records,
                                type_gates)
from applyflow.report import recheck_section
from applyflow.store import Store
from tests.helpers import example_profile
from tests.test_drafting import add_company, contact

PROFILE = example_profile()
DAY = date(2026, 10, 28)


def item(store, tid):
    return next(i for i in build_items(store, PROFILE) if i.target_id == tid)


def sent_store():
    store = Store(":memory:")
    add_company(store, "pool:hydra.example", "Hydra", website="https://hydra.example",
                contacts=[contact("founders@hydra.example", "published_inbox", role="founders@ inbox")])
    add_company(store, "pool:nuv.example", "Nuv", website="https://nuv.example",
                contacts=[contact("hrindia@nuv.example", "published_inbox", role="hr inbox")])
    mark_sent(store, PROFILE, "pool-hydra-example", date(2026, 10, 27))
    mark_sent(store, PROFILE, "pool-nuv-example", DAY)
    return store


class MarkRepliedTests(unittest.TestCase):
    def test_declined_warm_from_a_person_on_the_founders_thread(self):
        store = sent_store()
        it = mark_replied(store, PROFILE, "founders@hydra.example", "declined_warm", DAY, "2026-12-01",
                          "Nishkarsh Srivastava <Nishkarsh@hydra.example>")
        self.assertEqual((it.status, it.outcome, it.recheck, it.followups_stopped),
                         ("sent", "declined_warm", "2026-12-01", True))
        self.assertIn("recheck 2026-12-01", it.status_note)
        person = next(c for c in store.contacts_for("pool:hydra.example") if c["source"] == "reply")
        self.assertEqual((person["name"], person["email"], person["confidence"], person["proof"]),
                         ("Nishkarsh Srivastava", "nishkarsh@hydra.example", "verified", "reply"))
        from applyflow.outreach import contact_kind
        self.assertEqual(contact_kind(person["email"], person["confidence"], person["source_url"], person["name"],
                                      person["source"]), "verified_person")

    def test_replier_address_finds_the_target_by_domain(self):
        store = sent_store()
        it = mark_replied(store, PROFILE, "nkumar@nuv.example", "declined_warm", DAY, "2027-01-05",
                          "Naresh Kumar <nkumar@nuv.example>", role="HR")
        self.assertEqual((it.target_id, it.replied_by), ("pool-nuv-example", "Naresh Kumar <nkumar@nuv.example>"))

    def test_reply_contacts_survive_a_new_lookup(self):
        store = sent_store()
        mark_replied(store, PROFILE, "founders@hydra.example", "declined_warm", DAY, "2026-12-01",
                     "Nishkarsh Srivastava <nishkarsh@hydra.example>")
        t = Target(key="pool:hydra.example", company="Hydra", kind="general_outreach", hint="founder or CTO",
                   website="https://hydra.example", domain="hydra.example", team_size=20)
        store.save_contact_result(ContactResult(target=t, status="found",
                                                contacts=[contact("founders@hydra.example", "published_inbox")]))
        self.assertIn("nishkarsh@hydra.example", [c["email"] for c in store.contacts_for("pool:hydra.example")])

    def test_auto_ack_is_delivered_and_stops_follow_ups(self):
        store = sent_store()
        it = mark_replied(store, PROFILE, "hrindia@nuv.example", "auto_ack", DAY)
        self.assertEqual((it.status, it.outcome, it.followups_stopped), ("sent", "auto_ack", True))
        self.assertEqual(len(send_records(store)), 2)
        self.assertFalse(any(r.bounced_on for r in send_records(store)))      # delivered, not a bounce

    def test_errors(self):
        store = sent_store()
        with self.assertRaises(OutreachError):                # declined_warm needs a date
            mark_replied(store, PROFILE, "founders@hydra.example", "declined_warm", DAY)
        with self.assertRaises(OutreachError):                # unknown outcome
            mark_replied(store, PROFILE, "founders@hydra.example", "maybe", DAY)
        with self.assertRaises(OutreachError):                # nothing sent on that domain
            mark_replied(store, PROFILE, "a@elsewhere.example", "declined", DAY)
        with self.assertRaises(OutreachError):                # the replier must be on the company's domain
            mark_replied(store, PROFILE, "founders@hydra.example", "declined", DAY, contact="X Y <x@gmail.com>")
        with self.assertRaises(OutreachError):
            parse_contact("nishkarsh@hydra.example")

    def test_recheck_list_when_the_date_arrives(self):
        store = sent_store()
        mark_replied(store, PROFILE, "founders@hydra.example", "declined_warm", DAY, "2026-12-01",
                     "Nishkarsh Srivastava <nishkarsh@hydra.example>")
        mark_replied(store, PROFILE, "hrindia@nuv.example", "declined_warm", DAY, "2027-01-05")
        items = build_items(store, PROFILE)
        self.assertEqual(recheck_due(items, date(2026, 11, 30)), [])
        self.assertEqual([i.company for i in recheck_due(items, date(2026, 12, 1))], ["Hydra"])
        self.assertEqual(len(recheck_due(items, date(2027, 1, 5))), 2)
        html = recheck_section(recheck_due(items, date(2026, 12, 1)))
        self.assertIn("Recheck", html)
        self.assertIn("Nishkarsh Srivastava", html)
        self.assertEqual(recheck_section([]), "")


class StoppedReopenTests(unittest.TestCase):
    def guess_store(self):
        store = Store(":memory:")
        ev = {"format": "first.last", "address": "rohit.taneja@dec.example", "person": "Rohit Taneja",
              "role": "Founder", "published": "aditya.iyer@dec.example", "published_url": "https://dec.example/privacy"}
        g = contact("rohit.taneja@dec.example", "guessed", name="Rohit Taneja", role="Founder", source="pattern",
                    alternates=["rohit@dec.example"])
        g.evidence = ev
        add_company(store, "yc:dec", "Dec", website="https://dec.example", contacts=[g])
        mark_sent(store, PROFILE, "yc-dec", DAY)
        return store

    def test_evidence_guess_bounce_stops_and_counts_toward_the_gate(self):
        store = self.guess_store()
        it = mark_bounced(store, PROFILE, "yc-dec", DAY, stop=True)
        self.assertEqual(it.status, "stopped")                          # not rohit@ (another guess)
        gate = type_gates(send_records(store), DAY, PROFILE.outreach)["evidence_guess"]
        self.assertEqual(gate.state, "capped")                          # 1 bounce today: below the 2/day pause

    def test_reopens_only_through_a_verified_person_or_hiring_inbox(self):
        store = self.guess_store()
        mark_bounced(store, PROFILE, "yc-dec", DAY, stop=True)
        t = Target(key="yc:dec", company="Dec", kind="general_outreach", hint="founder or CTO",
                   website="https://dec.example", domain="dec.example", team_size=20)
        another_guess = contact("rohit@dec.example", "guessed", name="Rohit Taneja", role="Founder", source="pattern")
        another_guess.evidence = {"format": "first", "published": "aditya@dec.example"}
        store.save_contact_result(ContactResult(target=t, status="found", contacts=[another_guess]))
        self.assertEqual(item(store, "yc-dec").status, "stopped")
        careers = contact("careers@dec.example", "published_inbox", role="careers@ inbox")
        careers.proof = "company site"
        store.save_contact_result(ContactResult(target=t, status="found", contacts=[another_guess, careers]))
        it = item(store, "yc-dec")
        self.assertEqual((it.status, it.address.email), ("ready", "careers@dec.example"))
        self.assertNotIn("rohit@dec.example", [a.email for a in it.backups])


class GuessOutcomeTests(unittest.TestCase):
    def test_kinds_and_formats(self):
        self.assertEqual(evidence_kind({"published": "rahul@x.example"}), "first_name")
        self.assertEqual(evidence_kind('{"published": "aditya.iyer@x.example"}'), "staff_first_last")
        self.assertEqual(evidence_kind(None), "")
        self.assertEqual(guess_format({"format": "first"}), "first@")
        self.assertEqual(guess_format({"format": "first.last"}), "first.last@")
        self.assertEqual(guess_format({"format": "flast"}), "other")

    def test_outcomes_by_format_and_evidence(self):
        store = StoppedReopenTests().guess_store()
        mark_bounced(store, PROFILE, "yc-dec", DAY, stop=True)
        out = guess_outcomes(store)
        self.assertEqual(out["format"], {"first.last@": {"sent": 1, "bounced": 1, "replied": 0}})
        self.assertEqual(out["evidence"], {"staff_first_last": {"sent": 1, "bounced": 1, "replied": 0}})

    def test_first_name_evidence_ranked_above_staff_first_last(self):
        store = Store(":memory:")
        for key, published in (("yc:aaa", "priya.shah@aaa.example"), ("yc:bbb", "vikram@bbb.example")):
            dom = key.split(":")[1] + ".example"
            g = contact(f"asha@{dom}", "guessed", name="Asha Rao", role="Founder", source="pattern")
            g.evidence = {"format": "first", "address": f"asha@{dom}", "published": published,
                          "published_url": f"https://{dom}/about", "person_url": f"https://{dom}/team"}
            add_company(store, key, key.upper(), website=f"https://{dom}", contacts=[g])
        order = [i.target_id for i in build_items(store, PROFILE) if i.status == "ready"]
        self.assertLess(order.index("yc-bbb"), order.index("yc-aaa"))
        batch = make_queue(store, PROFILE, DAY, save=False)
        ids = [b.target_id for b in batch.items]
        if len(ids) == 2:
            self.assertEqual(ids, ["yc-bbb", "yc-aaa"])


class BatchOrderTests(unittest.TestCase):
    def test_people_and_founders_then_hiring_inboxes_at_established_companies_first(self):
        store = Store(":memory:")
        rows = (("tiny", "careers@tiny.example", 12, "published_inbox", ""),
                ("big", "careers@big.example", 120, "published_inbox", ""),
                ("fnd", "founders@fnd.example", 8, "published_inbox", ""),
                ("ppl", "asha@ppl.example", 8, "verified", "Asha Rao"))
        for slug, email, size, conf, name in rows:
            add_company(store, f"yc:{slug}", slug.title(), website=f"https://{slug}.example", team_size=size,
                        contacts=[contact(email, conf, name, "Founder" if name else f"{slug}@ inbox")])
        order = [i.target_id for i in build_items(store, PROFILE) if i.status == "ready"]
        self.assertEqual(order, ["yc-ppl", "yc-fnd", "yc-big", "yc-tiny"])


if __name__ == "__main__":
    unittest.main()
