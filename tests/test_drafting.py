"""Phase 2b: drafts, validation, resume action, send queue, bounces, add-target, packets, Gmail links."""

import contextlib
import io
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from applyflow.contacts import Contact, ContactResult, DoNotContact, Target, build_targets
from applyflow.drafting import (DraftInputs, display_company, gmail_url, load_facts, parse_written, resume_action,
                                split_body, tech_terms, template_draft, validate, word_count)
from applyflow.models import DiscoveredCompany, Job, ScoreResult
from applyflow.outreach import (add_target, build_items, ensure_context, import_written, make_queue,
                                mark_bounced, mark_sent, pick_batch, send_tier, write_packets)
from applyflow.store import Store
from applyflow.text import page_summary
from tests.helpers import FIXTURES, example_profile
from tests.test_contacts import ACME, acme_routes, client

D = FIXTURES / "drafting"
NIMBUS = "https://nimbus.example"
PROFILE = example_profile()
FACTS = load_facts(PROFILE)
TERMS = tech_terms(PROFILE)


def contact(email, confidence, name="", role="", source="website", alternates=()):
    return Contact(name=name, role=role, email=email, confidence=confidence, source=source,
                   source_url=f"https://{email.split('@')[1]}/team", alternates=list(alternates))


def add_company(store, key, name, *, description="", team_size=20, locations="Bengaluru, KA, India",
                website="", contacts=(), kind="general_outreach", status="found", batch="Winter 2024"):
    domain = (website or f"https://{key.split(':')[1]}.example").split("//")[1]
    store.upsert_discovered(DiscoveredCompany(key=key, name=name, website=website or f"https://{domain}",
                                              team_size=team_size, description=description, locations=locations,
                                              batch=batch, board_status="none"))
    target = Target(key=key, company=name, kind=kind, hint="founder or CTO", website=f"https://{domain}",
                    domain=domain, team_size=team_size)
    store.save_contact_result(ContactResult(target=target, status=status if contacts or status != "found" else "none",
                                            contacts=list(contacts)))


def add_role(store, company="Nimbus Labs", key="yc:nimbus", title="Backend Engineer Intern", description="",
             location_class="preferred_india", track="internship", contacts=()):
    add_company(store, key, company, description="Observability for serverless apps", team_size=12,
                website=NIMBUS, contacts=contacts, kind="role")
    job = Job(source="lever", external_id="nimbus-1", company=company, title=title, url="https://jobs.example/1",
              description=description or "Build APIs in Python and FastAPI on PostgreSQL. 0-1 years.",
              locations=["Bengaluru"], team_size=12)
    store.upsert_board("lever:nimbus", [job])
    store.save_scores([ScoreResult(job_id=job.id, accepted=True, score=72, tier="A", family="backend", track=track,
                                   location_class=location_class, action="apply_plus_email",
                                   contact_hint="founder or CTO", lead_project="CareerForge AI",
                                   matched_skills=["python", "fastapi", "postgresql"], reasons=["test"])])


def demo_store():
    """Role target (verified CTO), named founder (guessed + alternates + backup inbox), HR inbox, plain
    inbox, another guess, and a target with no contact."""
    s = Store(":memory:")
    add_role(s, contacts=[contact("priya@nimbus.example", "verified", "Priya Sharma", "CTO")])
    add_company(s, "yc:orbit-pay", "Orbit Pay, Inc.", description="Payments infrastructure for Indian SMBs",
                contacts=[contact("ravi@orbitpay.example", "guessed", "Ravi Kumar", "Founder", "pattern",
                                  ["ravi.kumar@orbitpay.example", "ravikumar@orbitpay.example"]),
                          contact("hello@orbitpay.example", "published_inbox", role="hello@ inbox")],
                website="https://orbitpay.example")
    add_company(s, "yc:lumen-health", "Lumen Health", description="At-home diagnostics for women",
                locations="Bengaluru, KA, India; Remote",
                contacts=[contact("careers@lumen.example", "published_inbox", role="careers@ inbox")],
                website="https://lumen.example")
    add_company(s, "yc:cobalt", "Cobalt", description="", team_size=8,
                contacts=[contact("hello@cobalt.example", "published_inbox", role="hello@ inbox")],
                website="https://cobalt.example")
    add_company(s, "yc:delta", "Delta AI", description="AI agents for warehouse planning", team_size=30,
                contacts=[contact("deepa@delta.example", "guessed", "Deepa Rao", "CTO", "pattern")],
                website="https://delta.example")
    add_company(s, "yc:ghost", "Ghost Co", description="Something", status="none")
    return s


def items_by_id(store, dnc=None):
    return {it.target_id: it for it in build_items(store, PROFILE, dnc)}


def body_of(draft):
    return split_body(draft.body)[1]


