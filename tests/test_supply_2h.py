"""Phase 2h: the wide contact search (sitemaps, blog / press / careers pages, linked PDFs), surname addresses, and
the direct-apply list."""

import unittest
import zlib
from datetime import date

from applyflow.cli import wide_improved
from applyflow.contacts import (Contact, ContactResult, DoNotContact, GitHubBudget, Person, Target,
                                evidence_for_guess, find_contacts, parse_sitemap, pdf_text, read_site,
                                sitemap_entries, surname_owner, wide_pages)
from applyflow.direct_apply import FRESHER_TITLE, board_rows, detail_links, posted_date, role_fit
from applyflow.models import Job
from applyflow.scoring import score_job
from tests.helpers import FakeClient, example_profile

PROFILE = example_profile()
ROOT = "https://acme.example"


class SitemapClient(FakeClient):
    """FakeClient with robots.txt Sitemap: lines and binary fetches."""

    def __init__(self, pages, sitemaps=(), binaries=None):
        super().__init__({}, pages)
        self._sitemaps = list(sitemaps)
        self.binaries = binaries or {}

    def sitemaps(self, url):
        return self._sitemaps

    def get_bytes(self, url):
        self.page_calls.append(url)
        if url not in self.binaries:
            from applyflow.http import FetchError
            raise FetchError(url, "HTTP 404", 404)
        return self.binaries[url]


INDEX = """<?xml version="1.0"?><sitemapindex>
<sitemap><loc>https://acme.example/post-sitemap.xml</loc></sitemap>
<sitemap><loc>https://acme.example/product-sitemap.xml</loc></sitemap>
<sitemap><loc>https://acme.example/page-sitemap.xml</loc></sitemap></sitemapindex>"""
PAGES = """<urlset><url><loc>https://acme.example/leadership</loc><lastmod>2026-09-01</lastmod></url>
<url><loc>https://acme.example/careers/backend-intern</loc></url>
<url><loc>https://acme.example/press</loc></url>
<url><loc>https://acme.example/hi/about</loc></url>
<url><loc>https://acme.example/tag/news</loc></url></urlset>"""
POSTS = """<urlset><url><loc>https://acme.example/blog/old-post</loc><lastmod>2024-01-01</lastmod></url>
<url><loc>https://acme.example/blog/new-post</loc><lastmod>2026-10-20</lastmod></url>
<url><loc><![CDATA[https://acme.example/blog/mid-post]]></loc><lastmod>2025-05-05</lastmod></url></urlset>"""


class SitemapTests(unittest.TestCase):
    def test_parse_index_and_urlset(self):
        pages, kids = parse_sitemap(INDEX)
        self.assertEqual(pages, [])
        self.assertEqual(len(kids), 3)
        pages, kids = parse_sitemap(POSTS)
        self.assertIn(("https://acme.example/blog/mid-post", "2025-05-05"), pages)
        self.assertEqual(kids, [])

    def test_index_followed_into_page_and_post_sitemaps_not_products(self):
        client = SitemapClient({ROOT + "/sitemap_index.xml": INDEX, ROOT + "/page-sitemap.xml": PAGES,
                                ROOT + "/post-sitemap.xml": POSTS}, sitemaps=[ROOT + "/sitemap_index.xml"])
        urls = [u for u, _ in sitemap_entries(client, ROOT)]
        self.assertIn(ROOT + "/leadership", urls)
        self.assertIn(ROOT + "/blog/new-post", urls)
        self.assertNotIn(ROOT + "/product-sitemap.xml", client.page_calls)
        self.assertLess(client.page_calls.index(ROOT + "/page-sitemap.xml"),
                        client.page_calls.index(ROOT + "/post-sitemap.xml"))

    def test_wide_pages_order_caps_and_skips(self):
        entries = parse_sitemap(PAGES)[0] + parse_sitemap(POSTS)[0]
        html = '<a href="/blog">Blog</a><a href="/files/brochure.pdf">Brochure</a><a href="https://other.example/team">x</a>'
        pages = wide_pages(html, ROOT, entries, 25)
        self.assertIn(ROOT + "/leadership", pages)
        self.assertIn(ROOT + "/careers/backend-intern", pages)
        self.assertNotIn(ROOT + "/hi/about", pages)                 # other language
        self.assertNotIn(ROOT + "/tag/news", pages)                 # tag pages
        self.assertNotIn("https://other.example/team", pages)
        blog = [p for p in pages if "/blog" in p]
        self.assertEqual(blog[:3], [ROOT + "/blog", ROOT + "/blog/new-post", ROOT + "/blog/mid-post"])
        self.assertEqual(pages[-1], ROOT + "/files/brochure.pdf")   # PDFs last
        self.assertLessEqual(len(wide_pages(html, ROOT, entries, 8)), 8)


