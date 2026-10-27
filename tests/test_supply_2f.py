"""Phase 2f: the per-type bounce gate, evidence-backed guesses, hiring inboxes at large companies, the ongoing-project
rule, RBI / Sahamati / GitHub list parsing, sector tagging and the classification of other published addresses."""

import json
import unittest
from dataclasses import asdict
from datetime import date

from applyflow.contacts import (Contact, ContactResult, DoNotContact, Target, address_format, evidence_for_guess,
                                large_inbox_search)
from applyflow.drafting import DraftInputs, load_facts, ongoing_failures, template_draft, validate
from applyflow.outreach import (SendRecord, build_items, classify_published, contact_kind, make_queue, pick_batch,
                                type_gates)
from applyflow.pool import Scored, consultancy_fit, select
from applyflow.scoring import APPLY_ONLY_HINT
from applyflow.sources import PoolCompany, pool_sector
from applyflow.store import Store
from applyflow.supply import (FINTECH_EXCLUDE, parse_header_table, parse_rbi_entities, parse_sahamati,
                              rbi_company_name, referral_note, sector_for)
from tests.helpers import FIXTURES, FakeClient, example_profile
from tests.test_drafting import add_company, contact

PROFILE = example_profile()
SUPPLY = FIXTURES / "supply"
DAY = date(2026, 10, 28)


def text(name: str) -> str:
    return (SUPPLY / name).read_text(encoding="utf-8")


class TypeGateTests(unittest.TestCase):
    """A.1 per-type bounce gate."""

    def gates(self, sends):
        return type_gates(sends, DAY, PROFILE.outreach)

    def test_two_evidence_guess_bounces_in_a_day_pause_it_the_next_day(self):
        sends = [SendRecord("2026-10-27", "evidence_guess", "2026-10-27"),
                 SendRecord("2026-10-27", "evidence_guess", "2026-10-27"),
                 SendRecord("2026-10-27", "evidence_guess")]
        g = self.gates(sends)
        self.assertEqual((g["evidence_guess"].state, g["evidence_guess"].limit), ("paused", 0))
        one = self.gates(sends[1:])
        self.assertEqual((one["evidence_guess"].state, one["evidence_guess"].limit), ("capped", 10))
        older = type_gates(sends, date(2026, 10, 30), PROFILE.outreach)       # the pause lasts a day
        self.assertEqual(older["evidence_guess"].state, "capped")

    def test_hello_same_rule_and_cap_five(self):
        g = self.gates([SendRecord("2026-10-27", "general_inbox", "2026-10-27")] * 2)
        self.assertEqual(g["general_inbox"].state, "paused")
        self.assertEqual(self.gates([])["general_inbox"].limit, 5)

    def test_hiring_inboxes_pause_at_three_of_the_last_twenty(self):
        sends = [SendRecord("2026-10-20", "hiring_inbox", "2026-10-21")] * 3 + \
                [SendRecord("2026-10-22", "hiring_inbox")] * 17
        self.assertEqual(self.gates(sends)["hiring_inbox"].state, "paused")
        later = sends + [SendRecord("2026-10-25", "hiring_inbox")]               # the oldest bounce leaves the window
        self.assertEqual(self.gates(later)["hiring_inbox"].state, "open")
        self.assertEqual(self.gates(sends[1:])["hiring_inbox"].state, "open")    # 2 of 19

    def test_people_and_founders_never_paused_by_others(self):
        sends = [SendRecord("2026-10-27", k, "2026-10-27") for k in ("hiring_inbox", "general_inbox",
                                                                    "evidence_guess")] * 5
        g = self.gates(sends)
        for k in ("verified_person", "founders"):
            self.assertEqual((g[k].state, g[k].limit), ("open", None))
        self.assertEqual(g["guessed"].state, "closed")

    def test_batch_caps_evidence_guesses_at_ten_and_never_sends_pure_guesses(self):
        store = Store(":memory:")
        ev = {"format": "first", "address": "x", "published": "rahul@x", "published_url": "u", "person_url": "p"}
        for n in range(12):
            c = contact(f"asha@e{n}.example", "guessed", name="Asha Rao", role="Founder", source="pattern")
            c.evidence = ev
            add_company(store, f"yc:e{n}", f"Ev {n}", website=f"https://e{n}.example", contacts=[c])
        for n in range(3):
            add_company(store, f"yc:g{n}", f"Pure {n}", website=f"https://g{n}.example",
                        contacts=[contact(f"ravi@g{n}.example", "guessed", name="Ravi K", role="CTO",
                                          source="pattern")])
        batch = make_queue(store, PROFILE, DAY, save=False)
        kinds = [it.address.kind for it in batch.items]
        self.assertEqual(kinds.count("evidence_guess"), 10)
        self.assertNotIn("guessed", kinds)
        self.assertEqual(batch.held_by_kind, {"evidence_guess": 2, "guessed": 3})


