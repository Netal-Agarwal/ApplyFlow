"""Phase 2e: refill the outreach pool with safe contacts.

HN "Who is hiring?" archive (location, role, filled-role skip, dedupe), YC job pages (parser and role
filter), Entrackr deal extraction and domain resolution, import-targets (parser and role match), the
deeper website search, and info@ / contact@ as unsendable (plus the hello@ daily cap and the
one-company-once guard).
"""

from __future__ import annotations

import json
import unittest
from datetime import date

from applyflow.contacts import Contact, ContactResult, DoNotContact, Target, find_contacts, read_site, GitHubBudget
from applyflow.http import FetchError
from applyflow.models import Job
from applyflow.outreach import build_items, contact_kind, make_queue, mark_sent, pick_batch
from applyflow.refill import (TARGETS_FILE_HEADER, Existing, best_header_role, board_role_match, careers_page_roles,
                              collect_hn_archive, collect_yc_jobs, domain_candidates, entrackr_report_urls,
                              existing_targets, hn_archive_decision, homepage_matches, import_targets, is_filled,
                              parse_entrackr_deals, parse_targets_file, parse_yc_jobs_page, report_text,
                              resolve_domain, safe_summary, yc_locations, yc_posting_job, yc_role_decision)
from applyflow.contacts import deep_pages
from applyflow.store import Store
from tests.helpers import FIXTURES, FakeClient, TODAY, example_profile
from tests.test_drafting import add_company, contact

PROFILE = example_profile()
REFILL = FIXTURES / "refill"


def text(name: str) -> str:
    return (REFILL / name).read_text(encoding="utf-8")


def hn_client() -> FakeClient:
    return FakeClient({"hn.algolia.com/api/v1/search_by_date": json.loads(text("hn_archive_stories.json")),
                       "hn.algolia.com/api/v1/search": json.loads(text("hn_archive_comments.json"))})


class TextClient(FakeClient):
    def get_text(self, url):
        return self.get_page(url)


class FakeMX:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}

    def check(self, domain):
        return self.statuses.get(domain, "ok")


# ---------------------------------------------------------------------------
# 1. HN archive
# ---------------------------------------------------------------------------