class RoleDraftTests(unittest.TestCase):
    def setUp(self):
        self.store = demo_store()
        self.it = items_by_id(self.store)["yc-nimbus"]

    def test_role_draft_names_role_project_availability_and_passes(self):
        d = self.it.draft
        self.assertEqual(d.source, "template")
        self.assertEqual(d.template_failures, [])
        self.assertTrue(d.body.startswith("Hi Priya,\n"))
        text = body_of(d)
        self.assertIn("Backend Engineer Intern role at Nimbus Labs", text)
        self.assertIn("(Observability for serverless apps)", text)        # one specific line about the company
        self.assertIn("CareerForge AI, a full-stack web app on FastAPI and PostgreSQL with 12 REST endpoints", text)
        self.assertIn("I also built Serverless FileFinder, a serverless file service", text)     # second project
        self.assertIn("Day to day I work in Python, FastAPI, PostgreSQL, REST APIs", text)       # posting first
        self.assertIn("a 6-month internship from January 2027", text)
        self.assertIn("My resume is linked below.", text)
        self.assertIn("I'd be happy to talk", text)
        self.assertIn("Before this, I interned at Example Research Lab.", text)
        self.assertNotIn("time zones", text)                              # preferred-city role
        self.assertTrue(160 <= word_count(text) <= 200)
        self.assertEqual(d.subject, "Backend Engineer Intern - B.Tech CS, Example University (2027)")
        self.assertEqual((d.project, d.resume_action), ("CareerForge AI + Serverless FileFinder", "link"))

    def test_signature_from_the_profile(self):
        sig = split_body(self.it.draft.body)[2]
        self.assertEqual(sig, ["Your Name", "B.Tech CS, Example University (2027)", "+91 90000 00000",
                               "LinkedIn: https://www.linkedin.com/in/your-handle/",
                               "GitHub: https://github.com/your-username", "Resume: https://example.com/your-resume.pdf"])

    def test_remote_role_gets_a_time_zone_line(self):
        store = Store(":memory:")
        add_role(store, location_class="remote_open", contacts=[contact("priya@nimbus.example", "verified",
                                                                        "Priya Sharma", "CTO")])
        d = items_by_id(store)["yc-nimbus"].draft
        self.assertIn("across time zones", body_of(d))
        self.assertEqual(d.template_failures, [])


class GeneralDraftTests(unittest.TestCase):
    def setUp(self):
        self.items = items_by_id(demo_store())

    def test_named_founder(self):
        d = self.items["yc-orbit-pay"].draft
        self.assertTrue(d.body.startswith("Hi Ravi,\n"))
        text = body_of(d)
        self.assertIn("Orbit Pay (Payments infrastructure for Indian SMBs) caught my attention", text)
        self.assertIn("Is either of these something Orbit Pay might have room for?", text)   # 2d: no restated roles
        self.assertIn("short conversation", text)
        self.assertEqual(d.project, "Serverless FileFinder + CareerForge AI")   # 'payments' / 'infrastructure' first
        self.assertIn("Serverless FileFinder", text)
        self.assertEqual(d.template_failures, [])
        self.assertEqual(d.subject, "January 2027 intern or 2027 new grad at Orbit Pay?")   # ", Inc." dropped
        self.assertIn("Before this, I interned at Example Research Lab.", text)

    def test_inbox_gets_team_greeting_and_remote_team_a_time_zone_line(self):
        d = self.items["yc-lumen-health"].draft
        self.assertTrue(d.body.startswith("Hi Lumen Health team,\n"))
        self.assertIn("time zones", body_of(d))                          # "Remote" in the company's locations
        self.assertEqual(d.template_failures, [])

    def test_reads_naturally_with_no_company_info(self):
        d = self.items["yc-cobalt"].draft
        self.assertIn("I'd like to work with the engineering team at Cobalt.", body_of(d))
        self.assertNotIn("()", d.body)
        self.assertEqual(d.template_failures, [])

    def test_truncated_or_salesy_one_liners_are_not_quoted(self):
        base = dict(target_id="x", company_key="yc:x", company="Acme", kind="general_outreach")
        for one_liner in ("Disrupting Indian insurance with AI", "Managed corporate housing for white collar…"):
            d = template_draft(DraftInputs(**base, one_liner=one_liner), FACTS, terms=TERMS)
            self.assertNotIn(one_liner[:12], d.body)
            self.assertEqual(d.template_failures, [])

    def test_long_inputs_still_fit_the_word_range(self):
        long_facts = load_facts(PROFILE)
        long_facts.projects[2].short = ""
        long_facts.projects[2].detail += (", with retries, dead-letter queues, structured logs and alarms on every route, "
                                          "plus upload checks, search filters, access rules and a cleanup job that "
                                          "removes stale files every night without anyone touching it")
        d = template_draft(DraftInputs(target_id="x", company_key="yc:x", company="Orbit Payments Infrastructure",
                                       kind="general_outreach", one_liner="Payments infrastructure for Indian SMBs",
                                       remote_global=True), long_facts, terms=TERMS)
        self.assertEqual(d.template_failures, [])
        self.assertTrue(160 <= word_count(body_of(d)) <= 200)
        self.assertIn("Serverless FileFinder", body_of(d))

    def test_display_company(self):
        self.assertEqual(display_company("Wifi Dabba, Inc."), "Wifi Dabba")
        self.assertEqual(display_company("Acme Pvt Ltd"), "Acme")
        self.assertEqual(display_company("Optifye.ai"), "Optifye.ai")