class EvidenceGuessTests(unittest.TestCase):
    """A.2 evidence-backed guesses: accept and reject cases."""

    NAMES = {"rahul", "priya", "asha"}

    def guess(self, **kw):
        base = dict(name="Asha Rao", role="Co-founder & CTO", email="asha@acme.example", confidence="guessed",
                    source="pattern", source_url="https://www.ycombinator.com/companies/acme", group="cto",
                    mx="ok", alternates=["asha.rao@acme.example", "asharao@acme.example"])
        base.update(kw)
        return Contact(**base)

    def published(self, email, url="https://acme.example/contact"):
        return [asdict(Contact(name="", role="unrecognised address", email=email, confidence="fallback",
                               source="website", source_url=url))]

    def test_first_format_accepted_with_both_addresses_and_pages(self):
        ev, why = evidence_for_guess(self.guess(), self.published("rahul@acme.example"), "acme.example", "Acme",
                                     names=self.NAMES)
        self.assertEqual(why, "")
        self.assertEqual((ev["address"], ev["format"], ev["published"]), ("asha@acme.example", "first",
                                                                         "rahul@acme.example"))
        self.assertEqual((ev["published_url"], ev["person_url"]), ("https://acme.example/contact",
                                                                   "https://www.ycombinator.com/companies/acme"))

    def test_first_last_format_moves_the_guess(self):
        ev, _ = evidence_for_guess(self.guess(), self.published("priya.sharma@acme.example"), "acme.example",
                                   names=self.NAMES)
        self.assertEqual(ev["address"], "asha.rao@acme.example")

    def test_rejections(self):
        pub = self.published("rahul@acme.example")
        cases = [
            (self.guess(source_url=""), pub, "aren't on a cited page"),
            (self.guess(mx="unchecked"), pub, "MX"),
            (self.guess(group="recruiting", role="Recruiter"), pub, "isn't a founder"),
            (self.guess(), [], "no published personal address"),
            (self.guess(), self.published("vendor@acme.example"), "no published personal address"),   # function word
            (self.guess(), self.published("rahul@other.example"), "no published personal address"),  # other domain
            (self.guess(), self.published("hello@acme.example"), "no published personal address"),   # an inbox
            (self.guess(), self.published("karan@acme.example"), "no published personal address"),   # not a known name
            (self.guess(name="Asha"), self.published("priya.sharma@acme.example"), "surname isn't known"),
        ]
        for guess, published, expected in cases:
            ev, why = evidence_for_guess(guess, published, "acme.example", "Acme", names=self.NAMES)
            self.assertEqual(ev, {}, why)
            self.assertIn(expected, why)

    def test_without_a_name_list_only_first_last_counts(self):
        self.assertEqual(evidence_for_guess(self.guess(), self.published("rahul@acme.example"), "acme.example")[0], {})
        self.assertTrue(evidence_for_guess(self.guess(), self.published("priya.sharma@acme.example"), "acme.example")[0])

    def test_address_format(self):
        self.assertEqual(address_format("rahul@a.example"), "first")
        self.assertEqual(address_format("priya.sharma@a.example"), "first.last")
        self.assertEqual(address_format("customer.success@a.example"), "")
        self.assertEqual(address_format("trackk@trackk.in", "Trackk"), "")
        self.assertEqual(address_format("ops2@a.example"), "")

    def test_kind_and_stored_evidence(self):
        self.assertEqual(contact_kind("asha@a.example", "guessed", evidence={"format": "first"}), "evidence_guess")
        self.assertEqual(contact_kind("asha@a.example", "guessed"), "guessed")
        store = Store(":memory:")
        c = self.guess()
        c.evidence = {"format": "first", "address": c.email, "published": "rahul@acme.example"}
        add_company(store, "yc:acme", "Acme", website="https://acme.example", contacts=[c])
        self.assertEqual(json.loads(store.contacts_for("yc:acme")[0]["evidence"])["published"], "rahul@acme.example")
        it = next(i for i in build_items(store, PROFILE) if i.target_id == "yc-acme")
        self.assertEqual(it.address.kind, "evidence_guess")