def pdf_with(text: bytes) -> bytes:
    stream = zlib.compress(b"BT (" + text + b") Tj ET")
    return b"%PDF-1.4\n1 0 obj << /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream\nendobj\n%%EOF"


class PdfTests(unittest.TestCase):
    def test_email_in_a_flate_stream(self):
        self.assertIn("anita.rao@acme.example", pdf_text(pdf_with(b"Contact anita.rao@acme.example for details")))


class SurnameTests(unittest.TestCase):
    EDRIN = Person("Edrin Thomas", "Founder & CTO", "yc", "https://www.ycombinator.com/companies/tendec")

    def test_surname_address_is_the_named_persons_own(self):
        text = "Who you'll talk to: Thomas, CTO. Book a meeting: thomas@tendec.example"
        self.assertIs(surname_owner("thomas", [self.EDRIN], text), self.EDRIN)

    def test_not_when_the_page_doesnt_name_them_or_its_someones_first_name(self):
        self.assertIsNone(surname_owner("thomas", [self.EDRIN], "Write to thomas@tendec.example"))
        thomas_first = Person("Thomas Mathew", "CEO", "website", "u")
        self.assertIsNone(surname_owner("thomas", [self.EDRIN, thomas_first], "Thomas, CTO. thomas@tendec.example"))
        designer = Person("Priya Thomas", "Designer", "website", "u")
        self.assertIsNone(surname_owner("thomas", [designer], "Priya Thomas. thomas@x.example"))

    def test_surname_address_is_never_first_name_evidence(self):
        guess = Contact(name="Dan Lee", role="Co-founder", email="dan@tendec.example", confidence="guessed",
                        source="pattern", source_url="https://tendec.example/team", group="founder", mx="ok")
        published = [{"email": "thomas@tendec.example", "confidence": "verified", "source": "website",
                      "source_url": "https://tendec.example"}]
        ev, _ = evidence_for_guess(guess, published, "tendec.example", "Tendec", "ok", {"thomas", "dan"})
        self.assertTrue(ev)                                           # 2f behaviour without surnames
        ev, why = evidence_for_guess(guess, published, "tendec.example", "Tendec", "ok", {"thomas", "dan"},
                                     surnames={"thomas"})
        self.assertEqual(ev, {})


class OkMX:
    def check(self, domain):
        return "ok"


TEAM = """<html><head><title>Acme</title></head><body><a href="/team">Team</a><a href="/blog">Blog</a>
<footer><a href="/privacy">Privacy</a></footer></body></html>"""
TEAM_PAGE = "<html><body><p>Asha Rao</p><p>Co-founder &amp; CEO</p></body></html>"
BLOG_INDEX = '<html><body><a href="/blog/launch">Launch</a><p>Ravi Kumar, CTO at Globex</p></body></html>'
BLOG_POST = "<html><body>Questions? Write to asha.rao@acme.example. Neha Jain, Founder</body></html>"


