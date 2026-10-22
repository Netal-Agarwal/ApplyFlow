"""Phase 2d email format: two projects, a skills line, the credentials line, 160-200 words, and the resume
attached or linked - never both. Targets queued or sent before 2d keep the 2c rules."""

import shutil
import tempfile
import unittest
from pathlib import Path

from applyflow.drafting import (DraftInputs, choose_projects, choose_skills, load_facts, parse_written, split_body,
                                template_draft, validate, word_count)
from applyflow.outreach import write_packets
from applyflow.store import Store
from tests.helpers import FIXTURES, example_profile
from tests.test_drafting import D, TERMS, add_role, body_of, contact, demo_store, items_by_id

PROFILE = example_profile()
FACTS = load_facts(PROFILE)
RESUME = "https://example.com/your-resume.pdf"


def inputs(**kw):
    base = dict(target_id="yc-acme", company_key="yc:acme", company="Acme", kind="general_outreach",
                to_email="ravi@acme.example", contact_name="Ravi Kumar", contact_group="founder")
    base.update(kw)
    return DraftInputs(**base)


class TemplateTests(unittest.TestCase):
    def test_every_template_has_two_projects_a_skills_line_and_credentials(self):
        for it in items_by_id(demo_store()).values():
            d = it.draft
            self.assertEqual(d.template_failures, [], it.target_id)
            text = body_of(d)
            self.assertTrue(160 <= word_count(text) <= 200, (it.target_id, word_count(text)))
            self.assertEqual(sum(p.name in text for p in FACTS.projects), 2, it.target_id)
            self.assertIn("Day to day I work in ", text)
            self.assertIn("Before this, I interned at Example Research Lab.", text)
            self.assertTrue(set(d.skills) <= set(FACTS.skills))
            self.assertTrue(4 <= len(d.skills) <= 7)

    def test_attach_drops_the_link_everywhere(self):
        it = items_by_id(demo_store())["yc-lumen-health"]                   # careers@ inbox
        d = it.draft
        self.assertEqual(d.resume_action, "attach")
        self.assertIn("I've attached my resume.", d.body)
        self.assertNotIn(RESUME, d.body)
        self.assertNotIn("Resume:", d.body)
        self.assertNotIn("linked below", d.body)

    def test_link_keeps_the_link_and_attaches_nothing(self):
        d = items_by_id(demo_store())["yc-orbit-pay"].draft
        self.assertEqual(d.resume_action, "link")
        self.assertIn("My resume is linked below.", d.body)
        self.assertEqual(split_body(d.body)[2][-1], f"Resume: {RESUME}")
        self.assertNotIn("attach", body_of(d))