class LargeCompanyTests(unittest.TestCase):
    """A.3 large companies: only their own published hiring inbox."""

    def client(self):
        return FakeClient({}, {"https://bigco.example": text("bigco_home.html"),
                               "https://bigco.example/careers": text("bigco_careers.html"),
                               "https://bigco.example/blog/post": text("bigco_blog.html")})

    def test_only_hiring_inboxes_on_careers_pages(self):
        t = Target(key="pool:bigco.example", company="Bigco", kind="general_outreach", hint=APPLY_ONLY_HINT,
                   website="https://bigco.example", domain="bigco.example", team_size=2000)
        result = large_inbox_search(self.client(), t, PROFILE, DoNotContact(), ContactResult(target=t, status="none"))
        emails = [c.email for c in result.contacts]
        self.assertEqual(emails, ["careers@bigco.example", "freshers@bigco.example"])    # invited first
        self.assertNotIn("priya.sharma@bigco.example", emails)        # never a named employee
        self.assertNotIn("talent@bigco.example", emails)              # homepage, not a careers / contact page
        self.assertNotIn("hr@bigco.example", emails)                  # a blog post
        self.assertIn("invites applications", result.contacts[0].note)
        self.assertEqual(result.status, "found")

    def test_build_items_keeps_only_the_hiring_inbox(self):
        store = Store(":memory:")
        t = Target(key="yc:bigco", company="Bigco", kind="general_outreach", hint=APPLY_ONLY_HINT,
                   website="https://bigco.example", domain="bigco.example", team_size=2000)
        hello = Contact(name="", role="hello@ inbox", email="hello@bigco.example", confidence="published_inbox",
                        source="website", source_url="https://bigco.example/contact", proof="company site")
        careers = Contact(name="", role="careers@ inbox", email="careers@bigco.example", confidence="published_inbox",
                          source="website", source_url="https://bigco.example/careers", group="hiring_inbox",
                          proof="company site")
        store.save_contact_result(ContactResult(target=t, status="found", contacts=[hello, careers]))
        it = next(i for i in build_items(store, PROFILE) if i.company_key == "yc:bigco")
        self.assertEqual((it.address.email, it.large), ("careers@bigco.example", True))
        self.assertEqual(it.backups, [])

    def test_it_services_never_emailed(self):
        from applyflow.pool import never_cold_email
        self.assertTrue(never_cold_email("Infosys", "infosys.com", PROFILE))
        self.assertTrue(never_cold_email("Something", "tcs.com", PROFILE))
        cs = [Scored(PoolCompany(key="pool:wipro.com", name="Wipro", website="https://wipro.com", domain="wipro.com",
                                 team_size=200000), 90, [])]
        out = select(cs, {}, PROFILE, 0)
        self.assertFalse(out[0].selected)
        self.assertIn("IT-services", out[0].why_selected)


