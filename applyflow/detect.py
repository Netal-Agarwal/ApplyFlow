"""Find which job board a company uses, starting from its website.

Guessing board slugs from company names mostly fails for Indian startups, so
discovery reads the company's own site instead: the homepage, its careers link,
and the usual /careers and /jobs paths. Links to a known ATS reveal the board.

Supported boards (we can read them):   Greenhouse, Lever, Ashby, Workable
Recognised but unsupported (no clean public API): Keka, Darwinbox, Zoho Recruit,
Freshteam, SmartRecruiters, Recruitee, Wellfound, Instahyre, Cutshort, Breezy.
Those companies are still recorded, because Phase 2 can email them directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from applyflow.http import FetchError

SUPPORTED_PATTERNS = [
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
]
_BAD_SLUGS = {"embed", "j", "api", "jobs", "job", "careers", "v1", "static", "favicon.ico"}

UNSUPPORTED_PATTERNS = [
    ("keka", re.compile(r"\.keka\.com/careers", re.I)),
    ("darwinbox", re.compile(r"darwinbox\.(?:in|com)", re.I)),
    ("zoho_recruit", re.compile(r"zohorecruit\.(?:in|com)", re.I)),
    ("freshteam", re.compile(r"\.freshteam\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com", re.I)),
    ("recruitee", re.compile(r"\.recruitee\.com", re.I)),
    ("wellfound", re.compile(r"(?:wellfound|angel)\.co/company/", re.I)),
    ("instahyre", re.compile(r"instahyre\.com", re.I)),
    ("cutshort", re.compile(r"cutshort\.io/company", re.I)),
    ("breezy", re.compile(r"\.breezy\.hr", re.I)),
]

_HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
_CAREERS_HINT = re.compile(r"career|jobs|join[-_ ]?us|work[-_ ]?with[-_ ]?us|hiring|open[-_ ]?roles", re.I)


@dataclass
class BoardMatch:
    ats: str
    slug: str = ""
    supported: bool = True
    found_on: str = ""


def find_board_in_html(html: str, page_url: str = "") -> BoardMatch | None:
    """Return the first supported board linked from this HTML, else an unsupported one, else None."""
    for ats, pattern in SUPPORTED_PATTERNS:
        for match in pattern.finditer(html):
            slug = match.group(1).strip("./").split("?")[0]
            if slug and slug.lower() not in _BAD_SLUGS:
                return BoardMatch(ats=ats, slug=slug, found_on=page_url)
    for ats, pattern in UNSUPPORTED_PATTERNS:
        if pattern.search(html):
            return BoardMatch(ats=ats, supported=False, found_on=page_url)
    return None


def careers_links(html: str, base_url: str, limit: int = 2) -> list[str]:
    """Links on the page that look like a careers page, on the same site or a careers subdomain."""
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    found: list[str] = []
    for href in _HREF_RE.findall(html):
        if not _CAREERS_HINT.search(href):
            continue
        url = urljoin(base_url, href.strip())
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if urlparse(url).scheme not in ("http", "https"):
            continue
        if host != base_host and not host.endswith("." + base_host):
            continue
        if url not in found:
            found.append(url)
        if len(found) >= limit:
            break
    return found


def normalize_website(website: str) -> str:
    website = (website or "").strip()
    if not website:
        return ""
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    parts = urlparse(website)
    return f"{parts.scheme}://{parts.netloc}"


def detect_board(client, website: str, max_pages: int = 4) -> BoardMatch | None:
    """Look for a job board on the company's website. Uses at most `max_pages` page fetches."""
    root = normalize_website(website)
    if not root:
        return None
    queue = [root]
    visited: set[str] = set()
    unsupported: BoardMatch | None = None
    fetched = 0
    while queue and fetched < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            html = client.get_page(url)
        except FetchError:
            if url == root:
                # Homepage unreachable: still try the conventional careers paths.
                queue.extend(u for u in (f"{root}/careers", f"{root}/jobs") if u not in visited)
            fetched += 1
            continue
        fetched += 1
        match = find_board_in_html(html, url)
        if match and match.supported:
            return match
        if match and unsupported is None:
            unsupported = match
        if url == root:
            links = careers_links(html, root)
            queue.extend(links or [f"{root}/careers", f"{root}/jobs"])
    return unsupported