class HNArchiveFilterTests(unittest.TestCase):
    def job(self, header, body="", poster="x"):
        from applyflow.collectors.hn import HNWhoIsHiringCollector
        hit = {"objectID": "1", "parent_id": 7, "author": poster, "created_at": "2026-10-02T00:00:00Z",
               "comment_text": f"{header}<p>{body}"}
        return HNWhoIsHiringCollector.parse([hit], "7")[0]

    def test_india_post_with_email_is_kept(self):
        d = hn_archive_decision(self.job("Kestrel | Backend Engineer Intern | Bengaluru, India",
                                         "Python. Email jobs@kestrel.example"), PROFILE, TODAY)
        self.assertTrue(d.keep, d.reason)
        self.assertEqual(d.location_class, "preferred_india")
        self.assertEqual(d.job.emails, ["jobs@kestrel.example"])

    def test_remote_must_be_open_to_india_or_anywhere(self):
        ok = hn_archive_decision(self.job("Drift | Software Engineer (New Grad) | REMOTE (Worldwide)",
                                          "Email a@drift.example"), PROFILE, TODAY)
        us = hn_archive_decision(self.job("Bay | Backend Engineer | REMOTE (US only)", "Email a@bay.example"),
                                 PROFILE, TODAY)
        unclear = hn_archive_decision(self.job("Fog | Backend Engineer | Remote", "Email a@fog.example"),
                                      PROFILE, TODAY)
        self.assertTrue(ok.keep, ok.reason)
        self.assertFalse(us.keep)
        self.assertIn("restricted", us.reason)
        self.assertFalse(unclear.keep)
        self.assertIn("remote_unclear", unclear.reason)

    def test_role_must_pass_title_and_family_filters(self):
        senior = hn_archive_decision(self.job("Q | Senior Backend Engineer | Bengaluru, India", "Email a@q.example"),
                                     PROFILE, TODAY)
        sales = hn_archive_decision(self.job("S | Account Executive | Bengaluru, India", "Email a@s.example"),
                                    PROFILE, TODAY)
        self.assertFalse(senior.keep)
        self.assertFalse(sales.keep)
        self.assertIn("no header role passes", sales.reason)

    def test_header_role_picks_the_fitting_one(self):
        job = self.job("Mix | Senior SRE, Software Engineers, Backend Engineer (Rust, MySQL) | Remote (Global)")
        self.assertEqual(best_header_role(job, PROFILE), "Software Engineer")        # plural read as the role
        job = self.job("Art | Backend Engineer (Rust, MySQL) | Remote (Global)")
        self.assertEqual(best_header_role(job, PROFILE), "Backend Engineer (Rust, MySQL)")

    def test_filled_posts_are_skipped(self):
        for body in ("[FILLED] thanks all", "EDIT: filled, thank you", "This role has been filled.",
                     "We're no longer hiring for this position"):
            self.assertTrue(is_filled(body), body)
        self.assertFalse(is_filled("We filled our seed round last year and are hiring now."))
        d = hn_archive_decision(self.job("T | Backend Engineer Intern | Remote (Worldwide)", "[FILLED] a@t.example"),
                                PROFILE, TODAY)
        self.assertEqual((d.keep, d.reason), (False, "the post says the role is filled"))

    def test_needs_an_email_the_post_invites_applicants_to_use(self):
        d = hn_archive_decision(self.job("N | Backend Engineer Intern | Remote (India)", "Apply at https://n.example/jobs"),
                                PROFILE, TODAY)
        self.assertEqual((d.keep, d.reason), (False, "no email the post invites applicants to use"))
        d = hn_archive_decision(self.job("P | Backend Engineer Intern | Bengaluru, India",
                                         "For accommodations email privacy@p.example"), PROFILE, TODAY)
        self.assertFalse(d.keep)                                           # boilerplate address only

    def test_archive_dedupes_and_keeps_the_newest_post(self):
        store = Store(":memory:")
        add_company(store, "yc:nimbus", "Nimbus Labs", website="https://nimbus.example",
                    contacts=[contact("priya@nimbus.example", "verified", "Priya Sharma", "CTO")])
        store.add_event("yc-nimbus", "yc:nimbus", "priya@nimbus.example", "sent", "2026-10-20")
        dnc = DoNotContact(domains={"blocked.example"})
        stats = collect_hn_archive(store, hn_client(), PROFILE, date(2026, 9, 1), date(2026, 10, 31),
                                   existing_targets(store, dnc), today=TODAY)
        kept = {d.job.company: d for d in stats["decisions"] if not d.dedupe}
        self.assertEqual(set(kept), {"Kestrel Labs", "Driftwave"})
        self.assertEqual(kept["Kestrel Labs"].job.external_id, "9101")             # October beats September
        self.assertEqual(stats["skipped"]["already contacted"], 1)                 # Nimbus: sent already
        self.assertEqual(stats["skipped"]["already do_not_contact"], 1)
        self.assertEqual((stats["kept"], stats["new"]), (2, 2))
        self.assertEqual(stats["skipped"]["the post says the role is filled"], 1)
        matches = store.role_matches()
        self.assertEqual(matches[kept["Kestrel Labs"].company_key][0]["source"], "hn_archive")
        self.assertEqual(kept["Kestrel Labs"].company_key, "co:kestrel")          # what build_targets uses
        self.assertEqual(store.get_job("hn:9101").board_size, None)
        self.assertIsNone(store.get_job("hn:9104"))                                # the senior post isn't stored

    def test_archive_roles_become_role_targets_and_cite_the_post(self):
        from applyflow.contacts import build_targets
        from applyflow.pipeline import score_all
        store = Store(":memory:")
        collect_hn_archive(store, hn_client(), PROFILE, date(2026, 9, 1), date(2026, 10, 31), Existing(), today=TODAY)
        score_all(store, PROFILE, TODAY)
        targets = {t.company: t for t in build_targets(store, PROFILE, [])}
        self.assertEqual(targets["Kestrel Labs"].kind, "role")
        self.assertEqual(targets["Kestrel Labs"].domain, "kestrel.example")
        result = find_contacts(None, Target(**{**targets["Driftwave"].__dict__, "domain": ""}), PROFILE,
                               DoNotContact(), GitHubBudget())
        self.assertEqual(result.contacts[0].email, "maya.patel@driftwave.example")
        self.assertEqual(result.contacts[0].name, "Maya")                      # greeted by name
        store.save_contact_result(result)
        it = next(i for i in build_items(store, PROFILE) if i.company == "Driftwave")
        self.assertEqual(it.inputs.role_where, "in your Hacker News post")
        self.assertIn("in your Hacker News post", it.draft.body)
        self.assertEqual(it.address.kind, "verified_person")


