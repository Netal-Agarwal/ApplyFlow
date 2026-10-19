"""Phase 2a.1: trustworthy contacts - posting emails, deliverability ranking, MX, alternates, links, apply-only."""

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applyflow.collectors import HNWhoIsHiringCollector
from applyflow.contacts import (DoNotContact, GitHubBudget, MXChecker, build_targets, find_contacts, parse_doh_mx,
                                search_links)
from applyflow.models import Job
from applyflow.pipeline import score_all
from applyflow.report import write_contacts_csv, write_html
from applyflow.scoring import APPLY_ONLY_HINT, contact_hint, decide_action
from applyflow.store import Store
from tests.helpers import TODAY, example_profile, fixture
from tests.test_contacts import client, run, target, text

YC = "https://www.ycombinator.com"
DELTA = "https://deltaai.example"


class FakeDNS:
    """Stands in for PoliteClient.get_api on the DNS-over-HTTPS endpoints: answers by queried name."""

    def __init__(self, answers, failing=("cloudflare-dns.com",)):
        self.answers, self.failing, self.calls = answers, failing, []

    def get_api(self, url, params=None, headers=None):
        from applyflow.http import FetchError
        self.calls.append((url, params["name"]))
        if any(host in url for host in self.failing) or params["name"] not in self.answers:
            raise FetchError(url, "HTTP 503", 503)
        return self.answers[params["name"]], {}


def dns(**names):
    return {name.replace("_", "."): fixture(f"contacts/doh_{kind}.json") for name, kind in names.items()}


class PostingEmailTests(unittest.TestCase):
    """1. An email a posting publishes for applicants is a verified contact, with or without a domain."""

    def setUp(self):
        jobs = HNWhoIsHiringCollector.parse(fixture("contacts/hn_posting_emails.json")["hits"], "49922569")
        self.jobs = {j.company: j for j in jobs}

    def test_domain_derived_from_posting_email(self):
        store = Store(":memory:")
        job = self.jobs["VectorDB Co"]
        job.description = job.description.replace("Python, Go,", "0-1 years experience. Python, FastAPI, AWS, REST APIs,")
        store.upsert_board("hn:1", [job])
        score_all(store, example_profile(), TODAY)
        targets = {t.company: t for t in build_targets(store, example_profile(), [])}
        t = targets["VectorDB Co"]
        self.assertEqual((t.domain, t.domain_source), ("vectordb.example", "posting email"))
        result, session = run(t, {})
        self.assertEqual((result.status, result.contacts[0].email, result.contacts[0].confidence),
                         ("found", "pavan.k@vectordb.example", "verified"))
        self.assertEqual(session.urls, [])                # good enough: no site search needed
        store.close()

    def test_freemail_posting_email_counts_without_a_domain(self):
        t = target(domain="", website="", jobs=[self.jobs["Solo Labs"]], company="Solo Labs")
        result, session = run(t, {})
        self.assertEqual((result.status, t.domain), ("found", ""))
        self.assertEqual((result.contacts[0].email, result.contacts[0].confidence),
                         ("solo.founder.hiring@gmail.com", "verified"))
        self.assertEqual(session.urls, [])

    def test_no_domain_and_no_posting_email_is_still_no_domain(self):
        result, _ = run(target(domain="", website=""), {})
        self.assertEqual(result.status, "no_domain")


class DeliverabilityRankingTests(unittest.TestCase):
    """2. verified > strong > published inbox > guessed; Cardboard-style founders@ beats a guessed founder."""

    def routes(self):
        return {DELTA: text("delta_home.html"),
                f"{YC}/robots.txt": text("yc_robots.txt"),
                f"{YC}/companies/gamma-health": text("yc_company_gamma.html")}

    def test_founders_inbox_beats_guessed_founder(self):
        result, _ = run(target(domain="deltaai.example", website=DELTA, yc_slug="gamma-health"), self.routes())
        self.assertEqual([(c.email, c.confidence) for c in result.contacts],
                         [("founders@deltaai.example", "published_inbox"),
                          ("careers@deltaai.example", "published_inbox"),
                          ("contact@deltaai.example", "published_inbox")])
        guessed = [c for c in result.considered if c.confidence == "guessed"]
        self.assertEqual([c.name for c in guessed], ["Kabir Rao", "Meera Iyer"])    # kept as backups
        self.assertEqual([c.email for c in result.considered][-2:],
                         ["kabir@deltaai.example", "meera@deltaai.example"])

    def test_founders_and_hiring_inboxes_before_info_and_contact(self):
        result, _ = run(target(domain="deltaai.example", website=DELTA), self.routes())
        self.assertEqual([c.email for c in result.considered],
                         ["founders@deltaai.example", "careers@deltaai.example", "contact@deltaai.example",
                          "info@deltaai.example"])

    def test_tool_and_system_addresses_are_dropped(self):
        result, _ = run(target(domain="deltaai.example", website=DELTA), self.routes())
        emails = {c.email for c in result.considered}
        for tool in ("jira@deltaai.example", "alerts@deltaai.example", "noreply@deltaai.example"):
            self.assertNotIn(tool, emails)

    def test_inboxes_are_only_fallback_above_fifty_people(self):
        result, _ = run(target(hint="engineering manager or tech lead", domain="deltaai.example", website=DELTA),
                        self.routes())
        self.assertEqual({c.confidence for c in result.considered}, {"fallback"})


