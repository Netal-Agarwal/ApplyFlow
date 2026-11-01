"""Phase 3: Gmail API access - OAuth (installed-app flow), a small REST client, MIME building and message parsing.

Scopes are gmail.send and gmail.readonly: ApplyFlow can send and read, never modify, label or delete. The client
talks to the REST API through google-auth's AuthorizedSession (requests underneath), so there's no Google API
client library. Credentials and the token live in config/private/ (gitignored).

Errors are sorted for the sending engine:
  GmailAuthError     401, invalid_grant / expired token (the app is in Testing mode: tokens can expire weekly)
  GmailBrakeError    403, or any abuse / quota / rate-limit error, or 429 still failing after back-off
  GmailTransientError 5xx still failing after back-off (the run stops; a rerun continues)
  GmailError         anything else (e.g. 400 invalid recipient: that one row fails)
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr
from pathlib import Path
from typing import Callable

SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
BACKOFF = (5, 20, 60)                 # seconds before each retry of a 429 / 5xx
_QUOTA_WORDS = re.compile(r"quota|rate ?limit|ratelimit|abuse|suspend|too many|limit exceeded|sending limit", re.I)


class GmailError(Exception):
    def __init__(self, message: str, status: int | None = None, reason: str = ""):
        super().__init__(message)
        self.status, self.reason = status, reason


class GmailAuthError(GmailError):
    """The token is missing, revoked or expired: run `gmail-login`."""


class GmailBrakeError(GmailError):
    """403 / abuse / quota: stop sending for the day."""


class GmailTransientError(GmailError):
    """5xx that didn't clear after back-off."""


def classify_error(status: int, payload: dict | str) -> GmailError:
    """The exception for a failed API call (after any retries)."""
    if isinstance(payload, dict):
        err = payload.get("error") or {}
        if isinstance(err, str):                   # OAuth endpoint shape: {"error": "invalid_grant", ...}
            reason, message = err, str(payload.get("error_description") or err)
        else:
            reasons = [e.get("reason", "") for e in err.get("errors") or [] if isinstance(e, dict)]
            reason, message = ",".join(r for r in reasons if r) or str(err.get("status") or ""), str(err.get("message") or "")
    else:
        reason, message = "", str(payload)[:300]
    text = f"HTTP {status} {reason}: {message}".strip()
    if status == 401 or "invalid_grant" in reason or "invalid_grant" in message:
        return GmailAuthError(text, status, reason)
    if status in (403, 429) or _QUOTA_WORDS.search(f"{reason} {message}"):
        return GmailBrakeError(text, status, reason)
    if status >= 500:
        return GmailTransientError(text, status, reason)
    return GmailError(text, status, reason)


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def gmail_login(credentials_path: Path, token_path: Path) -> str:
    """Run the installed-app OAuth flow on a local loopback port (a browser opens) and save the token.
    Returns the authorised account's address."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not credentials_path.exists():
        raise GmailAuthError(f"{credentials_path} not found: download the OAuth client (Desktop app) JSON from "
                             "Google Cloud Console > APIs & Services > Credentials and save it there")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(host="localhost", port=0, open_browser=True,
                                  authorization_prompt_message="Opening your browser to sign in to Gmail: {url}",
                                  success_message="ApplyFlow is authorised. You can close this tab.")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return GmailClient(creds, token_path).address()


def load_credentials(token_path: Path):
    """The saved token, refreshed when expired (and saved again). GmailAuthError when it's missing or refused."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not token_path.exists():
        raise GmailAuthError(f"no Gmail token at {token_path}: run gmail-login")
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (ValueError, json.JSONDecodeError) as exc:
        raise GmailAuthError(f"unreadable Gmail token ({exc}): run gmail-login") from exc
    if not creds.valid:
        if not creds.refresh_token:
            raise GmailAuthError("the Gmail token has no refresh token: run gmail-login")
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise GmailAuthError(f"the Gmail token was refused ({exc}): run gmail-login") from exc
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class GmailClient:
    """users.messages.send / list / get and users.getProfile, with back-off on 429 / 5xx."""

    def __init__(self, creds, token_path: Path | None = None, sleep: Callable[[float], None] = time.sleep,
                 session=None):
        from google.auth.transport.requests import AuthorizedSession
        self.creds, self.token_path, self.sleep = creds, token_path, sleep
        self.session = session or AuthorizedSession(creds)
        self._address = ""

    @classmethod
    def from_token(cls, token_path: Path) -> "GmailClient":
        return cls(load_credentials(token_path), token_path)

    def _call(self, method: str, path: str, **kw) -> dict:
        from google.auth.exceptions import RefreshError, TransportError
        for attempt in range(len(BACKOFF) + 1):
            try:
                resp = self.session.request(method, f"{API}{path}", timeout=60, **kw)
            except RefreshError as exc:
                raise GmailAuthError(f"the Gmail token was refused ({exc}): run gmail-login") from exc
            except (TransportError, OSError) as exc:     # requests' ConnectionError is an OSError
                if attempt == len(BACKOFF):
                    raise GmailTransientError(f"network error: {exc}") from exc
                self.sleep(BACKOFF[attempt])
                continue
            if resp.status_code < 400:
                return resp.json() if resp.content else {}
            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < len(BACKOFF):
                self.sleep(BACKOFF[attempt])
                continue
            raise classify_error(resp.status_code, payload)
        raise GmailTransientError("unreachable")

    def address(self) -> str:
        if not self._address:
            self._address = self._call("GET", "/profile").get("emailAddress", "")
        return self._address

    def send(self, raw: str, thread_id: str = "") -> dict:
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return self._call("POST", "/messages/send", json=body)

    def list_ids(self, query: str, limit: int = 500) -> list[dict]:
        out, token = [], ""
        while len(out) < limit:
            params = {"q": query, "maxResults": min(100, limit - len(out))}
            if token:
                params["pageToken"] = token
            page = self._call("GET", "/messages", params=params)
            out += page.get("messages") or []
            token = page.get("nextPageToken") or ""
            if not token:
                break
        return out

    def get(self, gmail_id: str, fmt: str = "full", headers: tuple[str, ...] = ()) -> dict:
        params: dict = {"format": fmt}
        if headers:
            params["metadataHeaders"] = list(headers)
        return self._call("GET", f"/messages/{gmail_id}", params=params)


