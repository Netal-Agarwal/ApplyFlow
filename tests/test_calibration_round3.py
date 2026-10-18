"""Regression tests for calibration round 3 (CLAUDE.md): trustworthy, India-focused discovery."""

import csv
import tempfile
import unittest
from pathlib import Path

from applyflow.discovery import (discover_yc, name_key, site_domain, verify_board, verify_saved_boards,
                                 yc_candidates)
from applyflow.models import DiscoveredCompany
from applyflow.pipeline import boards_to_collect, collect, score_all
from applyflow.report import write_companies_csv
from applyflow.store import Store
from tests.helpers import FIXTURES, TODAY, FakeClient, example_profile, fixture

LEVER = "api.lever.co/v0/postings/"
ASHBY = "api.ashbyhq.com/posting-api/job-board/"


def page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def yc(name, website, locations="Bengaluru, KA, India", team_size=20, slug=None):
    """One company in the yc-oss hiring.json format."""
    return {"id": abs(hash(name)) % 10_000, "name": name, "slug": slug or name.lower().replace(" ", "-"),
            "website": website, "all_locations": locations, "regions": ["India"] if "India" in locations else [],
            "team_size": team_size, "isHiring": True, "status": "Active", "batch": "S24", "one_liner": "x"}


# Three real mismatches from the 2026-10-19 run, plus a guessed board that is genuinely the company's.
DATA = [
    yc("Bloom", "https://bloom.diy"),                  # lever:bloom is a Toronto PR agency
    yc("Asimov", "https://tryasimov.ai"),              # ashby:asimov is a biotech (asimov.com)
    yc("Sapling.ai", "https://sapling.ai"),            # lever:sapling: one posting titled "pr9jp"
    yc("Netomi", "https://netomi.com"),                # lever:netomi links back to netomi.com
]
ROUTES = {
    LEVER + "bloom": fixture("lever_bloom.json"),
    ASHBY + "asimov": fixture("ashby_asimov.json"),
    LEVER + "sapling": fixture("lever_sapling.json"),
    LEVER + "netomi": fixture("lever_netomi.json"),
}
PAGES = {
    "https://jobs.lever.co/bloom": page("lever_bloom_page.html"),
    "https://jobs.ashbyhq.com/asimov": page("ashby_asimov_page.html"),
    "https://jobs.lever.co/sapling": page("lever_sapling_page.html"),
    "https://jobs.lever.co/netomi": page("lever_netomi_page.html"),
}


class VerifyGuessedBoardTests(unittest.TestCase):
    """1. A board found by guessing a slug is only saved as found with evidence it's the same company."""

    def setUp(self):
        self.store = Store(":memory:")
        self.client = FakeClient(dict(ROUTES), pages=dict(PAGES))
        self.summary = discover_yc(self.store, self.client, example_profile(), limit=10, log=lambda _: None,
                                   data=DATA)
        self.rows = {r["name"]: r for r in self.store.conn.execute("SELECT * FROM companies")}

    def tearDown(self):
        self.store.close()

    def test_known_mismatches_are_unverified(self):
        expected = {
            "Bloom": ("lever", "bloom", "lever:bloom belongs to buildwithbloom.com, not bloom.diy"),
            "Asimov": ("ashby", "asimov", "ashby:asimov belongs to asimov.com, not tryasimov.ai"),
            "Sapling.ai": ("lever", "sapling", "no evidence lever:sapling is Sapling.ai's (no matching website or name)"),
        }
        for name, (ats, slug, reason) in expected.items():
            r = self.rows[name]
            self.assertEqual((r["board_status"], r["ats"], r["slug"], r["detected_from"], r["status_reason"]),
                             ("unverified", ats, slug, "slug_guess", reason), name)

    def test_matching_board_is_found(self):
        r = self.rows["Netomi"]
        self.assertEqual((r["board_status"], r["detected_from"], r["status_reason"]),
                         ("found", "slug_guess", "board links to netomi.com"))
        self.assertEqual([c.name for c in self.summary.found], ["Netomi"])
        self.assertEqual(sorted(c.name for c in self.summary.unverified), ["Asimov", "Bloom", "Sapling.ai"])

    def test_unverified_boards_are_not_collected(self):
        boards = self.store.discovered_boards(("greenhouse", "lever", "ashby", "workable"))
        self.assertEqual([b.slug for b in boards], ["netomi"])

    def test_unverified_companies_listed_for_email_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_companies_csv(self.store.companies_without_board(), Path(tmp) / "s.csv")
            with open(path, encoding="utf-8") as fh:
                rows = {r["name"]: r for r in csv.DictReader(fh)}
        self.assertEqual(sorted(rows), ["Asimov", "Bloom", "Sapling.ai"])
        self.assertEqual(rows["Bloom"]["board"], "unverified (lever:bloom)")
        self.assertEqual(rows["Bloom"]["reason"], "lever:bloom belongs to buildwithbloom.com, not bloom.diy")

    def test_board_linked_from_website_is_trusted(self):
        store = Store(":memory:")
        client = FakeClient(dict(ROUTES), pages={"https://bloom.diy": '<a href="https://jobs.lever.co/bloom">Jobs</a>'})
        discover_yc(store, client, example_profile(), limit=1, log=lambda _: None, data=DATA[:1])
        r = store.conn.execute("SELECT * FROM companies").fetchone()
        self.assertEqual((r["board_status"], r["detected_from"], r["status_reason"]),
                         ("found", "website", "linked from https://bloom.diy"))
        store.close()


