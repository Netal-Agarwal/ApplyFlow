"""Phase 2c.1: large companies with no headcount (late rounds, big raises, the never_cold_email list), the
tech-fit floor for funding-news-only companies, non-software businesses and investment firms, and the offline
re-selection that refills dropped slots while keeping the pool size and protected (queued) targets."""

import copy
import unittest
from datetime import date

from applyflow.contacts import Contact, ContactResult, Target
from applyflow.outreach import build_items, pick_batch
from applyflow.pool import (Scored, _amount_mn, exclusion, large_reason, late_round, never_cold_email,
                            pool_target_hint, product_fit, reselect, score_company, select)
from applyflow.scoring import APPLY_ONLY_HINT
from applyflow.sources import FundingRound, PoolCompany
from applyflow.store import Store
from tests.helpers import example_profile, fixture

PROFILE = example_profile()
TODAY = date(2026, 10, 20)
HOMEPAGES = {k: v for k, v in fixture("pool/homepages_2c1.json").items() if not k.startswith("_")}


def site(key):
    h = HOMEPAGES[key]
    return " ".join([h["homepage_title"], h["meta_description"], h["snippet"]])


def company(name, domain=None, sources=("portfolio:x",), **kw):
    domain = domain or f"{name.lower().replace(' ', '')}.example"
    return PoolCompany(key=f"pool:{domain}", name=name, website=f"https://{domain}", domain=domain,
                       sources=list(sources), **kw)


def rnd(round_name="", amount="", subsector="", sector=""):
    return [FundingRound(company="x", round=round_name, amount=amount, date="2026-09-01", sector=sector,
                         subsector=subsector, source="inc42", source_url="https://inc42.com/buzz/x")]


def profile_with(**pool):
    p = copy.deepcopy(PROFILE)
    for k, v in pool.items():
        if k == "never_cold_email":
            p.never_cold_email = v
        else:
            p.pool[k] = v
    return p


# ---------------------------------------------------------------------------
# 1. Large companies with no headcount
# ---------------------------------------------------------------------------

class AmountTests(unittest.TestCase):
    def test_thousands_millions_billions(self):
        self.assertEqual(_amount_mn("$250K"), 0.25)                   # 2c read this as $250 Mn
        self.assertEqual(_amount_mn("$5 Mn"), 5)
        self.assertEqual(_amount_mn("$1.2 Bn"), 1200)
        self.assertEqual(_amount_mn(""), 0)
        self.assertEqual(_amount_mn("INR 40 Cr"), 0)                  # not converted

    def test_a_seed_round_in_thousands_is_not_large(self):
        self.assertEqual(large_reason(company("Tiny", funding=rnd("Seed", "$250K")), PROFILE), "")


class LateRoundTests(unittest.TestCase):
    def test_series_c_or_later(self):
        for r in ("Series C", "Series E", "Series H", "Series C*", "pre-IPO"):
            self.assertTrue(late_round(r), r)
        for r in ("Pre-Series C", "Series B", "Seed", "Pre-Series A", "Debt", ""):
            self.assertFalse(late_round(r), r)

    def test_threshold_is_configurable(self):
        self.assertFalse(late_round("Series C", "D"))
        self.assertTrue(late_round("Series D", "D"))


