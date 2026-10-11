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
        found.append(match.rstrip(".").lower())
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
