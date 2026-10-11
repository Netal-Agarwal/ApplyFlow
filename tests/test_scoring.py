import unittest

from applyflow.collectors import AshbyCollector, GreenhouseCollector, HNWhoIsHiringCollector, LeverCollector
from applyflow.models import Company, Job
from applyflow.scoring import (
    classify_location,
    detect_sponsorship,
    detect_track,
    required_years,
    score_job,
)
from tests.helpers import FakeClient, default_routes, example_profile, fixture


def job(title="Software Engineer", description="", locations=("Bengaluru",), source="greenhouse", **kw):
    return Job(source=source, external_id=kw.pop("external_id", "1"), company="Acme", title=title,
               url="https://example.com", description=description, locations=list(locations), **kw)


class RequiredYearsTests(unittest.TestCase):
    def test_patterns(self):
        self.assertEqual(required_years("3+ years of experience with Python"), 3)
        self.assertEqual(required_years("0-1 years experience"), 0)
        self.assertEqual(required_years("Experience: 2 to 4 years"), 2)
        self.assertEqual(required_years("at least two years of professional experience"), 2)
        self.assertEqual(required_years("1+ yrs exp. with AWS, 4+ years of experience overall"), 4)

    def test_ignores_unrelated_numbers(self):
        self.assertIsNone(required_years("Founded 10 years ago, we serve 5 million users."))
        self.assertIsNone(required_years("No experience required."))


class TrackTests(unittest.TestCase):
    def test_tracks(self):
        self.assertEqual(detect_track(job("Backend Engineer Intern")), "internship")
        self.assertEqual(detect_track(job("SDE", employment_type="Intern")), "internship")
        self.assertEqual(detect_track(job("Software Engineer - New Grad")), "new_grad")
        self.assertEqual(detect_track(job("SDE-1 Backend")), "new_grad")
        self.assertEqual(detect_track(job("Software Engineer I")), "new_grad")
        self.assertEqual(detect_track(job("Software Engineer", "Open to 2027 graduates")), "new_grad")
        self.assertEqual(detect_track(job("Software Engineer", "Build cool things")), "unspecified")
        self.assertEqual(detect_track(job("Software Engineer Infrastructure")), "unspecified")


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.profile = example_profile()

    def loc(self, *locations, remote=None):
        return classify_location(job(locations=locations, remote=remote), self.profile)

    def test_classes(self):
        self.assertEqual(self.loc("Bengaluru"), "preferred_india")
        self.assertEqual(self.loc("Gurugram, Haryana"), "preferred_india")
        self.assertEqual(self.loc("Pune"), "other_india")
        self.assertEqual(self.loc("Remote - India"), "remote_open")
        self.assertEqual(self.loc("Remote (Worldwide)"), "remote_open")
        self.assertEqual(self.loc("Remote (US)"), "remote_restricted")
        self.assertEqual(self.loc("Remote"), "remote_unclear")
        self.assertEqual(self.loc("London, UK"), "abroad_onsite")
        self.assertEqual(self.loc(), "unknown")
        self.assertEqual(self.loc("Toronto", remote=True), "remote_unclear")

    def test_best_location_wins(self):
        self.assertEqual(self.loc("San Francisco", "Bengaluru"), "preferred_india")
        self.assertEqual(self.loc("New York, NY (HQ)", "Remote (US)"), "abroad_onsite")

    def test_us_marker_is_whole_word(self):
        # "focus" contains "us" but shouldn't make a remote role US-only.
        self.assertEqual(self.loc("Remote, focus on async"), "remote_unclear")


class SponsorshipTests(unittest.TestCase):
    def test_signals(self):
        self.assertEqual(detect_sponsorship("We offer visa sponsorship and relocation support."), "positive")
        self.assertEqual(detect_sponsorship("Relocation assistance available."), "positive")
        self.assertEqual(detect_sponsorship("We do not offer visa sponsorship."), "negative")
        self.assertEqual(detect_sponsorship("We are unable to sponsor visas."), "negative")
        self.assertEqual(detect_sponsorship("Must be authorized to work in the US."), "negative")
        self.assertEqual(detect_sponsorship("Python and AWS."), "unknown")


