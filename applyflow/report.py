"""Export ranked opportunities (and Phase 2b outreach drafts) as CSV and as a self-contained HTML page."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime
from pathlib import Path
from sqlite3 import Row

ACTION_LABELS = {
    "apply": "Apply",
    "email": "Email",
    "apply_plus_email": "Apply + email",
}
LOCATION_LABELS = {
    "preferred_india": "Preferred city",
    "remote_open": "Remote (India OK)",
    "other_india": "India (other)",
    "remote_unclear": "Remote (check region)",
    "abroad_onsite": "Abroad (sponsored)",
    "unknown": "Not stated",
}
CSV_FIELDS = [
    "tier", "score", "company", "title", "track", "family", "location", "location_class",
    "action", "contact_hint", "team_size", "lead_project", "emails", "url", "apply_url", "source", "posted_at", "id",
]


def _row_dict(row: Row) -> dict[str, str]:
    return {
        "tier": row["tier"],
        "score": str(row["score"]),
        "company": row["company"],
        "title": row["title"],
        "track": row["track"],
        "family": row["family"],
        "location": " | ".join(json.loads(row["merged_locations"] or row["locations"] or "[]")),
        "location_class": row["location_class"],
        "action": row["action"],
        "contact_hint": row["contact_hint"] or "",
        "team_size": "" if row["team_size"] is None else str(row["team_size"]),
        "lead_project": row["lead_project"] or "",
        "emails": ", ".join(json.loads(row["emails"] or "[]")),
        "url": row["url"] or "",
        "apply_url": row["apply_url"] or "",
        "source": row["source"],
        "posted_at": (row["posted_at"] or "")[:10],
        "id": row["id"],
    }


COMPANY_FIELDS = ["name", "website", "team_size", "locations", "board", "reason", "description", "batch", "source"]


def _board_label(r: Row) -> str:
    if r["board_status"] == "unsupported":
        return r["ats"]
    if r["board_status"] == "unverified":
        return f"unverified ({r['ats']}:{r['slug']})"
    return "none found"


def write_companies_csv(rows: list[Row], path: Path) -> Path:
    """Hiring companies whose board we can't read - direct-outreach candidates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COMPANY_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "name": r["name"], "website": r["website"] or "",
                "team_size": "" if r["team_size"] is None else r["team_size"],
                "locations": r["locations"] or "",
                "board": _board_label(r), "reason": r["status_reason"] or "",
                "description": r["description"] or "", "batch": r["batch"] or "", "source": r["source"],
            })
    return path


_CONTACT_COLUMNS = ["name", "role", "email", "confidence", "alternates", "mx", "source_url", "proof",
                    "linkedin_search", "google_search"]
CONTACT_FIELDS = (
    ["target_type", "company", "domain", "team_size", "roles", "best_score", "hiring_likelihood", "status"]
    + [f"primary_{c}" for c in _CONTACT_COLUMNS] + ["primary_source"]
    + [f"backup1_{c}" for c in _CONTACT_COLUMNS] + [f"backup2_{c}" for c in _CONTACT_COLUMNS] + ["notes"]
)


def _contact_cells(c: Row, company: str) -> dict[str, str]:
    """A stored contact as CSV cells; guessed / published-inbox people get search links to check by hand."""
    from applyflow.contacts import needs_manual_check, search_links
    linkedin, google = search_links(c["name"], company) if needs_manual_check(c["confidence"], c["name"]) else ("", "")
    return {"name": c["name"] or "", "role": c["role"] or "", "email": c["email"], "confidence": c["confidence"],
            "alternates": "; ".join(json.loads(c["alternates"] or "[]")),
            "mx": "unchecked" if c["mx"] == "unchecked" else (c["mx"] or ""),
            "source_url": c["source_url"] or "", "proof": (c["proof"] if "proof" in c.keys() else "") or "",
            "linkedin_search": linkedin, "google_search": google}


