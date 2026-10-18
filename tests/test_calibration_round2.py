"""Regression tests for calibration round 2 (CLAUDE.md), found on the 2026-10-18 live run."""

import unittest

from applyflow.collectors import GreenhouseCollector, HNWhoIsHiringCollector, LeverCollector
from applyflow.models import Company, Job
from applyflow.scoring import classify_location, score_job, sponsorship_signal
from tests.helpers import TODAY, example_profile, fixture


def greenhouse_round2():
    jobs = GreenhouseCollector.parse(fixture("greenhouse_round2.json"), Company("Round2", "greenhouse", "round2"))
    return {j.external_id: j for j in jobs}


def hn_round2():
    jobs = HNWhoIsHiringCollector.parse(fixture("hn_round2.json")["hits"], "49922569")
    return {j.external_id: j for j in jobs}


class SecurityTitleTests(unittest.TestCase):
    """1. Security roles aren't a target; FamPay's "Security Engineer Intern" topped tier A."""

    def test_security_intern_rejected(self):
        r = score_job(greenhouse_round2()["3001"], example_profile(), TODAY)
        self.assertEqual(r.tier, "rejected")
        self.assertEqual(r.reasons[0], "title contains excluded term 'security engineer'")

    def test_other_security_titles(self):
        profile = example_profile()
        job = greenhouse_round2()["3001"]
        for title in ("Application Security Intern", "AppSec Engineer", "InfoSec Analyst Intern",
                      "Penetration Tester - Graduate", "Security Analyst, New Grad"):
            job.title = title
            self.assertEqual(score_job(job, profile, TODAY).tier, "rejected", title)


class RemoteForeignPlaceTests(unittest.TestCase):
    """2. "Remote - Ireland" means remote from Ireland, not from India."""

    def setUp(self):
        self.profile = example_profile()

    def loc(self, location):
        return classify_location(Job("greenhouse", "1", "X", "Backend Engineer", "u", locations=[location]),
                                 self.profile)

    def test_restricted(self):
        for location in ("Remote - Ireland", "Remote, San Francisco, CA", "Remote (UT or ID residents only)",
                         "Remote - residents of Ontario"):
            self.assertEqual(self.loc(location), "remote_restricted", location)

    def test_still_open(self):
        for location in ("Remote (Global)", "Remote, Global", "Full Time, Remote India", "Remote (Worldwide)",
                         "Remote - Pune", "REMOTE (anywhere, EST hours)"):
            self.assertEqual(self.loc(location), "remote_open", location)

    def test_still_unclear(self):
        for location in ("Remote", "Remote-first", "Remote, focus on async", "Remote (EST hours)"):
            self.assertEqual(self.loc(location), "remote_unclear", location)

    def test_fixture_jobs_rejected(self):
        jobs = greenhouse_round2()
        for job_id in ("3002", "3003"):           # Twilio "Remote - Ireland", Supabase "Remote, San Francisco, CA"
            r = score_job(jobs[job_id], self.profile, TODAY)
            self.assertEqual(r.tier, "rejected")
            self.assertIn("remote but restricted to another region", r.reasons[0])

    def test_hn(self):
        jobs = hn_round2()
        self.assertEqual(classify_location(jobs["60004"], self.profile), "remote_restricted")   # residents only
        self.assertEqual(classify_location(jobs["60005"], self.profile), "remote_restricted")   # Remote, San Francisco
        self.assertEqual(classify_location(jobs["60006"], self.profile), "remote_unclear")      # Remote (EST hours)
        self.assertEqual(classify_location(jobs["60003"], self.profile), "remote_open")         # company name isn't a place