# ---------------------------------------------------------------------------
# 2. YC job pages
# ---------------------------------------------------------------------------

class YCJobsTests(unittest.TestCase):
    def test_parser_reads_company_and_postings(self):
        company, postings = parse_yc_jobs_page(text("yc_jobs_ledgerly.html"))
        self.assertEqual(company["name"], "Ledgerly")
        self.assertEqual([p["title"] for p in postings][:2], ["Backend Engineer", "Backend Intern (Python)"])
        self.assertEqual(postings[0]["minExperience"], "Any (new grads ok)")
        self.assertEqual(postings[0]["salaryRange"], "₹1M - ₹2M INR")
        self.assertEqual(parse_yc_jobs_page("<html>no data</html>"), ({}, []))

    def test_locations_spell_out_india(self):
        self.assertEqual(yc_locations("Bengaluru, KA, IN / Remote (IN)"), ["Bengaluru, KA, India", "Remote (India)"])
        self.assertEqual(yc_locations("San Francisco, CA, US"), ["San Francisco, CA, US"])

    def test_role_filter(self):
        company, postings = parse_yc_jobs_page(text("yc_jobs_ledgerly.html"))
        got = {p["title"]: yc_role_decision(p, yc_posting_job(p, company), PROFILE) for p in postings}
        self.assertTrue(got["Backend Engineer"].keep, got["Backend Engineer"].reason)       # new grads ok
        self.assertTrue(got["Backend Intern (Python)"].keep, got["Backend Intern (Python)"].reason)   # remote India
        self.assertFalse(got["Senior Backend Engineer"].keep)
        self.assertIn("not an engineering role", got["Account Executive"].reason)
        self.assertFalse(got["Software Engineer"].keep)                                        # San Francisco
        self.assertFalse(got["Full Stack Engineer"].keep)                                      # 3+ years
        job = yc_posting_job(postings[0], company)
        self.assertEqual(job.url, "https://www.ycombinator.com/companies/ledgerly/jobs/101-x")
        self.assertEqual(job.team_size, 18)

    def test_collect_records_matches_and_new_grads_ok(self):
        store = Store(":memory:")
        base = "https://www.ycombinator.com/companies/"
        client = FakeClient({}, {base + "ledgerly/jobs": text("yc_jobs_ledgerly.html"),
                                 base + "ledgerly/jobs/101-x": text("yc_job_101.html"),
                                 base + "quiet/jobs": text("yc_jobs_empty.html"),
                                 base + "gone/jobs": FetchError(base + "gone/jobs", "HTTP 404", 404)})
        stats = collect_yc_jobs(store, client, PROFILE, [("ledgerly", "Ledgerly"), ("quiet", "Quiet"), ("gone", "Gone")],
                                Existing())
        self.assertEqual((stats["companies"], stats["failed"], stats["matched_companies"], stats["kept"]), (2, 1, 1, 2))
        self.assertEqual(stats["new_grads_ok"], [("Ledgerly", "Backend Engineer", "₹1M - ₹2M INR")])
        m = store.role_matches()
        self.assertEqual((m["yc:ledgerly"][0]["match"], m["yc:quiet"][0]["match"]), ("yes", "no"))
        self.assertIn("ledger APIs", store.get_job("ycjobs:101").description)       # the job page's description
        self.assertEqual(store.conn.execute("SELECT board_key FROM jobs WHERE id = 'ycjobs:101'").fetchone()[0],
                         "ycjobs:ledgerly")

    def test_yc_role_targets_use_the_yc_key_and_cite_the_page(self):
        from applyflow.contacts import build_targets
        from applyflow.pipeline import score_all
        store = Store(":memory:")
        base = "https://www.ycombinator.com/companies/"
        client = FakeClient({}, {base + "ledgerly/jobs": text("yc_jobs_ledgerly.html"),
                                 base + "ledgerly/jobs/101-x": text("yc_job_101.html")})
        collect_yc_jobs(store, client, PROFILE, [("ledgerly", "Ledgerly")], Existing())
        score_all(store, PROFILE, TODAY)
        from applyflow.pool import Scored
        from applyflow.sources import PoolCompany
        store.save_pool([Scored(PoolCompany(key="yc:ledgerly", name="Ledgerly", website="https://ledgerly.example",
                                            domain="ledgerly.example", sources=["yc"], yc_slug="ledgerly"), 50, [])])
        target = next(t for t in build_targets(store, PROFILE, []) if t.company == "Ledgerly")
        self.assertEqual((target.key, target.kind, target.domain), ("yc:ledgerly", "role", "ledgerly.example"))
        target.hint = "founder or CTO"
        store.save_contact_result(ContactResult(target=target, status="found", contacts=[
            Contact(name="Vikram Shah", role="CTO", email="vikram@ledgerly.example", confidence="guessed",
                    source="pattern", source_url=base + "ledgerly", group="cto")]))
        it = next(i for i in build_items(store, PROFILE) if i.company_key == "yc:ledgerly")
        self.assertEqual(it.inputs.role_where, "on your YC jobs page")
        self.assertIn("role on your YC jobs page", it.draft.body)