class ValidationTests(unittest.TestCase):
    """Every rule a written draft must pass."""

    def setUp(self):
        self.store = demo_store()
        self.inputs = items_by_id(self.store)["yc-nimbus"].inputs
        self.subject, self.body = parse_written((D / "written_nimbus_good.md").read_text(encoding="utf-8"))

    def check(self, subject=None, body=None, inputs=None, action="link"):
        return validate(self.subject if subject is None else subject, self.body if body is None else body,
                        inputs or self.inputs, FACTS, action, 160, 200, TERMS)

    def test_good_fixture_passes(self):
        self.assertEqual(self.check(), [])

    def test_word_count(self):
        subject, body = parse_written((D / "written_nimbus_short.md").read_text(encoding="utf-8"))
        self.assertTrue(any("words between greeting and sign-off (must be 160-200)" in f
                            for f in self.check(subject, body)))
        long_body = self.body.replace("about either project.", "about either project. " + "More detail here. " * 20)
        self.assertTrue(any("must be 160-200" in f for f in self.check(body=long_body)))

    def test_greeting_must_match_the_contact(self):
        failures = self.check(body=self.body.replace("Hi Priya,", "Hi Rahul,"))
        self.assertIn("greeting is 'Hi Rahul,', expected 'Hi Priya,'", failures)
        failures = self.check(body=self.body.replace("Hi Priya,", "Hi Nimbus Labs team,"))
        self.assertTrue(any("expected 'Hi Priya,'" in f for f in failures))

    def test_placeholders_and_brackets(self):
        self.assertTrue(any("placeholder" in f for f in self.check(body=self.body.replace("Nimbus Labs.", "[Company]."))))
        self.assertTrue(any("placeholder" in f for f in self.check(body=self.body.replace("Nimbus Labs.", "{company}."))))
        self.assertTrue(any("placeholder" in f for f in self.check(subject="Backend Engineer Intern - TODO")))

    def test_signature_and_sign_off(self):
        no_sig = self.body.split("Your Name")[0]
        failures = self.check(body=no_sig)
        self.assertTrue(any("signature missing or changed" in f for f in failures))
        changed = self.body.replace("+91 90000 00000", "+91 99999 99999")
        self.assertTrue(any("signature missing or changed" in f for f in self.check(body=changed)))
        no_signoff = self.body.replace("Best regards,\n", "")
        self.assertTrue(any("no sign-off line" in f for f in self.check(body=no_signoff)))

    def test_resume_line_follows_resume_action(self):
        no_line = self.body.replace("My resume is linked below. ", "")
        self.assertTrue(any("no resume line" in f for f in self.check(body=no_line)))
        attached = self.body.replace("My resume is linked below.", "I've attached my resume.")
        self.assertIn("says the resume is attached, but resume_action is link", self.check(body=attached))
        self.assertIn("resume_action is attach, but the body doesn't say the resume is attached",
                      self.check(action="attach"))
        # 2d: attached or linked, never both - an attached resume drops the signature's Resume: link.
        self.assertIn("resume_action is attach, but the resume link is still in the email: remove it from the body "
                      "and the signature (attached or linked, never both)", self.check(body=attached, action="attach"))
        no_link = attached.replace("\nResume: https://example.com/your-resume.pdf", "")
        self.assertEqual(self.check(body=no_link, action="attach"), [])

    def test_numbers_must_come_from_the_facts(self):
        body = self.body.replace("12 REST endpoints", "25 REST endpoints")
        self.assertIn("number '25' isn't in the candidate facts or the company's text", self.check(body=body))

    def test_technologies_must_come_from_the_facts(self):
        claim = self.body.replace("the posting's Python and FastAPI stack is familiar ground",
                                  "I have also run Kafka in production")
        self.assertIn("mentions 'kafka', which isn't in the candidate facts", self.check(body=claim))
        # Named in the posting and only said about them: fine.
        inputs = items_by_id(self.store)["yc-nimbus"].inputs
        inputs.role_text += " Our pipeline runs on Kafka."
        about_them = self.body.replace("the posting's Python and FastAPI stack is familiar ground",
                                       "your Kafka pipeline sounds like a good place to learn")
        self.assertEqual(self.check(body=about_them, inputs=inputs), [])

    def test_plain_english_words_are_not_tech_claims(self):
        body = self.body.replace("is a problem I'd enjoy", "sits on payment rails and is a problem I'd enjoy")
        self.assertFalse(any("rails" in f for f in self.check(body=body)))

    def test_buzzwords_and_clickbait(self):
        body = self.body.replace("a problem I'd enjoy", "a problem I'm passionate about")
        self.assertIn("buzzword or filler: 'passionate'", self.check(body=body))
        self.assertTrue(any("clickbait" in f for f in self.check(subject="Quick question!")))

    def test_credentials_line_required(self):
        body = self.body.replace(" Before that, I interned at Example Research Lab.", "")
        self.assertIn("no credentials line (mention the internship)", self.check(body=body))
        facts = load_facts(PROFILE)
        facts.patent.update(title="CSES", short="Android attention management")
        failures = validate(self.subject, self.body, self.inputs, facts, "link", 160, 200, TERMS)
        self.assertIn("no credentials line (mention the patent)", failures)

    def test_role_must_be_named(self):
        body = self.body.replace("for the Backend Engineer Intern role", "for a role")
        self.assertIn("doesn't name the role ('Backend Engineer Intern')",
                      self.check(subject="Internship - B.Tech CS (2027)", body=body))

    def test_time_zone_line_for_remote_targets(self):
        self.inputs.remote_global = True
        self.assertIn("remote / multi-country target, but no line about working across time zones", self.check())