class ProjectAndSkillChoiceTests(unittest.TestCase):
    def test_ai_company_leads_with_the_ai_project(self):
        projects, why = choose_projects(inputs(one_liner="LLM agents for legal research"), FACTS)
        self.assertEqual([p.name for p in projects], ["Judicial AI System", "CareerForge AI"])
        self.assertIn("company text mentions", why[0])

    def test_cloud_company_leads_with_the_serverless_project(self):
        projects, _ = choose_projects(inputs(one_liner="Serverless observability on AWS"), FACTS)
        self.assertEqual(projects[0].name, "Serverless FileFinder")

    def test_no_company_text_defaults_to_backend_pair(self):
        projects, _ = choose_projects(inputs(), FACTS)
        self.assertEqual([p.name for p in projects], ["CareerForge AI", "Serverless FileFinder"])

    def test_role_uses_the_lead_project_and_the_posting_skills(self):
        i = inputs(kind="role", role_title="Backend Intern", role_family="backend", lead_project="CareerForge AI",
                   role_skills=["postgres", "fastapi", "kafka"])
        projects, _ = choose_projects(i, FACTS)
        self.assertEqual(projects[0].name, "CareerForge AI")
        skills, why = choose_skills(i, FACTS, projects)
        self.assertEqual(skills[:2], ["PostgreSQL", "FastAPI"])           # posting first; kafka isn't a fact
        self.assertIn("in the posting", why[0])
        self.assertNotIn("kafka", [s.lower() for s in skills])

    def test_skills_named_on_the_company_site_come_before_the_usual_stack(self):
        skills, _ = choose_skills(inputs(homepage_snippet="Our app is built with React and MongoDB."), FACTS, [])
        self.assertEqual(skills[:2], ["MongoDB", "React"])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.inputs = items_by_id(demo_store())["yc-nimbus"].inputs
        self.subject, self.body = parse_written((D / "written_nimbus_good.md").read_text(encoding="utf-8"))

    def check(self, body, action="link", i=None):
        return validate(self.subject, body, i or self.inputs, FACTS, action, 160, 200, TERMS)

    def test_good_fixture_passes(self):
        self.assertEqual(self.check(self.body), [])

    def test_exactly_two_projects(self):
        one = self.body.replace("I also built Serverless FileFinder,", "I also built a tool,")
        self.assertTrue(any("names 1 project(s) from the facts (CareerForge AI)" in f for f in self.check(one)))
        three = self.body.replace("Before that,", "Judicial AI System came earlier. Before that,")
        self.assertTrue(any("names 3 project(s)" in f for f in self.check(three)))

    def test_skills_line_required_and_from_the_facts(self):
        none = self.body.replace("Day to day I work in Python, FastAPI, PostgreSQL, AWS Lambda and DynamoDB. ", "")
        self.assertTrue(any("no skills line" in f for f in self.check(none)))
        kafka = self.body.replace("AWS Lambda and DynamoDB", "Kafka and DynamoDB")
        self.assertTrue(any("skills line names 'Kafka', not in the facts' skills list" in f for f in self.check(kafka)))
        few = self.body.replace("Python, FastAPI, PostgreSQL, AWS Lambda and DynamoDB", "Python, FastAPI and S3")
        self.assertTrue(any("skills line lists 3 skills (must be 4-7)" in f for f in self.check(few)))
        many = self.body.replace("Python, FastAPI, PostgreSQL, AWS Lambda and DynamoDB",
                                 "Python, FastAPI, Flask, PostgreSQL, MongoDB, AWS Lambda, S3 and DynamoDB")
        self.assertTrue(any("skills line lists 8 skills (must be 4-7)" in f for f in self.check(many)))
        alias = self.body.replace("PostgreSQL, AWS Lambda", "Postgres, AWS Lambda")
        self.assertFalse(any("skills line" in f for f in self.check(alias)))

    def test_dotted_skill_names_parse(self):
        facts = load_facts(PROFILE)
        facts.skills.append("Node.js")
        facts.raw["skills"] = facts.skills                                   # the fact check reads the raw block
        body = self.body.replace("AWS Lambda and DynamoDB", "Node.js and DynamoDB")
        self.assertEqual(validate(self.subject, body, self.inputs, facts, "link", 160, 200, TERMS), [])

    def test_credentials_line_still_required(self):
        body = self.body.replace(" Before that, I interned at Example Research Lab.", "")
        self.assertIn("no credentials line (mention the internship)", self.check(body))

    def test_never_both(self):
        attached = self.body.replace("My resume is linked below.", "I've attached my resume.")
        both = self.check(attached, "attach")
        self.assertTrue(any("resume link is still in the email" in f for f in both))
        linked_too = attached.replace(f"\nResume: {RESUME}", "").replace("I've attached my resume.",
                                                                           "I've attached my resume, also linked here.")
        self.assertTrue(any("says the resume is linked, but resume_action is attach" in f
                            for f in self.check(linked_too, "attach")))
        self.assertIn("says the resume is attached, but resume_action is link", self.check(attached, "link"))

    def test_word_range_is_160_to_200(self):
        short = self.body.replace(" Either works for me, and I'd be glad to start the internship on whatever date "
                                  "in January suits the team.", "").replace(
            "Tracing every Lambda invocation to show where latency and cost come from is a problem I'd enjoy, since "
            "most of my own work runs on AWS Lambda.", "")
        n = word_count(split_body(short)[1])
        self.assertLess(n, 160)
        self.assertTrue(any(f"{n} words between greeting and sign-off (must be 160-200)" in f
                            for f in self.check(short)))