class LargeCompanyTests(unittest.TestCase):
    def test_series_c_and_big_raise_with_unknown_size(self):
        self.assertIn("Series C round with no headcount", large_reason(company("A", funding=rnd("Series C")), PROFILE))
        self.assertIn("raised $63 Mn in one round ($50 Mn+)",
                      large_reason(company("B", funding=rnd("Series B", "$63 Mn")), PROFILE))
        self.assertEqual(large_reason(company("C", funding=rnd("Series B", "$30 Mn")), PROFILE), "")

    def test_known_headcount_wins_over_funding(self):
        self.assertEqual(large_reason(company("A", team_size=80, funding=rnd("Series D", "$90 Mn")), PROFILE), "")

    def test_amount_threshold_is_configurable(self):
        p = profile_with(large_company={"min_series": "C", "min_amount_mn": 100})
        self.assertEqual(large_reason(company("B", funding=rnd("Series B", "$63 Mn")), p), "")

    def test_large_means_apply_only_and_not_selected(self):
        p = profile_with(target_size=5)
        big = company("BigCo", funding=rnd("Series C", "$40 Mn"))
        self.assertEqual(pool_target_hint(big, p), APPLY_ONLY_HINT)
        out = {s.company.name: s for s in select([Scored(big, 90, []), Scored(company("Small"), 50, [])], {}, p, 0)}
        self.assertTrue(out["BigCo"].selected)               # 2f: selected for its own hiring inbox only
        self.assertIn("selected: large (Series C round", out["BigCo"].why_selected)
        self.assertIn("hiring inbox only", out["BigCo"].why_selected)
        self.assertTrue(out["Small"].selected)
        reasons = score_company(big, p, TODAY)[1]
        self.assertTrue(any("team size unknown, but Series C round" in r for r in reasons))


class NeverColdEmailTests(unittest.TestCase):
    P = profile_with(never_cold_email=["Darwinbox", "bigcorp.com"])

    def test_matches_by_name_or_domain_whatever_the_size(self):
        self.assertEqual(never_cold_email("Darwinbox", "darwinbox.com", self.P), "Darwinbox")
        self.assertEqual(never_cold_email("DarwinBox", "", self.P), "Darwinbox")
        self.assertEqual(never_cold_email("Big Corp", "careers.bigcorp.com", self.P), "bigcorp.com")
        self.assertEqual(never_cold_email("Darwin Labs", "darwinlabs.io", self.P), "")
        c = company("Darwinbox", domain="darwinbox.com", team_size=40, sources=("funding:inc42",),
                    funding=rnd(subsector="Horizontal SaaS"))
        self.assertEqual(large_reason(c, self.P), "on the never_cold_email list (Darwinbox)")
        self.assertEqual(exclusion(c, self.P)[0], "large")

    def test_a_looked_up_target_becomes_apply_only_unless_queued(self):
        store = Store(":memory:")
        _save_check(store, "pool:darwinbox.com", "Darwinbox", "careers@darwinbox.com")
        _save_check(store, "pool:bigcorp.com", "Big Corp", "careers@bigcorp.com")
        store.save_pool([Scored(company("Darwinbox", domain="darwinbox.com"), 70, ["+70 test"], True),
                         Scored(company("Big Corp", domain="bigcorp.com"), 70, ["+70 test"], True)])
        store.save_queue("2026-10-21", [("pool-bigcorp-com", "careers@bigcorp.com", 1)], keep=set())
        items = {it.target_id: it for it in build_items(store, self.P, day=TODAY)}
        self.assertEqual(items["pool-darwinbox-com"].status, "apply_only")
        self.assertIn("never_cold_email", items["pool-darwinbox-com"].status_note)
        self.assertEqual(items["pool-bigcorp-com"].status, "ready")             # already queued: never touched


# ---------------------------------------------------------------------------
# 2. Tech-fit floor and non-software businesses
# ---------------------------------------------------------------------------