class VerifySavedBoardsTests(unittest.TestCase):
    """2. `verify-boards` re-checks boards saved before verification existed and demotes mismatches."""

    def setUp(self):
        self.store = Store(":memory:")
        for name, website, ats, slug in (("Bloom", "https://bloom.diy", "lever", "bloom"),
                                         ("Netomi", "https://netomi.com", "lever", "netomi"),
                                         ("Tiny Labs", "https://tinylabs.example", "workable", "tinylabs")):
            # Legacy rows: saved as found with no record of how the board was detected.
            self.store.upsert_discovered(DiscoveredCompany(key=f"yc:{slug}", name=name, website=website,
                                                           team_size=20, ats=ats, slug=slug, board_status="found"))
        self.store.conn.execute("UPDATE companies SET detected_from = NULL WHERE key = 'yc:tinylabs'")
        routes = dict(ROUTES)
        routes["apply.workable.com/api/v1/widget/accounts/tinylabs"] = fixture("workable_tiny.json")
        pages = dict(PAGES)
        pages["https://tinylabs.example"] = '<a href="https://apply.workable.com/tinylabs/">Careers</a>'
        self.client = FakeClient(routes, pages=pages)
        collect(self.store, self.client, boards_to_collect(self.store, []), include_hn=False, log=lambda _: None)
        score_all(self.store, example_profile(), TODAY)

    def tearDown(self):
        self.store.close()

    def test_mismatch_demoted_and_hidden(self):
        self.assertIn("lever:bloom", {r["board_key"] for r in self.store.conn.execute("SELECT board_key FROM jobs "
                                                                                      "WHERE active = 1")})
        demoted, promoted = verify_saved_boards(self.store, self.client, log=lambda _: None)
        self.assertEqual(promoted, [])
        self.assertEqual([(c.name, c.status_reason) for c in demoted],
                         [("Bloom", "lever:bloom belongs to buildwithbloom.com, not bloom.diy")])
        rows = {r["name"]: r for r in self.store.conn.execute("SELECT * FROM companies")}
        self.assertEqual(rows["Bloom"]["board_status"], "unverified")
        self.assertEqual((rows["Netomi"]["board_status"], rows["Netomi"]["status_reason"]),
                         ("found", "board links to netomi.com"))
        self.assertEqual((rows["Tiny Labs"]["board_status"], rows["Tiny Labs"]["detected_from"]),
                         ("found", "website"))

        active = {r[0] for r in self.store.conn.execute("SELECT board_key FROM jobs WHERE active = 1")}
        self.assertNotIn("lever:bloom", active)
        self.assertEqual([b.slug for b in self.store.discovered_boards(("lever", "workable"))], ["netomi", "tinylabs"])
        score_all(self.store, example_profile(), TODAY)
        self.assertFalse(any(r["board_key"] == "lever:bloom" for r in self.store.ranked(("A", "B", "skip"))))

    def test_unverified_board_promoted_when_it_passes(self):
        verify_saved_boards(self.store, self.client, log=lambda _: None)
        self.store.upsert_discovered(DiscoveredCompany(
            key="yc:fampay", name="FamPay", website="https://fampay.in", team_size=24, ats="lever", slug="fampay",
            board_status="unverified", detected_from="slug_guess", status_reason="lever:fampay belongs to famapp.in"))
        self.client.routes[LEVER + "fampay"] = fixture("lever_fampay.json")
        self.client.pages["https://jobs.lever.co/fampay"] = page("lever_fampay_page.html")
        demoted, promoted = verify_saved_boards(self.store, self.client, log=lambda _: None)
        self.assertEqual((demoted, [c.name for c in promoted]), ([], ["FamPay"]))
        self.assertIn("lever:fampay", [f"{b.ats}:{b.slug}" for b in self.store.discovered_boards(("lever",))])

    def test_cli_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            Store(db).close()
            from applyflow.cli import build_parser
            args = build_parser().parse_args(["--db", str(db), "verify-boards"])
            self.assertEqual(args.func.__name__, "cmd_verify_boards")


