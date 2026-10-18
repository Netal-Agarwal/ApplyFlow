import unittest

from applyflow.collectors import AshbyCollector, GreenhouseCollector, HNWhoIsHiringCollector, LeverCollector
from applyflow.collectors.hn import parse_header
from applyflow.models import Company
from tests.helpers import FakeClient, default_routes, fixture


class GreenhouseTests(unittest.TestCase):
    def test_parse(self):
        jobs = GreenhouseCollector.parse(fixture("greenhouse_acme.json"), Company("Acme", "greenhouse", "acme"))
        self.assertEqual(len(jobs), 8)
        intern = jobs[0]
        self.assertEqual(intern.id, "greenhouse:1001")
        self.assertEqual(intern.company, "Acme")          # seed name beats company_name
        self.assertEqual(intern.locations, ["Bengaluru"])
        self.assertIn("FastAPI", intern.description)
        self.assertNotIn("&lt;", intern.description)
        self.assertEqual(intern.board_size, 8)
        self.assertTrue(jobs[4].remote)            # "Remote - India"

    def test_fetch_requests_content(self):
        client = FakeClient(default_routes())
        GreenhouseCollector(client).fetch(Company("Acme", "greenhouse", "acme"))
        self.assertEqual(client.calls[0][1], {"content": "true"})


class LeverTests(unittest.TestCase):
    def test_parse(self):
        jobs = LeverCollector.parse(fixture("lever_zeta.json"), Company("Zeta", "lever", "zeta"))
        self.assertEqual(len(jobs), 2)
        job = jobs[0]
        self.assertEqual(job.id, "lever:aaaa-1111")
        self.assertEqual(job.employment_type, "Internship")
        self.assertEqual(job.locations, ["bengaluru"])
        self.assertFalse(job.remote)
        self.assertIn("Node.js", job.description)       # pulled from lists[].content
        self.assertEqual(job.emails, ["campus@zeta-example.com"])
        self.assertTrue(job.posted_at.startswith("2026-"))


class AshbyTests(unittest.TestCase):
    def test_parse_skips_unlisted_and_strips_titles(self):
        jobs = AshbyCollector.parse(fixture("ashby_nimbus.json"), Company("Nimbus", "ashby", "nimbus"))
        self.assertEqual([j.external_id for j in jobs], ["c1", "c2"])
        self.assertEqual(jobs[0].title, "Founding Backend Engineer")
        self.assertTrue(jobs[0].remote)
        self.assertEqual(jobs[1].locations, ["New York, NY (HQ)", "Remote (US)"])


class HNTests(unittest.TestCase):
    def test_header(self):
        h = parse_header("Lumen AI | Backend Engineer, Interns | Remote (India OK) | Full-time | https://lumen.ai")
        self.assertEqual(h["company"], "Lumen AI")
        self.assertEqual(h["roles"], ["Backend Engineer, Interns"])
        self.assertEqual(h["locations"], ["Remote (India OK)"])
        self.assertEqual(parse_header("Just a reply without pipes"), {})

    def test_fetch_latest_thread(self):
        jobs = HNWhoIsHiringCollector(FakeClient(default_routes())).fetch()
        # 4 hits: 3 top-level posts + 1 reply that must be skipped
        self.assertEqual([j.external_id for j in jobs], ["50001", "50002", "50004"])
        lumen = jobs[0]
        self.assertEqual(lumen.company, "Lumen AI")
        self.assertEqual(lumen.emails, ["jobs@lumen.ai"])
        self.assertEqual(lumen.url, "https://news.ycombinator.com/item?id=50001")
        self.assertTrue(lumen.remote)

    def test_picks_who_is_hiring_not_who_wants_to_be_hired(self):
        story = HNWhoIsHiringCollector(FakeClient(default_routes())).latest_story()
        self.assertEqual(story["objectID"], "49922569")


if __name__ == "__main__":
    unittest.main()