class LegacyRulesTests(unittest.TestCase):
    """Queued or sent before 2d: the old one-project, 120-160-word format stays valid."""

    def setUp(self):
        self.subject, self.body = parse_written((D / "written_nimbus_2c.md").read_text(encoding="utf-8"))

    def test_old_draft_passes_under_2c_and_fails_under_2d(self):
        store = demo_store()
        i = items_by_id(store)["yc-nimbus"].inputs
        self.assertTrue(any("no skills line" in f for f in validate(self.subject, self.body, i, FACTS, "link")))
        i.rules = "2c"
        self.assertEqual(validate(self.subject, self.body, i, FACTS, "link", 160, 200, TERMS), [])

    def test_legacy_template_is_the_old_format(self):
        d = template_draft(inputs(rules="2c"), FACTS, 160, 200, TERMS)
        self.assertEqual(d.template_failures, [])
        self.assertTrue(120 <= word_count(body_of(d)) <= 160)
        self.assertNotIn("Day to day", d.body)
        d = template_draft(inputs(rules="2c", to_email="careers@acme.example", contact_name="",
                                  contact_group="hiring_inbox"), FACTS, 160, 200, TERMS)
        self.assertEqual((d.resume_action, d.template_failures), ("attach", []))
        self.assertIn(f"Resume: {RESUME}", d.body)                         # the 2c signature kept the link

    def test_queued_and_sent_targets_are_locked_to_2c_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            store = Store(path)
            store.conn.execute("DROP TABLE draft_rules_2d_done")           # as if the DB predates 2d
            store.conn.execute("INSERT INTO draft_rules (target_id, rules, reason, locked_at) "
                               "VALUES ('co-old', '2b', 'x', 'now')")
            store.save_queue("2026-10-21", [("co-old", "a@old.example", 1)], keep=set())
            store.save_queue("2026-10-22", [("yc-queued", "b@q.example", 1)], keep=set())
            store.add_event("yc-sent", "yc:sent", "c@s.example", "sent", "2026-10-21")
            store.conn.commit()
            store.close()
            store = Store(path)
            self.assertEqual(store.draft_rules(), {"co-old": "2b", "yc-queued": "2c", "yc-sent": "2c"})
            store.save_queue("2026-10-23", [("yc-new", "d@n.example", 1)], keep=set())
            store.close()
            store = Store(path)
            self.assertNotIn("yc-new", store.draft_rules())                 # only once: later batches use 2d
            store.close()

    def test_locked_target_builds_with_2c_rules(self):
        store = Store(":memory:")
        add_role(store, contacts=[contact("priya@nimbus.example", "verified", "Priya Sharma", "CTO")])
        store.conn.execute("INSERT INTO draft_rules (target_id, rules, reason, locked_at) "
                           "VALUES ('yc-nimbus', '2c', 'queued', 'now')")
        it = items_by_id(store)["yc-nimbus"]
        self.assertEqual(it.inputs.rules, "2c")
        self.assertNotIn("Day to day", it.draft.body)
        self.assertEqual(it.draft.template_failures, [])


class PacketTests(unittest.TestCase):
    def test_packet_suggests_projects_skills_and_the_attach_signature(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out)
        items = list(items_by_id(demo_store()).values())
        write_packets(items, PROFILE, out)
        lumen = (out / "packets" / "yc-lumen-health.md").read_text(encoding="utf-8")
        self.assertIn("- Two projects (suggested, best first): ", lumen)
        self.assertIn("- Skills line (suggested, only facts skills): ", lumen)
        self.assertIn("160-200 words", lumen)
        self.assertNotIn(f"Resume: {RESUME}", lumen.split("## Rules for this draft")[1].split("## Candidate")[0])
        guide = (out / "packets" / "STYLE_GUIDE.md").read_text(encoding="utf-8")
        for needle in ("Day to day I work in", "attached or linked, never both", "160-200 words", "Two projects"):
            self.assertIn(needle, guide)