# ---------------------------------------------------------------------------
# 3. Entrackr
# ---------------------------------------------------------------------------

class EntrackrTests(unittest.TestCase):
    def test_report_urls_from_daily_sitemaps(self):
        client = TextClient({}, {"https://entrackr.com/sitemap_2026-10-17.xml": text("entrackr_sitemap.xml")})
        urls = entrackr_report_urls(client, date(2026, 10, 18), weeks=1)
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0][0].endswith("oct-12-oct-17-12683867"))
        self.assertEqual(urls[0][1], "2026-10-17")
        self.assertTrue(all("sitemap_2026-10-1" in u for u in client.page_calls))   # only Fri-Sun sitemaps read
        self.assertEqual(len(client.page_calls), 3)

    def test_deal_extraction_from_prose(self):
        body = report_text(text("entrackr_report.html"))
        self.assertTrue(body.startswith("This week, 9 Indian startups"))
        self.assertNotIn("city summary", body)                     # stops at the city / segment breakdown
        deals = {d.company: d for d in parse_entrackr_deals(body, "https://entrackr.com/r", "2026-10-17")}
        self.assertEqual(set(deals), {"Fernway Robotics", "Loomly", "Kestrel AI", "Ledgerbase", "Quillbot Labs",
                                      "Northstar AI", "Codewise", "Brightpath", "Circle"})
        self.assertEqual((deals["Ledgerbase"].round, deals["Ledgerbase"].amount), ("Series A", "$6 million"))
        self.assertEqual(deals["Ledgerbase"].sector, "fintech infrastructure startup")
        self.assertEqual(deals["Northstar AI"].amount, "$2.5 million")
        self.assertEqual(deals["Northstar AI"].round, "Seed")
        self.assertEqual(deals["Kestrel AI"].sector, "AI agent startup")
        self.assertEqual(deals["Codewise"].round, "Pre-Series A")
        self.assertEqual(deals["Circle"].amount, "")                # named in "Other funded startups"
        self.assertTrue(all(d.source == "entrackr" and d.date == "2026-10-17" for d in deals.values()))

    def test_domain_candidates(self):
        self.assertEqual(domain_candidates("Kestrel AI")[0], "kestrel.ai")
        self.assertIn("kestrelai.com", domain_candidates("Kestrel AI"))
        self.assertIn("ledgerbase.in", domain_candidates("Ledgerbase"))
        self.assertIn("bright-path.com", domain_candidates("Bright Path"))

    def test_homepage_must_name_the_company_with_an_india_or_sector_signal(self):
        self.assertTrue(homepage_matches(text("home_kestrel_ai.html"), "Kestrel AI", "AI agent startup")[0])
        ok, why = homepage_matches(text("home_circle_us.html"), "Circle", "Enterprise AI startup", "circle.com")
        self.assertFalse(ok)                                        # a namesake abroad
        self.assertIn("namesake", why)
        self.assertFalse(homepage_matches(text("home_unrelated.html"), "Ledgerbase", "fintech")[0])
        self.assertTrue(homepage_matches(text("home_codewise_in.html"), "Codewise", "", "codewise.in")[0])   # .in

    def test_resolve_accepts_only_a_named_homepage_with_mx(self):
        client = FakeClient({}, {"https://kestrel.ai": text("home_kestrel_ai.html"),
                                 "https://circle.com": text("home_circle_us.html"),
                                 "https://codewise.com": text("home_unrelated.html"),
                                 "https://codewise.in": text("home_codewise_in.html")})
        site, why = resolve_domain(client, FakeMX(), "Kestrel AI", "AI agent startup")
        self.assertEqual(site, "https://kestrel.ai")
        site, why = resolve_domain(client, FakeMX(), "Circle", "Enterprise AI startup")
        self.assertEqual(site, "")
        self.assertIn("circle.com", why)
        site, _ = resolve_domain(client, FakeMX(), "Codewise", "developer tools startup")
        self.assertEqual(site, "https://codewise.in")                  # codewise.com doesn't name it
        site, why = resolve_domain(client, FakeMX({"kestrel.ai": "no_mx"}), "Kestrel AI", "AI agent startup")
        self.assertEqual(site, "")
        self.assertIn("MX no_mx", why)

    def test_entrackr_rounds_join_the_pool_via_the_resolver(self):
        from applyflow.sources import FundingRound, attach_funding
        rounds = [FundingRound(company="Kestrel AI", round="", amount="$20 million", date="2026-10-17",
                               sector="AI agent startup", source="entrackr", source_url="https://entrackr.com/r"),
                  FundingRound(company="Circle", date="2026-10-17", source="entrackr", source_url="https://entrackr.com/r")]
        sites = {"Kestrel AI": "https://kestrel.ai", "Circle": ""}
        pool, stats = attach_funding([], rounds, resolve=lambda r: sites[r.company])
        self.assertEqual((stats["resolved"], stats["dropped"]), (1, 1))
        self.assertEqual(pool[0].key, "pool:kestrel.ai")
        self.assertEqual(pool[0].sources, ["funding:entrackr"])
        self.assertEqual(pool[0].description, "AI agent startup")

    def test_recent_funding_is_selected_past_the_cap(self):
        from applyflow.pool import Scored, select
        from applyflow.sources import FundingRound, PoolCompany
        def co(name, day):
            return PoolCompany(key=f"pool:{name}.example", name=name, website=f"https://{name}.example",
                               domain=f"{name}.example", sources=["funding:entrackr"], description="AI agents SaaS API",
                               funding=[FundingRound(company=name, round="Seed", date=day, source="entrackr",
                                                     source_url="https://entrackr.com/r")])
        prof = example_profile()
        prof.pool = {**prof.pool, "target_size": 0}
        out = {s.company.name: s for s in select([Scored(co("fresh", "2026-10-10"), 40, []),
                                                  Scored(co("old", "2026-05-01"), 40, [])],
                                                 {}, prof, 0, today=date(2026, 10, 23))}
        self.assertTrue(out["fresh"].selected)
        self.assertIn("funded in the last 12 weeks", out["fresh"].why_selected)
        self.assertFalse(out["old"].selected)