class OngoingProjectTests(unittest.TestCase):
    """A.4 projects ending in 'Present' are ongoing."""

    FACTS = load_facts(PROFILE)

    def test_validator(self):
        ongoing = [p.name for p in self.FACTS.projects if p.ongoing]
        self.assertTrue(ongoing, "the example profile needs an ongoing project")
        name = ongoing[0]
        self.assertTrue(ongoing_failures(f"Most recently I built {name}, a platform.", self.FACTS))
        self.assertTrue(ongoing_failures(f"I've developed {name} on FastAPI.", self.FACTS))
        self.assertEqual(ongoing_failures(f"I'm currently building {name}, a platform.", self.FACTS), [])
        self.assertEqual(ongoing_failures(f"Right now I'm working on {name}.", self.FACTS), [])
        done = [p.name for p in self.FACTS.projects if not p.ongoing]
        if done:
            self.assertEqual(ongoing_failures(f"I built {done[0]} last year.", self.FACTS), [])

    def test_template_uses_ongoing_wording_and_passes(self):
        inputs = DraftInputs(target_id="yc-acme", company_key="yc:acme", company="Acme", kind="general_outreach",
                             to_email="founders@acme.example", one_liner="APIs for payments")
        draft = template_draft(inputs, self.FACTS)
        self.assertNotIn("I built " + next(p.name for p in self.FACTS.projects if p.ongoing), draft.body)
        self.assertEqual(draft.template_failures, [])

    def test_older_rule_sets_skip_the_check(self):
        name = next(p.name for p in self.FACTS.projects if p.ongoing)
        inputs = DraftInputs(target_id="yc-acme", company_key="yc:acme", company="Acme", kind="general_outreach",
                             to_email="founders@acme.example", rules="2e")
        body = template_draft(DraftInputs(**{**inputs.__dict__, "rules": "2f"}), self.FACTS).body
        body = body.replace("Right now I'm building", "Most recently I built")
        failures = validate("Hello", body, inputs, self.FACTS, "link")
        self.assertFalse(any("ongoing" in f for f in failures))
        inputs.rules = "2f"
        self.assertTrue(any("ongoing" in f for f in validate("Hello", body, inputs, self.FACTS, "link")))
        self.assertIn(name, body)

    def test_store_locks_queued_targets_to_2e_once(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.db"
            store = Store(path)
            store.conn.execute("DROP TABLE draft_rules_2f_done")
            store.save_queue("2026-10-27", [("yc-acme", "a@acme.example", 1)], keep=set())
            store.close()
            store = Store(path)
            self.assertEqual(store.draft_rules().get("yc-acme"), "2e")
            store.save_queue("2026-10-28", [("yc-new", "a@new.example", 1)], keep=set())
            store.close()
            store = Store(path)
            self.assertNotIn("yc-new", store.draft_rules())                  # once
            store.close()


class ListParsingTests(unittest.TestCase):
    """B.1 RBI / Sahamati lists, B.2 GitHub header tables."""

    def test_rbi_entities(self):
        names = parse_rbi_entities(text("rbi_nbfc_aa.html"))
        self.assertEqual(names, ["Finvu Technologies Private Limited",
                                 "Perfios Account Aggregation Services Private Limited",
                                 "Example Co-operative Bank Ltd"])
        self.assertEqual(rbi_company_name(names[0]), "Finvu")
        self.assertTrue(FINTECH_EXCLUDE.search(names[2]))
        for bad in ("State Bank of India", "NPCI", "Mastercard", "RuPay", "NSDL"):
            self.assertTrue(FINTECH_EXCLUDE.search(bad), bad)
        self.assertFalse(FINTECH_EXCLUDE.search("Setu"))

    def test_sahamati(self):
        self.assertEqual(parse_sahamati(text("sahamati_tsp.html")), ["https://setu.co", "https://finarkein.com"])

    def test_header_table(self):
        cfg = {"id": "india-deeptech", "repo": "r/x", "branch": "main", "path": "README.md", "locations": "India"}
        rows = parse_header_table(text("deeptech_readme.md"), cfg)
        self.assertEqual([(c.name, c.website) for c in rows],
                         [("AccuKnox", "https://accuknox.com"), ("Eugenie Technologies", "https://eugenie.ai")])
        self.assertIn("Zero Trust", rows[0].description)
        self.assertEqual(rows[0].sources, ["list:india-deeptech"])


class SectorTests(unittest.TestCase):
    """Sector tags and the consultancy rules."""

    def company(self, **kw):
        base = dict(key="pool:x.example", name="X", website="https://x.example", domain="x.example")
        base.update(kw)
        return PoolCompany(**base)

    def test_pool_sector(self):
        self.assertEqual(pool_sector(self.company(sources=["consultancy:seed"])), "consultancy")
        self.assertEqual(pool_sector(self.company(sources=["fintech:sahamati-tsp"])), "fintech")
        self.assertEqual(pool_sector(self.company(sources=["yc"], tags=["Fintech", "B2B"])), "fintech")
        self.assertEqual(pool_sector(self.company(description="UPI payments for merchants")), "fintech")
        self.assertEqual(pool_sector(self.company(description="Observability for serverless apps")), "")

    def test_sector_for_targets(self):
        inputs = DraftInputs(target_id="a", company_key="a", company="A", kind="general_outreach")
        self.assertEqual(sector_for("A", None, inputs, 40), "startup")
        self.assertEqual(sector_for("A", None, inputs, 4000), "other")
        inputs.one_liner = "Lending infrastructure for NBFCs"
        self.assertEqual(sector_for("A", None, inputs, 40), "fintech")

    def test_consultancy_exempt_from_services_words_but_needs_tech(self):
        good = self.company(name="Codecraft", sources=["consultancy:seed"], sector="consultancy",
                            description="Software consulting: we build cloud platforms and AI products for clients")
        weak = self.company(key="pool:y.example", domain="y.example", name="Y", sources=["consultancy:seed"],
                            sector="consultancy", description="Consulting and staffing services")
        self.assertEqual(consultancy_fit(good, PROFILE), "")
        self.assertIn("names no software work", consultancy_fit(weak, PROFILE))
        out = {s.company.name: s for s in select([Scored(good, 20, []), Scored(weak, 20, [])], {}, PROFILE, 10**6)}
        self.assertTrue(out["Codecraft"].selected)
        self.assertIn("consultancy", out["Codecraft"].why_selected)
        self.assertFalse(out["Y"].selected)


class PublishedAddressTests(unittest.TestCase):
    """A.6 / B.6 classifying "other" published addresses."""

    def test_classification(self):
        cases = {
            "hrindia@n.example": "hiring_inbox", "people@e.example": "hiring_inbox", "hey@k.example": "general_inbox",
            "customer-success@w.example": "unsendable", "sales@s.example": "unsendable",
            "admin@m.example": "unsendable", "legalnotices@d.example": "unsendable", "billing2@b.example": "unsendable",
            "newsroom@l.example": "unsendable", "connect@f.example": "unsendable", "bilal@v.example": "unsendable",
            "hrishikesh@h.example": "unsendable",                       # a person's name, not hr@
        }
        for email, expected in cases.items():
            kind = contact_kind(email, "fallback")
            if kind == "other":
                kind = classify_published(email)[0]
            self.assertEqual(kind, expected, email)
        self.assertIn("support", classify_published("customer-success@w.example")[1])
        self.assertIn("role not confirmed", classify_published("bilal@v.example")[1])
        self.assertIn("company's own name", classify_published("trackk@trackk.in", company="Trackk")[1])

    def test_website_person_needs_a_name_and_a_hiring_role(self):
        url = "https://v.example"
        self.assertEqual(contact_kind("kumar.kothare@v.example", "verified", url, "", "website"), "other")
        self.assertEqual(classify_published("kumar.kothare@v.example")[0], "unsendable")
        self.assertEqual(contact_kind("asha.rao@v.example", "verified", url, "Asha Rao", "website",
                                      role="Head of Marketing"), "other")
        self.assertEqual(contact_kind("asha.rao@v.example", "verified", url, "Asha Rao", "website",
                                      role="Co-founder & CTO"), "verified_person")
        self.assertEqual(contact_kind("jobs.team@v.example", "verified", "", "", "posting"), "hiring_inbox")
        self.assertEqual(contact_kind("pavan.k@v.example", "verified", "https://hn.example", "", "posting"),
                         "verified_person")                       # a posting's own address: applicants are invited

    def test_classified_unsendable_addresses_are_never_queued(self):
        store = Store(":memory:")
        add_company(store, "yc:w", "Wonderly", website="https://w.example",
                    contacts=[contact("customer-success@w.example", "verified")])
        add_company(store, "yc:n", "Nuvizz", website="https://n.example",
                    contacts=[contact("hrindia@n.example", "fallback")])
        items = {it.target_id: it for it in build_items(store, PROFILE)}
        self.assertEqual(items["yc-w"].status, "unsendable")
        self.assertEqual((items["yc-n"].status, items["yc-n"].address.kind), ("ready", "hiring_inbox"))


class FreshnessTests(unittest.TestCase):
    """Pre-send freshness check: an address no longer on its source page is dropped and recorded."""

    def store(self):
        store = Store(":memory:")
        add_company(store, "yc:a", "Alpha", website="https://a.example",
                    contacts=[contact("careers@a.example", "published_inbox", role="careers@ inbox")])
        add_company(store, "yc:b", "Beta", website="https://b.example",
                    contacts=[contact("careers@b.example", "published_inbox", role="careers@ inbox")])
        add_company(store, "yc:c", "Gamma", website="https://c.example",
                    contacts=[contact("founders@c.example", "published_inbox", role="founders' inbox")])
        add_company(store, "yc:d", "Delta", website="https://d.example",
                    contacts=[contact("careers@d.example", "published_inbox", role="careers@ inbox")])
        return store

    def test_gone_address_dropped_recorded_and_replaced(self):
        from applyflow.http import FetchError
        from applyflow.outreach import Freshness
        store = self.store()
        client = FakeClient({}, {"https://a.example/team": "<p>Write to careers@a.example</p>",
                                 "https://b.example/team": "<p>We moved to a form.</p>",        # gone
                                 "https://c.example/team": FetchError("u", "HTTP 503", 503),  # unreadable: kept
                                 "https://d.example/team": "<a href='mailto:careers@d.example'>Jobs</a>"})
        fresh = Freshness(client, store, DAY)
        batch = make_queue(store, PROFILE, DAY, size=3, save=False, fresh=fresh)
        ids = [it.target_id for it in batch.items]
        self.assertNotIn("yc-b", ids)
        self.assertEqual(sorted(ids), ["yc-a", "yc-c", "yc-d"])            # Delta takes Beta's place
        self.assertIn("careers@b.example", store.excluded_contacts())
        self.assertIn("no longer on https://b.example/team", store.excluded_contacts()["careers@b.example"])
        status = {c.target_id: c.status for c in batch.fresh}
        self.assertEqual((status["yc-b"], status["yc-c"]), ("gone", "unchecked"))
        items = {it.target_id: it for it in build_items(store, PROFILE)}
        self.assertNotEqual(items["yc-b"].status, "ready")                  # never queued again

    def test_404_counts_as_gone_and_postings_are_not_rechecked(self):
        from applyflow.outreach import Freshness
        store = Store(":memory:")
        add_company(store, "yc:a", "Alpha", website="https://a.example",
                    contacts=[contact("careers@a.example", "published_inbox", role="careers@ inbox")])
        add_company(store, "yc:p", "Posty", website="https://p.example",
                    contacts=[contact("jobs@p.example", "verified", role="posting", source="posting")])
        fresh = Freshness(FakeClient({}, {}), store, DAY)                  # every page 404s
        items = {it.target_id: it for it in build_items(store, PROFILE)}
        self.assertFalse(fresh.check(items["yc-a"]))
        self.assertTrue(fresh.check(items["yc-p"]))
        self.assertEqual(len(fresh.client.page_calls), 1)

    def test_evidence_guess_rechecks_the_published_address_and_the_person(self):
        from applyflow.outreach import Freshness
        store = Store(":memory:")
        c = contact("asha@e.example", "guessed", name="Asha Rao", role="CTO", source="pattern")
        c.source_url = "https://e.example/about"
        c.evidence = {"format": "first", "address": c.email, "published": "rahul@e.example",
                      "published_url": "https://e.example/contact", "person_url": "https://e.example/about"}
        add_company(store, "yc:e", "Eps", website="https://e.example", contacts=[c])
        it = next(i for i in build_items(store, PROFILE) if i.target_id == "yc-e")
        ok = FakeClient({}, {"https://e.example/contact": "rahul@e.example",
                             "https://e.example/about": "<h3>Asha Rao</h3><p>CTO</p>"})
        self.assertTrue(Freshness(ok, store, DAY).check(it))
        left = FakeClient({}, {"https://e.example/contact": "rahul@e.example",
                               "https://e.example/about": "<h3>Someone Else</h3>"})
        self.assertFalse(Freshness(left, store, DAY).check(it))
        self.assertIn("Asha Rao's name no longer on", store.excluded_contacts()["asha@e.example"])


class QueuedAddressTests(unittest.TestCase):
    def test_a_queued_unsent_target_keeps_its_queued_address_and_role_email(self):
        """The report / outbox must show the address the batch stored, not a stored primary the 2f rules skip."""
        store = Store(":memory:")
        add_company(store, "yc:v", "Velo", website="https://v.example",
                    contacts=[contact("kumar.kothare@v.example", "verified"),          # unnamed: not sendable
                              contact("careers@v.example", "published_inbox", role="careers@ inbox")])
        store.save_role_match("yc:v", "careers", "yes", title="Backend Engineer", url="https://v.example/careers")
        before = next(i for i in build_items(store, PROFILE) if i.target_id == "yc-v")
        self.assertEqual((before.address.email, before.kind), ("careers@v.example", "role"))
        store.save_queue("2026-10-28", [("yc-v", "careers@v.example", 1)], keep=set())
        after = next(i for i in build_items(store, PROFILE) if i.target_id == "yc-v")
        self.assertEqual((after.address.email, after.kind, after.protected), ("careers@v.example", "role", True))


class InferredAndStoppedTests(unittest.TestCase):
    def test_inferred_pattern_addresses_are_guesses(self):
        self.assertEqual(contact_kind("asha@a.example", "strong"), "guessed")
        self.assertEqual(contact_kind("asha@a.example", "strong", evidence={"format": "first"}), "evidence_guess")

    def test_bounced_and_stopped_target_never_moves_to_a_guess(self):
        from applyflow.outreach import mark_bounced, mark_sent
        store = Store(":memory:")
        add_company(store, "yc:z", "Zed", website="https://z.example",
                    contacts=[contact("careers@z.example", "published_inbox", role="careers@ inbox"),
                              contact("asha@z.example", "guessed", name="Asha Rao", role="CTO", source="pattern")])
        mark_sent(store, PROFILE, "yc-z", date(2026, 10, 27))
        it = mark_bounced(store, PROFILE, "yc-z", date(2026, 10, 27), stop=True)
        self.assertEqual(it.status, "stopped")


class ReferralTests(unittest.TestCase):
    def test_note_is_at_most_300_characters_and_asks_for_a_referral(self):
        note = referral_note("Razorpay", "Software Development Engineer Intern - Payments Platform (Bengaluru)",
                             "I'm building CareerForge AI, a FastAPI + PostgreSQL platform with 15 JWT-secured REST "
                             "endpoints.")
        self.assertLessEqual(len(note), 300)
        self.assertIn("Razorpay", note)
        self.assertIn("referring me", note)


if __name__ == "__main__":
    unittest.main()