def write_contacts_csv(store, path: Path) -> Path:
    """One row per target company: primary contact plus up to two backups (Phase 2a)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pool = {r["key"]: r for r in store.pool_rows()}
    pool_domains = store.pool_by_domain()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CONTACT_FIELDS)
        writer.writeheader()
        for check in store.contact_checks():
            hl = pool.get(check["company_key"]) or pool_domains.get(check["domain"] or "")
            contacts = store.contacts_for(check["company_key"])
            details = json.loads(check["details"] or "{}")
            row = {
                "target_type": "role" if check["kind"] == "role" else "general_outreach",
                "company": check["company"], "domain": check["domain"] or "",
                "team_size": "" if check["team_size"] is None else check["team_size"],
                "roles": "; ".join(json.loads(check["roles"] or "[]")),
                "best_score": "" if check["best_score"] is None else check["best_score"],
                "hiring_likelihood": hl["score"] if hl else "",
                "status": check["status"],
                "notes": " | ".join(details.get("reasons", [])) if not contacts else "",
            }
            for i, c in enumerate(contacts[:3]):
                prefix = "primary" if i == 0 else f"backup{i}"
                row.update({f"{prefix}_{k}": v for k, v in _contact_cells(c, check["company"]).items()})
                if i == 0:
                    row["primary_source"] = c["source"]
            writer.writerow(row)
    return path


def write_csv(rows: list[Row], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_row_dict(row))
    return path


POOL_FIELDS = ["rank", "score", "selected", "why_selected", "name", "website", "region", "sources", "team_size",
               "locations", "batch", "founded_year", "funding_round", "funding_amount", "funding_date", "funding_source",
               "description", "reasons"]


def write_pool_csv(scored, path: Path) -> Path:
    """Phase 2c: every pool company with its hiring-likelihood score and why."""
    from applyflow.sources import latest_funding
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=POOL_FIELDS)
        writer.writeheader()
        for s in scored:
            c = s.company
            f = latest_funding(c)
            writer.writerow({
                "rank": s.rank, "score": s.score, "selected": "yes" if s.selected else "", "why_selected": s.why_selected,
                "name": c.name, "website": c.website, "region": c.region, "sources": "; ".join(c.sources),
                "team_size": "" if c.team_size is None else c.team_size, "locations": c.locations, "batch": c.batch,
                "founded_year": c.founded_year or "", "funding_round": f.round if f else "",
                "funding_amount": f.amount if f else "", "funding_date": f.date if f else "",
                "funding_source": f.source_url if f else "", "description": c.description,
                "reasons": " | ".join(s.reasons)})
    return path


OUTBOX_FIELDS = ["priority", "target_id", "company", "type", "why", "hiring_likelihood", "status", "send_tier",
                 "in_batch", "to",
                 "contact_name", "contact_role", "confidence", "backups", "subject", "body", "resume_action",
                 "draft_source", "validation_notes", "sent_date", "gmail_url"]


def write_outbox_csv(items, batch_ids: set[str], path: Path) -> Path:
    """Every outreach target's current draft, ready to send or not (Phase 2b)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTBOX_FIELDS)
        writer.writeheader()
        for it in items:
            a, d = it.address, it.draft
            writer.writerow({
                "priority": it.priority, "target_id": it.target_id, "company": it.company, "type": it.kind,
                "why": it.why, "hiring_likelihood": "" if it.hl_score is None else it.hl_score,
                "status": it.status, "send_tier": it.send_tier or "",
                "in_batch": "yes" if it.target_id in batch_ids else "",
                "to": it.sent_email or (a.email if a else ""), "contact_name": a.name if a else "",
                "contact_role": a.role if a else "", "confidence": a.confidence if a else "",
                "backups": "; ".join(b.email for b in it.backups), "subject": d.subject, "body": d.body,
                "resume_action": d.resume_action, "draft_source": d.source,
                "validation_notes": " | ".join(d.failures or d.template_failures),
                "sent_date": it.sent_date, "gmail_url": it.gmail,
            })
    return path