class ResumeActionTests(unittest.TestCase):
    def inputs(self, **kw):
        return DraftInputs(target_id="x", company_key="yc:x", company="Acme", kind="role", **kw)

    def test_recruiter_or_hr_gets_an_attachment(self):
        self.assertEqual(resume_action(self.inputs(contact_group="recruiting"))[0], "attach")
        self.assertEqual(resume_action(self.inputs(contact_group="hiring_inbox"))[0], "attach")

    def test_posting_that_asks_for_a_resume_by_email(self):
        for text in ("Send your resume to jobs@acme.example", "Email your CV and a short note to hiring@acme.example",
                     "Resumes to careers at acme dot example"):
            self.assertEqual(resume_action(self.inputs(contact_group="founder", role_text=text))[0], "attach", text)

    def test_everyone_else_gets_the_link(self):
        for group in ("founder", "cto", "general_inbox", "posting", "other"):
            self.assertEqual(resume_action(self.inputs(contact_group=group, role_text="Apply on our site."))[0], "link")

    def test_hr_inbox_target_is_attach_and_tier_1(self):
        it = items_by_id(demo_store())["yc-lumen-health"]
        self.assertEqual((it.draft.resume_action, it.send_tier), ("attach", 1))
        self.assertIn("I've attached my resume.", it.draft.body)


class SendTierAndQueueTests(unittest.TestCase):
    def test_send_tiers(self):
        self.assertEqual(send_tier("verified", "founder"), 1)
        self.assertEqual(send_tier("published_inbox", "hiring_inbox"), 1)
        self.assertEqual(send_tier("published_inbox", "general_inbox"), 2)
        self.assertEqual(send_tier("strong", "cto"), 2)
        self.assertEqual(send_tier("guessed", "founder"), 3)
        self.assertIsNone(send_tier("", ""))

    def test_order_tier1_then_2_and_tier3_held_before_any_send(self):
        store = demo_store()
        batch = make_queue(store, PROFILE, date(2026, 10, 20))
        self.assertEqual([it.target_id for it in batch.items], ["yc-nimbus", "yc-lumen-health", "yc-cobalt"])
        self.assertEqual(batch.tier3_held, 2)              # Orbit Pay and Delta AI are guesses
        self.assertFalse(batch.tier3_open)
        self.assertEqual([r["target_id"] for r in store.queue("2026-10-20")], [it.target_id for it in batch.items])

    def test_pure_guesses_are_never_queued_2f(self):
        """2f: the per-type gate replaced the tier-3 unlock: a pure pattern guess is never queued, on any day."""
        store = demo_store()
        mark_sent(store, PROFILE, "yc-nimbus", date(2026, 10, 20))
        for day in (date(2026, 10, 22), date(2026, 11, 5)):
            batch = make_queue(store, PROFILE, day)
            ids = [it.target_id for it in batch.items]
            self.assertNotIn("yc-orbit-pay", ids)
            self.assertNotIn("yc-delta", ids)
            self.assertEqual(batch.gates["guessed"].state, "closed")
            self.assertEqual(batch.held_by_kind.get("guessed"), 2)

    def test_a_bounce_before_unlock_keeps_tier3_locked(self):
        store = demo_store()
        mark_sent(store, PROFILE, "yc-cobalt", date(2026, 10, 20))
        mark_bounced(store, PROFILE, "yc-cobalt", date(2026, 10, 21))
        batch = make_queue(store, PROFILE, date(2026, 10, 25))
        self.assertEqual(batch.gate.state, "paused")                     # 1 of 1 bounced: 100%
        for tid in ("yc-nimbus", "yc-lumen-health"):
            mark_sent(store, PROFILE, tid, date(2026, 10, 25))
        for i in range(20):
            store.add_event("x", "x", f"a{i}@x.example", "sent", "2026-10-25")
        batch = make_queue(store, PROFILE, date(2026, 10, 26))
        self.assertEqual(batch.gate.state, "locked")                     # rate under 5%, but a bounce happened
        self.assertIn("needs zero bounces", batch.gate.reason)
        self.assertNotIn(3, [it.send_tier for it in batch.items])