class WideReadTests(unittest.TestCase):
    def client(self):
        return SitemapClient({ROOT: TEAM, ROOT + "/team": TEAM_PAGE, ROOT + "/blog": BLOG_INDEX,
                              ROOT + "/blog/launch": BLOG_POST})

    def test_blog_index_followed_people_only_from_own_pages(self):
        site = read_site(self.client(), ROOT, "acme.example", 12, wide=True)
        self.assertIn(ROOT + "/blog/launch", site.read)
        self.assertIn(("asha.rao@acme.example", ROOT + "/blog/launch"), site.emails)
        names = [p.name for p in site.people]
        self.assertIn("Asha Rao", names)
        self.assertNotIn("Neha Jain", names)                      # named in a blog post: not taken as the team
        self.assertLessEqual(len(site.read) + len(site.failed) + len(site.blocked), 26)

    def test_wide_lookup_finds_the_founders_published_address(self):
        target = Target(key="pool:acme.example", company="Acme", kind="general_outreach", hint="founder or CTO",
                        website=ROOT, domain="acme.example")
        res = find_contacts(self.client(), target, PROFILE, DoNotContact(), GitHubBudget(), mx=OkMX(), wide=True)
        self.assertEqual(res.contacts[0].email, "asha.rao@acme.example")
        self.assertEqual((res.contacts[0].name, res.contacts[0].confidence), ("Asha Rao", "verified"))
        self.assertTrue(any(r.startswith("website (wide)") for r in res.reasons))
        self.assertTrue(wide_improved(res))

    def test_pure_guess_is_not_an_improvement(self):
        res = ContactResult(target=None, status="found", contacts=[
            Contact(name="Asha Rao", role="CEO", email="asha@acme.example", confidence="guessed", source="pattern",
                    source_url="u", group="founder")])
        self.assertFalse(wide_improved(res))


class DirectApplyTests(unittest.TestCase):
    TODAY = date(2026, 11, 3)

    def scored(self, title, location="Bengaluru, India", description="", posted="2026-10-30"):
        job = Job(source="greenhouse", external_id="1", company="Bigco", title=title, url="u",
                  description=description, locations=[location], posted_at=posted)
        return score_job(job, PROFILE, self.TODAY)

    def test_backend_intern_in_india_fits(self):
        self.assertEqual(role_fit(self.scored("Backend Engineer Intern"), True), "")
        self.assertEqual(role_fit(self.scored("Software Engineer - New Grad"), True), "")
        self.assertEqual(role_fit(self.scored("Software Engineer", posted=""), True), "")   # no stated years (tier skip)

    def test_wrong_family_years_or_place(self):
        self.assertIn("family", role_fit(self.scored("Machine Learning Engineer Intern"), True))
        self.assertNotEqual(role_fit(self.scored("Backend Engineer", description="3+ years of experience"), True), "")
        self.assertNotEqual(role_fit(self.scored("Backend Engineer Intern", location="Berlin, Germany"), True), "")

    def test_posted_dates_and_detail_links(self):
        self.assertEqual(posted_date("Posted on 12 Oct 2026"), "2026-10-12")
        self.assertEqual(posted_date("Posted: October 5, 2026"), "2026-10-05")
        self.assertEqual(posted_date("no date here"), "")
        html = '<a href="/careers/be-intern">Backend Developer Intern</a><a href="https://x.example/j">Backend Dev</a>'
        self.assertEqual(detail_links(html, ROOT + "/careers", ["Backend Developer Intern"]),
                         {"Backend Developer Intern": ROOT + "/careers/be-intern"})
        self.assertTrue(FRESHER_TITLE.search("SDE I - Backend"))

    def test_careers_page_titles_need_a_detail_page_or_a_fresher_word(self):
        careers = ROOT + "/careers"
        page = ('<html><body><a href="/careers/be">Backend Engineer</a> <a href="/careers/py">Python Developer Intern'
                '</a> <p>Backend Developer</p></body></html>').replace("</a> ", "</a>\n")
        detail = "<html><body>Backend Engineer. Location: Pune, India. 4+ years of experience required.</body></html>"
        client = FakeClient({}, {ROOT: "<html><body>Acme</body></html>", careers: page, ROOT + "/careers/be": detail,
                                 ROOT + "/careers/py": "<html><body>Python Developer Intern. Location: Pune, India. "
                                                       "Posted on 01 Nov 2026</body></html>"})
        rows, note = board_rows(client, PROFILE, "Acme", ROOT, careers, True, "large", self.TODAY)
        self.assertEqual([r.role_title for r in rows], ["Python Developer Intern"])
        self.assertEqual((rows[0].location, rows[0].posted, rows[0].apply_link),
                         ("Pune, India", "2026-11-01", ROOT + "/careers/py"))