_STATUS_LABELS = {"ready": "ready", "sent": "sent", "exhausted": "all addresses bounced",
                  "stopped": "bounced (on hold)",
                  "do_not_contact": "do not contact", "no_contact": "no contact found",
                  "apply_only": "apply via posting", "no_domain": "no domain", "dropped": "dropped (2c.1)"}


def _outreach_card(it, in_batch: bool) -> str:
    e = html.escape
    a, d = it.address, it.draft
    tier = f"T{it.send_tier}" if it.send_tier else "–"
    to = it.sent_email or (a.email if a else "")
    tags = [f'<span class="tag action"{f" title={chr(34)}{e(it.status_note)}{chr(34)}" if it.status_note else ""}>'
            f'{e(_STATUS_LABELS.get(it.status, it.status))}'
            + (f" {e(it.sent_date)}" if it.sent_date else "") + "</span>",
            f'<span class="tag">{e(it.kind.replace("_", " "))}</span>']
    if in_batch:
        tags.append('<span class="tag action">today\'s batch</span>')
    contact = ""
    if a:
        who = f"{a.name} ({a.role})" if a.name else a.role
        mx = ", MX unchecked" if a.mx == "unchecked" else ""
        contact = (f'<div class="line"><b>To:</b> {e(to)} · {e(who)} '
                   f'<span class="tag">{e(a.confidence.replace("_", " "))}{mx}</span></div>')
    if it.backups:
        contact += ('<div class="line muted"><b>Backups:</b> '
                    + e("; ".join(b.label() for b in it.backups[:4])) + "</div>")
    if it.bounced:
        contact += f'<div class="line muted"><b>Bounced:</b> {e(", ".join(it.bounced))}</div>'
    links = " · ".join(f'<a href="{e(url)}" target="_blank" rel="noopener">{e(label)}</a>'
                       for label, url in it.manual_links)
    if a and a.proof:
        contact += f'<div class="line muted"><b>Source proof:</b> {e(a.proof)}</div>'
    if it.hl_score is not None:
        contact += (f'<details><summary>Hiring likelihood {it.hl_score}/100</summary><ul>'
                    + "".join(f"<li>{e(r)}</li>" for r in it.hl_reasons) + "</ul></details>")
    notes = d.failures or d.template_failures
    source = "Claude-written" if d.source == "claude" else "template"
    note_html = ""
    if d.failures:
        note_html = ('<div class="line warn">Written draft rejected, showing the template: '
                     + e("; ".join(d.failures)) + "</div>")
    elif d.template_failures:
        note_html = '<div class="line warn">Template check: ' + e("; ".join(d.template_failures)) + "</div>"
    buttons = ""
    if to:
        buttons = (f'<button data-text="{e(to)}">Copy address</button>'
                   f'<button data-text="{e(d.subject)}">Copy subject</button>'
                   f'<button data-text="{e(d.body)}">Copy body</button>'
                   f'<a class="btn" href="{e(it.gmail)}" target="_blank" rel="noopener">Open in Gmail</a>')
    return (
        f'<article class="target" data-tier="{e(str(it.send_tier or ""))}" data-kind="{e(it.kind)}" '
        f'data-status="{e(it.status)}">'
        f'<div class="top"><span class="score">{e(tier)}</span><span class="title">{e(it.company)}</span>'
        f'<span class="company">#{it.priority} · {e(it.why)}</span></div>'
        f'<div class="tags">{"".join(tags)}</div>{contact}'
        + (f'<div class="line muted"><b>Check:</b> {links}</div>' if links else "")
        + f'<div class="line"><b>Subject:</b> {e(d.subject)}</div>'
        f'<pre class="mail">{e(d.body)}</pre>'
        f'<div class="line"><b>Resume:</b> {e(d.resume_action)} <span class="muted">({e(d.resume_reason)})</span>'
        f' · <b>Draft:</b> {e(source)}' + (f' · <b>Project:</b> {e(d.project)}' if d.project else "") + "</div>"
        + note_html + (f'<div class="actions">{buttons}</div>' if buttons else "") + "</article>"
    )