class Tier3GateTests(unittest.TestCase):
    """Guessed addresses: unlock after 2 days with no bounces, 5/day, 10/day after 3 more days, pause at 5%."""

    CFG = PROFILE.outreach["tier3"]

    def gate(self, day, first="2026-10-20", sent=10, bounced=0, started=None):
        from applyflow.outreach import tier3_gate
        return tier3_gate({"first_sent": first, "sent": sent, "bounced": bounced, "tier3_first_sent": started},
                          date.fromisoformat(day), self.CFG)

    def test_locked_until_two_calendar_days_pass(self):
        g = self.gate("2026-10-20", first=None, sent=0)
        self.assertEqual((g.state, g.limit), ("locked", 0))
        self.assertIn("nothing sent yet", g.reason)
        self.assertEqual(self.gate("2026-10-21").state, "locked")
        self.assertEqual((self.gate("2026-10-22").state, self.gate("2026-10-22").limit), ("limited", 5))

    def test_ramps_to_ten_three_days_after_the_first_guessed_send(self):
        self.assertEqual(self.gate("2026-10-24", started="2026-10-22").limit, 5)
        g = self.gate("2026-10-25", started="2026-10-22", sent=40, bounced=1)
        self.assertEqual((g.state, g.limit), ("full", 10))

    def test_pauses_at_five_percent(self):
        g = self.gate("2026-10-25", started="2026-10-22", sent=40, bounced=2)
        self.assertEqual((g.state, g.limit), ("paused", 0))
        self.assertIn("2 bounce(s) in 40 send(s), 5%", g.reason)
        self.assertEqual(self.gate("2026-10-25", started="2026-10-22", sent=41, bounced=2).state, "full")

    def test_bounces_before_unlock_keep_it_locked(self):
        g = self.gate("2026-10-23", sent=30, bounced=1)
        self.assertEqual(g.state, "locked")
        self.assertIn("needs zero bounces", g.reason)

    def test_batch_size(self):
        store = demo_store()
        self.assertEqual(len(make_queue(store, PROFILE, date(2026, 10, 20), size=2).items), 2)

    def test_do_not_contact_excluded(self):
        store = demo_store()
        dnc = DoNotContact(emails={"hello@cobalt.example"}, domains={"lumen.example"})
        items = items_by_id(store, dnc)
        self.assertEqual(items["yc-cobalt"].status, "do_not_contact")
        self.assertEqual(items["yc-lumen-health"].status, "do_not_contact")
        ids = [it.target_id for it in make_queue(store, PROFILE, date(2026, 10, 20), dnc=dnc).items]
        self.assertEqual(ids, ["yc-nimbus"])

    def test_already_sent_excluded_and_mark_sent_batch(self):
        store = demo_store()
        make_queue(store, PROFILE, date(2026, 10, 20))
        from applyflow.outreach import mark_batch_sent
        marked = mark_batch_sent(store, PROFILE, date(2026, 10, 20))
        self.assertEqual(len(marked), 3)
        items = items_by_id(store)
        self.assertEqual(items["yc-cobalt"].status, "sent")
        self.assertEqual(items["yc-cobalt"].sent_date, "2026-10-20")
        nxt = make_queue(store, PROFILE, date(2026, 10, 21))
        self.assertEqual(nxt.items, [])                     # the rest are tier 3, day 2
        with self.assertRaises(Exception):
            mark_sent(store, PROFILE, "yc-cobalt", date(2026, 10, 21))

    def test_requeue_same_day_keeps_sent_entries(self):
        store = demo_store()
        make_queue(store, PROFILE, date(2026, 10, 20))
        mark_sent(store, PROFILE, "yc-cobalt", date(2026, 10, 20))
        batch = make_queue(store, PROFILE, date(2026, 10, 20))
        self.assertEqual(sorted(it.target_id for it in batch.items), ["yc-cobalt", "yc-lumen-health", "yc-nimbus"])

    def test_one_person_per_company(self):
        store = demo_store()
        add_target(store, PROFILE, None, "Cobalt Labs", "cobalt.example", email="founder@cobalt.example",
                   person="Asha Iyer", role="Founder")
        batch = make_queue(store, PROFILE, date(2026, 10, 20))
        cobalt = [it for it in batch.items if it.domain == "cobalt.example"]
        self.assertEqual(len(cobalt), 1)
        self.assertEqual(cobalt[0].target_id, "manual-cobalt")          # tier 1 (address given) beats tier 2
        # 2e: the other target for the company is marked a duplicate when items are built, not only per batch
        items = {it.target_id: it for it in build_items(store, PROFILE)}
        self.assertEqual(items["yc-cobalt"].status, "duplicate")
        self.assertEqual(batch.skipped_same_company, 0)