class MXTests(unittest.TestCase):
    """3. Contacts on a domain without MX records are dropped; a failed lookup keeps them, marked."""

    def acme_routes(self):
        from tests.test_contacts import acme_routes
        return acme_routes()

    def test_parse(self):
        self.assertEqual(parse_doh_mx(fixture("contacts/doh_mx_ok.json")), "ok")
        self.assertEqual(parse_doh_mx(fixture("contacts/doh_no_mx.json")), "no_mx")
        self.assertEqual(parse_doh_mx(fixture("contacts/doh_nxdomain.json")), "no_mx")
        self.assertEqual(parse_doh_mx(fixture("contacts/doh_null_mx.json")), "no_mx")
        with self.assertRaises(ValueError):
            parse_doh_mx(fixture("contacts/doh_servfail.json"))

    def test_ok_domain_kept_and_marked(self):
        mx = MXChecker(FakeDNS(dns(acmerobotics_example="mx_ok")))      # Cloudflare down: Google answers
        result, _ = run(target(), self.acme_routes(), mx=mx)
        self.assertTrue(result.contacts)
        self.assertEqual({c.mx for c in result.considered}, {"ok"})
        self.assertIn("MX: acmerobotics.example ok", result.reasons)

    def test_no_mx_domain_dropped_with_reason(self):
        mx = MXChecker(FakeDNS(dns(acmerobotics_example="no_mx")))
        result, _ = run(target(), self.acme_routes(), mx=mx)
        self.assertEqual((result.status, result.contacts), ("none", []))
        self.assertIn(("priya.sharma@acmerobotics.example", "acmerobotics.example has no MX records (can't receive "
                                                            "email)"), result.excluded)

    def test_dns_failure_keeps_contacts_marked_unchecked(self):
        mx = MXChecker(FakeDNS({}, failing=("cloudflare-dns.com", "dns.google")))
        result, _ = run(target(), self.acme_routes(), mx=mx)
        self.assertTrue(result.contacts)
        self.assertEqual({c.mx for c in result.contacts}, {"unchecked"})
        self.assertEqual(mx.failures, 1)
        self.assertIn("MX: acmerobotics.example unchecked", result.reasons)

    def test_cached_for_thirty_days(self):
        store = Store(":memory:")
        dns_client = FakeDNS(dns(deltaai_example="mx_ok"))
        self.assertEqual(MXChecker(dns_client, store).check("deltaai.example"), "ok")
        calls = len(dns_client.calls)
        again = MXChecker(dns_client, store)
        self.assertEqual((again.check("deltaai.example"), len(dns_client.calls), again.cached), ("ok", calls, 1))
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
        store.conn.execute("UPDATE mx_cache SET checked_at = ?", (old,))
        MXChecker(dns_client, store).check("deltaai.example")
        self.assertGreater(len(dns_client.calls), calls)                 # expired: looked up again
        failing = MXChecker(FakeDNS({}, failing=("cloudflare-dns.com", "dns.google")), store)
        failing.check("broken.example")
        self.assertIsNone(store.get_mx("broken.example", 30))            # failures aren't cached
        store.close()