class ClosingAskTests(unittest.TestCase):
    """2d step 2: the ask points back at the roles the availability sentence named, never restates them."""

    def setUp(self):
        self.subject, self.body = parse_written((D / "written_nimbus_good.md").read_text(encoding="utf-8"))
        self.inputs = items_by_id(demo_store())["yc-nimbus"].inputs

    def test_restated_roles_after_availability_fail(self):
        body = self.body.replace("My resume is linked below.", "Would you take a January 2027 intern or a 2027 new "
                                                               "grad? My resume is linked below.")
        failures = validate(self.subject, body, self.inputs, FACTS, "link", 160, 200, TERMS)
        self.assertTrue(any("the ask repeats 'January 2027 intern' after the availability sentence" in f
                            for f in failures), failures)

    def test_roles_before_the_availability_sentence_are_fine(self):
        from applyflow.drafting import restated_roles
        self.assertEqual(restated_roles("Is a January 2027 intern useful? I'm available for a 6-month internship "
                                        "from January 2027, and for a full-time new-grad role from mid-2027."), [])

    def test_general_templates_vary_the_ask_and_pass(self):
        asks = set()
        for it in items_by_id(demo_store()).values():
            text = body_of(it.draft)
            self.assertNotIn("January 2027 intern", text)
            self.assertEqual(it.draft.template_failures, [])
            if it.kind != "role":
                asks.add(text.split("\n\n")[-1].split("?")[0])
        self.assertGreater(len(asks), 1)

    def test_legacy_drafts_keep_their_old_ask(self):
        d = template_draft(inputs(rules="2c"), FACTS, 160, 200, TERMS)
        self.assertIn("open to a January 2027 intern or a 2027 new grad", d.body)
        self.assertEqual(d.template_failures, [])


class BounceStopAndOrderTests(unittest.TestCase):
    def test_stop_puts_alternates_and_backups_on_hold(self):
        from datetime import date
        from applyflow.outreach import make_queue, mark_bounced, mark_sent
        store = demo_store()
        mark_sent(store, PROFILE, "yc-orbit-pay", date(2026, 10, 20))
        it = mark_bounced(store, PROFILE, "yc-orbit-pay", date(2026, 10, 22), stop=True)
        self.assertEqual(it.status, "stopped")                            # not ravi.kumar@ / hello@
        self.assertEqual(store.send_history()["bounced"], 1)               # one bounce, counted once
        batch = make_queue(store, PROFILE, date(2026, 10, 23), save=False)
        self.assertNotIn("yc-orbit-pay", [b.target_id for b in batch.items])
        mark_sent(store, PROFILE, "yc-nimbus", date(2026, 10, 20))
        self.assertEqual(mark_bounced(store, PROFILE, "yc-nimbus", date(2026, 10, 22)).status, "exhausted")

    def test_tiers_1_2_ordered_by_bounce_risk(self):
        from applyflow.contacts import Target, ContactResult
        from tests.test_drafting import add_company
        store = Store(":memory:")
        for slug, email, conf, name in (("info", "info@info.example", "published_inbox", ""),
                                        ("hello", "hello@hello.example", "published_inbox", ""),
                                        ("founders", "founders@founders.example", "published_inbox", ""),
                                        ("careers", "careers@careers.example", "published_inbox", ""),
                                        ("person", "asha@person.example", "verified", "Asha Rao"),
                                        ("staff", "priya.nair@staff.example", "verified", ""),
                                        ("relief", "program-relief@relief.example", "verified", ""),
                                        ("guess", "ravi@guess.example", "guessed", "Ravi Kumar")):
            add_company(store, f"yc:{slug}", slug.title(), website=f"https://{slug}.example",
                        contacts=[contact(email, conf, name, "Founder" if name else f"{slug}@ inbox")])
        order = [it.target_id for it in build_items_(store) if it.status == "ready"]
        self.assertEqual(set(order[:2]), {"yc-person", "yc-staff"})        # stored name, or a first.last address
        self.assertEqual(order[2:], ["yc-careers", "yc-founders", "yc-hello", "yc-relief", "yc-info", "yc-guess"])

    def test_daily_batch_default_is_30(self):
        from applyflow.config import DEFAULT_OUTREACH
        self.assertEqual(DEFAULT_OUTREACH["daily_batch"], 30)
        self.assertEqual(PROFILE.outreach["daily_batch"], 30)


def build_items_(store):
    from applyflow.outreach import build_items
    return build_items(store, PROFILE)