# ---------------------------------------------------------------------------
# 4. import-targets
# ---------------------------------------------------------------------------

class ImportTargetsTests(unittest.TestCase):
    def test_parser(self):
        lines, errors = parse_targets_file(text("manual_targets.txt"))
        self.assertEqual([(t.company, t.website, t.role_seen, t.note) for t in lines], [
            ("Acme Robotics", "https://acme-robotics.example", "Backend Engineer Intern", "seen on Wellfound | Bengaluru"),
            ("Nimbus Data", "https://nimbusdata.example", "", "data infra, seed")])
        self.assertEqual(len(errors), 2)
        self.assertIn("line 6", errors[0])                     # "Lonely Co": no website
        self.assertIn("isn't a company website", errors[1])

    def test_header_has_the_format_and_two_commented_examples(self):
        lines, errors = parse_targets_file(TARGETS_FILE_HEADER)
        self.assertEqual((lines, errors), ([], []))
        examples = [ln for ln in TARGETS_FILE_HEADER.splitlines() if ln.startswith("# ") and ln.count("|") == 3]
        self.assertEqual(len(examples), 3)                     # the format line and two examples

    def test_careers_page_roles(self):
        fits, seen = careers_page_roles("Open roles\nSenior Product Manager\nBackend Engineer Intern\n"
                                        "Software Engineer - New Grad\nHead of Sales\n", PROFILE)
        self.assertEqual(fits, ["Backend Engineer Intern", "Software Engineer - New Grad"])
        self.assertEqual(seen, 3)

    def test_board_role_match_from_careers_page(self):
        client = FakeClient({}, {"https://acme.example": '<a href="/careers">Careers</a>',
                                 "https://acme.example/careers": text("careers_page.html"),
                                 "https://none.example": '<a href="/careers">Careers</a>',
                                 "https://none.example/careers": text("careers_page_none.html")})
        res = board_role_match(client, "https://acme.example", PROFILE, "https://acme.example/careers")
        self.assertEqual((res["match"], res["title"]), ("yes", "Backend Engineer Intern"))
        res = board_role_match(client, "https://none.example", PROFILE, "https://none.example/careers")
        self.assertEqual(res["match"], "no")
        res = board_role_match(client, "https://empty.example", PROFILE)
        self.assertEqual(res["match"], "unknown")

    def test_import_creates_targets_with_role_match_and_role_email(self):
        import tempfile
        from pathlib import Path
        store = Store(":memory:")
        client = FakeClient({}, {
            "https://acme-robotics.example": '<title>Acme Robotics</title><a href="mailto:careers@acme-robotics.example">x</a>',
            "https://nimbusdata.example": '<title>Nimbus Data - data pipelines API</title><a href="/careers">Careers</a>'
                                          '<a href="mailto:founders@nimbusdata.example">f</a>',
            "https://nimbusdata.example/careers": text("careers_page_none.html")})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manual_targets.txt"
            results, errors = import_targets(store, PROFILE, client, path)
            self.assertTrue(path.exists())                     # created with the header on first run
            self.assertEqual(results, [])
            path.write_text(text("manual_targets.txt"), encoding="utf-8")
            results, errors = import_targets(store, PROFILE, client, path, out_dir=Path(tmp) / "drafts")
            self.assertTrue((Path(tmp) / "drafts" / "packets" / "manual-acmerobotics.md").exists())
        got = {r.line.company: r for r in results}
        self.assertEqual(got["Acme Robotics"].role["match"], "yes")
        self.assertEqual(got["Acme Robotics"].kind, "hiring_inbox")
        self.assertEqual(got["Nimbus Data"].role["match"], "no")
        self.assertEqual(got["Nimbus Data"].kind, "founders")
        items = {it.target_id: it for it in build_items(store, PROFILE)}
        acme = items["manual-acmerobotics"]
        self.assertEqual((acme.inputs.kind, acme.inputs.role_title), ("role", "Backend Engineer Intern"))
        self.assertIn("Backend Engineer Intern", acme.draft.body)
        self.assertEqual(items["manual-nimbusdata"].inputs.kind, "manual")     # general outreach
        self.assertEqual(store.get_manual_target("manual:acmerobotics")["role_seen"], "Backend Engineer Intern")