class BounceTests(unittest.TestCase):
    def test_bounce_walks_alternates_then_backups(self):
        store = demo_store()
        mark_sent(store, PROFILE, "yc-orbit-pay", date(2026, 10, 27))
        it = mark_bounced(store, PROFILE, "yc-orbit-pay", date(2026, 10, 28))
        self.assertEqual((it.status, it.address.email), ("ready", "ravi.kumar@orbitpay.example"))
        self.assertEqual(it.inputs.greeting, "Hi Ravi,")
        mark_sent(store, PROFILE, "yc-orbit-pay", date(2026, 10, 28))
        it = mark_bounced(store, PROFILE, "yc-orbit-pay", date(2026, 10, 28))
        self.assertEqual(it.address.email, "ravikumar@orbitpay.example")
        mark_sent(store, PROFILE, "yc-orbit-pay", date(2026, 10, 29))
        it = mark_bounced(store, PROFILE, "yc-orbit-pay", date(2026, 10, 29))
        self.assertEqual((it.address.email, it.send_tier), ("hello@orbitpay.example", 2))   # the backup inbox
        self.assertEqual(it.inputs.greeting, "Hi Orbit Pay team,")
        self.assertTrue(it.draft.body.startswith("Hi Orbit Pay team,"))
        self.assertEqual(it.bounced, ["ravi@orbitpay.example", "ravi.kumar@orbitpay.example",
                                      "ravikumar@orbitpay.example"])
        mark_sent(store, PROFILE, "yc-orbit-pay", date(2026, 10, 30))
        it = mark_bounced(store, PROFILE, "yc-orbit-pay", date(2026, 10, 30))
        self.assertEqual((it.status, it.address), ("exhausted", None))

    def test_written_draft_for_the_old_person_falls_back_to_the_template(self):
        store = demo_store()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        good_orbit = items_by_id(store)["yc-orbit-pay"].draft
        (tmp / "yc-orbit-pay.md").write_text(f"Subject: {good_orbit.subject}\n\nBody:\n{good_orbit.body}\n",
                                             encoding="utf-8")
        results = import_written(store, PROFILE, tmp)
        self.assertTrue(results[0].ok, results[0].failures)
        self.assertEqual(items_by_id(store)["yc-orbit-pay"].draft.source, "claude")
        for day in (27, 28, 29):
            mark_sent(store, PROFILE, "yc-orbit-pay", date(2026, 10, day))
            it = mark_bounced(store, PROFILE, "yc-orbit-pay", date(2026, 10, day))
        self.assertEqual(it.draft.source, "template")
        self.assertIn("greeting is 'Hi Ravi,', but hello@orbitpay.example is a shared inbox "
                      "(expected 'Hi Orbit Pay team,')", it.draft.failures)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.store = demo_store()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_valid_draft_replaces_the_template(self):
        shutil.copy(D / "written_nimbus_good.md", self.tmp / "yc-nimbus.md")
        results = import_written(self.store, PROFILE, self.tmp)
        self.assertEqual([(r.target_id, r.ok) for r in results], [("yc-nimbus", True)])
        it = items_by_id(self.store)["yc-nimbus"]
        self.assertEqual(it.draft.source, "claude")
        self.assertIn("Tracing every Lambda invocation", it.draft.body)
        stored = {r["target_id"]: r for r in self.store.drafts()}
        self.assertEqual(stored["yc-nimbus"]["source"], "claude")

    def test_invalid_draft_stays_on_the_template_with_the_reason(self):
        shutil.copy(D / "written_nimbus_short.md", self.tmp / "yc-nimbus.md")
        results = import_written(self.store, PROFILE, self.tmp)
        self.assertFalse(results[0].ok)
        it = items_by_id(self.store)["yc-nimbus"]
        self.assertEqual(it.draft.source, "template")
        self.assertTrue(any("must be 160-200" in f for f in it.draft.failures))

    def test_unknown_id_and_bad_format(self):
        (self.tmp / "yc-nobody.md").write_text("Subject: x\n\nBody:\nHi,\n", encoding="utf-8")
        (self.tmp / "yc-cobalt.md").write_text("Hello there\n", encoding="utf-8")
        results = {r.target_id: r for r in import_written(self.store, PROFILE, self.tmp)}
        self.assertIn("no target with id 'yc-nobody'", results["yc-nobody"].failures)
        self.assertTrue(results["yc-cobalt"].failures[0].startswith("not in the fixed format"))

    def test_parse_written_tolerates_bom_crlf_and_missing_body_label(self):
        subject, body = parse_written("﻿Subject: Hello\r\n\r\nHi Priya,\r\nText\r\n")
        self.assertEqual((subject, body), ("Hello", "Hi Priya,\nText"))


