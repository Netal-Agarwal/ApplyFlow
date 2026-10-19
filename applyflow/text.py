"""Text helpers: HTML to plain text, keyword matching, email extraction."""

from __future__ import annotations

import html
import re
from functools import lru_cache

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section)\b[^>]*>", re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


def html_to_text(raw: str | None) -> str:
    """Convert (possibly entity-escaped) HTML into readable plain text.

    Greenhouse returns HTML that is itself entity-escaped (``&lt;p&gt;``), so we
    unescape before stripping tags, then unescape again for entities inside text.
    """
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


@lru_cache(maxsize=4096)
def _phrase_regex(phrase: str) -> re.Pattern[str]:
    # Word boundaries that also work for phrases starting/ending in symbols
    # like "c#", ".net" or "node.js".
    escaped = re.escape(phrase.lower()).replace(r"\ ", r"[\s\-]+")
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")


def contains_phrase(text: str, phrase: str) -> bool:
    """Case-insensitive whole-word/phrase match."""
    return bool(_phrase_regex(phrase).search(text.lower()))


def matching_phrases(text: str, phrases: list[str]) -> list[str]:
    lowered = text.lower()
    return [p for p in phrases if _phrase_regex(p).search(lowered)]


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Bracketed "at" ("jobs [at] acme.com", "jobs (at) acme [dot] com") is unambiguous.
# A bare " at " only counts with a spelled-out " dot " ("jobs at acme dot com"),
# so ordinary prose like "work with us at Acme. We..." isn't mistaken for an email.
_OBFUSCATED_EMAIL_RES = [
    re.compile(
        r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\{at\})\s*"
        r"([A-Za-z0-9\-]+(?:\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+|\.)\s*[A-Za-z0-9\-]+)+)",
        re.I,
    ),
    re.compile(
        r"([A-Za-z0-9._%+\-]+)\s+at\s+"
        r"([A-Za-z0-9\-]+(?:\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+)\s*[A-Za-z0-9\-]+)+)",
        re.I,
    ),
]
_DOT_RE = re.compile(r"\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+)\s*", re.I)


def extract_emails(text: str) -> list[str]:
    """Find emails, including common obfuscations like 'jobs [at] acme [dot] com'."""
    found: list[str] = []
    for match in _EMAIL_RE.findall(text):
        # Page scripts JSON-escape "<" / ">" as < / >, which glue onto the address.
        found.append(re.sub(r"^(?:u00[0-9a-f]{2})+", "", match.rstrip(".").lower()))
    for pattern in _OBFUSCATED_EMAIL_RES:
        for user, domain in pattern.findall(text):
            domain = _DOT_RE.sub(".", domain).replace(" ", "")
            if "." in domain:
                found.append(f"{user}@{domain}".lower())
    seen: set[str] = set()
    unique = []
    for email in found:
        if email not in seen and not email.endswith((".png", ".jpg", ".gif")):
            seen.add(email)
            unique.append(email)
    return unique


# ATS postings end in legal boilerplate: "If you need an accommodation ... contact
# hr@rubrik.com", privacy notices, scam warnings. Those addresses aren't for applicants.
_BOILERPLATE_RE = re.compile(
    r"accommodat|disabilit|\beeo\b|equal (employment )?opportunity|privacy|personal data|"
    r"\bbenefits?\b|fraud|scam|suspicious|legitimate|alternative method|unable to access",
    re.I,
)
_BOILERPLATE_LOCAL_PARTS = ("privacy", "security", "accommodation", "accommodations", "legal",
                            "benefits", "accessibility", "dataprotection", "dpo", "gdpr")


def extract_contact_emails(text: str) -> list[str]:
    """Emails a job board posting invites applicants to use, without EEO/privacy boilerplate ones."""
    lowered = text.lower()
    kept = []
    for email in extract_emails(text):
        local = email.split("@", 1)[0]
        if local in _BOILERPLATE_LOCAL_PARTS:
            continue
        at = lowered.find(email)
        if at < 0:
            at = lowered.find(local + "@")
        window = ""
        if at >= 0:     # the email's own paragraph: up to 200 chars before it, 60 after
            start = max(at - 200, lowered.rfind("\n", 0, at) + 1)
            end = lowered.find("\n", at)
            window = lowered[start: min(at + len(email) + 60, end if end >= 0 else len(lowered))]
        if _BOILERPLATE_RE.search(window):
            continue
        kept.append(email)
    return kept