class ProductFitTests(unittest.TestCase):
    def fit(self, c, key=""):
        return product_fit(c, PROFILE, site(key) if key else "")[0]

    def test_investment_firm_is_dropped(self):
        c = company("Redwood Capital", domain="redwood.capital", sources=("funding:inc42",),
                    funding=rnd("", "$20 Mn", "Investment Tech", "Fintech"))
        why = self.fit(c, "pool:redwood.capital")
        self.assertIn("investment firm", why)
        self.assertIn("'capital' in the name", why)

    def test_hardware_maker_is_dropped(self):
        c = company("GyroNav", domain="gyronav.example", sources=("funding:inc42",),
                    funding=rnd("Pre-Series B", "$4.7 Mn", "Space Tech", "Advanced Hardware & Technology"))
        why = self.fit(c, "pool:gyronav.example")
        self.assertIn("not a software-product business", why)
        self.assertIn("hardware", why)

    def test_hardware_with_a_software_product_on_its_site_passes(self):
        c = company("Sensorly", domain="sensorly.example", sources=("funding:inc42",),
                    funding=rnd("Seed", "$2 Mn", "Internet Of Things (IoT) & Hardware", "Advanced Hardware & Technology"))
        why, notes = product_fit(c, PROFILE, site("pool:sensorly.example"))
        self.assertEqual(why, "")
        self.assertTrue(any("but it names a software product" in n for n in notes))

    def test_logistics_operator_with_a_platform_word_is_still_dropped(self):
        c = company("Roadrunner", domain="roadrunner.example", sources=("funding:inc42",),
                    funding=rnd("Series A", "$1 Mn", "Logistic SaaS", "Logistics"))
        self.assertIn("logistics", self.fit(c, "pool:roadrunner.example"))

    def test_funding_only_floor(self):
        one_word = company("Circlet", sources=("funding:inc42",), funding=rnd("Seed", "$1 Mn", "Recommerce", "AI"))
        self.assertIn("funding news only, tech fit +10 below the floor (20)", self.fit(one_word))
        saas = company("LedgerAPI", domain="ledgerapi.example", sources=("funding:inc42",),
                       funding=rnd("Seed", "$1 Mn", "Fintech SaaS", "Fintech"))
        self.assertEqual(self.fit(saas, "pool:ledgerapi.example"), "")
        self.assertEqual(self.fit(saas), "")                          # "fintech" + a software subsector

    def test_an_ai_application_startup_with_no_readable_site_passes(self):
        c = company("Runner AI", sources=("funding:inc42",), funding=rnd("Seed", "$3 Mn", "Application Layer", "AI"))
        self.assertEqual(self.fit(c), "")

    def test_floor_is_configurable_and_only_for_funding_news(self):
        c = company("Circlet", sources=("funding:inc42",), funding=rnd("Seed", "$1 Mn", "Recommerce", "AI"))
        self.assertEqual(product_fit(c, profile_with(funding_only_min_tech=10))[0], "")
        portfolio = company("Circlet", sources=("portfolio:antler", "funding:inc42"),
                            funding=rnd("Seed", "$1 Mn", "Recommerce", "AI"))
        self.assertEqual(self.fit(portfolio), "")

    def test_yc_companies_are_exempt(self):
        c = company("Drone Co", sources=("yc",), description="Drone hardware for agriculture")
        self.assertEqual(self.fit(c), "")

    def test_select_drops_them_with_the_reason(self):
        cs = [Scored(company("GyroNav", domain="gyronav.example", sources=("funding:inc42",),
                             funding=rnd(subsector="Space Tech", sector="Advanced Hardware & Technology")), 70, []),
              Scored(company("LedgerAPI", domain="ledgerapi.example", sources=("funding:inc42",),
                             funding=rnd(subsector="Fintech SaaS", sector="Fintech")), 60, [])]
        out = {s.company.name: s for s in select(cs, {}, PROFILE, 0, {k: site(k) for k in HOMEPAGES})}
        self.assertFalse(out["GyroNav"].selected)
        self.assertTrue(out["GyroNav"].why_selected.startswith("not selected: tech-fit floor - not a software"))
        self.assertTrue(out["LedgerAPI"].selected)


class BugReportAddressTests(unittest.TestCase):
    def test_bug_report_inboxes_are_tool_addresses(self):
        from applyflow.contacts import classify_address
        for email in ("reportbug@m2pfintech.com", "bugs@acme.example", "bugreport-india@acme.example"):
            self.assertIsNone(classify_address(email), email)
        self.assertIsNotNone(classify_address("careers@acme.example"))


# ---------------------------------------------------------------------------
# 3. Re-selection refills dropped slots
# ---------------------------------------------------------------------------

def _save_check(store, key, name, email, confidence="published_inbox"):
    domain = key.split(":", 1)[1]
    target = Target(key=key, company=name, kind="general_outreach", hint="founder or CTO",
                    website=f"https://{domain}", domain=domain)
    store.save_contact_result(ContactResult(target=target, status="found", contacts=[
        Contact(name="", role="inbox", email=email, confidence=confidence, source="website",
                source_url=f"https://{domain}", group="hiring_inbox")]))