# ---------------------------------------------------------------------------
# MIME
# ---------------------------------------------------------------------------

@dataclass
class Outgoing:
    to: str
    subject: str
    body: str
    from_name: str
    from_email: str
    attachment: Path | None = None
    in_reply_to: str = ""            # the original's Message-ID (follow-ups)
    references: str = ""
    thread_id: str = ""

    def message(self) -> EmailMessage:
        """Plain-text UTF-8, no HTML, no tracking pixel; the resume PDF only when `attachment` is set."""
        msg = EmailMessage()
        msg["From"] = formataddr((self.from_name, self.from_email))
        msg["To"] = self.to
        msg["Subject"] = self.subject
        msg["Message-ID"] = make_msgid(domain=self.from_email.split("@")[-1] or None)
        if self.in_reply_to:
            msg["In-Reply-To"] = self.in_reply_to
            msg["References"] = self.references or self.in_reply_to
        msg.set_content(self.body.replace("\r\n", "\n"), subtype="plain", charset="utf-8")
        if self.attachment is not None:
            data = self.attachment.read_bytes()
            ctype = mimetypes.guess_type(self.attachment.name)[0] or "application/pdf"
            main, sub = ctype.split("/", 1)
            msg.add_attachment(data, maintype=main, subtype=sub, filename=self.attachment.name)
        return msg

    def raw(self) -> str:
        return base64.urlsafe_b64encode(self.message().as_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Reading messages
# ---------------------------------------------------------------------------

@dataclass
class Mail:
    """A Gmail API message resource (format=full), flattened."""
    gmail_id: str
    thread_id: str
    headers: dict[str, str]          # lower-case name -> value (first occurrence)
    text: str                        # every text/plain and delivery-status part, decoded
    received_at: datetime
    labels: list[str] = field(default_factory=list)
    content_types: list[str] = field(default_factory=list)
    snippet: str = ""

    @property
    def sender(self) -> str:
        return self.headers.get("from", "")

    @property
    def from_email(self) -> str:
        return parseaddr(self.sender)[1].lower()

    @property
    def from_name(self) -> str:
        return parseaddr(self.sender)[0]

    @property
    def subject(self) -> str:
        return self.headers.get("subject", "")

    def recipients(self) -> list[str]:
        vals = [self.headers.get(h, "") for h in ("to", "cc")]
        return [a.lower() for _, a in getaddresses(vals) if a]


def _decode(data: str) -> str:
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _walk(part: dict, texts: list[str], types: list[str], html: list[str]) -> None:
    mime = (part.get("mimeType") or "").lower()
    types.append(mime)
    body = part.get("body") or {}
    if mime in ("text/plain", "message/delivery-status", "text/rfc822-headers", "message/rfc822-headers"):
        texts.append(_decode(body.get("data", "")))
    elif mime == "text/html":
        html.append(_decode(body.get("data", "")))
    for sub in part.get("parts") or []:
        _walk(sub, texts, types, html)


def parse_mail(resource: dict) -> Mail:
    payload = resource.get("payload") or {}
    headers: dict[str, str] = {}
    for h in payload.get("headers") or []:
        headers.setdefault(h.get("name", "").lower(), h.get("value", ""))
    texts: list[str] = []
    types: list[str] = []
    html: list[str] = []
    _walk(payload, texts, types, html)
    text = "\n".join(t for t in texts if t)
    if not text.strip() and html:              # an HTML-only reply: strip the tags
        from applyflow.text import html_to_text
        text = "\n".join(html_to_text(h) for h in html)
    ms = int(resource.get("internalDate") or 0)
    return Mail(gmail_id=resource.get("id", ""), thread_id=resource.get("threadId", ""), headers=headers, text=text,
                received_at=datetime.fromtimestamp(ms / 1000, tz=timezone.utc), labels=resource.get("labelIds") or [],
                content_types=types, snippet=resource.get("snippet", ""))


def header(resource: dict, name: str) -> str:
    for h in (resource.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def thread_link(thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}"