def outreach_sections(items, batch: list, batch_day: str, batch_stored: bool, facts_lines: list[str],
                      tier3_note: str = "") -> str:
    """Today's batch and every outreach target (Phase 2b), as HTML."""
    e = html.escape
    batch_ids = {it.target_id for it in batch}
    counts: dict[str, int] = {}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    written = sum(it.draft.source == "claude" for it in items)
    origin = "from `queue`" if batch_stored else "preview - run `queue` to fix it"
    batch_html = "".join(_outreach_card(it, True) for it in batch) or \
        '<p class="empty">Nothing to send today.</p>'
    all_html = "".join(_outreach_card(it, it.target_id in batch_ids) for it in items)
    stats = [("Targets", len(items)), ("Ready", counts.get("ready", 0)), ("Sent", counts.get("sent", 0)),
             ("Claude-written", written), ("Today", len(batch))]
    return (
        f'<h1>Today\'s batch · {e(batch_day)}</h1>'
        f'<p class="sub">{len(batch)} emails ({e(origin)}). Nothing is sent by ApplyFlow: open each in Gmail, '
        f'attach the resume where it says <b>attach</b>, send, then <code>mark-sent</code>.'
        + (f'<br>{e(tier3_note)}' if tier3_note else "") + '</p>'
        f'<div class="stats">{"".join(f"<div class=stat><b>{v}</b><span>{e(k)}</span></div>" for k, v in stats)}</div>'
        f'{batch_html}'
        f'<details class="facts"><summary>Candidate facts the drafts may use</summary><ul>'
        + "".join(f"<li>{e(f)}</li>" for f in facts_lines) + "</ul></details>"
        f'<h1 style="margin-top:32px">All outreach targets</h1>'
        f'<div class="filters"><select id="o-tier"><option value="">All send tiers</option><option value="1">Tier 1</option>'
        f'<option value="2">Tier 2</option><option value="3">Tier 3</option></select>'
        f'<select id="o-kind"><option value="">All types</option><option value="role">Role</option>'
        f'<option value="general_outreach">General outreach</option><option value="manual">Manual</option></select>'
        f'<select id="o-status"><option value="">All statuses</option>'
        + "".join(f'<option value="{k}">{e(v)}</option>' for k, v in _STATUS_LABELS.items())
        + f'</select><input id="o-text" type="search" placeholder="Search company, contact, draft…"></div>'
        f'<div id="targets">{all_html}</div><h1 style="margin-top:32px">Ranked opportunities</h1>'
    )


_CSS = """
:root { --bg:#f7f7f5; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e4e4e0;
        --a:#1f7a4d; --b:#8a6d1a; --accent:#2b59c3; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#121214; --card:#1b1b1e; --ink:#ececee; --muted:#9a9aa2; --line:#2c2c31;
          --a:#4cc38a; --b:#d9b44a; --accent:#7aa2ff; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 24px; }
.stats { display:flex; gap:12px; flex-wrap:wrap; margin-bottom: 20px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; }
.stat b { display:block; font-size:20px; }
.stat span { color:var(--muted); font-size:13px; }
.filters { display:flex; gap:8px; flex-wrap:wrap; margin-bottom: 16px; }
.filters select, .filters input { background:var(--card); color:var(--ink); border:1px solid var(--line);
       border-radius:8px; padding:6px 10px; font:inherit; }
.job { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px;
       margin-bottom:10px; }
.top { display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; }
.score { font-weight:700; font-variant-numeric: tabular-nums; min-width: 2.5em; }
.tier-A .score { color: var(--a); } .tier-B .score { color: var(--b); }
.title { font-weight:600; } .title a { color: inherit; text-decoration: none; }
.title a:hover { text-decoration: underline; }
.company { color: var(--muted); }
.tags { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }
.tag { font-size:12px; border:1px solid var(--line); border-radius:999px; padding:1px 8px; color:var(--muted); }
.tag.action { color: var(--accent); border-color: var(--accent); }
details { margin-top:6px; color: var(--muted); font-size: 13px; }
details ul { margin: 6px 0 0 18px; padding: 0; }
.empty { color: var(--muted); padding: 24px 0; }
.target { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px;
          margin-bottom:12px; }
.line { margin-top:6px; font-size:14px; overflow-wrap:anywhere; }
.muted { color: var(--muted); } .warn { color: var(--b); }
pre.mail { white-space: pre-wrap; overflow-wrap:anywhere; background:var(--bg); border:1px solid var(--line);
           border-radius:8px; padding:10px 12px; font:13px/1.5 ui-monospace, Consolas, monospace; margin:8px 0; }
.actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }
.actions button, .actions .btn { background:var(--card); color:var(--accent); border:1px solid var(--accent);
           border-radius:8px; padding:5px 10px; font:inherit; font-size:13px; cursor:pointer; text-decoration:none; }
.facts { margin: 12px 0 0; }
"""

