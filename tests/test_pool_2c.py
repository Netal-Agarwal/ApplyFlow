"""Phase 2c: company sources, dedupe/merge, hiring-likelihood score, selection, source proof, greetings,
shared-inbox subjects, funding mentions, inbox preference, queue order and resumable contact batches."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from applyflow.contacts import (Contact, ContactResult, DoNotContact, GitHubBudget, Target, classify_address,
                                find_contacts, inbox_preference, name_for_address, name_from_handle, pending_targets,
                                shared_inbox, verify_sources)
from applyflow.drafting import DraftInputs, application_subject, funding_line, load_facts, template_draft, validate
from applyflow.http import FetchError
from applyflow.models import DiscoveredCompany
from applyflow.outreach import build_items, make_queue, pick_batch
from applyflow.pool import Scored, build_pool, score_company, select, tech_fit
from applyflow.sources import (FundingRound, PoolCompany, attach_funding, batch_year, clean_name, collect_portfolio,
                               company_website, merge, parse_feed, parse_funding_table, parse_markdown_table,
                               parse_name_url_json, parse_portfolio, website_from_profile, yc_pool)
from applyflow.store import Store
from tests.helpers import FIXTURES, FakeClient, example_profile

P = FIXTURES / "pool"
PROFILE = example_profile()
FACTS = load_facts(PROFILE)
TODAY = date(2026, 10, 19)


def text(name):
    return (P / name).read_text(encoding="utf-8")


class TextClient(FakeClient):
    """FakeClient plus get_text (RSS feeds and raw files), served from `pages`."""

    def get_text(self, url):
        return self.get_page(url)


PORTFOLIOS = {
    "better": {"id": "better-capital", "url": "https://www.bettercapital.vc/portfolio", "item": '<tr class="bg-w">.*?</tr>',
               "name": '<a href="[^"]+"[^>]*>([^<]+)</a>', "website": '<a href="([^"]+)"',
               "description": '</td>\\s*<td class="border-bottom">(.*?)</td>', "locations": "India"},
    "axilor": {"id": "axilor", "url": "https://axilor.com/portfolio/",
               "item": '<a href="https?://[^"]+" target="new" class="box-link".*?</figcaption>',
               "name": '<h3 class="h4">([^<]+)</h3>', "website": 'href="([^"]+)"',
               "description": 'data-tooltip="([^"]*)"', "locations": "Bengaluru, India"},
    "antler": {"id": "antler", "url": "https://www.antler.co/portfolio",
               "item": '<div class="portco_card">.*?class="clickable_link"',
               "name": 'fs-cmsfilter-field="name"[^>]*>([^<]+)<',
               "website": 'href="(https?://[^"]+)" class="clickable_link',
               "description": 'fs-cmsfilter-field="description"[^>]*>([^<]+)<', "keep": 'tag_small_text">India<',
               "year": 'fs-cmsfilter-field="year"[^>]*>\\s*<div[^>]*>(\\d{4})<', "locations": "India",
               "pages": {"param": "0b933bfd_page", "max": 5}},
}
PORTFOLIOS["antler"]["item"] = '<div class="portco_card">.*?class="clickable_link'
INC42 = {"id": "inc42", "feed": "https://inc42.com/tag/funding-galore/feed/",
         "company_page": "https://inc42.com/company/{slug}/", "max_articles": 30}


# ---------------------------------------------------------------------------
# (a)-(d) source parsers
# ---------------------------------------------------------------------------

class YCDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.pool = {c.name: c for c in yc_pool(json.loads(text("yc_all.json")))}

    def test_india_any_hiring_status_and_remote_global_10_to_150(self):
        self.assertEqual(set(self.pool), {"Ledgerly", "Driftwork", "Masala Box"})
        self.assertEqual(self.pool["Ledgerly"].region, "india")            # isHiring false still counts
        self.assertEqual(self.pool["Driftwork"].region, "remote_global")
        self.assertNotIn("BigRemote", self.pool)                           # 400 people: outside 10-150
        self.assertNotIn("Valley Only", self.pool)                         # "Remote" alone isn't Fully Remote
        self.assertNotIn("OldChai", self.pool)                             # inactive

    def test_fields(self):
        c = self.pool["Ledgerly"]
        self.assertEqual((c.key, c.domain, c.website, c.team_size, c.founded_year),
                         ("yc:ledgerly", "ledgerly.in", "https://ledgerly.in", 24, 2025))
        self.assertIn("SaaS", c.tags)
        self.assertEqual(self.pool["Masala Box"].founded_year, 2017)       # from launched_at, no batch

    def test_batch_year(self):
        self.assertEqual([batch_year(b) for b in ("Winter 2024", "S21", "X25", "")], [2024, 2021, 2025, None])


class PortfolioTests(unittest.TestCase):
    def test_better_capital_rows(self):
        found = parse_portfolio(text("better_capital.html"), PORTFOLIOS["better"], PORTFOLIOS["better"]["url"])
        self.assertEqual([(c.name, c.domain) for c in found], [("Shram", "shram.ai"), ("Skydo", "skydo.com")])
        self.assertEqual(found[1].description, "Cross border payments for businesses")   # LinkedIn row dropped
        self.assertEqual((found[0].key, found[0].sources, found[0].region),
                         ("pool:shram.ai", ["portfolio:better-capital"], "india"))

    def test_axilor_cards_and_name_cleaning(self):
        found = parse_portfolio(text("axilor.html"), PORTFOLIOS["axilor"])
        self.assertEqual([c.name for c in found], ["Securden", "Emitrr"])
        self.assertEqual(found[0].description, "Access & Identity Management")
        self.assertEqual(found[1].locations, "Bengaluru, India")

    def test_antler_keeps_the_india_tag_and_stops_at_an_empty_page(self):
        url = PORTFOLIOS["antler"]["url"]
        client = TextClient({}, {url: text("antler_p1.html"), f"{url}?0b933bfd_page=2": text("antler_empty.html")})
        found, note = collect_portfolio(client, PORTFOLIOS["antler"])
        self.assertEqual([(c.name, c.founded_year) for c in found], [("Ledgerly", 2024)])
        self.assertEqual(client.page_calls, [url, f"{url}?0b933bfd_page=2"])
        self.assertIn("1 companies from 2 page(s)", note)

    def test_a_page_robots_disallows_is_skipped_with_the_reason(self):
        url = PORTFOLIOS["better"]["url"]
        client = TextClient({}, {url: FetchError(url, "disallowed by robots.txt")})
        found, note = collect_portfolio(client, PORTFOLIOS["better"])
        self.assertEqual(found, [])
        self.assertIn("robots.txt", note)

    def test_company_website_rejects_social_and_aggregator_links(self):
        self.assertEqual(company_website("http://www.Skydo.com/pricing"), "https://skydo.com")
        for url in ("https://linkedin.com/company/x", "https://inc42.com/company/x", "https://play.google.com/x", ""):
            self.assertEqual(company_website(url), "")
        self.assertEqual(company_website("https://pragmaticleaders.io, https://ankai.com"), "https://pragmaticleaders.io")
        self.assertEqual(company_website("66cents.com (yet to be alive)"), "https://66cents.com")
        for junk in ("santu.uvce@gmail.com", "coming soon...", "www,greatcio.com", "not available."):
            self.assertEqual(company_website(junk), "", junk)
        self.assertEqual(clean_name("SQUADSTACK**"), "Squadstack")
        self.assertEqual(clean_name("IDFC"), "IDFC")


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.articles = parse_feed(text("inc42_feed.xml"))
        self.rounds = [r for a in self.articles for r in parse_funding_table(a["content"], a["url"], a["published"])]

    def test_feed_and_table(self):
        self.assertEqual([a["published"] for a in self.articles], ["2026-10-10", "2026-03-07"])
        stock = next(r for r in self.rounds if r.company == "StockGro")
        self.assertEqual((stock.round, stock.amount, stock.date, stock.sector), ("Series B", "$10.4 Mn", "2026-10-06",
                                                                                 "Fintech"))
        self.assertTrue(stock.source_url.startswith("https://inc42.com/buzz/from-dailyobjects"))
        quan = next(r for r in self.rounds if r.company == "Quanfluence")
        self.assertEqual((quan.round, quan.subsector), ("", ""))           # "–" means not stated

    def test_a_date_typo_falls_back_to_the_article_date(self):
        page = ("<table><tr><td>Date</td><td>Name</td><td>Funding Round Type</td></tr>"
                "<tr><td>5 Oct 2926</td><td>Typo Co</td><td>Seed</td></tr></table>")
        self.assertEqual(parse_funding_table(page, "u", "2026-10-10")[0].date, "2026-10-10")

    def test_match_by_name_resolve_via_profile_or_drop(self):
        pool = [PoolCompany(key="pool:emitrr.com", name="Emitrr", website="https://emitrr.com", domain="emitrr.com",
                            sources=["portfolio:axilor"])]
        client = TextClient({}, {"https://inc42.com/company/desible-ai/": text("inc42_company.html")})
        wanted = lambda r: tech_fit(f"{r.company} {r.sector} {r.subsector}", PROFILE)[0] != "low"
        resolved = {}
        out, stats = attach_funding(pool, self.rounds, client, INC42, resolved, wanted=wanted)
        by = {c.name: c for c in out}
        self.assertEqual(by["Emitrr"].funding[0].round, "Series A")        # matched by name
        self.assertIn("funding:inc42", by["Emitrr"].sources)
        self.assertEqual((by["Desible.ai"].domain, by["Desible.ai"].funding[0].round), ("desible.ai", "Seed"))
        self.assertNotIn("DailyObjects", by)                               # D2C: not looked up, dropped
        self.assertNotIn("Quanfluence", by)                                # no Inc42 profile: dropped
        self.assertEqual((stats["matched"], stats["resolved"], stats["dropped"]), (1, 1, 3))
        self.assertEqual(resolved["desible"], "https://desible.ai")         # cached (by name_key) for the next run
        self.assertNotIn("https://inc42.com/company/dailyobjects/", client.page_calls)

    def test_a_funding_row_fixes_a_squashed_name(self):
        pool = [PoolCompany(key="pool:desible.ai", name="Desibleai", website="https://desible.ai", domain="desible.ai",
                            sources=["portfolio:prime-vp"])]
        rnd = next(r for r in self.rounds if r.company == "Desible.ai")
        rnd.website = "https://desible.ai"
        out, _ = attach_funding(pool, [rnd])
        self.assertEqual(out[0].name, "Desible.ai")

    def test_profile_page_website(self):
        self.assertEqual(website_from_profile(text("inc42_company.html")), "https://desible.ai")


class GitHubListTests(unittest.TestCase):
    def test_name_url_json(self):
        cfg = {"id": "bangalore-startups", "repo": "hemanth/bangalore-startups", "branch": "master",
               "path": "startups.json", "locations": "Bengaluru, India"}
        found = parse_name_url_json(json.loads(text("bangalore_startups.json")), cfg)
        self.assertEqual(sorted(c.domain for c in found), ["99tests.com", "emitrr.com", "moengage.com"])
        self.assertTrue(found[0].source_urls[0].startswith("https://raw.githubusercontent.com/hemanth/"))

    def test_markdown_table(self):
        cfg = {"id": "ai-startups-india", "repo": "rushout09/ai-startups-india", "branch": "main",
               "path": "README.md"}
        found = parse_markdown_table(text("ai_startups_readme.md"), cfg)
        self.assertEqual([(c.name, c.domain) for c in found], [("Portkey AI", "portkey.ai"), ("Lightup AI", "lightup.ai")])
        self.assertEqual(found[1].description, "AI-driven data monitoring and anomaly detection.")
        self.assertEqual(found[0].tags, ["Development"])


class MergeTests(unittest.TestCase):
    def test_dedupe_by_domain_merges_every_source(self):
        yc = PoolCompany(key="yc:emitrr", name="Emitrr", website="https://emitrr.com", domain="emitrr.com",
                         sources=["yc"], team_size=120, batch="Winter 2019", region="remote_global")
        axilor = parse_portfolio(text("axilor.html"), PORTFOLIOS["axilor"])
        blr = parse_name_url_json(json.loads(text("bangalore_startups.json")),
                                  {"id": "bangalore-startups", "repo": "r", "branch": "b", "path": "p",
                                   "locations": "Bengaluru, India"})
        merged = {c.domain: c for c in merge(axilor + blr + [yc])}
        e = merged["emitrr.com"]
        self.assertEqual(e.key, "yc:emitrr")                               # YC gives key and name
        self.assertEqual(e.sources, ["yc", "portfolio:axilor", "list:bangalore-startups"])
        self.assertEqual((e.team_size, e.description[:5]), (120, "Helps"))  # empty fields filled from others
        self.assertEqual(e.region, "remote_global")                       # YC's own region is kept
        self.assertEqual(len(merged), 4)                                   # securden, emitrr, moengage, 99tests


# ---------------------------------------------------------------------------
# Hiring likelihood and selection
# ---------------------------------------------------------------------------

def company(name, **kw):
    domain = kw.pop("domain", f"{name.lower().replace(' ', '')}.example")
    return PoolCompany(key=f"pool:{domain}", name=name, website=f"https://{domain}", domain=domain,
                       sources=kw.pop("sources", ["portfolio:x"]), **kw)


def funded(day, rnd="Series A"):
    return [FundingRound(company="x", round=rnd, amount="$5 Mn", date=day, source="inc42",
                         source_url="https://inc42.com/buzz/x")]


class ScoreTests(unittest.TestCase):
    def score(self, c, board=False):
        return score_company(c, PROFILE, TODAY, board)

    def test_every_factor_is_a_reason(self):
        c = company("Ledgerly", description="Reconciliation APIs and payments infrastructure for SaaS finance teams",
                    team_size=40, locations="Bengaluru, India", founded_year=2025, funding=funded("2026-09-01"))
        score, reasons = self.score(c, board=True)
        self.assertEqual(score, 100)                                       # 105 points, capped at 100
        joined = " | ".join(reasons)
        for part in ("+25 funding within 6 months: Series A $5 Mn 2026-09-01", "+20 team of 40", "+12 careers page",
                     "+20 tech fit", "+10 preferred city: bengaluru", "+10 recent", "+8 India-based"):
            self.assertIn(part, joined)

    def test_funding_6_months_beats_12_and_older_counts_nothing(self):
        base = dict(description="APIs", team_size=40)
        s6 = self.score(company("A", funding=funded("2026-06-01"), **base))[0]
        s12 = self.score(company("B", funding=funded("2026-01-01"), **base))[0]
        s_old = self.score(company("C", funding=funded("2025-06-01"), **base))[0]
        s_none = self.score(company("D", **base))[0]
        self.assertGreater(s6, s12)
        self.assertGreater(s12, s_old)
        self.assertEqual(s_old, s_none)

    def test_unsourced_funding_is_ignored(self):
        c = company("A", funding=[FundingRound(company="A", round="Seed", date="2026-09-01")])
        self.assertIn("+0 no sourced funding", " ".join(self.score(c)[1]))

    def test_team_size_bands(self):
        pts = {n: self.score(company("A", team_size=n))[0] for n in (5, 40, 300, 900)}
        self.assertGreater(pts[40], pts[5])
        self.assertGreater(pts[5], pts[300])
        self.assertGreater(pts[300], pts[900])

    def test_late_stage_round_without_headcount_counts_as_big(self):
        late = self.score(company("Z", funding=funded("2026-09-01", "Series D")))[1]
        self.assertIn("+0 team size unknown, but Series D round with no headcount (Series C+), likely 300+ people", late)
        cs = [Scored(company("Z", funding=funded("2026-09-01", "Series D")), 90, []),
              Scored(company("Huge", funding=[FundingRound(company="Huge", round="", amount="$120 Mn",
                                                           date="2026-09-01", source_url="u")]), 90, []),
              Scored(company("Seedy", funding=funded("2026-09-01", "Seed")), 50, [])]
        out = {s.company.name: s for s in select(cs, {}, PROFILE, 0)}
        self.assertEqual([n for n, s in out.items() if s.selected], ["Seedy"])

    def test_tech_fit_low_for_d2c_food_fashion(self):
        low = self.score(company("Masala", description="D2C spice and snacks brand"))[1]
        self.assertTrue(any(r.startswith("-10 weak tech fit") for r in low))
        self.assertEqual(tech_fit("AI agents and APIs for developers", PROFILE)[0], "high")

    def test_remote_global_ranks_below_the_same_company_in_india(self):
        india = self.score(company("A", description="APIs for developers", team_size=40))[0]
        remote = self.score(company("B", description="APIs for developers", team_size=40, region="remote_global"))[0]
        self.assertGreater(india, remote)


class SelectionTests(unittest.TestCase):
    def test_size_cap_remote_cap_and_fixed_targets(self):
        profile = example_profile()
        profile.pool.update({"target_size": 4, "remote_global_cap": 1})
        cs = [Scored(company("Fixed", team_size=40), 10, []),
              Scored(company("Big", team_size=900), 95, []),
              Scored(company("R1", region="remote_global"), 90, []),
              Scored(company("R2", region="remote_global"), 89, []),
              Scored(company("I1"), 80, []), Scored(company("I2"), 70, []), Scored(company("I3"), 60, []),
              Scored(company("Weekday"), 99, [])]
        out = {s.company.name: s for s in select(cs, {"fixed.example": "kept: already a target"}, profile, 1)}
        self.assertTrue(out["Fixed"].selected)                             # always in, whatever its score
        self.assertFalse(out["Big"].selected)
        self.assertIn("apply via postings", out["Big"].why_selected)
        self.assertEqual([n for n in ("R1", "R2") if out[n].selected], ["R1"])
        self.assertIn("remote-global cap", out["R2"].why_selected)
        self.assertEqual(sorted(n for n, s in out.items() if s.selected), ["Fixed", "I1", "R1"])   # 4 - 1 role
        self.assertEqual(out["Weekday"].rank, 1)                           # ranked by score regardless...
        self.assertIn("recruiting marketplace", out["Weekday"].why_selected)  # ...but never selected

    def test_build_pool_end_to_end_offline(self):
        store = Store(":memory:")
        url = PORTFOLIOS["axilor"]["url"]
        client = TextClient({}, {url: text("axilor.html"), INC42["feed"]: text("inc42_feed.xml"),
                                 "https://inc42.com/company/desible-ai/": text("inc42_company.html")})
        summary = build_pool(store, client, PROFILE, TODAY, log=lambda _: None,
                             sources={"portfolios": [PORTFOLIOS["axilor"]], "funding": [INC42]},
                             yc_data=json.loads(text("yc_all.json")))
        self.assertEqual(summary.per_source["yc (India)"], 2)
        self.assertEqual(summary.per_source["portfolio:axilor"], 2)
        rows = {r["name"]: r for r in store.pool_rows()}
        self.assertIn("Desible.ai", rows)                                  # new via its Inc42 profile
        emitrr = json.loads(rows["Emitrr"]["data"])
        self.assertEqual(emitrr["funding"][0]["source_url"],
                         "https://inc42.com/buzz/from-flam-to-verifaix-indian-startups-raised-about-59-mn-this-week/")
        self.assertTrue(json.loads(rows["Ledgerly"]["reasons"]))
        self.assertEqual(summary.funding["12m"], 2)                        # Emitrr (Mar), Desible.ai (Oct)
        self.assertEqual(summary.funding["6m"], 1)


# ---------------------------------------------------------------------------
# Contacts: greeting names, inbox preference, source proof, batches
# ---------------------------------------------------------------------------

class NameTests(unittest.TestCase):
    def test_handle(self):
        self.assertEqual(name_from_handle("panchamkhaitan", "pkhaitan"), "Pancham")
        self.assertEqual(name_from_handle("priyasharma", "priya"), "Priya")
        self.assertEqual(name_from_handle("pkhaitan", "pkhaitan"), "")       # nothing to split
        self.assertEqual(name_from_handle("throwaway99", "pkhaitan"), "")

    def test_from_the_post_text(self):
        post = "Acme | Backend | Bengaluru\nWe build ledgers. Email Priya Sharma at psharma@acme.example."
        self.assertEqual(name_for_address("psharma@acme.example", [post]), "Priya")
        self.assertEqual(name_for_address("pkhaitan@singlestore.com", ["SingleStore | AI Platform"],
                                          ["panchamkhaitan"]), "Pancham")

    def test_first_dot_last_address_names_its_owner(self):
        from applyflow.contacts import name_from_local
        self.assertEqual(name_from_local("gaurav.gautam@centricity.co.in"), "Gaurav")
        self.assertEqual(name_from_local("shradha.vadhone@compup.io"), "Shradha")
        for email in ("team.india@a.in", "join-us@a.in", "rahul@a.in", "hr.team@a.in"):
            self.assertEqual(name_from_local(email), "", email)

    def test_hyphenated_hiring_inbox(self):
        self.assertEqual(classify_address("join-us@parallelloop.ai")[0], "hiring_inbox")
        self.assertTrue(shared_inbox("join-us@parallelloop.ai"))
        self.assertEqual(inbox_preference("join-us@parallelloop.ai"), 0)

    def test_ambiguous_or_shared_gives_no_name(self):
        post = "Talk to Priya Sharma or Pooja Sharma: psharma@acme.example"
        self.assertEqual(name_for_address("psharma@acme.example", [post]), "")
        self.assertEqual(name_for_address("careers@acme.example", ["Priya Careers"], ["careers"]), "")
        self.assertEqual(name_for_address("rahul@acme.example", ["Apply now"]), "")


class InboxPreferenceTests(unittest.TestCase):
    def test_order(self):
        order = sorted(["contact@a.in", "hello@a.in", "founders@a.in", "info@a.in", "careers@a.in", "team@a.in",
                        "hi@a.in", "talent@a.in"], key=lambda e: (inbox_preference(e), e))
        self.assertEqual(order, ["careers@a.in", "founders@a.in", "talent@a.in", "hello@a.in", "hi@a.in",
                                 "team@a.in", "contact@a.in", "info@a.in"])
        self.assertTrue(shared_inbox("founders@a.in") and shared_inbox("info@a.in"))
        self.assertFalse(shared_inbox("priya@a.in"))

    def test_contact_finder_prefers_founders_then_hello_then_info(self):
        site = "https://inbox.example"
        page = ('<a href="mailto:info@inbox.example">x</a> <a href="mailto:hello@inbox.example">y</a> '
                '<a href="mailto:founders@inbox.example">z</a>')
        client = TextClient({}, {site: f"<html><body>{page}</body></html>"})
        target = Target(key="pool:inbox.example", company="Inbox", kind="general_outreach", hint="founder or CTO",
                        website=site, domain="inbox.example", team_size=12)
        result = find_contacts(client, target, PROFILE, DoNotContact(), GitHubBudget())
        self.assertEqual([c.email for c in result.contacts],
                         ["founders@inbox.example", "hello@inbox.example", "info@inbox.example"])
        self.assertTrue(all(c.proof == "company site" and c.source_url == site for c in result.contacts))


def proof_store():
    store = Store(":memory:")
    for key, name, domain, email, url in (
            ("yc:rocket", "Rocket", "rocket.example", "contact@rocket.example", "https://rocket.example/about"),
            ("yc:moved", "Moved", "moved.example", "hello@moved.example", "https://moved.example/about"),
            ("yc:fine", "Fine", "fine.example", "team@fine.example", "https://fine.example/contact")):
        target = Target(key=key, company=name, kind="general_outreach", hint="founder or CTO",
                        website=f"https://{domain}", domain=domain, team_size=10)
        store.save_contact_result(ContactResult(target=target, status="found", contacts=[
            Contact(name="", role="inbox", email=email, confidence="published_inbox", source="website",
                    source_url=url, group="general_inbox")]))
    return store


class SourceProofTests(unittest.TestCase):
    def test_confirm_move_or_demote(self):
        store = proof_store()
        client = TextClient({}, {
            "https://rocket.example/about": "<p>About us. Write to us via the form.</p>",
            "https://rocket.example": "<p>Rocket</p>",
            "https://moved.example/about": "<p>No address here</p>",
            "https://moved.example": '<a href="/contact">Contact</a>',
            "https://moved.example/contact": '<a href="mailto:hello@moved.example">mail</a>',
            "https://fine.example/contact": "<p>Say hi: team@fine.example</p>"})
        results = {r.company: r for r in verify_sources(store, client, PROFILE)}
        self.assertEqual(results["Fine"].status, "confirmed")
        self.assertEqual((results["Moved"].status, results["Moved"].url), ("moved", "https://moved.example/contact"))
        self.assertEqual(results["Rocket"].status, "demoted")
        rocket = store.contacts_for("yc:rocket")[0]
        self.assertEqual(rocket["confidence"], "guessed")
        self.assertTrue(rocket["proof"].startswith("demoted: not found on https://rocket.example/about"))
        self.assertEqual(store.contacts_for("yc:fine")[0]["proof"], "company site")
        self.assertEqual(store.contacts_for("yc:moved")[0]["source_url"], "https://moved.example/contact")
        items = {it.target_id: it for it in build_items(store, PROFILE)}
        # a guess now (tier 3); and as a contact@ address it isn't sent to at all (2e)
        self.assertEqual(items["yc-rocket"].status, "unsendable")

    def test_posting_addresses_keep_the_posting_as_proof(self):
        store = Store(":memory:")
        target = Target(key="co:acme", company="Acme", kind="role", hint="founder or CTO", domain="acme.example")
        store.save_contact_result(ContactResult(target=target, status="found", contacts=[
            Contact(name="", role="posting", email="jobs@acme.example", confidence="verified", source="posting",
                    source_url="https://news.ycombinator.com/item?id=1", group="posting")]))
        results = verify_sources(store, TextClient({}, {}), PROFILE)
        self.assertEqual(results[0].status, "kept")
        self.assertEqual(store.contacts_for("co:acme")[0]["proof"], "posting")


class RobotsUnreachableTests(unittest.TestCase):
    def test_a_connection_failure_on_robots_txt_is_not_cached_as_disallow(self):
        import requests
        from applyflow.http import PoliteClient

        class Session:
            headers = {}
            calls = 0

            def get(self, url, params=None, timeout=None, headers=None):
                Session.calls += 1
                if Session.calls == 1:
                    raise requests.ConnectionError("getaddrinfo failed")

                class R:
                    status_code, headers, content = 200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"
                    text = "User-agent: *\nAllow: /\n"
                return R()
        client = PoliteClient("t", session=Session(), max_retries=0, sleep=lambda _: None)
        with self.assertRaises(FetchError) as ctx:
            client.allowed_by_robots("https://flaky.example/about")
        self.assertIn("site unreachable", str(ctx.exception))
        self.assertNotIn("robots.txt", str(ctx.exception).split("(")[0])     # not reported as robots-blocked
        self.assertTrue(client.allowed_by_robots("https://flaky.example/about"))   # asked again, not cached


class BatchResumeTests(unittest.TestCase):
    def test_limit_picks_up_where_the_last_run_stopped(self):
        store = Store(":memory:")
        targets = [Target(key=f"pool:c{i}.example", company=f"C{i}", kind="general_outreach", hint="founder or CTO",
                          domain=f"c{i}.example") for i in range(5)]
        batch, pending = pending_targets(store, targets, 30, limit=2)
        self.assertEqual([t.company for t in batch], ["C0", "C1"])
        for t in batch:
            store.save_contact_result(ContactResult(target=t, status="none"))
        batch, pending = pending_targets(store, targets, 30, limit=2)
        self.assertEqual([t.company for t in batch], ["C2", "C3"])
        self.assertEqual(len(pending), 3)
        self.assertEqual(len(pending_targets(store, targets, 30, force=True)[1]), 5)

    def test_retry_unread_requeues_lookups_that_read_no_page(self):
        store = Store(":memory:")
        targets = [Target(key=f"pool:c{i}.example", company=f"C{i}", kind="general_outreach", hint="founder or CTO",
                          domain=f"c{i}.example") for i in range(2)]
        store.save_contact_result(ContactResult(target=targets[0], status="none",
                                                reasons=["website: read 0 page(s), 0 email(s), 0 named people"]))
        store.save_contact_result(ContactResult(target=targets[1], status="none",
                                                reasons=["website: read 3 page(s), 0 email(s), 0 named people"]))
        self.assertEqual(pending_targets(store, targets, 30)[1], [])
        self.assertEqual([t.company for t in pending_targets(store, targets, 30, retry_unread=True)[1]], ["C0"])


# ---------------------------------------------------------------------------
# Drafts: greeting, subjects, funding mentions, the 2b lock
# ---------------------------------------------------------------------------

def inputs(**kw):
    base = dict(target_id="pool-acme-example", company_key="pool:acme.example", company="Acme",
                kind="general_outreach", one_liner="Payment APIs for Indian SMBs", website="https://acme.example")
    base.update(kw)
    return DraftInputs(**base)


class GreetingTests(unittest.TestCase):
    def test_greeting_by_address_kind(self):
        self.assertEqual(inputs(to_email="ravi@acme.example", contact_name="Ravi Kumar").greeting, "Hi Ravi,")
        self.assertEqual(inputs(to_email="ravi@acme.example").greeting, "Hi,")           # person, name unknown
        self.assertEqual(inputs(to_email="hello@acme.example").greeting, "Hi Acme team,")
        self.assertEqual(inputs(to_email="founders@acme.example").greeting, "Hi Acme team,")
        self.assertEqual(inputs(to_email="ravi@acme.example", rules="2b").greeting, "Hi Acme team,")

    def test_template_and_validation(self):
        i = inputs(to_email="ravi@acme.example", confidence="verified", contact_group="other")
        d = template_draft(i, FACTS)
        self.assertTrue(d.body.startswith("Hi,\n"))
        self.assertEqual(d.template_failures, [])
        wrong = d.body.replace("Hi,\n", "Hi Acme team,\n", 1)
        fails = validate(d.subject, wrong, i, FACTS, d.resume_action)
        self.assertTrue(any("is a person's address: 'Hi <Company> team,' is only for shared inboxes" in f
                            for f in fails), fails)

    def test_pancham_from_the_singlestore_handle(self):
        self.assertEqual(inputs(to_email="pkhaitan@singlestore.com",
                                contact_name=name_for_address("pkhaitan@singlestore.com", [], ["panchamkhaitan"])
                                ).greeting, "Hi Pancham,")


class SubjectTests(unittest.TestCase):
    def test_general_inboxes_get_an_application_subject(self):
        for local in ("hello", "hi", "team", "info", "contact"):
            i = inputs(to_email=f"{local}@acme.example", confidence="published_inbox", contact_group="general_inbox")
            d = template_draft(i, FACTS)
            self.assertEqual(d.subject, "Internship / new-grad application – Your Name (Backend, Jan 2027)")
            self.assertEqual(d.template_failures, [])

    def test_role_word_follows_fit(self):
        self.assertIn("(AI, Jan 2027)", application_subject(inputs(one_liner="LLM agents for support"), FACTS))
        self.assertIn("(Cloud, Jan 2027)", application_subject(inputs(one_liner="Kubernetes cost control"), FACTS))
        self.assertIn("(Full-stack, Jan 2027)",
                      application_subject(inputs(one_liner="A marketplace for tutors"), FACTS))

    def test_founders_and_people_keep_the_conversational_subject(self):
        for email in ("founders@acme.example", "ravi@acme.example"):
            d = template_draft(inputs(to_email=email, contact_name="Ravi K" if "ravi" in email else ""), FACTS)
            self.assertEqual(d.subject, "January 2027 intern or 2027 new grad at Acme?")

    def test_validation_rejects_a_conversational_subject_to_info(self):
        i = inputs(to_email="info@acme.example", confidence="published_inbox", contact_group="general_inbox")
        d = template_draft(i, FACTS)
        fails = validate("January 2027 intern or 2027 new grad at Acme?", d.body, i, FACTS, d.resume_action)
        self.assertTrue(any("must read as an application" in f for f in fails), fails)
        locked = inputs(to_email="info@acme.example", rules="2b")
        self.assertEqual(validate("January 2027 intern or 2027 new grad at Acme?",
                                  template_draft(locked, FACTS).body, locked, FACTS, "link"), [])


SIGNAL = {"round": "Series A", "amount": "$8 Mn", "date": "2026-09-03", "source_url": "https://inc42.com/buzz/x",
          "recent": "yes"}


class FundingMentionTests(unittest.TestCase):
    def body_with(self, i, sentence):
        d = template_draft(i, FACTS)
        return d, d.body.replace("I'm Your, a final-year", f"{sentence} I'm Your, a final-year", 1)

    def test_template_congratulates_only_with_a_sourced_recent_signal(self):
        d = template_draft(inputs(to_email="ravi@acme.example", contact_name="Ravi K", funding=SIGNAL), FACTS)
        self.assertIn("Congratulations on the Series A announced in September 2026.", d.body)
        self.assertEqual(d.template_failures, [])
        self.assertNotIn("Congratulations", template_draft(inputs(to_email="ravi@acme.example"), FACTS).body)
        old = dict(SIGNAL, recent="")
        self.assertEqual(funding_line(inputs(funding=old)), "")             # 6-12 months: not in the template

    def test_raises_the_bar_is_not_a_funding_mention(self):
        i = inputs(to_email="ravi@acme.example", contact_name="Ravi K")
        d, body = self.body_with(i, "Running real loan books raises the bar for correctness.")
        self.assertEqual(validate(d.subject, body, i, FACTS, d.resume_action), [])

    def test_a_fundraising_product_is_not_a_funding_mention(self):
        i = inputs(to_email="hello@give.example", one_liner="The fundraising platform for schools", company="Give")
        d = template_draft(i, FACTS)
        self.assertIn("fundraising platform", d.body)
        self.assertEqual(d.template_failures, [])

    def test_long_company_names_still_fit(self):
        i = inputs(to_email="james@cp.example", contact_name="James", company="Community Phone Company",
                   one_liner="The phone that gives families peace of mind about aging parents", remote_global=True)
        self.assertEqual(template_draft(i, FACTS).template_failures, [])

    def test_mention_without_a_signal_fails(self):
        i = inputs(to_email="ravi@acme.example", contact_name="Ravi K")
        d, body = self.body_with(i, "Congratulations on the recent raise.")
        fails = validate(d.subject, body, i, FACTS, d.resume_action)
        self.assertTrue(any("no sourced funding signal" in f for f in fails), fails)

    def test_wrong_round_or_month_fails_right_one_passes(self):
        i = inputs(to_email="ravi@acme.example", contact_name="Ravi K", funding=SIGNAL)
        d = template_draft(i, FACTS)
        wrong = d.body.replace("the Series A announced in September 2026", "the Series B announced in August 2026")
        fails = validate(d.subject, wrong, i, FACTS, d.resume_action)
        self.assertTrue(any("'Series B'" in f for f in fails), fails)
        self.assertTrue(any("'August 2026'" in f for f in fails), fails)
        self.assertEqual(validate(d.subject, d.body, i, FACTS, d.resume_action), [])

    def test_amount_must_match_the_signal(self):
        i = inputs(to_email="ravi@acme.example", contact_name="Ravi K", funding=SIGNAL)
        d = template_draft(i, FACTS)
        body = d.body.replace("announced in September 2026.", "announced in September 2026 ($9 Mn).")
        self.assertTrue(any("number '9'" in f for f in validate(d.subject, body, i, FACTS, d.resume_action)))


class DraftLockTests(unittest.TestCase):
    def test_targets_queued_before_2c_are_locked_to_the_2b_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            store = Store(path)
            store.conn.execute("DROP TABLE draft_rules_done")              # as if the DB predates Phase 2c
            store.conn.execute("DELETE FROM draft_rules")
            store.save_queue("2026-10-21", [("co-singlestore", "pkhaitan@singlestore.com", 1)], keep=set())
            store.conn.commit()
            store.close()
            store = Store(path)
            self.assertEqual(store.draft_rules(), {"co-singlestore": "2b"})
            store.save_queue("2026-10-22", [("yc-other", "a@b.c", 1)], keep=set())
            store.close()
            store = Store(path)
            self.assertEqual(store.draft_rules(), {"co-singlestore": "2b"})       # only once
            store.close()


# ---------------------------------------------------------------------------
# Queue ordering
# ---------------------------------------------------------------------------

def pool_company_store():
    """Three tier-2 general-outreach targets: different hiring likelihood and inbox kinds."""
    from applyflow.pool import Scored as S
    store = Store(":memory:")
    rows = []
    for key, name, email, score in (("pool:low.example", "Low", "founders@low.example", 40),
                                    ("pool:info.example", "InfoCo", "info@info.example", 70),
                                    ("pool:hello.example", "HelloCo", "hello@hello.example", 70)):
        domain = key.split(":")[1]
        target = Target(key=key, company=name, kind="general_outreach", hint="founder or CTO",
                        website=f"https://{domain}", domain=domain, team_size=20)
        group = classify_address(email)[0]
        store.save_contact_result(ContactResult(target=target, status="found", contacts=[
            Contact(name="", role="inbox", email=email, confidence="published_inbox", source="website",
                    source_url=f"https://{domain}", group=group)]))
        rows.append(S(company(name, domain=domain, team_size=20), score, [f"+{score} test"], True))
    store.save_pool(rows)
    return store


class QueueOrderTests(unittest.TestCase):
    def test_bounce_risk_then_hiring_likelihood_within_tiers_1_2(self):
        # 2d: founders@ (lower bounce risk) before hello@, info@ last; hiring likelihood only breaks ties.
        items = build_items(pool_company_store(), PROFILE)
        self.assertEqual([it.company for it in items], ["Low", "HelloCo", "InfoCo"])
        self.assertEqual([it.hl_score for it in items], [40, 70, 70])

    def test_earlier_stored_batch_holds_its_targets(self):
        store = pool_company_store()
        make_queue(store, PROFILE, date(2026, 10, 21), size=1)
        later = make_queue(store, PROFILE, date(2026, 10, 22), save=False)
        self.assertEqual([it.company for it in later.items], ["HelloCo"])      # InfoCo: info@ is unsendable (2e)
        self.assertEqual(later.held_earlier, 1)


if __name__ == "__main__":
    unittest.main()