def refill_store():
    """target_size 4: three looked-up targets (one fails the rules, one fails but is queued), five candidates."""
    store = Store(":memory:")
    hw = dict(sources=["funding:inc42"], funding=rnd("Seed", "$1 Mn", "Space Tech", "Advanced Hardware & Technology"))
    rows = [Scored(company("Good", sources=["yc"]), 80, ["+80"], True),
            Scored(company("GyroNav", domain="gyronav.example", **hw), 75, ["+75"], True),
            Scored(company("Queued Hardware", domain="qhw.example", **hw), 74, ["+74"], True),
            Scored(company("Next India", sources=["portfolio:axilor"]), 60, ["+60"]),
            Scored(company("Next Remote", sources=["yc"], region="remote_global"), 65, ["+65"]),
            Scored(company("Old List", sources=["list:bangalore-startups"]), 34, ["+34"]),
            Scored(company("Tied Portfolio", sources=["portfolio:antler"]), 34, ["+34"]),
            Scored(company("Big Raise", funding=rnd("Series B", "$80 Mn")), 90, ["+90"])]
    store.save_pool(rows)
    _save_check(store, "pool:good.example", "Good", "careers@good.example")
    _save_check(store, "pool:gyronav.example", "GyroNav", "hr@gyronav.example")
    _save_check(store, "pool:qhw.example", "Queued Hardware", "hr@qhw.example")
    store.save_context("pool:gyronav.example", {"title": HOMEPAGES["pool:gyronav.example"]["homepage_title"],
                                                "description": HOMEPAGES["pool:gyronav.example"]["meta_description"]})
    store.save_queue("2026-10-21", [("pool-qhw-example", "hr@qhw.example", 1)], keep=set())
    return store


class ReselectTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile_with(target_size=4, remote_global_cap=0)
        self.store = refill_store()
        self.scored, self.changes = reselect(self.store, self.profile, TODAY)
        self.rows = {r["name"]: r for r in self.store.pool_rows()}

    def test_pool_size_is_kept(self):
        # 4 slots, plus Big Raise: a large company is selected for its own hiring inbox only (2f), outside the slots
        self.assertEqual(sum(r["selected"] for r in self.rows.values()), 5)

    def test_failing_target_dropped_and_next_best_added(self):
        self.assertFalse(self.rows["GyroNav"]["selected"])
        self.assertIn("tech-fit floor", self.rows["GyroNav"]["why_selected"])
        self.assertTrue(self.rows["Next India"]["selected"])
        self.assertEqual({c.company.name for c in self.changes if c.was_selected}, {"GyroNav"})
        self.assertEqual({c.company.name for c in self.changes if not c.was_selected},
                         {"Next India", "Tied Portfolio", "Big Raise"})

    def test_queued_target_is_protected(self):
        self.assertTrue(self.rows["Queued Hardware"]["selected"])
        self.assertIn("queued / locked", self.rows["Queued Hardware"]["why_selected"])

    def test_large_and_remote_cap_respected_and_ties_go_to_portfolios(self):
        self.assertIn("hiring inbox only", self.rows["Big Raise"]["why_selected"])   # 2f
        self.assertFalse(self.rows["Next Remote"]["selected"])          # cap 0 keeps the India / remote balance
        self.assertTrue(self.rows["Tied Portfolio"]["selected"])        # 34 = 34: portfolio before the old list
        self.assertFalse(self.rows["Old List"]["selected"])

    def test_dropped_target_is_not_queued(self):
        items = build_items(self.store, self.profile, day=TODAY)
        by = {it.company: it for it in items}
        self.assertEqual(by["GyroNav"].status, "dropped")
        self.assertIn("tech-fit floor", by["GyroNav"].status_note)
        self.assertEqual(by["Queued Hardware"].status, "ready")
        batch = pick_batch(items, date(2026, 10, 22), 10, {}, self.profile.outreach["tier3"])
        self.assertNotIn("GyroNav", [it.company for it in batch.items])


if __name__ == "__main__":
    unittest.main()