_JS = """
const q = s => document.querySelector(s);
function apply() {
  const tier = q('#f-tier').value, track = q('#f-track').value, action = q('#f-action').value;
  const text = q('#f-text').value.toLowerCase();
  let shown = 0;
  document.querySelectorAll('.job').forEach(el => {
    const ok = (!tier || el.dataset.tier === tier) && (!track || el.dataset.track === track)
      && (!action || el.dataset.action === action)
      && (!text || el.textContent.toLowerCase().includes(text));
    el.style.display = ok ? '' : 'none';
    if (ok) shown++;
  });
  q('#count').textContent = shown;
}
document.querySelectorAll('.filters select, .filters input').forEach(el => el.addEventListener('input', apply));
function applyTargets() {
  const t = q('#o-tier'), k = q('#o-kind'), st = q('#o-status'), x = q('#o-text');
  if (!t) return;
  const text = x.value.toLowerCase();
  document.querySelectorAll('#targets .target').forEach(el => {
    const ok = (!t.value || el.dataset.tier === t.value) && (!k.value || el.dataset.kind === k.value)
      && (!st.value || el.dataset.status === st.value) && (!text || el.textContent.toLowerCase().includes(text));
    el.style.display = ok ? '' : 'none';
  });
}
['#o-tier', '#o-kind', '#o-status', '#o-text'].forEach(s => { const el = q(s); if (el) el.addEventListener('input', applyTargets); });
function copyText(text, button) {
  const done = () => { const old = button.textContent; button.textContent = 'Copied'; setTimeout(() => button.textContent = old, 1200); };
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(done, () => fallback()); }
  else { fallback(); }
  function fallback() {
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); ta.remove(); done();
  }
}
document.querySelectorAll('button[data-text]').forEach(b => b.addEventListener('click', () => copyText(b.dataset.text, b)));
"""