class RelocationOnlyTests(unittest.TestCase):
    """3. 'Relocation assistance' in a benefits list isn't visa sponsorship for an onsite role abroad."""

    def setUp(self):
        self.profile = example_profile()
        jobs = LeverCollector.parse(fixture("lever_palantir.json"), Company("Palantir", "lever", "palantir"))
        self.jobs = {j.external_id: j for j in jobs}

    def test_palantir_chicago_rejected(self):
        r = score_job(self.jobs["com-2"], self.profile, TODAY)
        self.assertEqual(r.tier, "rejected")
        self.assertEqual(r.reasons[0], "onsite abroad (Chicago, IL) and no sponsorship signal "
                                       "('Relocation assistance' without a visa isn't sponsorship)")

    def test_visa_statement_still_kept(self):
        self.assertTrue(score_job(self.jobs["com-1"], self.profile, TODAY).accepted)   # "We sponsor work visas"

    def test_odoo_visa_offer_kept(self):
        # 7. Odoo's "Why join us: Visa sponsorship + relocation support" is a genuine offer.
        r = score_job(greenhouse_round2()["3006"], self.profile, TODAY)
        self.assertEqual((r.location_class, r.sponsorship), ("abroad_onsite", "positive"))
        self.assertTrue(r.accepted, r.reasons)
        self.assertIn("posting signals visa sponsorship ('Visa sponsorship + relocation support')", r.reasons)

    def test_signals(self):
        self.assertEqual(sponsorship_signal("Relocation assistance available.")[0], "unknown")
        for text in ("We offer visa sponsorship and relocation support.", "Relocation support including visas.",
                     "Relocation package and work permit support.", "Immigration support is provided."):
            self.assertEqual(sponsorship_signal(text)[0], "positive", text)



class FresherSkillFloorTests(unittest.TestCase):
    """4. A thin intern JD (Rubrik "Software Engineer (CPD) - Winter Intern") still reaches tier A."""

    def setUp(self):
        self.profile = example_profile()
        self.job = greenhouse_round2()["3004"]
        self.job.board_size = 200          # Rubrik's board is big: no startup bonus

    def test_floor_lifts_thin_intern_jd_to_a(self):
        r = score_job(self.job, self.profile, TODAY)
        self.assertEqual(r.tier, "A", r.reasons)
        self.assertIn("skill floor: thin internship JD, primary family in a preferred city -> 25/50", r.reasons)

    def test_floor_is_configurable(self):
        self.profile.fresher_skill_floor = 0
        r = score_job(self.job, self.profile, TODAY)
        self.assertNotEqual(r.tier, "A")
        self.assertFalse(any(x.startswith("skill floor") for x in r.reasons))

    def test_floor_only_for_preferred_city_fresher_primary_roles(self):
        for title, location in (("Software Engineer (CPD) - Winter Intern", "Pune"),     # other India city
                                ("Software Engineer", "Bangalore"),                       # unspecified track
                                ("Data Engineer Intern", "Bangalore")):                   # stretch family
            self.job.title, self.job.locations = title, [location]
            r = score_job(self.job, self.profile, TODAY)
            self.assertFalse(any(x.startswith("skill floor") for x in r.reasons), (title, location))

    def test_strong_jd_unaffected(self):
        self.job.description = "Python, FastAPI, AWS, Lambda, PostgreSQL, REST APIs, microservices, backend."
        r = score_job(self.job, self.profile, TODAY)
        self.assertFalse(any(x.startswith("skill floor") for x in r.reasons))



class DevRelTitleTests(unittest.TestCase):
    """5. Developer relations / advocacy roles are excluded."""

    def test_excluded(self):
        profile = example_profile()
        job = greenhouse_round2()["3005"]                      # "Developer Relations Engineer", Bengaluru
        r = score_job(job, profile, TODAY)
        self.assertEqual(r.reasons[0], "title contains excluded term 'developer relations'")
        for title in ("DevRel Engineer", "Developer Advocate, New Grad", "Technical Evangelist Intern"):
            job.title = title
            self.assertEqual(score_job(job, profile, TODAY).tier, "rejected", title)



class VagueHNPostTests(unittest.TestCase):
    """6. HN headers that name no specific engineering role are skipped with a reason."""

    @classmethod
    def setUpClass(cls):
        profile = example_profile()
        cls.results = {jid: score_job(j, profile, TODAY) for jid, j in hn_round2().items()}

    def test_multiple_roles_skipped(self):
        r = self.results["60001"]          # "Multiple engineering + go-to-market roles"
        self.assertEqual(r.tier, "skip")
        self.assertFalse(r.accepted)
        self.assertIn("vague HN post: header roles 'Multiple engineering + go-to-market roles' name no "
                      "specific engineering role -> skip (read the post yourself)", r.reasons)

    def test_see_post_skipped(self):
        r = self.results["60002"]          # header without a role segment -> "(see post)"
        self.assertEqual(r.tier, "skip")
        self.assertIn("vague HN post", r.reasons[-1])

    def test_specific_roles_scored_normally(self):
        self.assertTrue(self.results["60003"].accepted, self.results["60003"].reasons)   # "Founding Engineer"
        self.assertFalse(any("vague" in x for x in self.results["60006"].reasons))       # "Backend Engineer"


if __name__ == "__main__":
    unittest.main()