# ---------------------------------------------------------------------------
# 6. Deeper website search
# ---------------------------------------------------------------------------

class DeepSearchTests(unittest.TestCase):
    def client(self):
        return FakeClient({}, {"https://fernway.example": text("deep_home.html"),
                               "https://fernway.example/legal/privacy": text("deep_privacy.html"),
                               "https://fernway.example/about-us": text("deep_about.html")})

    def test_deep_pages_follow_footer_links_then_deep_paths(self):
        pages = deep_pages(text("deep_home.html"), "https://fernway.example", 20)
        self.assertEqual(pages[:2], ["https://fernway.example/legal/privacy", "https://fernway.example/legal/terms"])
        self.assertIn("https://fernway.example/about-us", pages)
        self.assertIn("https://fernway.example/join-us", pages)
        self.assertNotIn("https://fernway.example/blog/launch", pages)
        self.assertFalse(any("twitter.com" in p for p in pages))

    def test_deep_read_finds_founders_inbox_and_people_the_normal_read_misses(self):
        normal = read_site(self.client(), "https://fernway.example", "fernway.example", 5)
        deep = read_site(self.client(), "https://fernway.example", "fernway.example", 12, deep=True)
        self.assertNotIn("founders@fernway.example", [e for e, _ in normal.emails])
        self.assertIn("founders@fernway.example", [e for e, _ in deep.emails])
        self.assertIn("careers@fernway.example", [e for e, _ in deep.emails])
        self.assertEqual([p.name for p in deep.people], ["Kiran Mehta"])

    def test_deep_lookup_ranks_a_safe_inbox_over_the_guess(self):
        target = Target(key="pool:fernway.example", company="Fernway", kind="general_outreach", hint="founder or CTO",
                        website="https://fernway.example", domain="fernway.example")
        result = find_contacts(self.client(), target, PROFILE, DoNotContact(), GitHubBudget(), deep=True)
        emails = [c.email for c in result.contacts]
        self.assertIn(emails[0], ("founders@fernway.example", "careers@fernway.example"))
        self.assertNotIn("privacy@fernway.example", [c.email for c in result.considered])    # tool address dropped
        self.assertIn("kiran@fernway.example", [c.email for c in result.considered])        # the CTO, guessed
        self.assertTrue(any(r.startswith("website (deep): read") for r in result.reasons))


# ---------------------------------------------------------------------------
# info@ / contact@ unsendable, hello@ cap, one company once
# ---------------------------------------------------------------------------

