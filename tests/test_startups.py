"""Startup-first features: Workable, board detection, YC discovery, startup scoring."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from applyflow.collectors import WorkableCollector
from applyflow.detect import careers_links, detect_board, find_board_in_html, normalize_website
from applyflow.discovery import discover_yc, slug_guesses, yc_candidates
from applyflow.http import FetchError, PoliteClient
from applyflow.models import Company, DiscoveredCompany, Job
from applyflow.pipeline import boards_to_collect, collect, score_all
from applyflow.scoring import contact_hint, score_job
from applyflow.store import Store
from tests.helpers import TODAY, FakeClient, example_profile, fixture

TINY_WORKABLE = "apply.workable.com/api/v1/widget/accounts/tinylabs"


class WorkableTests(unittest.TestCase):
    def test_parse(self):
        jobs = WorkableCollector.parse(fixture("workable_tiny.json"), Company("Tiny", "workable", "tinylabs", team_size=12))
        self.assertEqual(len(jobs), 2)
        fresher = jobs[0]
        self.assertEqual(fresher.id, "workable:AB12CD34EF")
        self.assertEqual(fresher.company, "Tiny Labs")
        self.assertEqual(fresher.locations, ["Bengaluru, Karnataka, India"])
        self.assertIn("FastAPI", fresher.description)
        self.assertIn("Entry level", fresher.description)
        self.assertEqual(fresher.team_size, 12)
        self.assertEqual(jobs[1].locations, ["Remote (Paris, Île-de-France, France)"])
        self.assertTrue(jobs[1].remote)


class DetectTests(unittest.TestCase):
    def test_supported_boards(self):
        cases = {
            '<a href="https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited">Jobs</a>':
                ("greenhouse", "razorpaysoftwareprivatelimited"),
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>': ("greenhouse", "acme"),
            '<a href="https://jobs.lever.co/cred/abc-123">Apply</a>': ("lever", "cred"),
            '<a href="https://jobs.ashbyhq.com/sarvam">Careers</a>': ("ashby", "sarvam"),
            '<a href="https://apply.workable.com/tinylabs/">Open roles</a>': ("workable", "tinylabs"),
        }
        for html, expected in cases.items():
            match = find_board_in_html(html)
            self.assertEqual((match.ats, match.slug, match.supported), (*expected, True), html)

    def test_ignores_non_slugs(self):
        self.assertIsNone(find_board_in_html('<a href="https://apply.workable.com/j/AB12">job</a>'))

    def test_unsupported_board(self):
        match = find_board_in_html('<a href="https://acme.keka.com/careers">Careers</a>')
        self.assertEqual((match.ats, match.supported), ("keka", False))

    def test_supported_beats_unsupported(self):
        html = '<a href="https://acme.keka.com/careers">x</a><a href="https://jobs.lever.co/acme">y</a>'
        self.assertEqual(find_board_in_html(html).ats, "lever")

    def test_careers_links_same_site_only(self):
        html = ('<a href="/careers">Careers</a><a href="https://careers.acme.io/">x</a>'
                '<a href="https://twitter.com/jobs">no</a><a href="/blog">no</a>')
        self.assertEqual(careers_links(html, "https://acme.io"), ["https://acme.io/careers", "https://careers.acme.io/"])

    def test_normalize_website(self):
        self.assertEqual(normalize_website("acme.io/about"), "https://acme.io")
        self.assertEqual(normalize_website(""), "")

    def test_detect_follows_careers_link(self):
        client = FakeClient({}, pages={
            "https://acme.io": '<a href="/join-us">Join us</a>',
            "https://acme.io/join-us": '<a href="https://jobs.ashbyhq.com/acme">See roles</a>',
        })
        match = detect_board(client, "https://acme.io")
        self.assertEqual((match.ats, match.slug, match.found_on), ("ashby", "acme", "https://acme.io/join-us"))

    def test_detect_falls_back_to_conventional_paths(self):
        client = FakeClient({}, pages={"https://acme.io/careers": '<a href="https://jobs.lever.co/acme">x</a>'})
        self.assertEqual(detect_board(client, "acme.io").ats, "lever")
        self.assertLessEqual(len(client.page_calls), 4)

    def test_detect_returns_unsupported_when_nothing_better(self):
        client = FakeClient({}, pages={"https://acme.io": '<a href="https://acme.keka.com/careers">Careers</a>'})
        self.assertEqual(detect_board(client, "https://acme.io").ats, "keka")


class RobotsTests(unittest.TestCase):
    class Resp:
        def __init__(self, status, text=""):
            self.status_code, self.text, self.headers = status, text, {}

    class Session:
        def __init__(self, robots_status, robots_text=""):
            self.headers = {}
            self.robots = (robots_status, robots_text)
            self.urls = []

        def get(self, url, params=None, timeout=None, headers=None):
            self.urls.append(url)
            if url.endswith("/robots.txt"):
                return RobotsTests.Resp(*self.robots)
            return RobotsTests.Resp(200, "<html>ok</html>")

    def client(self, status, text=""):
        return PoliteClient("ApplyFlow-test", session=self.Session(status, text), sleep=lambda s: None, min_interval=0)

    def test_disallowed_page_is_not_fetched(self):
        c = self.client(200, "User-agent: *\nDisallow: /careers")
        with self.assertRaises(FetchError):
            c.get_page("https://acme.io/careers")
        self.assertNotIn("https://acme.io/careers", c.session.urls)
        self.assertEqual(c.get_page("https://acme.io/"), "<html>ok</html>")

    def test_missing_robots_allows(self):
        self.assertEqual(self.client(404).get_page("https://acme.io/careers"), "<html>ok</html>")

    def test_robots_fetched_once_per_site(self):
        c = self.client(404)
        c.get_page("https://acme.io/a")
        c.get_page("https://acme.io/b")
        self.assertEqual(sum(u.endswith("robots.txt") for u in c.session.urls), 1)


class YCDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.profile = example_profile()
        self.data = fixture("yc_hiring.json")

    def test_filters_and_order(self):
        names = [c.name for c in yc_candidates(self.data, self.profile)]
        # India first (smallest team first, unknown size last), then remote companies.
        self.assertEqual(names, ["Tiny Labs", "Keka Co", "Mystery", "Remote Rocket"])

    def test_slug_guesses(self):
        self.assertEqual(slug_guesses("Tiny Labs", "https://www.tinylabs.example"), ["tinylabs", "tiny-labs"])

    def test_discover_end_to_end(self):
        client = FakeClient(
            {TINY_WORKABLE: fixture("workable_tiny.json")},
            pages={
                "https://tinylabs.example": '<a href="https://apply.workable.com/tinylabs/">Careers</a>',
                "https://kekaco.example": '<a href="https://kekaco.keka.com/careers">Careers</a>',
            },
        )
        store = Store(":memory:")
        logs = []
        summary = discover_yc(store, client, self.profile, limit=10, log=logs.append, data=self.data)
        self.assertEqual(summary.candidates, 4)
        self.assertEqual([c.name for c in summary.found], ["Tiny Labs"])
        self.assertEqual((summary.unsupported, summary.none), (1, 2))

        boards = store.discovered_boards(("greenhouse", "lever", "ashby", "workable"))
        self.assertEqual([(b.ats, b.slug, b.team_size) for b in boards], [("workable", "tinylabs", 12)])
        no_board = [r["name"] for r in store.companies_without_board()]
        self.assertEqual(no_board[0], "Keka Co")                 # India and smallest first

        # A second run skips everything checked recently.
        again = discover_yc(store, client, self.profile, limit=10, log=lambda _: None, data=self.data)
        self.assertEqual((again.checked, again.skipped_recent), (0, 4))
        store.close()

    def test_limit(self):
        store = Store(":memory:")
        summary = discover_yc(store, FakeClient({}), self.profile, limit=2, log=lambda _: None, data=self.data)
        self.assertEqual(summary.checked, 2)
        store.close()


class StartupScoringTests(unittest.TestCase):
    def setUp(self):
        self.profile = example_profile()

    def job(self, **kw):
        base = dict(source="workable", external_id="1", company="X", title="Backend Developer - Fresher",
                    url="u", locations=["Bengaluru"],
                    description="New grad. Python, FastAPI, AWS, PostgreSQL, REST APIs")
        base.update(kw)
        return Job(**base)

    def test_startup_bonus(self):
        desc = "New grad. Python, AWS"      # few skills, so neither score hits the 100 cap
        small = score_job(self.job(team_size=20, description=desc), self.profile)
        big = score_job(self.job(team_size=5000, description=desc), self.profile)
        self.assertEqual(small.score - big.score, self.profile.startup["score_bonus"])
        self.assertTrue(any("startup (20 people)" in r for r in small.reasons))

    def test_small_board_counts_as_startup_when_size_unknown(self):
        r = score_job(self.job(board_size=4), self.profile)
        self.assertTrue(any("startup (4 open roles)" in x for x in r.reasons))

    def test_contact_hints(self):
        self.assertEqual(contact_hint(self.job(team_size=20), self.profile), "founder or CTO")
        self.assertEqual(contact_hint(self.job(team_size=150), self.profile), "engineering manager or tech lead")
        self.assertEqual(contact_hint(self.job(team_size=5000), self.profile), "recruiter or engineering manager")
        self.assertEqual(contact_hint(self.job(board_size=3), self.profile), "founder or CTO")
        hn = self.job(source="hn", emails=["a@b.co"])
        self.assertEqual(contact_hint(hn, self.profile), "the email in the post")


class StartupPipelineTests(unittest.TestCase):
    def test_discovered_boards_are_collected_and_scored(self):
        store = Store(":memory:")
        store.upsert_discovered(DiscoveredCompany(
            key="yc:tiny-labs", name="Tiny Labs", website="https://tinylabs.example", team_size=12,
            ats="workable", slug="tinylabs", board_status="found"))
        seeds = [Company("Tiny dup", "workable", "tinylabs")]       # same board as discovered: no duplicate
        self.assertEqual(len(boards_to_collect(store, seeds)), 1)
        self.assertEqual(len(boards_to_collect(store, [])), 1)

        client = FakeClient({TINY_WORKABLE: fixture("workable_tiny.json")})
        collect(store, client, boards_to_collect(store, []), include_hn=False, log=lambda _: None)
        score_all(store, example_profile(), TODAY)
        top = store.ranked()[0]
        self.assertEqual((top["title"], top["team_size"], top["contact_hint"]),
                         ("Backend Developer - Fresher", 12, "founder or CTO"))
        store.close()


class MigrationTests(unittest.TestCase):
    def test_old_database_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                  company TEXT NOT NULL, board_key TEXT, title TEXT NOT NULL, url TEXT, apply_url TEXT,
                  description TEXT, locations TEXT, remote INTEGER, employment_type TEXT, department TEXT,
                  posted_at TEXT, emails TEXT, board_size INTEGER, first_seen TEXT NOT NULL,
                  last_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE scores (job_id TEXT PRIMARY KEY, accepted INTEGER NOT NULL, score INTEGER NOT NULL,
                  tier TEXT NOT NULL, family TEXT, track TEXT, location_class TEXT, experience_years INTEGER,
                  sponsorship TEXT, action TEXT, lead_project TEXT, matched_skills TEXT, reasons TEXT,
                  scored_at TEXT NOT NULL);
            """)
            conn.close()
            store = Store(path)
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(jobs)")}
            self.assertIn("team_size", cols)
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(scores)")}
            self.assertIn("contact_hint", cols)
            store.close()


if __name__ == "__main__":
    unittest.main()