class AddTargetTests(unittest.TestCase):
    def test_with_email_no_lookup_and_a_packet(self):
        store = Store(":memory:")
        c, session = client({f"{NIMBUS}/robots.txt": "User-agent: *\nAllow: /",
                             NIMBUS: (D / "nimbus_home.html").read_text(encoding="utf-8")})
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out)
        it = add_target(store, PROFILE, c, "Nimbus Labs", "nimbus.example", note="their tracing blog post",
                        email="Asha@Nimbus.example", person="Asha Iyer", role="CTO", out_dir=out)
        self.assertEqual(session.urls, [f"{NIMBUS}/robots.txt", NIMBUS])     # homepage context only
        self.assertEqual((it.kind, it.status, it.send_tier), ("manual", "ready", 1))
        self.assertEqual((it.address.email, it.address.confidence, it.address.source),
                         ("asha@nimbus.example", "verified", "manual"))
        self.assertTrue(it.draft.body.startswith("Hi Asha,"))
        self.assertEqual(it.draft.template_failures, [])
        self.assertIn("Nimbus Labs traces every Lambda invocation", it.inputs.meta_description)
        packet = (out / "packets" / "manual-nimbus.md").read_text(encoding="utf-8")
        self.assertIn("their tracing blog post", packet)
        self.assertIn("Greeting to use: `Hi Asha,`", packet)
        self.assertIn("manual:nimbus", [t.key for t in build_targets(store, PROFILE, [])])

    def test_without_email_runs_contact_finding(self):
        store = Store(":memory:")
        c, session = client(acme_routes())
        it = add_target(store, PROFILE, c, "Acme Robotics", ACME, note="warehouse robots")
        self.assertIn(f"{ACME}/team", session.urls)
        self.assertEqual(it.address.email, "priya.sharma@acmerobotics.example")
        self.assertEqual(it.send_tier, 1)
        self.assertEqual(store.get_context("manual:acmerobotics")["homepage_title"],
                         "Acme Robotics - warehouse robots")             # kept from the contact search

    def test_dnc_address_refused(self):
        from applyflow.outreach import OutreachError
        with self.assertRaises(OutreachError):
            add_target(Store(":memory:"), PROFILE, None, "X", "x.example", email="a@x.example",
                       dnc=DoNotContact(domains={"x.example"}))


class ContextTests(unittest.TestCase):
    def test_page_summary(self):
        ctx = page_summary((D / "nimbus_home.html").read_text(encoding="utf-8"))
        self.assertEqual(ctx["title"], "Nimbus Labs - observability for serverless apps")
        self.assertTrue(ctx["description"].startswith("Nimbus Labs traces every Lambda"))
        self.assertIn("See every serverless request, end to end", ctx["snippet"])
        self.assertNotIn("script text", ctx["snippet"])
        self.assertNotIn("Book a demo", ctx["snippet"])                  # too short to be content
        self.assertNotIn("enable JavaScript", ctx["snippet"])
        junk = page_summary('<body class="home wp-singular page"><div class="x">Manage your team across many '
                            'locations today</div><p>M a k e a n y v i d e o</p></body>')
        self.assertEqual(junk["snippet"], "Manage your team across many locations today")

    def test_ensure_context_fetches_missing_and_respects_robots(self):
        store = demo_store()
        c, session = client({"https://orbitpay.example/robots.txt": "User-agent: *\nDisallow: /",
                             f"{NIMBUS}/robots.txt": "", NIMBUS: (D / "nimbus_home.html").read_text(encoding="utf-8")})
        items = [it for it in build_items(store, PROFILE) if it.target_id in ("yc-nimbus", "yc-orbit-pay")]
        self.assertEqual(ensure_context(store, c, items), 2)
        self.assertEqual(store.get_context("yc:nimbus")["status"], "ok")
        self.assertEqual(store.get_context("yc:orbit-pay")["status"], "blocked")
        self.assertNotIn("https://orbitpay.example", session.urls)
        self.assertEqual(ensure_context(store, c, items), 0)               # stored: not fetched again
        it = items_by_id(store)["yc-nimbus"]
        self.assertIn("traces every Lambda invocation", it.inputs.homepage_snippet)


class PageDecodingTests(unittest.TestCase):
    """Homepages without a charset header are UTF-8 far more often than Latin-1."""

    class Resp:
        def __init__(self, content, content_type):
            self.content, self.headers = content, {"Content-Type": content_type}
            self.text = content.decode("latin-1")         # what requests guesses with no charset

    def test_utf8_without_charset(self):
        from applyflow.http import decode_page
        page = "<title>PrimeVault — the new architecture · CEO – Co-founder</title>".encode("utf-8")
        self.assertIn("PrimeVault — the new", decode_page(self.Resp(page, "text/html")))

    def test_declared_charset_and_non_utf8_bytes(self):
        from applyflow.http import decode_page
        latin = "Café".encode("latin-1")
        self.assertEqual(decode_page(self.Resp(latin, "text/html; charset=ISO-8859-1")), "Café")
        self.assertEqual(decode_page(self.Resp(latin, "text/html")), "Café")