class SendabilityTests(unittest.TestCase):
    def test_contact_kinds(self):
        self.assertEqual(contact_kind("info@a.example", "published_inbox"), "unsendable")
        self.assertEqual(contact_kind("contact@a.example", "verified"), "unsendable")
        self.assertEqual(contact_kind("founders@a.example", "published_inbox"), "founders")
        self.assertEqual(contact_kind("join-us@a.example", "published_inbox"), "hiring_inbox")
        self.assertEqual(contact_kind("work@a.example", "fallback"), "hiring_inbox")
        self.assertEqual(contact_kind("hello@a.example", "published_inbox"), "general_inbox")
        self.assertEqual(contact_kind("asha.rao@a.example", "verified"), "verified_person")
        self.assertEqual(contact_kind("asha@a.example", "guessed"), "guessed")

    def test_info_and_contact_are_never_sent_to(self):
        store = Store(":memory:")
        add_company(store, "yc:only-info", "Only Info", website="https://onlyinfo.example",
                    contacts=[contact("info@onlyinfo.example", "published_inbox", role="info@ inbox")])
        add_company(store, "yc:mixed", "Mixed", website="https://mixed.example",
                    contacts=[contact("contact@mixed.example", "published_inbox", role="contact@ inbox"),
                              contact("careers@mixed.example", "published_inbox", role="careers@ inbox")])
        items = {it.target_id: it for it in build_items(store, PROFILE)}
        self.assertEqual(items["yc-only-info"].status, "unsendable")
        self.assertEqual(items["yc-only-info"].status_note, "info@onlyinfo.example")
        self.assertEqual(items["yc-mixed"].address.email, "careers@mixed.example")

    def test_a_queued_info_address_is_left_alone(self):
        store = Store(":memory:")
        add_company(store, "yc:queued", "Queued", website="https://queued.example",
                    contacts=[contact("info@queued.example", "published_inbox", role="info@ inbox")])
        store.save_queue("2026-10-21", [("yc-queued", "info@queued.example", 2)], keep=set())
        it = next(i for i in build_items(store, PROFILE) if i.target_id == "yc-queued")
        self.assertEqual((it.status, it.address.email), ("ready", "info@queued.example"))

    def test_hello_inboxes_capped_per_day(self):
        store = Store(":memory:")
        for n in range(7):
            add_company(store, f"yc:h{n}", f"Hello {n}", website=f"https://h{n}.example",
                        contacts=[contact(f"hello@h{n}.example", "published_inbox", role="hello@ inbox")])
        for n in range(3):
            add_company(store, f"yc:c{n}", f"Careers {n}", website=f"https://c{n}.example",
                        contacts=[contact(f"careers@c{n}.example", "published_inbox", role="careers@ inbox")])
        batch = make_queue(store, PROFILE, date(2026, 10, 24), save=False)
        kinds = [it.address.kind for it in batch.items]
        self.assertEqual(kinds.count("general_inbox"), 5)
        self.assertEqual(kinds.count("hiring_inbox"), 3)
        self.assertEqual(batch.general_held, 2)

    def test_a_company_already_emailed_is_never_emailed_again(self):
        store = Store(":memory:")
        add_company(store, "yc:acme", "Acme", website="https://acme.example",
                    contacts=[contact("careers@acme.example", "published_inbox", role="careers@ inbox")])
        mark_sent(store, PROFILE, "yc-acme", date(2026, 10, 22))
        add_company(store, "co:acme-hn", "Acme", website="https://acme.example",
                    contacts=[contact("jobs@acme.example", "verified", role="posting")])
        items = {it.target_id: it for it in build_items(store, PROFILE)}
        self.assertEqual(items["co-acme-hn"].status, "duplicate")
        self.assertIn("yc-acme", items["co-acme-hn"].status_note)

    def test_safe_summary_counts_days_with_caps(self):
        store = Store(":memory:")
        for n in range(40):
            add_company(store, f"yc:s{n}", f"Safe {n}", website=f"https://s{n}.example",
                        contacts=[contact(f"careers@s{n}.example", "published_inbox", role="careers@ inbox")])
        for n in range(10):
            add_company(store, f"yc:h{n}", f"Hello {n}", website=f"https://h{n}.example",
                        contacts=[contact(f"hello@h{n}.example", "published_inbox", role="hello@ inbox")])
        s = safe_summary(build_items(store, PROFILE), daily=30, general_cap=5, guessed_cap=7)
        self.assertEqual((s["safe"], s["general"]), (40, 10))
        self.assertEqual(s["days"], 2)                  # day 1: 5 hello + 25 careers; day 2: 5 hello + 15 careers


