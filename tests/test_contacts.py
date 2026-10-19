"""Phase 2a: contact finding. Offline, against fixtures mirroring real pages and the GitHub REST API."""

import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applyflow.config import PROFILE_EXAMPLE_PATH
from applyflow.contacts import (DoNotContact, GitHubBudget, Target, build_targets, find_contacts, name_parts,
                                pattern_of, people_in_text, title_names_other_org)
from applyflow.http import PoliteClient
from applyflow.models import Company, DiscoveredCompany, Job
from applyflow.pipeline import score_all
from applyflow.report import write_contacts_csv
from applyflow.scoring import MARKETPLACE_HINT, score_job
from applyflow.store import Store
from tests.helpers import FIXTURES, TODAY, example_profile

C = FIXTURES / "contacts"
ACME = "https://acmerobotics.example"
GH = "https://api.github.com"
RATE_OK = {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "57", "x-ratelimit-reset": "1793000000"}


def text(name):
    return (C / name).read_text(encoding="utf-8")


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self.headers = status, headers or {}
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Serves fixtures by URL (query string ignored); anything else is a 404."""

    def __init__(self, routes):
        self.routes, self.headers, self.urls = routes, {}, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.urls.append(url)
        value = self.routes.get(url.rstrip("/") if url.count("/") > 3 else url, self.routes.get(url))
        if value is None:
            return Resp(404, "not found")
        if isinstance(value, tuple):
            return Resp(*value)
        return Resp(200, value, RATE_OK if url.startswith(GH) else {})


def acme_routes():
    return {
        f"{ACME}/robots.txt": text("acme_robots.txt"),
        ACME: text("acme_home.html"),
        f"{ACME}/about": text("acme_about.html"),
        f"{ACME}/team": text("acme_team.html"),
        f"{ACME}/contact": text("acme_contact.html"),
        f"{ACME}/careers": "<p>Open roles: Backend Engineer Intern</p>",
        f"{GH}/orgs/acme-robotics/public_members": json.loads(text("github_public_members.json")),
        f"{GH}/users/kiran-rao-dev": json.loads(text("github_user_kiran.json")),
        f"{GH}/users/octo-helper": json.loads(text("github_user_octo.json")),
    }


def client(routes):
    session = FakeSession(routes)
    return PoliteClient("ApplyFlow-test", session=session, sleep=lambda s: None, min_interval=0), session


def target(hint="founder or CTO", domain="acmerobotics.example", website=ACME, **kw):
    return Target(key=kw.pop("key", "yc:acme-robotics"), company=kw.pop("company", "Acme Robotics"),
                  kind=kw.pop("kind", "role"), hint=hint, website=website, domain=domain, **kw)


def run(t, routes=None, dnc=None, github=None, profile=None, mx=None):
    c, session = client(routes if routes is not None else acme_routes())
    result = find_contacts(c, t, profile or example_profile(), dnc or DoNotContact(), github or GitHubBudget(), mx=mx)
    return result, session


class ConfidenceLevelTests(unittest.TestCase):
    def test_verified_from_posting_stops_early(self):
        job = Job("lever", "1", "Acme Robotics", "Backend Intern", f"{ACME}/jobs/1",
                  emails=["jobs@acmerobotics.example"])
        result, session = run(target(jobs=[job]))
        p = result.contacts[0]
        self.assertEqual((p.email, p.confidence, p.source, p.source_url),
                         ("jobs@acmerobotics.example", "verified", "posting", f"{ACME}/jobs/1"))
        self.assertFalse(any("acmerobotics" in u for u in session.urls))     # good enough: site never read

    def test_strong_from_confirmed_pattern(self):
        # priya.sharma@ is published for "Priya Sharma" -> first.last applied to the CTO named on /about.
        result, _ = run(target())
        arjun = next(c for c in result.considered if c.name == "Arjun Mehta")
        self.assertEqual((arjun.role, arjun.email, arjun.confidence, arjun.source),
                         ("Co-founder & CTO", "arjun.mehta@acmerobotics.example", "strong", "pattern"))
        self.assertEqual(arjun.note, "pattern first.last confirmed by priya.sharma@acmerobotics.example")
        self.assertEqual(arjun.alternates, [])                  # alternates are only for guesses
        # Deliverability first: the published (verified) addresses rank above the pattern ones.
        self.assertEqual([(c.email, c.confidence) for c in result.contacts],
                         [("priya.sharma@acmerobotics.example", "verified"),
                          ("kiran@acmerobotics.example", "verified"),
                          ("arjun.mehta@acmerobotics.example", "strong")])

    def test_guessed_when_no_pattern_is_confirmed(self):
        routes = {"https://betaledger.example": text("beta_home.html"),
                  "https://betaledger.example/company": text("beta_company.html")}
        result, _ = run(target(domain="betaledger.example", website="https://betaledger.example"), routes)
        # A published hiring inbox beats a guess; the guess stays as a backup with alternates (fix 4).
        self.assertEqual([(c.name, c.email, c.confidence) for c in result.contacts],
                         [("", "jobs@betaledger.example", "published_inbox"),
                          ("Neha Gupta", "neha@betaledger.example", "guessed")])
        self.assertEqual(result.contacts[1].alternates,
                         ["neha.gupta@betaledger.example", "nehagupta@betaledger.example"])

    def test_published_inbox_at_small_company_fallback_at_bigger(self):
        routes = {"https://gammahealth.example": text("gamma_home.html")}
        small, _ = run(target(domain="gammahealth.example", website="https://gammahealth.example"), routes)
        self.assertEqual([(c.email, c.confidence) for c in small.contacts],
                         [("careers@gammahealth.example", "published_inbox"),
                          ("hello@gammahealth.example", "published_inbox")])
        mid, _ = run(target(hint="engineering manager or tech lead", domain="gammahealth.example",
                            website="https://gammahealth.example"), routes)
        self.assertEqual({c.confidence for c in mid.contacts}, {"fallback"})

    def test_unrecognised_address_is_only_a_fallback(self):
        routes = {"https://tools.example": "<p>Bugs: <a href='mailto:jira@tools.example'>jira@tools.example</a>. "
                                           "Ops: anita.rao@tools.example. Ping rahul@tools.example. "
                                           "noreply@tools.example, help-desk@tools.example, billing@tools.example</p>"}
        result, _ = run(target(domain="tools.example", website="https://tools.example"), routes)
        got = {c.email: (c.confidence, c.role) for c in result.considered}
        self.assertEqual(got, {"anita.rao@tools.example": ("verified", "person at the company"),
                               "rahul@tools.example": ("fallback", "unrecognised address")})   # tool addresses out

    def test_none_lists_what_was_tried(self):
        routes = {"https://empty.example": "<html><body><p>Coming soon.</p></body></html>"}
        result, _ = run(target(domain="empty.example", website="https://empty.example"), routes)
        self.assertEqual((result.status, result.contacts), ("none", []))
        self.assertIn("nothing found", result.reasons)
        self.assertTrue(any(r.startswith("website: read 1 page(s)") for r in result.reasons))

    def test_no_domain_is_skipped(self):
        result, session = run(target(domain="", website=""))
        self.assertEqual(result.status, "no_domain")
        self.assertEqual(session.urls, [])


class SiteTests(unittest.TestCase):
    def test_robots_disallowed_page_not_fetched(self):
        result, session = run(target())
        self.assertNotIn(f"{ACME}/careers", session.urls)
        self.assertTrue(any("robots.txt disallowed /careers" in r for r in result.reasons))

    def test_people_and_titles(self):
        people = {p.name: p.role for p in people_in_text(
            "Meet the team\nRahul Verma\nCo-founder & CEO\nVikram Das\nRobotics Engineer\n"
            "Arjun Mehta, Co-founder & CTO\nOur Story\nHead of Engineering", "u")}
        self.assertEqual(people, {"Rahul Verma": "Co-founder & CEO", "Arjun Mehta": "Co-founder & CTO"})

    def test_testimonials_and_advisors_ignored(self):
        result, _ = run(target())
        names = [p.name for p in result.people]
        self.assertNotIn("Jane Doe", names)          # "CTO at Globex Logistics"
        self.assertNotIn("Adam Wiggins", names)      # "Founder - Heroku"
        self.assertNotIn("jane@acmerobotics.example", [c.email for c in result.considered])
        self.assertTrue(any(r.startswith("ignored Jane Doe (CTO at Globex Logistics)") for r in result.reasons))
        self.assertFalse(title_names_other_org("Co-founder & COO, Acme Robotics", "Acme Robotics"))
        self.assertFalse(title_names_other_org("CEO • Co-founder", "Acme Robotics"))
        self.assertTrue(title_names_other_org("CEO's Office", "Acme Robotics"))

    def test_no_guess_from_an_initial(self):
        self.assertEqual(name_parts("J Richards"), ("j", "richards"))
        self.assertEqual(name_parts("Priya K. Sharma"), ("priya", "sharma"))
        routes = {"https://landed.example": "<p>J Richards, Founder</p><p>Neha Gupta, Co-founder &amp; CEO</p>"}
        result, _ = run(target(domain="landed.example", website="https://landed.example"), routes)
        self.assertEqual([c.email for c in result.contacts], ["neha@landed.example"])

    def test_blog_links_and_role_inboxes_are_not_people(self):
        people = people_in_text("Survival Guide\nFounder's story\nNeha Gupta\nFounder", "u")
        self.assertEqual([p.name for p in people], ["Neha Gupta"])
        self.assertEqual(len(people_in_text("Survival Guide (Founder's story)", "u")), 0)
        from applyflow.contacts import classify_address
        self.assertIsNone(classify_address("help-events@supabase.example"))
        self.assertEqual(classify_address("priya.sharma@acme.example")[2], "verified")

    def test_patterns(self):
        self.assertEqual(pattern_of("priya.sharma", "Priya Sharma"), "first.last")
        self.assertEqual(pattern_of("psharma", "Priya Sharma"), "flast")
        self.assertEqual(pattern_of("p.sharma", "Priya Sharma"), "f.last")
        self.assertEqual(pattern_of("priya_sharma", "Priya Sharma"), "first_last")
        self.assertEqual(pattern_of("priyasharma", "Priya Sharma"), "firstlast")
        self.assertEqual(pattern_of("priya", "Priya Sharma"), "first")
        self.assertEqual(pattern_of("ps", "Priya Sharma"), "")


class RoleRankingTests(unittest.TestCase):
    def test_role_fit_orders_contacts_within_a_confidence_level(self):
        def strong(hint):
            result, session = run(target(hint=hint))
            return [c.name for c in result.considered if c.confidence == "strong"], result, session
        small, result, _ = strong("founder or CTO")
        mid, mid_result, session = strong("engineering manager or tech lead")
        large, _, _ = strong("recruiter or engineering manager")
        self.assertEqual(small, ["Arjun Mehta", "Rahul Verma", "Sana Khan"])      # CTO, CEO first
        self.assertEqual(mid, ["Arjun Mehta", "Rahul Verma", "Sana Khan"])        # CTO (fit 0) before CEO (1)
        self.assertEqual(large, ["Sana Khan", "Arjun Mehta", "Rahul Verma"])      # recruiter first
        self.assertEqual(mid_result.contacts[0].name, "Priya Sharma")             # verified beats strong
        self.assertIn("most deliverable (verified), then best role fit for 'founder or CTO'", result.why_primary)
        # The EM was found verified on the site, so GitHub wasn't needed.
        self.assertFalse(any(u.startswith(GH) for u in session.urls))

    def test_confidence_breaks_ties_within_a_role(self):
        result, _ = run(target(hint="engineering manager or tech lead"))
        fit0 = [(c.name, c.confidence) for c in result.considered if c.fit == 0]
        self.assertEqual(fit0, [("Priya Sharma", "verified"), ("Arjun Mehta", "strong")])
        ranks = [c.confidence for c in result.considered]
        order = ["verified", "strong", "published_inbox", "guessed", "fallback"]
        self.assertEqual(ranks, sorted(ranks, key=order.index))

    def test_keeps_top_three(self):
        result, _ = run(target())
        self.assertEqual(len(result.contacts), 3)
        self.assertGreater(len(result.considered), 3)


class YCPageTests(unittest.TestCase):
    def routes(self, robots):
        return {"https://gammahealth.example": text("gamma_home.html"),
                "https://www.ycombinator.com/robots.txt": text(robots),
                "https://www.ycombinator.com/companies/gamma-health": text("yc_company_gamma.html")}

    def gamma(self):
        return target(domain="gammahealth.example", website="https://gammahealth.example", yc_slug="gamma-health")

    def test_founders_from_yc_page(self):
        result, _ = run(self.gamma(), self.routes("yc_robots.txt"))
        self.assertIn("YC page: 2 founder(s)", result.reasons)
        guessed = [c for c in result.considered if c.confidence == "guessed"]
        self.assertEqual([(c.name, c.email, c.source_url) for c in guessed],
                         [("Kabir Rao", "kabir@gammahealth.example", "https://www.ycombinator.com/companies/gamma-health"),
                          ("Meera Iyer", "meera@gammahealth.example",
                           "https://www.ycombinator.com/companies/gamma-health")])
        self.assertEqual(result.contacts[0].email, "careers@gammahealth.example")    # published inbox first

    def test_skipped_when_robots_disallows(self):
        result, session = run(self.gamma(), self.routes("yc_robots_disallow.txt"))
        self.assertIn("YC page disallowed by robots.txt", result.reasons)
        self.assertNotIn("https://www.ycombinator.com/companies/gamma-health", session.urls)
        self.assertEqual(result.contacts[0].email, "careers@gammahealth.example")


class GitHubTests(unittest.TestCase):
    def test_member_with_public_company_email(self):
        result, session = run(target())
        gh = [c for c in result.considered if c.source == "github"]
        self.assertEqual([(c.name, c.email, c.confidence, c.source_url) for c in gh],
                         [("Kiran Rao", "kiran@acmerobotics.example", "verified", "https://github.com/kiran-rao-dev")])
        self.assertIn("GitHub org acme-robotics: 2 public member(s), 1 with an email on acmerobotics.example",
                      result.reasons)
        calls = [u for u in session.urls if u.startswith(GH)]
        self.assertEqual(len(calls), 3)

    def test_rate_limit_exhausted_is_recorded_and_respected(self):
        routes = acme_routes()
        routes[f"{GH}/orgs/acme-robotics/public_members"] = (
            403, {"message": "API rate limit exceeded for 203.0.113.7."},
            {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "4102444800"})
        budget = GitHubBudget()
        result, session = run(target(), routes, github=budget)
        self.assertTrue(budget.exhausted)
        self.assertTrue(any(r.startswith("GitHub org acme-robotics skipped: GitHub rate limit exhausted after 1")
                            for r in result.reasons))
        # A second company in the same run doesn't call GitHub again before the reset.
        again, session2 = run(target(), routes, github=budget)
        self.assertFalse(any(u.startswith(GH) for u in session2.urls))
        self.assertEqual(again.contacts[0].name, "Priya Sharma")      # other sources still work


class DoNotContactTests(unittest.TestCase):
    def test_excluded_everywhere_and_noted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dnc.txt"
            path.write_text("# comment\narjun.mehta@acmerobotics.example\n@gammahealth.example\n", encoding="utf-8")
            dnc = DoNotContact.load(path)
        result, _ = run(target(), dnc=dnc)
        self.assertNotIn("arjun.mehta@acmerobotics.example", [c.email for c in result.considered])
        self.assertEqual([c.name for c in result.considered if c.confidence == "strong"][0], "Rahul Verma")
        self.assertEqual(result.excluded, [("arjun.mehta@acmerobotics.example",
                                            "on the do-not-contact list (arjun.mehta@acmerobotics.example)")])
        gamma, _ = run(target(domain="gammahealth.example", website="https://gammahealth.example"),
                       {"https://gammahealth.example": text("gamma_home.html")}, dnc=dnc)
        self.assertEqual((gamma.status, len(gamma.excluded)), ("none", 2))
        self.assertTrue(any("do-not-contact" in r for r in gamma.reasons))

    def test_missing_file_blocks_nothing(self):
        self.assertEqual(DoNotContact.load(Path("does-not-exist.txt")).blocks("a@b.example"), "")


class MarketplaceTests(unittest.TestCase):
    def job(self, company="Weekday AI", description="0-1 years experience. Python, FastAPI, Node.js, AWS, REST APIs."):
        return Job("workable", "w1", company, "Backend Engineer - New Grad", "https://apply.workable.com/j/w1",
                   description=description, locations=["Bengaluru"], team_size=38)

    def test_marketplace_roles_stay_with_apply_action(self):
        r = score_job(self.job(), example_profile(), TODAY)
        self.assertTrue(r.accepted)
        self.assertEqual((r.action, r.contact_hint), ("apply", MARKETPLACE_HINT))
        self.assertTrue(any("recruiting marketplace" in x for x in r.reasons))

    def test_our_client_heuristic(self):
        r = score_job(self.job("Talent Bridge", "One of our clients, a Series A fintech, is hiring. 0-1 years "
                                               "experience. Python, FastAPI, AWS, REST APIs."), example_profile(), TODAY)
        self.assertEqual(r.contact_hint, MARKETPLACE_HINT)
        self.assertIn("posting is for a client company ('One of our clients')", r.reasons[-1])

    def test_client_boilerplate_is_not_a_marketplace(self):
        # Rubrik's EEO paragraph: "...driving greater innovation for our clients." is a normal employer.
        for text in ("Inclusion drives greater innovation for our clients. Python, AWS.",
                     "We protect our client's data. Python, AWS."):
            r = score_job(self.job("Rubrik", "0-1 years experience. " + text), example_profile(), TODAY)
            self.assertNotEqual(r.contact_hint, MARKETPLACE_HINT, text)
        r = score_job(self.job("Talent Bridge", "Our client, a Series A fintech, is hiring. Python, AWS."),
                      example_profile(), TODAY)
        self.assertEqual(r.contact_hint, MARKETPLACE_HINT)

    def test_never_a_contact_target(self):
        store = Store(":memory:")
        store.upsert_board("workable:weekday-1", [self.job()])
        store.upsert_board("workable:acme", [self.job("Acme Robotics")])
        score_all(store, example_profile(), TODAY)
        names = [t.company for t in build_targets(store, example_profile(), [])]
        self.assertEqual(names, ["Acme Robotics"])
        store.close()


class TargetTests(unittest.TestCase):
    def test_domains_and_general_outreach(self):
        store = Store(":memory:")
        desc = "0-1 years experience. Python, FastAPI, AWS, REST APIs, PostgreSQL."
        store.upsert_board("workable:acme", [Job("workable", "a1", "Acme Robotics", "Backend Engineer - New Grad",
                                                 "u1", description=desc, locations=["Bengaluru"])])
        store.upsert_board("hn:1", [Job("hn", "h1", "Lumen AI", "Backend Engineer Intern", "u2",
                                        description="Lumen AI | Backend Engineer Intern | Remote (India OK) | "
                                                    "https://lumen.example\n" + desc,
                                        locations=["Remote (India OK)"])])
        store.upsert_board("workable:nodomain", [Job("workable", "n1", "Nodomain", "Backend Engineer - New Grad",
                                                     "u3", description=desc, locations=["Bengaluru"])])
        for key, name, site, locations, status in (
                ("yc:beta", "Beta Ledger", "https://betaledger.example", "Pune, MH, India", "none"),
                ("yc:usco", "US Co", "https://us.example", "San Francisco, CA, USA", "none")):
            store.upsert_discovered(DiscoveredCompany(key=key, name=name, website=site, locations=locations,
                                                      team_size=20, board_status=status))
        score_all(store, example_profile(), TODAY)
        seeds = [Company("Acme Robotics", "workable", "acme", website="https://www.acmerobotics.example")]
        targets = {t.company: t for t in build_targets(store, example_profile(), seeds)}
        self.assertEqual(set(targets), {"Acme Robotics", "Lumen AI", "Nodomain", "Beta Ledger"})
        self.assertEqual((targets["Acme Robotics"].domain, targets["Acme Robotics"].domain_source),
                         ("acmerobotics.example", "seeds.yaml"))
        self.assertEqual((targets["Lumen AI"].domain, targets["Lumen AI"].domain_source), ("lumen.example", "HN header"))
        self.assertEqual(targets["Nodomain"].domain, "")
        beta = targets["Beta Ledger"]
        self.assertEqual((beta.kind, beta.domain, beta.yc_slug, beta.hint),
                         ("general_outreach", "betaledger.example", "beta", "founder or CTO"))
        self.assertEqual(targets["Acme Robotics"].kind, "role")
        self.assertTrue(targets["Acme Robotics"].roles[0].startswith("Backend Engineer - New Grad ("))
        store.close()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_thirty_day_recheck(self):
        result, _ = run(target())
        self.store.save_contact_result(result)
        self.assertTrue(self.store.contacts_checked_recently("yc:acme-robotics", 30))
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE contact_checks SET checked_at = ?", (old,))
        self.assertFalse(self.store.contacts_checked_recently("yc:acme-robotics", 30))
        no_domain, _ = run(target(key="co:x", domain="", website=""))
        self.store.save_contact_result(no_domain)
        self.assertFalse(self.store.contacts_checked_recently("co:x", 30))   # cheap to retry

    def test_contacts_table_and_csv(self):
        result, _ = run(target(roles=["Backend Engineer Intern (82)"], best_score=82, team_size=20))
        self.store.save_contact_result(result)
        rows = self.store.contacts_for("yc:acme-robotics")
        self.assertEqual([(r["rank"], r["is_primary"]) for r in rows], [(0, 1), (1, 0), (2, 0)])
        self.assertEqual(self.store.contact_counts(), {"verified": 1})
        with tempfile.TemporaryDirectory() as tmp:
            path = write_contacts_csv(self.store, Path(tmp) / "contacts.csv")
            with open(path, encoding="utf-8") as fh:
                row = next(csv.DictReader(fh))
        self.assertEqual((row["target_type"], row["company"], row["domain"], row["roles"], row["primary_name"],
                          row["primary_confidence"], row["backup1_name"], row["backup2_email"]),
                         ("role", "Acme Robotics", "acmerobotics.example", "Backend Engineer Intern (82)",
                          "Priya Sharma", "verified", "Kiran Rao", "arjun.mehta@acmerobotics.example"))

    def test_explain_contact_command(self):
        from applyflow.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            store = Store(db)
            result, _ = run(target())
            store.save_contact_result(result)
            store.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--db", str(db), "--profile", str(PROFILE_EXAMPLE_PATH), "explain-contact", "Acme"])
        text_out = out.getvalue()
        self.assertEqual(code, 0)
        primary_line = text_out.split("PRIMARY", 1)[1].split("\n", 1)[0]
        self.assertIn("priya.sharma@acmerobotics.example", primary_line)
        self.assertIn("Why the primary won: most deliverable (verified)", text_out)


class CandidateDetailsTests(unittest.TestCase):
    def test_example_profile_has_placeholders_only(self):
        candidate = example_profile().candidate
        self.assertEqual(candidate["phone"], "+91 90000 00000")
        self.assertIn("your-handle", candidate["linkedin_url"])
        self.assertIn("your-username", candidate["github_url"])


if __name__ == "__main__":
    unittest.main()