def write_html(rows: list[Row], path: Path, stats: dict[str, object] | None = None,
               contacts: dict[str, Row] | None = None, outreach: str = "") -> Path:
    """`contacts` maps a company's name_key to its primary contact (Phase 2a), shown on each opening;
    `outreach` is the Phase 2b HTML (today's batch, every target), shown above the openings."""
    from applyflow.discovery import name_key
    path.parent.mkdir(parents=True, exist_ok=True)
    e = html.escape
    contacts = contacts or {}
    cards = []
    for row in rows:
        d = _row_dict(row)
        contact = contacts.get(name_key(d["company"]))
        reasons = json.loads(row["reasons"] or "[]")
        tags = [
            f'<span class="tag action">{e(ACTION_LABELS.get(d["action"], d["action"]))}</span>',
            f'<span class="tag">{e(d["track"].replace("_", " "))}</span>',
            f'<span class="tag">{e(d["family"].replace("_", " "))}</span>',
            f'<span class="tag">{e(LOCATION_LABELS.get(d["location_class"], d["location_class"]))}</span>',
        ]
        if d["location"]:
            tags.append(f'<span class="tag">{e(d["location"][:80])}</span>')
        if d["emails"]:
            tags.append(f'<span class="tag">✉ {e(d["emails"])}</span>')
        if d["team_size"]:
            tags.append(f'<span class="tag">{e(d["team_size"])} people</span>')
        if contact:
            from applyflow.contacts import needs_manual_check, search_links
            who = f'{contact["name"]} ({contact["role"]})' if contact["name"] else contact["role"]
            mx = ", MX unchecked" if contact["mx"] == "unchecked" else ""
            tags.append(f'<span class="tag action">✉ {e(who)}: {e(contact["email"])} '
                        f'[{e(contact["confidence"].replace("_", " "))}{mx}]</span>')
            alternates = json.loads(contact["alternates"] or "[]")
            if alternates:
                tags.append(f'<span class="tag">if it bounces: {e(", ".join(alternates))}</span>')
            if needs_manual_check(contact["confidence"], contact["name"]):
                linkedin, google = search_links(contact["name"], contact["company"] or d["company"])
                tags.append(f'<span class="tag"><a href="{e(linkedin)}" target="_blank" rel="noopener">LinkedIn</a>'
                            f' · <a href="{e(google)}" target="_blank" rel="noopener">Google</a></span>')
        elif d["contact_hint"]:
            tags.append(f'<span class="tag">contact: {e(d["contact_hint"])}</span>')
        if d["lead_project"]:
            tags.append(f'<span class="tag">lead with: {e(d["lead_project"])}</span>')
        cards.append(
            f'<article class="job tier-{e(d["tier"])}" data-tier="{e(d["tier"])}" '
            f'data-track="{e(d["track"])}" data-action="{e(d["action"])}">'
            f'<div class="top"><span class="score">{e(d["score"])}</span>'
            f'<span class="title"><a href="{e(d["url"])}" target="_blank" rel="noopener">{e(d["title"])}</a></span>'
            f'<span class="company">{e(d["company"])} · {e(d["source"])}</span></div>'
            f'<div class="tags">{"".join(tags)}</div>'
            f'<details><summary>Why this score</summary><ul>'
            + "".join(f"<li>{e(r)}</li>" for r in reasons)
            + "</ul></details></article>"
        )

    stats = stats or {}
    by_tier = stats.get("by_tier", {}) if isinstance(stats.get("by_tier"), dict) else {}
    stat_boxes = [
        ("Active openings", stats.get("active_jobs", len(rows))),
        ("Tier A", by_tier.get("A", 0)),
        ("Tier B", by_tier.get("B", 0)),
        ("Filtered out", by_tier.get("rejected", 0) + by_tier.get("skip", 0)),
    ]
    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ApplyFlow Opportunities</title><style>{_CSS}</style></head>
<body><main>
{outreach or '<h1>Ranked opportunities</h1>'}
<p class="sub">Generated {e(generated)} · <span id="count">{len(rows)}</span> shown</p>
<div class="stats">{"".join(f'<div class="stat"><b>{e(str(v))}</b><span>{e(k)}</span></div>' for k, v in stat_boxes)}</div>
<div class="filters">
  <select id="f-tier"><option value="">All tiers</option><option>A</option><option>B</option></select>
  <select id="f-track"><option value="">All tracks</option><option value="internship">Internship</option>
    <option value="new_grad">New grad</option><option value="unspecified">Unspecified</option></select>
  <select id="f-action"><option value="">All actions</option><option value="apply">Apply</option>
    <option value="email">Email</option><option value="apply_plus_email">Apply + email</option></select>
  <input id="f-text" type="search" placeholder="Search title, company, skill…">
</div>
{"".join(cards) or '<p class="empty">Nothing in tier A or B yet. Run collect and score, or loosen the profile.</p>'}
</main><script>{_JS}</script></body></html>
"""
    path.write_text(page, encoding="utf-8")
    return path