if __name__ == "__main__":
    unittest.main()


class ReviewRulesTests(unittest.TestCase):
    """Rules added while reviewing the 27 Oct batch."""

    def test_unnamed_address_on_a_legal_page_is_unsendable(self):
        self.assertEqual(contact_kind("aditya.iyer@x.example", "verified", "https://x.example/privacy", "", "website"),
                         "unsendable")
        self.assertEqual(contact_kind("rishabh.roy@x.example", "verified",
                                      "https://x.example/legal/account-deletion-policy", "", "website"), "unsendable")
        # a founder whose own address is on the privacy page is still a person
        self.assertEqual(contact_kind("vineet.mehta@x.example", "verified", "https://x.example/privacy-policy",
                                      "Vineet Mehta", "website"), "verified_person")
        # a hiring inbox on a legal page stays a hiring inbox
        self.assertEqual(contact_kind("hr@x.example", "published_inbox", "https://x.example/legal/privacy-policy", "",
                                      "website"), "hiring_inbox")

    def test_role_word_addresses_are_not_people(self):
        self.assertEqual(contact_kind("customer-success@x.example", "verified"), "other")
        self.assertEqual(contact_kind("partner.institute@x.example", "verified"), "other")
        self.assertEqual(contact_kind("priya.nair@x.example", "verified"), "verified_person")

    def test_a_page_full_of_unnamed_addresses_is_sample_data(self):
        names = ["bo.wagner", "jill.rhodes", "ethan.brooks", "sophia.kim"]
        mock = "<p>" + " ".join(f"Assignee {n.title()} ({n}@intryc.example)" for n in names) + "</p>"
        client = FakeClient({}, {"https://intryc.example": "<p>Intryc</p>",
                                 "https://intryc.example/about": mock,
                                 "https://intryc.example/careers": "<p>careers@intryc.example</p>"})
        target = Target(key="yc:intryc", company="Intryc", kind="general_outreach", hint="founder or CTO",
                        website="https://intryc.example", domain="intryc.example")
        result = find_contacts(client, target, PROFILE, DoNotContact(), GitHubBudget())
        emails = [c.email for c in result.considered]
        self.assertNotIn("bo.wagner@intryc.example", emails)
        self.assertIn("careers@intryc.example", emails)
        self.assertTrue(any("sample data" in why for _, why in result.excluded))

    def test_reviewed_out_addresses_are_skipped(self):
        store = Store(":memory:")
        add_company(store, "yc:contlo", "Contlo", website="https://contlo.example",
                    contacts=[contact("shrikant.shinde@contlo.example", "verified", role="person at the company"),
                              contact("careers@contlo.example", "published_inbox", role="careers@ inbox")])
        store.conn.execute("UPDATE contacts SET proof = 'excluded: sales / demo contact' "
                           "WHERE email = 'shrikant.shinde@contlo.example'")
        it = next(i for i in build_items(store, PROFILE) if i.target_id == "yc-contlo")
        self.assertEqual(it.address.email, "careers@contlo.example")


class EmailRegexSpeedTests(unittest.TestCase):
    def test_long_unbroken_text_doesnt_hang(self):
        import time
        from applyflow.text import extract_emails
        text = ("x" * 60000 + " at ") * 3 + "a" * 80000 + " jobs [at] acme [dot] com"
        start = time.perf_counter()
        self.assertEqual(extract_emails(text), ["jobs@acme.com"])
        self.assertLess(time.perf_counter() - start, 2.0)


class DurableExclusionTests(unittest.TestCase):
    def test_exclusions_survive_a_new_lookup(self):
        store = Store(":memory:")
        add_company(store, "yc:contlo", "Contlo", website="https://contlo.example",
                    contacts=[contact("shrikant.shinde@contlo.example", "verified", role="person at the company")])
        store.exclude_contact("shrikant.shinde@contlo.example", "sales / demo contact")
        # a later lookup re-saves the same contact (proof column reset)
        add_company(store, "yc:contlo", "Contlo", website="https://contlo.example",
                    contacts=[contact("shrikant.shinde@contlo.example", "verified", role="person at the company"),
                              contact("careers@contlo.example", "published_inbox", role="careers@ inbox")])
        it = next(i for i in build_items(store, PROFILE) if i.target_id == "yc-contlo")
        self.assertEqual(it.address.email, "careers@contlo.example")
