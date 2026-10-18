"""Regression tests for the calibration backlog found on the first live run (CLAUDE.md)."""

import csv
import tempfile
import unittest
from pathlib import Path

from applyflow.collectors import AshbyCollector, GreenhouseCollector, HNWhoIsHiringCollector, LeverCollector
from applyflow.models import Company, Job
from applyflow.pipeline import score_all
from applyflow.report import write_csv
from applyflow.scoring import normalized_title, score_job, sponsorship_signal
from applyflow.store import Store
from applyflow.text import extract_contact_emails
from tests.helpers import TODAY, example_profile, fixture


class AshbyRemoteTests(unittest.TestCase):
    """1. Ashby sets isRemote on hybrid roles; only workplaceType or the location text count."""

    @classmethod
    def setUpClass(cls):
        cls.profile = example_profile()
        jobs = AshbyCollector.parse(fixture("ashby_hybrid.json"), Company("HybridCo", "ashby", "hybridco"))
        cls.jobs = {j.external_id: j for j in jobs}

    def test_hybrid_is_not_remote(self):
        job = self.jobs["h1"]
        self.assertFalse(job.remote)
        r = score_job(job, self.profile, TODAY)
        self.assertEqual(r.location_class, "abroad_onsite")
        self.assertEqual(r.action, "apply")           # kept only because it offers relocation

    def test_is_remote_flag_alone_is_ignored(self):
        job = self.jobs["h2"]
        self.assertIsNone(job.remote)
        r = score_job(job, self.profile, TODAY)
        self.assertEqual(r.tier, "rejected")
        self.assertIn("onsite abroad", r.reasons[0])

    def test_remote_location_text_still_counts(self):
        job = self.jobs["h3"]
        self.assertTrue(job.remote)
        self.assertEqual(score_job(job, self.profile, TODAY).location_class, "remote_open")

    def test_remote_in_foreign_countries_only_is_restricted(self):
        r = score_job(self.jobs["h4"], self.profile, TODAY)     # Cohere: Remote, "Canada | Dubai | London | US"
        self.assertEqual(r.tier, "rejected")
        self.assertIn("restricted to another region", r.reasons[0])


class USOnlyRoleTests(unittest.TestCase):
    """2. Citizenship / clearance / ITAR requirements beat 'Relocation assistance' in the benefits."""

    @classmethod
    def setUpClass(cls):
        cls.profile = example_profile()
        jobs = LeverCollector.parse(fixture("lever_palantir.json"), Company("Palantir", "lever", "palantir"))
        cls.results = {j.external_id: score_job(j, cls.profile, TODAY) for j in jobs}

    def test_clearance_role_rejected_with_phrase(self):
        r = self.results["gov-1"]
        self.assertEqual(r.tier, "rejected")
        self.assertIn("says no sponsorship ('Security Clearance')", r.reasons[0])

    def test_itar_us_person_rejected(self):
        r = self.results["def-1"]
        self.assertEqual(r.tier, "rejected")
        self.assertIn("'U.S. person'", r.reasons[0])

    def test_military_is_not_itar(self):
        r = self.results["com-1"]
        self.assertTrue(r.accepted, r.reasons)
        self.assertIn("('We sponsor')", r.reasons[-1])

    def test_signals(self):
        self.assertEqual(sponsorship_signal("Must be a US citizen.")[0], "negative")
        self.assertEqual(sponsorship_signal("Must be eligible for clearance.")[0], "negative")
        self.assertEqual(sponsorship_signal("Relocation assistance; time off for military service")[0], "positive")


def job(title="Backend Engineer - New Grad", description="0-1 years experience. Python, FastAPI, AWS, REST APIs.",
        locations=("Bengaluru",), source="greenhouse", company="Acme", **kw):
    return Job(source=source, external_id=kw.pop("external_id", "1"), company=company, title=title,
               url="https://example.com", description=description, locations=list(locations), **kw)


class FreshnessTests(unittest.TestCase):
    """3. Evergreen postings from 2014/2020 are rejected; ones older than ~4 months lose points."""

    def setUp(self):
        self.profile = example_profile()

    def test_fresh_posting_unaffected(self):
        fresh = score_job(job(posted_at="2026-10-01T10:00:00Z"), self.profile, TODAY)
        undated = score_job(job(), self.profile, TODAY)
        self.assertEqual(fresh.score, undated.score)
        self.assertFalse(any("stale" in r for r in fresh.reasons))

    def test_stale_posting_penalized(self):
        fresh = score_job(job(posted_at="2026-10-01"), self.profile, TODAY)
        stale = score_job(job(posted_at="2026-03-01"), self.profile, TODAY)
        self.assertTrue(stale.accepted)
        self.assertEqual(fresh.score - stale.score, 10)
        self.assertIn("stale: posted 231 days ago (2026-03-01) -> -10", stale.reasons)

    def test_evergreen_posting_rejected(self):
        r = score_job(job(posted_at="2014-01-23T00:00:00+00:00"), self.profile, TODAY)
        self.assertEqual(r.tier, "rejected")
        self.assertIn("posting is 4651 days old", r.reasons[0])

    def test_profile_can_tune_thresholds(self):
        self.profile.freshness["max_age_days"] = 5000
        self.assertNotEqual(score_job(job(posted_at="2014-01-23"), self.profile, TODAY).tier, "rejected")