class FixtureScoringTests(unittest.TestCase):
    """End-to-end expectations on realistic postings."""

    @classmethod
    def setUpClass(cls):
        cls.profile = example_profile()
        gh = GreenhouseCollector.parse(fixture("greenhouse_acme.json"), Company("Acme", "greenhouse", "acme"))
        lv = LeverCollector.parse(fixture("lever_zeta.json"), Company("Zeta", "lever", "zeta"))
        ab = AshbyCollector.parse(fixture("ashby_nimbus.json"), Company("Nimbus", "ashby", "nimbus"))
        hn = HNWhoIsHiringCollector(FakeClient(default_routes())).fetch()
        cls.results = {j.id: score_job(j, cls.profile) for j in gh + lv + ab + hn}

    def r(self, job_id):
        return self.results[job_id]

    def test_strong_internship_is_tier_a(self):
        r = self.r("greenhouse:1001")
        self.assertEqual((r.tier, r.track, r.family, r.location_class), ("A", "internship", "backend", "preferred_india"))
        self.assertEqual(r.action, "apply_plus_email")
        self.assertIn("fastapi", r.matched_skills)

    def test_rejections(self):
        self.assertIn("excluded term 'senior'", self.r("greenhouse:1002").reasons[0])
        self.assertIn("3+ years", self.r("greenhouse:1003").reasons[0])
        self.assertIn("role family", self.r("greenhouse:1004").reasons[0])
        self.assertIn("restricted to another region", self.r("greenhouse:1006").reasons[0])
        self.assertIn("says no sponsorship", self.r("greenhouse:1008").reasons[0])
        self.assertEqual(self.r("lever:bbbb-2222").tier, "rejected")      # Product Designer
        self.assertEqual(self.r("ashby:c2").tier, "rejected")             # NYC + Remote (US)

    def test_remote_india_new_grad_is_accepted(self):
        r = self.r("greenhouse:1005")
        self.assertTrue(r.accepted)
        self.assertEqual((r.track, r.location_class), ("new_grad", "remote_open"))

    def test_sponsored_relocation_kept_with_apply_action(self):
        r = self.r("greenhouse:1007")
        self.assertTrue(r.accepted, r.reasons)
        self.assertEqual((r.location_class, r.sponsorship, r.action), ("abroad_onsite", "positive", "apply"))

    def test_lever_intern(self):
        r = self.r("lever:aaaa-1111")
        self.assertEqual((r.track, r.location_class), ("internship", "preferred_india"))
        self.assertTrue(r.accepted, r.reasons)

    def test_ashby_worldwide_remote(self):
        r = self.r("ashby:c1")
        self.assertTrue(r.accepted, r.reasons)
        self.assertEqual(r.location_class, "remote_open")

    def test_hn(self):
        lumen = self.r("hn:50001")
        self.assertTrue(lumen.accepted, lumen.reasons)
        self.assertEqual((lumen.action, lumen.track), ("email", "internship"))
        self.assertEqual(self.r("hn:50002").tier, "rejected")   # all roles senior
        self.assertEqual(self.r("hn:50004").tier, "rejected")   # remote US only

    def test_scores_bounded(self):
        for r in self.results.values():
            self.assertGreaterEqual(r.score, 0)
            self.assertLessEqual(r.score, 100)


class ProfileKnobTests(unittest.TestCase):
    def test_turning_off_a_track_rejects_it(self):
        profile = example_profile()
        profile.tracks["internship"] = False
        r = score_job(job("Backend Engineer Intern", "Python FastAPI AWS"), profile)
        self.assertEqual(r.tier, "rejected")
        self.assertIn("turned off", r.reasons[0])

    def test_stretch_family_weighted_down(self):
        profile = example_profile()
        desc = "0-1 years experience. Python, AWS, SQL, PostgreSQL, REST APIs, microservices, backend, Lambda"
        backend = score_job(job("Backend Engineer - New Grad", desc), profile)
        data = score_job(job("Data Engineer - New Grad", desc), profile)
        self.assertEqual(data.family, "data")
        self.assertLess(data.score, backend.score)
        self.assertNotEqual(data.tier, "A")

    def test_negative_skills_pull_score_down(self):
        profile = example_profile()
        base = "New grad. Python, AWS, REST APIs, PostgreSQL"
        plain = score_job(job("Backend Engineer", base), profile)
        golang = score_job(job("Backend Engineer", base + ", Golang, Spring Boot, Kotlin"), profile)
        self.assertLess(golang.score, plain.score)


if __name__ == "__main__":
    unittest.main()