class PoolLockTests(unittest.TestCase):
    def test_second_holder_waits_and_stale_lock_is_cleared(self):
        import os
        import tempfile
        import time
        from pathlib import Path
        from applyflow.supply import PoolLock
        path = Path(tempfile.mkdtemp()) / "pool.lock"
        with PoolLock(path):
            self.assertTrue(path.exists())
            with self.assertRaises(TimeoutError):
                with PoolLock(path, wait_s=0.2, poll_s=0.05):
                    pass
        self.assertFalse(path.exists())
        path.write_text("crashed run")
        old = time.time() - 7200
        os.utime(path, (old, old))
        with PoolLock(path, wait_s=0.2, poll_s=0.05):
            pass
        self.assertFalse(path.exists())



class CareersTitleTests(unittest.TestCase):
    def test_navigation_lines_years_and_other_stacks_are_not_openings(self):
        from applyflow.refill import careers_page_roles
        text = "\n".join(["Find a Developer", "Developer Resources", "Your AI-native product engineer",
                          "AI platforms & developer tools", "Developer Console", "SharePoint Developer",
                          "Python Developer (3-6 Years)", "Python Developer (0-1 Years)", "Backend Engineer",
                          "React / Next.js Developer", "Junior Hybrid Developer"])
        fits, _ = careers_page_roles(text, PROFILE)
        self.assertEqual(sorted(fits), ["Backend Engineer", "Python Developer (0-1 Years)"])



class MarkBatchOnceTests(unittest.TestCase):
    def test_bounced_row_reopened_by_a_hiring_inbox_isnt_marked_sent_again(self):
        from applyflow.outreach import mark_batch_sent, mark_bounced
        from applyflow.store import Store
        from tests.test_drafting import add_company, contact
        day = date(2026, 11, 3)
        store = Store(":memory:")
        add_company(store, "yc:cas", "Cas", contacts=[contact("careers@cas.example", "published_inbox"),
                                                      contact("hr@cas.example", "published_inbox")])
        store.save_queue(day.isoformat(), [("yc-cas", "careers@cas.example", 2)], set())
        self.assertEqual(len(mark_batch_sent(store, PROFILE, day)), 1)
        mark_bounced(store, PROFILE, "yc-cas", day, stop=True)
        self.assertEqual(mark_batch_sent(store, PROFILE, day), [])        # the daily rerun marks nothing twice
        self.assertEqual(sum(e["event"] == "sent" for e in store.events()), 1)



class CareersPathTests(unittest.TestCase):
    def test_only_careers_or_jobs_pages_count(self):
        from urllib.parse import urlparse
        from applyflow.direct_apply import CAREERS_PATH
        ok = ["https://tezo.com/careers", "https://x.example/company/careers", "https://x.example/join-us",
              "https://x.example/jobs/backend"]
        for u in ok:
            self.assertTrue(CAREERS_PATH.search(urlparse(u).path), u)
        self.assertIsNone(CAREERS_PATH.search("/companies-where-our-students-got-jobs"))     # alumni, not openings
        self.assertIsNone(CAREERS_PATH.search("/about-us"))


if __name__ == "__main__":
    unittest.main()