class DuplicateTests(unittest.TestCase):
    """4. One opening posted once per city becomes one row with every location."""

    def setUp(self):
        self.store = Store(":memory:")
        desc = "0-1 years experience. Python, FastAPI, AWS, REST APIs, PostgreSQL."
        self.store.upsert_board("greenhouse:acme", [
            job("Backend Engineer - New Grad", desc, ["Hyderabad"], external_id="1"),
            job("Backend Engineer - New Grad", desc, ["Bengaluru"], external_id="2"),
            job("Backend Engineer - New Grad (Pune)", desc, ["Pune"], external_id="3"),
            job("Backend Engineer Intern", desc, ["Bengaluru"], external_id="4"),
        ])

    def tearDown(self):
        self.store.close()

    def test_normalized_title_drops_own_city(self):
        self.assertEqual(normalized_title(job("Backend Engineer - New Grad (Pune)", locations=["Pune, India"])),
                         "backend engineer new grad")

    def test_merged_into_best_posting(self):
        results = {r.job_id: r for r in score_all(self.store, example_profile(), TODAY)}
        kept = [r for r in results.values() if r.accepted]
        self.assertEqual(sorted(r.job_id for r in kept), ["greenhouse:1", "greenhouse:4"])
        self.assertEqual(results["greenhouse:1"].merged_locations, ["Hyderabad", "Bengaluru", "Pune"])
        self.assertIn("merged 2 duplicate posting(s) for other locations", results["greenhouse:1"].reasons)
        self.assertEqual(results["greenhouse:3"].reasons[0],
                         "duplicate posting (same company + title; kept greenhouse:1)")

        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(self.store.ranked(), Path(tmp) / "o.csv")
            with open(path, encoding="utf-8") as fh:
                rows = {r["id"]: r for r in csv.DictReader(fh)}
        self.assertEqual(rows["greenhouse:1"]["location"], "Hyderabad | Bengaluru | Pune")
        self.assertEqual(self.store.stats()["top_reject_reasons"], [("duplicate posting", 2)])



class BoilerplateEmailTests(unittest.TestCase):
    """5. ATS emails from accommodation / EEO / privacy / scam-warning paragraphs are dropped."""

    @classmethod
    def setUpClass(cls):
        jobs = GreenhouseCollector.parse(fixture("greenhouse_rubrik.json"), Company("Rubrik", "greenhouse", "rubrik"))
        cls.jobs = {j.external_id: j for j in jobs}

    def test_accommodation_email_dropped(self):
        self.assertEqual(self.jobs["2001"].emails, [])

    def test_application_email_kept(self):
        self.assertEqual(self.jobs["2002"].emails, ["noopur.mer@rubrik.com"])

    def test_contact_emails(self):
        self.assertEqual(extract_contact_emails("HOW TO APPLY Email hiring@composio.dev with your resume"),
                         ["hiring@composio.dev"])
        self.assertEqual(extract_contact_emails("Need an alternative method for applying? Email recruiting@x.com"), [])

    def test_hn_keeps_every_email(self):
        hit = {"objectID": "9", "parent_id": "1", "created_at": "2026-10-02T10:00:00Z",
               "comment_text": "Acme | Backend Intern | Bengaluru<p>Privacy-first payments. Email jobs@acme.dev</p>"}
        self.assertEqual(HNWhoIsHiringCollector.parse([hit], "1")[0].emails, ["jobs@acme.dev"])



class CompanyNameTests(unittest.TestCase):
    """6. The seed / discovered name wins over Greenhouse's board title ("Rubrik Job Board")."""

    def test_greenhouse(self):
        jobs = GreenhouseCollector.parse(fixture("greenhouse_rubrik.json"), Company("Rubrik", "greenhouse", "rubrik"))
        self.assertEqual({j.company for j in jobs}, {"Rubrik"})

    def test_ats_name_used_when_company_has_none(self):
        jobs = GreenhouseCollector.parse(fixture("greenhouse_rubrik.json"), Company("", "greenhouse", "rubrik"))
        self.assertEqual(jobs[0].company, "Rubrik Job Board")



class MidLevelTitleTests(unittest.TestCase):
    """7. 'Intermediate', 'mid-level' and 'experienced' titles aren't fresher roles."""

    def test_excluded(self):
        profile = example_profile()
        for title in ("Intermediate Backend Engineer, India", "Mid-Level Software Engineer",
                      "Mid Level Backend Developer", "Experienced Python Developer"):
            r = score_job(job(title, locations=["Bangalore, India"]), profile, TODAY)
            self.assertEqual(r.tier, "rejected", title)
            self.assertIn("title contains excluded term", r.reasons[0])


if __name__ == "__main__":
    unittest.main()
