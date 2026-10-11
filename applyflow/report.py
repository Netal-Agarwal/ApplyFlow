"""Export ranked opportunities as CSV and as a self-contained HTML page."""

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
    "action", "lead_project", "emails", "url", "apply_url", "source", "posted_at", "id",
]


def _row_dict(row: Row) -> dict[str, str]:
    return {
        "tier": row["tier"],
        "score": str(row["score"]),
        "company": row["company"],
        "title": row["title"],
        "track": row["track"],
        "family": row["family"],
        "location": " | ".join(json.loads(row["locations"] or "[]")),
        "location_class": row["location_class"],
        "action": row["action"],
        "lead_project": row["lead_project"] or "",
        "emails": ", ".join(json.loads(row["emails"] or "[]")),
        "url": row["url"] or "",
        "apply_url": row["apply_url"] or "",
        "source": row["source"],
        "posted_at": (row["posted_at"] or "")[:10],
        "id": row["id"],
    }


def write_csv(rows: list[Row], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_row_dict(row))
    return path


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
"""


def write_html(rows: list[Row], path: Path, stats: dict[str, object] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    e = html.escape
    cards = []
    for row in rows:
        d = _row_dict(row)
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
<h1>Ranked opportunities</h1>
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