class IndiaFocusTests(unittest.TestCase):
    """3. Discovery checks India-located companies, 10-300 people first; "Remote" alone doesn't qualify."""

    DATA = [
        yc("Remote US", "https://r.example", locations="San Francisco, CA, USA", team_size=40),
        yc("Tiny India", "https://t.example", team_size=3),
        yc("Mid India", "https://m.example", locations="Gurugram, HR, India", team_size=120),
        yc("Big India", "https://b.example", locations="Pune, India", team_size=800),
        yc("Small India", "https://s.example", locations="Hyderabad, TS, India", team_size=12),
        yc("Unknown India", "https://u.example", team_size=None),
        yc("Blank Location", "https://bl.example", locations="", team_size=30),
    ]

    def setUp(self):
        self.DATA[0]["regions"] = ["United States of America", "Remote", "Fully Remote"]
        self.DATA[-1]["regions"] = ["India"]          # no all_locations: fall back to YC's regions

    def test_india_only_and_ordered(self):
        names = [c.name for c in yc_candidates(self.DATA, example_profile())]
        self.assertEqual(names, ["Small India", "Blank Location", "Mid India", "Tiny India", "Big India",
                                 "Unknown India"])

    def test_configurable(self):
        profile = example_profile()
        profile.discovery["yc"] = {**profile.discovery["yc"], "regions_any": ["India", "Remote"],
                                   "preferred_team_size": [1, 50]}
        names = [c.name for c in yc_candidates(self.DATA, profile)]
        self.assertIn("Remote US", names)                              # opt back in to "Remote"
        self.assertEqual(names[:3], ["Tiny India", "Small India", "Blank Location"])
        self.assertEqual(names[-1], "Remote US")                       # India-located still first


class SolutionEngineerTests(unittest.TestCase):
    """4. "Solution engineer" (singular) is excluded like the plural."""

    def test_netomi_agentic_solution_engineer_rejected(self):
        from applyflow.collectors import LeverCollector
        from applyflow.models import Company
        from applyflow.scoring import score_job
        job = LeverCollector.parse(fixture("lever_netomi.json"), Company("Netomi", "lever", "netomi"))[0]
        job.title = "Agentic Solution Engineer"
        r = score_job(job, example_profile(), TODAY)
        self.assertEqual(r.tier, "rejected")
        self.assertEqual(r.reasons[0], "title contains excluded term 'solution engineer'")


class EvidenceTests(unittest.TestCase):
    def company(self, name, website):
        return DiscoveredCompany(key="yc:x", name=name, website=website)

    def test_ats_company_name_counts_when_board_names_no_website(self):
        client = FakeClient({"boards-api.greenhouse.io/v1/boards/acme": fixture("greenhouse_board_acme.json")})
        ok, reason = verify_board(client, self.company("Acme Labs", "https://acme.example"), "greenhouse", "acme", [])
        self.assertEqual((ok, reason), (True, "board name 'Acme' matches"))

    def test_postings_mentioning_the_domain_count(self):
        from applyflow.models import Job
        jobs = [Job("lever", "1", "", "Engineer", "u", description="Apply at careers.tinylabs.example")]
        ok, reason = verify_board(FakeClient({}), self.company("Tiny Labs", "https://tinylabs.example"), "lever",
                                  "tiny", jobs)
        self.assertEqual((ok, reason), (True, "postings mention tinylabs.example"))

    def test_renamed_company_is_verified(self):
        # lever:fampay links to famapp.in, but its postings say "Fam (previously FamPay)".
        client = FakeClient({LEVER + "fampay": fixture("lever_fampay.json")},
                            pages={"https://jobs.lever.co/fampay": page("lever_fampay_page.html")})
        from applyflow.collectors import LeverCollector
        from applyflow.models import Company
        jobs = LeverCollector.parse(fixture("lever_fampay.json"), Company("", "lever", "fampay"))
        ok, reason = verify_board(client, self.company("FamPay", "https://fampay.in"), "lever", "fampay", jobs)
        self.assertEqual((ok, reason), (True, "board links to famapp.in; postings say 'previously FamPay' (renamed)"))

    def test_helpers(self):
        self.assertEqual(site_domain("https://www.Acme.ai/about"), "acme.ai")
        self.assertEqual(site_domain("brumby"), "")
        self.assertEqual(name_key("Weekday AI"), name_key("Weekday"))
        self.assertEqual(name_key("Brumby (Formerly GrazeMate)"), "brumby")
        self.assertNotEqual(name_key("Sapling - Partner"), name_key("Sapling.ai"))


if __name__ == "__main__":
    unittest.main()
