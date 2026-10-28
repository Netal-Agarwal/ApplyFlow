"""Phase 2g: seed lists at scale - consultancies (client software work), mid-size product / SaaS companies (sector
saas), fintech seeds - verified on their own websites, and the FACE member list."""

import json
import unittest

from applyflow.drafting import DraftInputs
from applyflow.pool import Scored, priority_2f, select
from applyflow.sources import PoolCompany, pool_sector
from applyflow.http import FetchError
from applyflow.supply import collect_seeds, parse_sahamati, seed_fit, sector_for, www_retry
from tests.helpers import FIXTURES, FakeClient, example_profile

PROFILE = example_profile()


class OkMX:
    def __init__(self, bad=()):
        self.bad = set(bad)

    def check(self, domain):
        return "no MX" if domain in self.bad else "ok"


def home(title: str, body: str, description: str = "") -> str:
    return (f"<html><head><title>{title}</title><meta name=\"description\" content=\"{description}\"></head>"
            f"<body><main>{body}</main></body></html>")


CONSULTANCY = home("Brightloop Labs | Product engineering partner",
                   "We build web and mobile products for our clients. Case studies. Offices in Pune, India.")
STAFFING = home("Hirewave Solutions | Talent",
                "Hirewave places contract staff with enterprises. Offices in Pune, India.")
SAAS = home("Ledgerly - invoicing software for Indian SMBs",
            "Ledgerly is a SaaS platform with GST-ready invoicing and an API. Book a demo. Bengaluru, India.")
ABROAD = home("Northwind Dev | Software development company",
              "Custom software development for clients in Ohio.")


class FaceListTests(unittest.TestCase):
    def test_member_sites_without_the_association_its_form_or_socials(self):
        page = (FIXTURES / "supply" / "face_members.html").read_text(encoding="utf-8")
        self.assertEqual(parse_sahamati(page), ["https://finvu.in", "https://decentro.tech", "https://kissht.com"])


class SeedFitTests(unittest.TestCase):
    def test_consultancy_needs_client_software_work(self):
        self.assertEqual(seed_fit("consultancy", {"title": "Brightloop"}, "We build apps for our clients"), "")
        self.assertEqual(seed_fit("consultancy", {"description": "Offshore software development company"}), "")
        self.assertIn("client-work", seed_fit("consultancy", {"title": "Hirewave"}, "We place contract staff"))

    def test_saas_and_fintech_need_a_product(self):
        self.assertEqual(seed_fit("saas", {"title": "Ledgerly"}, "A SaaS platform. Book a demo."), "")
        self.assertEqual(seed_fit("fintech", {"description": "Payments API for merchants"}), "")
        self.assertIn("product", seed_fit("saas", {"title": "Ledgerly"}, "We are hiring. Contact us."))


class CollectSeedsTests(unittest.TestCase):
    def test_verified_consultancy_kept_with_size_and_sector(self):
        client = FakeClient({}, {"https://brightloop.example": CONSULTANCY, "https://hirewave.example": STAFFING,
                                 "https://northwind.example": ABROAD})
        seeds = [{"name": "Brightloop Labs", "website": "brightloop.example", "team_size": 120, "locations": "Pune"},
                 {"name": "Hirewave Solutions", "website": "hirewave.example", "team_size": 80},
                 {"name": "Northwind Dev", "website": "northwind.example"},
                 {"name": "Gone Co", "website": "gone.example"}]
        got, stats = collect_seeds(client, OkMX(), seeds, PROFILE, "consultancy")
        self.assertEqual([c.name for c in got], ["Brightloop Labs"])
        c = got[0]
        self.assertEqual((c.sector, c.sources, c.team_size, c.locations, c.region),
                         ("consultancy", ["consultancy:seed"], 120, "Pune", "india"))
        self.assertTrue(c.context.get("title"))
        self.assertNotIn("text", c.context)                     # the homepage text isn't stored as context
        dropped = " | ".join(stats["dropped"])
        self.assertIn("Hirewave Solutions: no client-work words", dropped)
        self.assertIn("Northwind Dev: no India office", dropped)
        self.assertIn("Gone Co: homepage failed", dropped)
        self.assertEqual((stats["found"], stats["kept"]), (4, 1))

    def test_saas_seed_sector_and_mx(self):
        client = FakeClient({}, {"https://ledgerly.example": SAAS})
        seeds = [{"name": "Ledgerly", "website": "ledgerly.example", "team_size": 900}]
        got, _ = collect_seeds(client, OkMX(), seeds, PROFILE, "saas")
        self.assertEqual((got[0].sector, got[0].sources, got[0].tags),
                         ("saas", ["saas:seed"], ["Indian product / SaaS company"]))
        none, stats = collect_seeds(client, OkMX(bad={"ledgerly.example"}), seeds, PROFILE, "saas")
        self.assertEqual(none, [])
        self.assertIn("MX no MX", stats["dropped"][0])

    def test_cert_failure_on_the_bare_domain_retried_once_on_www(self):
        cert = FetchError("https://brightloop.example", "site unreachable (robots.txt: SSLError("
                          "SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')))")
        client = FakeClient({}, {"https://brightloop.example": cert, "https://www.brightloop.example": CONSULTANCY})
        got, _ = collect_seeds(client, OkMX(), [{"name": "Brightloop Labs", "website": "brightloop.example"}], PROFILE)
        self.assertEqual((got[0].website, got[0].domain), ("https://www.brightloop.example", "brightloop.example"))
        self.assertEqual(client.page_calls[:2], ["https://brightloop.example", "https://www.brightloop.example"])
        # still failing on www. -> dropped; other errors (DNS, 404) and www. hosts get no retry
        client = FakeClient({}, {"https://brightloop.example": cert, "https://www.brightloop.example": cert})
        got, stats = collect_seeds(client, OkMX(), [{"name": "Brightloop Labs", "website": "brightloop.example"}],
                                   PROFILE)
        self.assertEqual(got, [])
        self.assertIn("www.brightloop.example too", stats["dropped"][0])
        self.assertEqual(www_retry("https://x.example", FetchError("u", "NameResolutionError")), "")
        self.assertEqual(www_retry("https://www.x.example", cert), "")

    def test_it_services_giants_never_fetched(self):
        client = FakeClient({}, {})
        got, stats = collect_seeds(client, OkMX(), [{"name": "Infosys", "website": "infosys.com"}], PROFILE)
        self.assertEqual(got, [])
        self.assertIn("IT services", stats["dropped"][0])
        self.assertEqual(client.page_calls, [])