class AlternatesAndLinksTests(unittest.TestCase):
    """4. Guesses carry two alternates.  5. Guessed / inbox people get search links (never fetched)."""

    def setUp(self):
        routes = {"https://betaledger.example": text("beta_home.html"),
                  "https://betaledger.example/company": text("beta_company.html")}
        self.result, self.session = run(target(domain="betaledger.example", website="https://betaledger.example",
                                               company="Beta Ledger", roles=["Backend Intern (80)"]), routes)
        self.store = Store(":memory:")
        self.store.save_contact_result(self.result)

    def tearDown(self):
        self.store.close()

    def test_alternates_stored_and_in_csv(self):
        rows = self.store.contacts_for("yc:acme-robotics")
        self.assertEqual(json.loads(rows[1]["alternates"]),
                         ["neha.gupta@betaledger.example", "nehagupta@betaledger.example"])
        with tempfile.TemporaryDirectory() as tmp:
            with open(write_contacts_csv(self.store, Path(tmp) / "c.csv"), encoding="utf-8") as fh:
                row = next(csv.DictReader(fh))
        self.assertEqual((row["primary_email"], row["primary_confidence"], row["primary_alternates"]),
                         ("jobs@betaledger.example", "published_inbox", ""))
        self.assertEqual((row["backup1_email"], row["backup1_alternates"]),
                         ("neha@betaledger.example", "neha.gupta@betaledger.example; nehagupta@betaledger.example"))
        linkedin, google = search_links("Neha Gupta", "Beta Ledger")
        self.assertEqual((row["backup1_linkedin_search"], row["backup1_google_search"]), (linkedin, google))
        self.assertEqual(row["primary_linkedin_search"], "")             # an unnamed inbox: nothing to search

    def test_link_format_and_never_fetched(self):
        linkedin, google = search_links("Neha Gupta", "Beta Ledger")
        self.assertEqual(linkedin, "https://www.linkedin.com/search/results/people/?keywords=Neha%20Gupta%20Beta%20Ledger")
        self.assertEqual(google, "https://www.google.com/search?q=%22Neha%20Gupta%22%20Beta%20Ledger")
        self.assertFalse(any("linkedin.com" in u or "google.com" in u for u in self.session.urls))

    def test_html_report_shows_links_and_alternates(self):
        # Make the guessed founder the primary to see her tags on an opening.
        self.store.conn.execute("UPDATE contacts SET is_primary = (rank = 1)")
        primary = {"betaledger": self.store.primary_contacts()[0]}
        rows = [{"tier": "A", "score": 80, "company": "Beta Ledger", "title": "Backend Intern", "track": "internship",
                 "family": "backend", "locations": "[]", "location_class": "preferred_india", "action": "apply",
                 "contact_hint": "founder or CTO", "team_size": 20, "lead_project": "", "emails": "[]", "url": "u",
                 "apply_url": "u", "source": "lever", "posted_at": "", "id": "lever:1", "reasons": "[]",
                 "merged_locations": "[]"}]
        with tempfile.TemporaryDirectory() as tmp:
            page = write_html(rows, Path(tmp) / "r.html", {}, primary).read_text(encoding="utf-8")
        self.assertIn("neha@betaledger.example [guessed]", page)
        self.assertIn("if it bounces: neha.gupta@betaledger.example, nehagupta@betaledger.example", page)
        self.assertIn("https://www.linkedin.com/search/results/people/?keywords=Neha%20Gupta%20Beta%20Ledger", page)


class ApplyOnlyTests(unittest.TestCase):
    """6. Companies over 300 people (or, size unknown, with a large board) are apply-only."""

    def job(self, **kw):
        base = dict(source="greenhouse", external_id="1", company="Bigco", title="Software Engineer, Intern",
                    url="u", locations=["Bengaluru"], description="Python, AWS, REST APIs.")
        base.update(kw)
        return Job(**base)

    def test_hint_and_action(self):
        profile = example_profile()
        for kw in ({"team_size": 5000}, {"board_size": 725}):
            self.assertEqual(contact_hint(self.job(**kw), profile), APPLY_ONLY_HINT, kw)
            self.assertEqual(decide_action(self.job(**kw), "preferred_india", profile), "apply", kw)
        self.assertNotEqual(contact_hint(self.job(team_size=250), profile), APPLY_ONLY_HINT)

    def test_threshold_configurable(self):
        profile = example_profile()
        profile.contacts["apply_only_team_size"] = 10_000
        self.assertNotEqual(contact_hint(self.job(team_size=5000), profile), APPLY_ONLY_HINT)

    def test_no_contact_search_unless_a_posting_lists_an_email(self):
        big = target(hint=APPLY_ONLY_HINT, board_size=725, company="Bigco")
        result, session = run(big)
        self.assertEqual((result.status, session.urls), ("apply_only", []))
        self.assertIn("big company (725 open roles): apply via the posting; no contact search", result.reasons)
        with_email = target(hint=APPLY_ONLY_HINT, board_size=725, company="Bigco",
                            jobs=[self.job(emails=["campus@acmerobotics.example"])])
        result, session = run(with_email)
        self.assertEqual([(c.email, c.confidence) for c in result.contacts],
                         [("campus@acmerobotics.example", "verified")])
        self.assertEqual(session.urls, [])                                   # the site isn't searched

    def test_counted_as_apply_only(self):
        store = Store(":memory:")
        result, _ = run(target(hint=APPLY_ONLY_HINT, board_size=725))
        store.save_contact_result(result)
        self.assertEqual(store.contact_counts(), {"apply_only": 1})
        store.close()


if __name__ == "__main__":
    unittest.main()


class AddressCleanupTests(unittest.TestCase):
    def test_service_words_anywhere_and_escaped_prefixes(self):
        from applyflow.contacts import classify_address, guess_alternates
        from applyflow.text import extract_emails
        self.assertIsNone(classify_address("loansupport@clickpe.example"))
        self.assertIsNone(classify_address("customercare@shop.example"))
        self.assertEqual(extract_emails(r'{"html":"\u003ebilling@codingal.example\u003c"}'),
                         ["billing@codingal.example"])
        self.assertEqual(guess_alternates("tanmaay", "k", "apollo.example"), [])
        self.assertEqual(guess_alternates("neha", "gupta", "b.example"), ["neha.gupta@b.example", "nehagupta@b.example"])