class PacketTests(unittest.TestCase):
    def test_packets_index_and_style_guide(self):
        store = demo_store()
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out)
        items = build_items(store, PROFILE)
        paths = write_packets(items, PROFILE, out)
        self.assertEqual(len(paths), len(items))
        packet = (out / "packets" / "yc-orbit-pay.md").read_text(encoding="utf-8")
        for needle in ("Greeting to use: `Hi Ravi,`", "resume_action: **link**", "If it bounces: ravi.kumar@",
                       "## Candidate facts (the only facts you may use)", "Project: CareerForge AI",
                       "One-liner: Payments infrastructure for Indian SMBs", "## Style guide",
                       "output/drafts/written/yc-orbit-pay.md", "Best regards,"):
            self.assertIn(needle, packet)
        role = (out / "packets" / "yc-nimbus.md").read_text(encoding="utf-8")
        self.assertIn("## The opening", role)
        self.assertIn("Build APIs in Python and FastAPI", role)
        self.assertTrue((out / "packets" / "STYLE_GUIDE.md").exists())
        index = (out / "packets" / "INDEX.md").read_text(encoding="utf-8")
        self.assertLess(index.index("yc-nimbus"), index.index("yc-orbit-pay"))
        self.assertTrue((out / "written").is_dir())


class GmailUrlTests(unittest.TestCase):
    def test_everything_is_encoded(self):
        body = "Hi Priya,\n\nC++ & Python #1 at 100% — 50/50?\nBest regards,"
        url = gmail_url("priya+jobs@nimbus.example", "Backend & APIs: intern?", body)
        self.assertTrue(url.startswith("https://mail.google.com/mail/?view=cm&to=priya%2Bjobs@nimbus.example&su="))
        query = url.split("?", 1)[1]
        for raw in ("\n", " ", "&&", "#", "+&", "%2"):
            pass
        self.assertNotIn(" ", url)
        self.assertNotIn("\n", url)
        self.assertNotIn("#", url)
        self.assertIn("%0A%0A", url)
        self.assertIn("C%2B%2B%20%26%20Python%20%231", url)
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params["to"], ["priya+jobs@nimbus.example"])
        self.assertEqual(params["su"], ["Backend & APIs: intern?"])
        self.assertEqual(params["body"], [body])
        self.assertEqual(params["view"], ["cm"])

    def test_report_card_has_gmail_and_copy_buttons(self):
        from applyflow.report import outreach_sections, write_outbox_csv
        store = demo_store()
        items = build_items(store, PROFILE)
        html_out = outreach_sections(items, items[:2], "2026-10-20", False, FACTS.listing())
        self.assertIn("Today's batch · 2026-10-20", html_out)
        self.assertIn("https://mail.google.com/mail/?view=cm&amp;to=priya@nimbus.example&amp;su=", html_out)
        self.assertIn("Copy body", html_out)
        self.assertIn("All outreach targets", html_out)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        import csv
        path = write_outbox_csv(items, {items[0].target_id}, tmp / "outbox.csv")
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        self.assertEqual(rows[0]["in_batch"], "yes")
        self.assertEqual(rows[0]["to"], "priya@nimbus.example")
        self.assertTrue(rows[0]["gmail_url"].startswith("https://mail.google.com/"))
        ghost = next(r for r in rows if r["target_id"] == "yc-ghost")
        self.assertEqual((ghost["status"], ghost["to"], ghost["gmail_url"]), ("no_contact", "", ""))


class CliTests(unittest.TestCase):
    def test_queue_mark_sent_and_bounce_via_cli(self):
        from applyflow.cli import main
        from applyflow.config import PROFILE_EXAMPLE_PATH
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        db = tmp / "t.db"
        store = Store(db)
        add_company(store, "yc:orbit-pay", "Orbit Pay", description="Payments infrastructure",
                    contacts=[contact("ravi@orbitpay.example", "verified", "Ravi Kumar", "Founder")],
                    website="https://orbitpay.example")
        store.close()
        args = ["--db", str(db), "--profile", str(PROFILE_EXAMPLE_PATH)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(args + ["queue", "--date", "2026-10-20"]), 0)
            self.assertEqual(main(args + ["mark-sent", "--batch", "2026-10-20"]), 0)
            self.assertEqual(main(args + ["mark-bounced", "yc-orbit-pay", "--date", "2026-10-21"]), 0)
        text = out.getvalue()
        self.assertIn("T1 Orbit Pay", text)
        self.assertIn("By contact type (2f per-type gate):", text)
        self.assertIn("Marked 1 sent from the 2026-10-20 batch", text)
        self.assertIn("No more addresses for this company.", text)


if __name__ == "__main__":
    unittest.main()