class IndiaEngineeringTests(unittest.TestCase):
    def test_office_or_engineering_in_an_indian_city_counts_customers_do_not(self):
        from applyflow.supply import india_engineering
        home_ = home("Ledgr | Payments for SaaS", "Trusted by merchants in India and the US. Pricing in INR.")
        careers = home("Careers", "Our engineering team is based in Bengaluru and San Francisco.")
        client = FakeClient({}, {"https://ledgr.example": home_, "https://ledgr.example/careers": careers})
        self.assertIn("/careers: engineering team is based in Bengaluru", india_engineering(client, "ledgr.example"))
        client = FakeClient({}, {"https://ledgr.example": home_})
        self.assertEqual(india_engineering(client, "ledgr.example"), "")


class CareersPageNoiseTests(unittest.TestCase):
    def test_closed_community_people_and_other_stacks_are_not_openings(self):
        from applyflow.refill import careers_page_roles
        text = "\n".join(["Flutter Developer Closed", "Join our developer community", "Developer Hub",
                          "Alok Kumar SDE - I", "Salesforce Developer", "Software Developer (DotNet)",
                          "React Native Developer", "Python Developer", "SDE 1, Backend", "Java Developer"])
        fits, _ = careers_page_roles(text, PROFILE)
        self.assertEqual(fits, ["SDE 1, Backend", "Python Developer", "Java Developer"])    # best fit first
        links = "\n".join(["DEVELOPER PORTAL", "Developer docs", "Shape the future of developer tooling",
                           "Onboard a Pre-Vetted Developer in 72 Hours", "Dedicated Developer", "Developer",
                           "CPQ Developer", "Python / FastAPI Backend Developer"])
        self.assertEqual(careers_page_roles(links, PROFILE)[0], ["Python / FastAPI Backend Developer"])
        from applyflow.refill import clean_role_title
        self.assertEqual(clean_role_title("CLO-2026-099 - Backend AI Engineer"), "Backend AI Engineer")
        self.assertEqual(clean_role_title("AL/ML Engineer Noida"), "AL/ML Engineer")
        self.assertEqual(careers_page_roles("AngularJS Developer", PROFILE)[0], [])


class EvidenceNameTests(unittest.TestCase):
    def test_headings_and_role_inboxes_are_not_people(self):
        from applyflow.contacts import NOT_NAMES, people_in_text
        people = people_in_text("Customer Testimonials\nPresident & CEO\nPrashant Halari\nCEO & Managing Director",
                                "https://s.example/team")
        self.assertEqual([p.name for p in people], ["Prashant Halari"])
        for local in ("enterprise", "customer", "clients"):
            self.assertIn(local, NOT_NAMES)                      # enterprise@ never backs a first@ guess


class SaasSelectionTests(unittest.TestCase):
    def company(self, **kw):
        base = dict(key="pool:l.example", name="Ledgerly", website="https://l.example", domain="l.example",
                    sources=["saas:seed"], sector="saas", region="india",
                    description="SaaS invoicing platform with an API for Indian SMBs")
        base.update(kw)
        return PoolCompany(**base)

    def test_sector_and_priority(self):
        self.assertEqual(pool_sector(PoolCompany(key="k", name="L", website="", domain="", sources=["saas:seed"])),
                         "saas")
        self.assertIn("SaaS", priority_2f(self.company(), PROFILE))
        self.assertEqual(priority_2f(self.company(region="remote_global"), PROFILE), "")

    def test_selected_past_target_size_and_large_ones_hiring_inbox_only(self):
        small = self.company(team_size=120)
        big = self.company(key="pool:b.example", domain="b.example", name="Bigly", team_size=900)
        out = {s.company.name: s for s in select([Scored(small, 10, []), Scored(big, 10, [])], {}, PROFILE, 10**6)}
        self.assertTrue(out["Ledgerly"].selected)
        self.assertIn("SaaS", out["Ledgerly"].why_selected)
        self.assertTrue(out["Bigly"].selected)
        self.assertIn("hiring inbox only", out["Bigly"].why_selected)

    def test_target_sector_from_the_pool_record(self):
        inputs = DraftInputs(target_id="a", company_key="pool:l.example", company="Ledgerly", kind="general_outreach")
        row = {"data": json.dumps(self.company().to_json())}
        self.assertEqual(sector_for("Ledgerly", row, inputs, 120), "saas")


if __name__ == "__main__":
    unittest.main()
