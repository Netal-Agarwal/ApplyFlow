"""Regression tests for the calibration backlog found on the first live run (CLAUDE.md)."""

import unittest

from applyflow.collectors import AshbyCollector, LeverCollector
from applyflow.models import Company
from applyflow.scoring import score_job, sponsorship_signal
from tests.helpers import example_profile, fixture


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
        r = score_job(job, self.profile)
        self.assertEqual(r.location_class, "abroad_onsite")
        self.assertEqual(r.action, "apply")           # kept only because it offers relocation

    def test_is_remote_flag_alone_is_ignored(self):
        job = self.jobs["h2"]
        self.assertIsNone(job.remote)
        r = score_job(job, self.profile)
        self.assertEqual(r.tier, "rejected")
        self.assertIn("onsite abroad", r.reasons[0])

    def test_remote_location_text_still_counts(self):
        job = self.jobs["h3"]
        self.assertTrue(job.remote)
        self.assertEqual(score_job(job, self.profile).location_class, "remote_open")

    def test_remote_in_foreign_countries_only_is_restricted(self):
        r = score_job(self.jobs["h4"], self.profile)     # Cohere: Remote, "Canada | Dubai | London | US"
        self.assertEqual(r.tier, "rejected")
        self.assertIn("restricted to another region", r.reasons[0])


class USOnlyRoleTests(unittest.TestCase):
    """2. Citizenship / clearance / ITAR requirements beat 'Relocation assistance' in the benefits."""

    @classmethod
    def setUpClass(cls):
        cls.profile = example_profile()
        jobs = LeverCollector.parse(fixture("lever_palantir.json"), Company("Palantir", "lever", "palantir"))
        cls.results = {j.external_id: score_job(j, cls.profile) for j in jobs}

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


if __name__ == "__main__":
    unittest.main()
