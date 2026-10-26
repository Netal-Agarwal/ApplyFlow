"""SQLite storage.

Step 1 uses the standard-library sqlite3 module behind this small interface, so
the pipeline has no database dependency to install. When the dashboard arrives
(FastAPI), this module is the one place that moves to SQLAlchemy.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from applyflow.models import Company, DiscoveredCompany, Job, ScoreResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,          -- "<source>:<external_id>"
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    company         TEXT NOT NULL,
    board_key       TEXT,                      -- "<ats>:<slug>" or "hn:<story_id>"
    title           TEXT NOT NULL,
    url             TEXT,
    apply_url       TEXT,
    description     TEXT,
    locations       TEXT,                      -- JSON list
    remote          INTEGER,                   -- 1 / 0 / NULL
    employment_type TEXT,
    department      TEXT,
    posted_at       TEXT,
    emails          TEXT,                      -- JSON list
    board_size      INTEGER,
    team_size       INTEGER,
    poster          TEXT,                      -- HN username of the post's author
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_board ON jobs(board_key);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(active);

CREATE TABLE IF NOT EXISTS scores (
    job_id           TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    accepted         INTEGER NOT NULL,
    score            INTEGER NOT NULL,
    tier             TEXT NOT NULL,
    family           TEXT,
    track            TEXT,
    location_class   TEXT,
    experience_years INTEGER,
    sponsorship      TEXT,
    action           TEXT,
    contact_hint     TEXT,
    lead_project     TEXT,
    matched_skills   TEXT,                      -- JSON list
    reasons          TEXT,                      -- JSON list
    merged_locations TEXT,                      -- JSON list: own + merged duplicates' locations
    scored_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_tier ON scores(tier);

CREATE TABLE IF NOT EXISTS companies (
    key           TEXT PRIMARY KEY,            -- e.g. "yc:razorpay"
    name          TEXT NOT NULL,
    website       TEXT,
    source        TEXT NOT NULL,               -- yc | hn | manual
    team_size     INTEGER,
    description   TEXT,
    locations     TEXT,
    batch         TEXT,
    ats           TEXT,                        -- supported ATS, unsupported one (e.g. keka), or ''
    slug          TEXT,
    board_status  TEXT NOT NULL,               -- found | unverified | unsupported | none | error
    detected_from TEXT,                        -- website | slug_guess
    status_reason TEXT,                        -- why the board was (not) verified
    checked_at    TEXT NOT NULL,
    added_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(board_status);

CREATE TABLE IF NOT EXISTS contact_checks (
    company_key   TEXT PRIMARY KEY,            -- discovered key ("yc:slug") or "co:<name>"
    company       TEXT NOT NULL,
    kind          TEXT NOT NULL,               -- role | general_outreach
    domain        TEXT,
    domain_source TEXT,                        -- seeds.yaml | discovery | HN header | board
    team_size     INTEGER,
    roles         TEXT,                        -- JSON list of "Title (score)"
    best_score    INTEGER,
    hint          TEXT,                        -- who we looked for
    status        TEXT NOT NULL,               -- found | none | no_domain
    details       TEXT,                        -- JSON: every candidate, reasons, why the primary won
    checked_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key   TEXT NOT NULL,
    company       TEXT,
    name          TEXT,
    role          TEXT,
    email         TEXT NOT NULL,
    confidence    TEXT NOT NULL,               -- verified | strong | guessed | fallback
    source        TEXT,                        -- posting | website | yc | github | pattern
    source_url    TEXT,
    found_at      TEXT NOT NULL,
    is_primary    INTEGER NOT NULL,
    rank          INTEGER NOT NULL,
    alternates    TEXT,                        -- JSON list: what to try if a guessed address bounces
    mx            TEXT,                        -- ok | unchecked (DNS lookup failed) | '' (not checked)
    proof         TEXT                         -- 2c: company site | posting | manual | demoted: ... | ''
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_key);

CREATE TABLE IF NOT EXISTS mx_cache (
    domain        TEXT PRIMARY KEY,
    status        TEXT NOT NULL,               -- ok | no_mx
    checked_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_context (   -- Phase 2b: what personalization is built from
    company_key      TEXT PRIMARY KEY,
    website          TEXT,
    homepage_title   TEXT,
    meta_description TEXT,
    snippet          TEXT,                     -- first readable lines of the homepage
    status           TEXT NOT NULL,            -- ok | blocked (robots.txt) | failed
    note             TEXT,
    fetched_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_targets (    -- add-target
    key           TEXT PRIMARY KEY,            -- "manual:<name>"
    name          TEXT NOT NULL,
    website       TEXT,
    note          TEXT,
    email         TEXT,
    person_name   TEXT,
    person_role   TEXT,
    added_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (            -- the current draft per target (template or written)
    target_id      TEXT PRIMARY KEY,
    company_key    TEXT NOT NULL,
    company        TEXT,
    kind           TEXT,
    to_email       TEXT,
    contact_name   TEXT,
    contact_role   TEXT,
    confidence     TEXT,
    send_tier      INTEGER,
    status         TEXT,                       -- ready | sent | no_contact | apply_only | ...
    resume_action  TEXT,                       -- link | attach
    subject        TEXT,
    body           TEXT,
    source         TEXT,                       -- template | claude
    notes          TEXT,                       -- JSON: why a written draft was rejected
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS written_drafts (    -- import-drafts: the text as written, validated on use
    target_id   TEXT PRIMARY KEY,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    file        TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach_events (   -- sent / bounced history (never deleted)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id   TEXT NOT NULL,
    company_key TEXT,
    email       TEXT NOT NULL,
    event       TEXT NOT NULL,                 -- sent | bounced | stopped (bounced, no further addresses)
    send_tier   INTEGER,                       -- the address's tier when sent (tier-3 gate)
    event_date  TEXT NOT NULL,                 -- YYYY-MM-DD
    batch_date  TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_target ON outreach_events(target_id);

CREATE TABLE IF NOT EXISTS send_queue (
    batch_date  TEXT NOT NULL,
    position    INTEGER NOT NULL,
    target_id   TEXT NOT NULL,
    email       TEXT NOT NULL,
    send_tier   INTEGER,
    PRIMARY KEY (batch_date, target_id)
);

CREATE TABLE IF NOT EXISTS pool_companies (    -- Phase 2c: every company from every source, scored
    key           TEXT PRIMARY KEY,            -- yc:<slug> | pool:<domain>
    name          TEXT NOT NULL,
    website       TEXT,
    domain        TEXT,
    region        TEXT,                        -- india | remote_global
    sources       TEXT,                        -- JSON list: yc, portfolio:<id>, funding:<id>, list:<id>
    data          TEXT NOT NULL,               -- JSON: the merged PoolCompany (incl. funding rounds)
    score         INTEGER NOT NULL,            -- hiring likelihood, 0-100
    reasons       TEXT,                        -- JSON list, one per factor
    selected      INTEGER NOT NULL,
    rank          INTEGER,
    why_selected  TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pool_domain ON pool_companies(domain);

CREATE TABLE IF NOT EXISTS funding_lookups (   -- startup name -> website from its Inc42 profile ('' = none)
    name_key    TEXT PRIMARY KEY,
    website     TEXT,
    checked_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_rules (       -- drafts held to an older rule set (already scheduled)
    target_id   TEXT PRIMARY KEY,
    rules       TEXT NOT NULL,                 -- "2b": rules from before Phase 2c; "2c": before the 2d format
    reason      TEXT,
    locked_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_matches (     -- Phase 2e: an open role that fits, per target and source
    company_key  TEXT NOT NULL,
    source       TEXT NOT NULL,                -- hn_archive | yc_jobs | careers | board | manual
    match        TEXT NOT NULL,                -- yes | no | unknown
    title        TEXT,
    url          TEXT,
    job_id       TEXT,                         -- jobs.id when the role is a stored posting
    location     TEXT,
    experience   TEXT,                         -- "Any (new grads ok)", "1+ years", ...
    salary       TEXT,
    new_grads_ok INTEGER,
    posted_at    TEXT,
    note         TEXT,                         -- why it matched, or why not
    checked_at   TEXT NOT NULL,
    PRIMARY KEY (company_key, source)
);

CREATE TABLE IF NOT EXISTS excluded_contacts ( -- 2e: addresses reviewed out (sales desk, mockup, wrong fit); survive re-lookups
    email       TEXT PRIMARY KEY,
    why         TEXT NOT NULL,
    excluded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    board_key   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    job_count   INTEGER,
    error       TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        Phase 2c: drafts already in a stored queue when the new greeting / subject rules arrived were
        scheduled in Gmail under the 2b rules, so they're locked to those (draft_rules), once."""
        if not self.conn.execute("SELECT 1 FROM draft_rules LIMIT 1").fetchone() and                 not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'draft_rules_done'").fetchone():
            now = _now()
            for row in self.conn.execute("SELECT DISTINCT target_id, batch_date FROM send_queue").fetchall():
                self.conn.execute("INSERT OR IGNORE INTO draft_rules (target_id, rules, reason, locked_at) "
                                  "VALUES (?, '2b', ?, ?)", (row[0], f"queued for {row[1]} before Phase 2c", now))
            self.conn.execute("CREATE TABLE IF NOT EXISTS draft_rules_done (at TEXT)")
        # Phase 2d: targets already queued or sent when the two-project format arrived keep the 2c rules, once.
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'draft_rules_2d_done'").fetchone():
            now = _now()
            for row in self.conn.execute("SELECT target_id, 'queued' FROM send_queue UNION "
                                         "SELECT target_id, event FROM outreach_events").fetchall():
                self.conn.execute("INSERT OR IGNORE INTO draft_rules (target_id, rules, reason, locked_at) "
                                  "VALUES (?, '2c', ?, ?)", (row[0], f"{row[1]} before the 2d format", now))
            self.conn.execute("CREATE TABLE IF NOT EXISTS draft_rules_2d_done (at TEXT)")
        for table, column, decl in (("jobs", "team_size", "INTEGER"), ("scores", "contact_hint", "TEXT"),
                                     ("scores", "merged_locations", "TEXT"), ("companies", "detected_from", "TEXT"),
                                     ("companies", "status_reason", "TEXT"), ("contacts", "alternates", "TEXT"),
                                     ("contacts", "mx", "TEXT"), ("outreach_events", "send_tier", "INTEGER"),
                                     ("jobs", "poster", "TEXT"), ("contacts", "proof", "TEXT"),
                                     ("manual_targets", "role_seen", "TEXT")):
            cols = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- jobs ---------------------------------------------------------------

    def upsert_board(self, board_key: str, jobs: Iterable[Job]) -> dict[str, int]:
        """Save one board's current jobs; mark jobs no longer listed as inactive."""
        now = _now()
        jobs = list(jobs)
        seen_ids = []
        new = 0
        with self.conn:
            for job in jobs:
                seen_ids.append(job.id)
                exists = self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,)).fetchone()
                if not exists:
                    new += 1
                self.conn.execute(
                    """
                    INSERT INTO jobs (id, source, external_id, company, board_key, title, url, apply_url,
                                      description, locations, remote, employment_type, department, posted_at,
                                      emails, board_size, team_size, poster, first_seen, last_seen, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET
                        company=excluded.company, title=excluded.title, url=excluded.url,
                        apply_url=excluded.apply_url, description=excluded.description,
                        locations=excluded.locations, remote=excluded.remote,
                        employment_type=excluded.employment_type, department=excluded.department,
                        posted_at=excluded.posted_at, emails=excluded.emails,
                        board_size=excluded.board_size, board_key=excluded.board_key,
                        team_size=excluded.team_size, poster=excluded.poster,
                        last_seen=excluded.last_seen, active=1
                    """,
                    (
                        job.id, job.source, job.external_id, job.company, board_key, job.title, job.url,
                        job.apply_url, job.description, json.dumps(job.locations),
                        None if job.remote is None else int(job.remote), job.employment_type,
                        job.department, job.posted_at, json.dumps(job.emails), job.board_size,
                        job.team_size, job.poster, now, now,
                    ),
                )
            placeholders = ",".join("?" for _ in seen_ids) or "''"
            closed = self.conn.execute(
                f"UPDATE jobs SET active = 0 WHERE board_key = ? AND active = 1 AND id NOT IN ({placeholders})",
                (board_key, *seen_ids),
            ).rowcount
        return {"seen": len(jobs), "new": new, "closed": closed}

    def upsert_jobs(self, board_key: str, jobs: Iterable[Job]) -> int:
        """Save these jobs (2e archives, YC job pages) without closing the board's other jobs. Returns new ones."""
        new = 0
        now = _now()
        with self.conn:
            for job in jobs:
                if not self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,)).fetchone():
                    new += 1
                self.conn.execute(
                    "INSERT INTO jobs (id, source, external_id, company, board_key, title, url, apply_url, description, "
                    "locations, remote, employment_type, department, posted_at, emails, board_size, team_size, poster, "
                    "first_seen, last_seen, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT(id) DO UPDATE SET company=excluded.company, title=excluded.title, url=excluded.url, "
                    "description=excluded.description, locations=excluded.locations, remote=excluded.remote, "
                    "employment_type=excluded.employment_type, posted_at=excluded.posted_at, emails=excluded.emails, "
                    "team_size=excluded.team_size, poster=excluded.poster, last_seen=excluded.last_seen, active=1",
                    (job.id, job.source, job.external_id, job.company, board_key, job.title, job.url, job.apply_url,
                     job.description, json.dumps(job.locations), None if job.remote is None else int(job.remote),
                     job.employment_type, job.department, job.posted_at, json.dumps(job.emails), job.board_size,
                     job.team_size, job.poster, now, now))
        return new

    def log_run(self, board_key: str, ok: bool, job_count: int | None = None, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO collection_runs (board_key, started_at, ok, job_count, error) VALUES (?, ?, ?, ?, ?)",
                (board_key, _now(), int(ok), job_count, error),
            )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            source=row["source"],
            external_id=row["external_id"],
            company=row["company"],
            title=row["title"],
            url=row["url"] or "",
            apply_url=row["apply_url"] or "",
            description=row["description"] or "",
            locations=json.loads(row["locations"] or "[]"),
            remote=None if row["remote"] is None else bool(row["remote"]),
            employment_type=row["employment_type"] or "",
            department=row["department"] or "",
            posted_at=row["posted_at"] or "",
            emails=json.loads(row["emails"] or "[]"),
            board_size=row["board_size"],
            team_size=row["team_size"],
            poster=(row["poster"] if "poster" in row.keys() else "") or "",
        )

    def active_jobs(self) -> list[Job]:
        rows = self.conn.execute("SELECT * FROM jobs WHERE active = 1 ORDER BY company, title").fetchall()
        return [self._row_to_job(r) for r in rows]

    def get_job(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def find_jobs(self, needle: str) -> list[Job]:
        like = f"%{needle}%"
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE id LIKE ? OR title LIKE ? OR company LIKE ? ORDER BY company LIMIT 20",
            (like, like, like),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    # -- scores -------------------------------------------------------------

    def save_scores(self, results: Iterable[ScoreResult]) -> None:
        now = _now()
        with self.conn:
            self.conn.execute("DELETE FROM scores")
            for r in results:
                self.conn.execute(
                    """
                    INSERT INTO scores (job_id, accepted, score, tier, family, track, location_class,
                                        experience_years, sponsorship, action, contact_hint, lead_project,
                                        matched_skills, reasons, merged_locations, scored_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r.job_id, int(r.accepted), r.score, r.tier, r.family, r.track, r.location_class,
                        r.experience_years, r.sponsorship, r.action, r.contact_hint, r.lead_project,
                        json.dumps(r.matched_skills), json.dumps(r.reasons),
                        json.dumps(r.merged_locations), now,
                    ),
                )

    def ranked(self, tiers: tuple[str, ...] = ("A", "B")) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in tiers)
        return self.conn.execute(
            f"""
            SELECT j.*, s.score, s.tier, s.family, s.track, s.location_class, s.experience_years,
                   s.sponsorship, s.action, s.contact_hint, s.lead_project, s.matched_skills, s.reasons,
                   s.merged_locations
            FROM scores s JOIN jobs j ON j.id = s.job_id
            WHERE j.active = 1 AND s.tier IN ({placeholders})
            ORDER BY s.tier, s.score DESC, j.company, j.title          -- "A" < "B"
            """,
            tiers,
        ).fetchall()

    def get_score(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM scores WHERE job_id = ?", (job_id,)).fetchone()

    # -- discovered companies ----------------------------------------------------

    def upsert_discovered(self, c: DiscoveredCompany) -> None:
        now = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO companies (key, name, website, source, team_size, description, locations, batch,
                                       ats, slug, board_status, detected_from, status_reason, checked_at, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    name=excluded.name, website=excluded.website, team_size=excluded.team_size,
                    description=excluded.description, locations=excluded.locations, batch=excluded.batch,
                    ats=excluded.ats, slug=excluded.slug, board_status=excluded.board_status,
                    detected_from=excluded.detected_from, status_reason=excluded.status_reason,
                    checked_at=excluded.checked_at
                """,
                (c.key, c.name, c.website, c.source, c.team_size, c.description, c.locations, c.batch,
                 c.ats, c.slug, c.board_status, c.detected_from, c.status_reason, now, now),
            )

    def recently_checked(self, key: str, days: int) -> bool:
        row = self.conn.execute("SELECT checked_at, board_status FROM companies WHERE key = ?", (key,)).fetchone()
        if row is None or row["board_status"] == "error":
            return False
        checked = datetime.fromisoformat(row["checked_at"])
        return datetime.now(timezone.utc) - checked < timedelta(days=days)

    def discovered_boards(self, supported: tuple[str, ...]) -> list[Company]:
        placeholders = ",".join("?" for _ in supported)
        rows = self.conn.execute(
            f"SELECT * FROM companies WHERE board_status = 'found' AND ats IN ({placeholders}) ORDER BY name",
            supported,
        ).fetchall()
        return [Company(name=r["name"], ats=r["ats"], slug=r["slug"], team_size=r["team_size"],
                        website=r["website"] or "", source=r["source"]) for r in rows]

    def companies_with_board(self, statuses: tuple[str, ...] = ("found",)) -> list[DiscoveredCompany]:
        """Discovered companies with a supported board in one of these statuses."""
        placeholders = ",".join("?" for _ in statuses)
        rows = self.conn.execute(f"SELECT * FROM companies WHERE board_status IN ({placeholders}) AND slug != '' "
                                 "ORDER BY name", statuses).fetchall()
        return [DiscoveredCompany(
            key=r["key"], name=r["name"], website=r["website"] or "", source=r["source"], team_size=r["team_size"],
            description=r["description"] or "", locations=r["locations"] or "", batch=r["batch"] or "",
            ats=r["ats"] or "", slug=r["slug"] or "", board_status=r["board_status"],
            detected_from=r["detected_from"] or "", status_reason=r["status_reason"] or "") for r in rows]

    def set_board_status(self, c: DiscoveredCompany) -> None:
        """Record a re-check of a saved board without touching checked_at (discovery's own clock)."""
        with self.conn:
            self.conn.execute(
                "UPDATE companies SET board_status = ?, detected_from = ?, status_reason = ? WHERE key = ?",
                (c.board_status, c.detected_from, c.status_reason, c.key))

    def deactivate_board(self, board_key: str) -> int:
        """Stop showing a board's jobs (e.g. it turned out to be another company's board)."""
        with self.conn:
            return self.conn.execute(
                "UPDATE jobs SET active = 0 WHERE board_key = ? AND active = 1", (board_key,)).rowcount

    def companies_without_board(self) -> list[sqlite3.Row]:
        """Hiring companies whose board we can't read: candidates for direct outreach in Phase 2."""
        return self.conn.execute(
            "SELECT * FROM companies WHERE board_status IN ('unsupported', 'unverified', 'none') "
            "ORDER BY CASE WHEN lower(locations) LIKE '%india%' THEN 0 ELSE 1 END, "
            "COALESCE(team_size, 100000), name"
        ).fetchall()

    def discovery_counts(self) -> dict[str, int]:
        return dict(self.conn.execute(
            "SELECT board_status, COUNT(*) FROM companies GROUP BY board_status").fetchall())

    # -- contacts (Phase 2a) ---------------------------------------------------

    def save_contact_result(self, result) -> None:
        """Replace a company's contacts with this lookup's top 3 (primary first)."""
        t, now = result.target, _now()
        with self.conn:
            self.conn.execute("DELETE FROM contacts WHERE company_key = ?", (t.key,))
            for rank, c in enumerate(result.contacts):
                self.conn.execute(
                    "INSERT INTO contacts (company_key, company, name, role, email, confidence, source, source_url, "
                    "found_at, is_primary, rank, alternates, mx, proof) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (t.key, t.company, c.name, c.role, c.email, c.confidence, c.source, c.source_url, now,
                     int(rank == 0), rank, json.dumps(c.alternates), c.mx, getattr(c, "proof", "")))
            if getattr(result, "context", None):          # homepage text read during the lookup (2b)
                self._write_context(t.key, result.context)
            from applyflow.contacts import result_to_json
            self.conn.execute(
                """
                INSERT INTO contact_checks (company_key, company, kind, domain, domain_source, team_size, roles,
                                            best_score, hint, status, details, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_key) DO UPDATE SET
                    company=excluded.company, kind=excluded.kind, domain=excluded.domain,
                    domain_source=excluded.domain_source, team_size=excluded.team_size, roles=excluded.roles,
                    best_score=excluded.best_score, hint=excluded.hint, status=excluded.status,
                    details=excluded.details, checked_at=excluded.checked_at
                """,
                (t.key, t.company, t.kind, t.domain, t.domain_source, t.team_size, json.dumps(t.roles),
                 t.best_score, t.hint, result.status, json.dumps(result_to_json(result)), now))

    def get_mx(self, domain: str, days: int) -> str | None:
        """Cached MX status (ok | no_mx) if looked up within `days`."""
        row = self.conn.execute("SELECT status, checked_at FROM mx_cache WHERE domain = ?", (domain,)).fetchone()
        if row is None or datetime.now(timezone.utc) - datetime.fromisoformat(row["checked_at"]) >= timedelta(days=days):
            return None
        return row["status"]

    def save_mx(self, domain: str, status: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO mx_cache (domain, status, checked_at) VALUES (?, ?, ?) "
                              "ON CONFLICT(domain) DO UPDATE SET status=excluded.status, checked_at=excluded.checked_at",
                              (domain, status, _now()))

    def contacts_checked_recently(self, company_key: str, days: int) -> bool:
        """Looked up within `days` (a missing domain doesn't count: that's cheap to retry)."""
        row = self.conn.execute("SELECT checked_at, status FROM contact_checks WHERE company_key = ?",
                                (company_key,)).fetchone()
        if row is None or row["status"] == "no_domain":
            return False
        return datetime.now(timezone.utc) - datetime.fromisoformat(row["checked_at"]) < timedelta(days=days)

    def contact_checks(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM contact_checks ORDER BY CASE kind WHEN 'role' THEN 0 ELSE 1 END, "
            "COALESCE(best_score, -1) DESC, company").fetchall()

    def contacts_for(self, company_key: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM contacts WHERE company_key = ? ORDER BY rank",
                                 (company_key,)).fetchall()

    def find_contact_checks(self, needle: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM contact_checks WHERE company LIKE ? OR company_key LIKE ? "
                                 "ORDER BY company", (f"%{needle}%", f"%{needle}%")).fetchall()

    def primary_contacts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM contacts WHERE is_primary = 1").fetchall()

    def contact_counts(self) -> dict[str, int]:
        """Companies looked up, by their primary contact's confidence (or none / no_domain)."""
        counts: dict[str, int] = {}
        for row in self.conn.execute(
                "SELECT COALESCE(c.confidence, k.status) FROM contact_checks k "
                "LEFT JOIN contacts c ON c.company_key = k.company_key AND c.is_primary = 1"):
            counts[row[0]] = counts.get(row[0], 0) + 1
        return counts

    # -- outreach (Phase 2b) ---------------------------------------------------

    def save_context(self, company_key: str, ctx: dict[str, str]) -> None:
        with self.conn:
            self._write_context(company_key, ctx)

    def _write_context(self, company_key: str, ctx: dict[str, str]) -> None:
        self.conn.execute(
            "INSERT INTO company_context (company_key, website, homepage_title, meta_description, snippet, status, "
            "note, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(company_key) DO UPDATE SET "
            "website=excluded.website, homepage_title=excluded.homepage_title, "
            "meta_description=excluded.meta_description, snippet=excluded.snippet, status=excluded.status, "
            "note=excluded.note, fetched_at=excluded.fetched_at",
            (company_key, ctx.get("website", ""), ctx.get("title", ""), ctx.get("description", ""),
             ctx.get("snippet", ""), ctx.get("status", "ok"), ctx.get("note", ""), _now()))

    def get_context(self, company_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM company_context WHERE company_key = ?", (company_key,)).fetchone()

    def discovered_company(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM companies WHERE key = ?", (key,)).fetchone()

    def upsert_manual_target(self, key: str, name: str, website: str, note: str = "", email: str = "",
                             person_name: str = "", person_role: str = "", role_seen: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO manual_targets (key, name, website, note, email, person_name, person_role, added_at, "
                "role_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET name=excluded.name, "
                "website=excluded.website, note=excluded.note, email=excluded.email, "
                "person_name=excluded.person_name, person_role=excluded.person_role, role_seen=excluded.role_seen",
                (key, name, website, note, email.lower(), person_name, person_role, _now(), role_seen))

    def manual_targets(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM manual_targets ORDER BY name").fetchall()

    def get_manual_target(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM manual_targets WHERE key = ?", (key,)).fetchone()

    def save_drafts(self, items) -> None:
        """Replace the stored drafts with these outreach items' current drafts."""
        now = _now()
        with self.conn:
            self.conn.execute("DELETE FROM drafts")
            for it in items:
                d, i = it.draft, it.inputs
                self.conn.execute(
                    "INSERT INTO drafts (target_id, company_key, company, kind, to_email, contact_name, contact_role, "
                    "confidence, send_tier, status, resume_action, subject, body, source, notes, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (it.target_id, it.company_key, i.company, i.kind, i.to_email, i.contact_name, i.contact_role,
                     i.confidence, it.send_tier, it.status, d.resume_action, d.subject, d.body, d.source,
                     json.dumps(d.failures), now))

    def drafts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM drafts ORDER BY target_id").fetchall()

    def save_written(self, target_id: str, subject: str, body: str, file: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO written_drafts (target_id, subject, body, file, imported_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(target_id) DO UPDATE SET subject=excluded.subject, body=excluded.body, "
                "file=excluded.file, imported_at=excluded.imported_at", (target_id, subject, body, file, _now()))

    def written_drafts(self) -> dict[str, sqlite3.Row]:
        return {r["target_id"]: r for r in self.conn.execute("SELECT * FROM written_drafts")}

    def add_event(self, target_id: str, company_key: str, email: str, event: str, event_date: str,
                  batch_date: str = "", send_tier: int | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_events (target_id, company_key, email, event, event_date, batch_date, "
                "recorded_at, send_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (target_id, company_key, email.lower(), event, event_date, batch_date, _now(), send_tier))

    def events(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM outreach_events ORDER BY event_date, id").fetchall()

    def first_sent_date(self) -> str | None:
        return self.conn.execute("SELECT MIN(event_date) FROM outreach_events WHERE event = 'sent'").fetchone()[0]

    def send_history(self, upto: str | None = None) -> dict[str, object]:
        """Totals the tier-3 gate needs, counting events dated up to `upto` (YYYY-MM-DD) when given."""
        cond, args = ("AND event_date <= ?", (upto,)) if upto else ("", ())
        row = self.conn.execute(
            "SELECT MIN(CASE WHEN event = 'sent' THEN event_date END), SUM(event = 'sent'), SUM(event = 'bounced'), "
            "MIN(CASE WHEN event = 'sent' AND send_tier = 3 THEN event_date END) "
            f"FROM outreach_events WHERE 1 = 1 {cond}", args).fetchone()
        return {"first_sent": row[0], "sent": row[1] or 0, "bounced": row[2] or 0, "tier3_first_sent": row[3]}

    def save_queue(self, batch_date: str, rows: list[tuple[str, str, int | None]], keep: set[str]) -> None:
        """Replace a day's queue with rows of (target_id, email, tier), keeping entries in `keep` (already sent)."""
        with self.conn:
            kept = [r for r in self.queue(batch_date) if r["target_id"] in keep]
            self.conn.execute("DELETE FROM send_queue WHERE batch_date = ?", (batch_date,))
            ordered = [(r["target_id"], r["email"], r["send_tier"]) for r in kept]
            ordered += [r for r in rows if r[0] not in {k[0] for k in ordered}]
            for pos, (target_id, email, tier) in enumerate(ordered, start=1):
                self.conn.execute("INSERT INTO send_queue (batch_date, position, target_id, email, send_tier) "
                                  "VALUES (?, ?, ?, ?, ?)", (batch_date, pos, target_id, email, tier))

    def queue(self, batch_date: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM send_queue WHERE batch_date = ? ORDER BY position",
                                 (batch_date,)).fetchall()

    def draft_rules(self) -> dict[str, str]:
        return {r["target_id"]: r["rules"] for r in self.conn.execute("SELECT * FROM draft_rules")}

    # -- outreach pool (Phase 2c) ------------------------------------------------

    def save_pool(self, scored) -> None:
        """Replace the stored pool with these scored companies."""
        now = _now()
        with self.conn:
            self.conn.execute("DELETE FROM pool_companies")
            for s in scored:
                c = s.company
                self.conn.execute(
                    "INSERT INTO pool_companies (key, name, website, domain, region, sources, data, score, reasons, "
                    "selected, rank, why_selected, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (c.key, c.name, c.website, c.domain, c.region, json.dumps(c.sources), json.dumps(c.to_json()),
                     s.score, json.dumps(s.reasons), int(s.selected), s.rank, s.why_selected, now))

    def pool_rows(self, selected_only: bool = False) -> list[sqlite3.Row]:
        cond = "WHERE selected = 1" if selected_only else ""
        return self.conn.execute(f"SELECT * FROM pool_companies {cond} ORDER BY rank").fetchall()

    def pool_row(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM pool_companies WHERE key = ?", (key,)).fetchone()

    def pool_by_domain(self) -> dict[str, sqlite3.Row]:
        return {r["domain"]: r for r in self.conn.execute("SELECT * FROM pool_companies WHERE domain != ''")}

    def funding_lookups(self) -> dict[str, str]:
        return {r["name_key"]: r["website"] or "" for r in self.conn.execute("SELECT * FROM funding_lookups")}

    def save_funding_lookups(self, resolved: dict[str, str]) -> None:
        now = _now()
        with self.conn:
            for k, site in resolved.items():
                self.conn.execute("INSERT INTO funding_lookups (name_key, website, checked_at) VALUES (?, ?, ?) "
                                  "ON CONFLICT(name_key) DO NOTHING", (k, site, now))

    def protected_target_ids(self) -> set[str]:
        """2c.1: target ids that pool clean-ups never touch - queued on any day, sent / bounced, or held to
        an older draft rule set (already scheduled in Gmail)."""
        out: set[str] = set()
        for sql in ("SELECT target_id FROM send_queue", "SELECT target_id FROM outreach_events",
                    "SELECT target_id FROM draft_rules"):
            out.update(r[0] for r in self.conn.execute(sql))
        return out

    def site_texts(self) -> dict[str, str]:
        """company_key -> homepage title + meta description + snippet: the company's own words (2c.1)."""
        return {r["company_key"]: " ".join(x or "" for x in (r["homepage_title"], r["meta_description"], r["snippet"]))
                for r in self.conn.execute("SELECT * FROM company_context WHERE status = 'ok'")}

    def context_texts(self) -> dict[str, str]:
        """company_key -> homepage title + meta description (fetched context), for tech fit."""
        return {r["company_key"]: f"{r['homepage_title'] or ''} {r['meta_description'] or ''}"
                for r in self.conn.execute("SELECT * FROM company_context WHERE status = 'ok'")}

    def demote_contact(self, contact_id: int, why: str) -> None:
        """Source proof failed: the address is only a guess now (send tier 3)."""
        with self.conn:
            self.conn.execute("UPDATE contacts SET confidence = 'guessed', proof = ? WHERE id = ?",
                              (f"demoted: {why}", contact_id))

    def set_contact_proof(self, contact_id: int, url: str, proof: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE contacts SET source_url = ?, proof = ? WHERE id = ?", (url, proof, contact_id))

    # -- role matches (Phase 2e) ---------------------------------------------------

    def save_role_match(self, company_key: str, source: str, match: str, title: str = "", url: str = "",
                        job_id: str = "", location: str = "", experience: str = "", salary: str = "",
                        new_grads_ok: bool = False, posted_at: str = "", note: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO role_matches (company_key, source, match, title, url, job_id, location, experience, salary, "
                "new_grads_ok, posted_at, note, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(company_key, source) DO UPDATE SET match=excluded.match, title=excluded.title, "
                "url=excluded.url, job_id=excluded.job_id, location=excluded.location, experience=excluded.experience, "
                "salary=excluded.salary, new_grads_ok=excluded.new_grads_ok, posted_at=excluded.posted_at, "
                "note=excluded.note, checked_at=excluded.checked_at",
                (company_key, source, match, title, url, job_id, location, experience, salary, int(new_grads_ok),
                 posted_at, note, _now()))

    def role_matches(self) -> dict[str, list[sqlite3.Row]]:
        """company_key -> its role matches, best first (yes before unknown before no; postings before pages)."""
        out: dict[str, list[sqlite3.Row]] = {}
        for r in self.conn.execute(
                "SELECT * FROM role_matches ORDER BY company_key, CASE match WHEN 'yes' THEN 0 WHEN 'unknown' THEN 1 "
                "ELSE 2 END, CASE source WHEN 'hn_archive' THEN 0 WHEN 'yc_jobs' THEN 1 WHEN 'manual' THEN 2 "
                "WHEN 'board' THEN 3 ELSE 4 END, posted_at DESC"):
            out.setdefault(r["company_key"], []).append(r)
        return out

    def exclude_contact(self, email: str, why: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO excluded_contacts (email, why, excluded_at) VALUES (?, ?, ?) "
                              "ON CONFLICT(email) DO UPDATE SET why=excluded.why", (email.lower(), why, _now()))

    def excluded_contacts(self) -> dict[str, str]:
        return {r["email"]: r["why"] for r in self.conn.execute("SELECT * FROM excluded_contacts")}

    def role_rows(self) -> list[sqlite3.Row]:
        """Openings that make a role target: tier A/B, plus 2e postings (HN archive, YC job pages) that passed the
        title / role-family / location filters even when their score is below B (tier 'skip', never 'rejected')."""
        rows = list(self.ranked(("A", "B")))
        seen = {r["id"] for r in rows}
        rows += [r for r in self.conn.execute(
            "SELECT j.*, s.score, s.tier, s.family, s.track, s.location_class, s.experience_years, s.sponsorship, "
            "s.action, s.contact_hint, s.lead_project, s.matched_skills, s.reasons, s.merged_locations "
            "FROM role_matches m JOIN jobs j ON j.id = m.job_id JOIN scores s ON s.job_id = j.id "
            "WHERE m.match = 'yes' AND j.active = 1 AND s.tier = 'skip' ORDER BY s.score DESC, j.company")
            if r["id"] not in seen]
        return rows

    def _reject_reason_counts(self, limit: int = 8) -> list[tuple[str, int]]:
        """Group rejection reasons into broad categories ("asks for N+ years" -> one bucket)."""
        counts: dict[str, int] = {}
        rows = self.conn.execute(
            "SELECT s.reasons FROM scores s JOIN jobs j ON j.id = s.job_id "
            "WHERE j.active = 1 AND s.tier = 'rejected'"
        ).fetchall()
        for (reasons,) in rows:
            first = (json.loads(reasons) or ["unknown"])[0]
            category = re.sub(r"\s*\(.*$", "", first)          # drop "(location text)"
            category = re.sub(r"'[^']*'", "'…'", category)       # drop the specific term
            category = re.sub(r"\d+", "N", category)
            counts[category] = counts.get(category, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]

    def stats(self) -> dict[str, object]:
        c = self.conn
        return {
            "active_jobs": c.execute("SELECT COUNT(*) FROM jobs WHERE active = 1").fetchone()[0],
            "by_source": dict(c.execute(
                "SELECT source, COUNT(*) FROM jobs WHERE active = 1 GROUP BY source").fetchall()),
            "by_tier": dict(c.execute(
                "SELECT s.tier, COUNT(*) FROM scores s JOIN jobs j ON j.id = s.job_id "
                "WHERE j.active = 1 GROUP BY s.tier").fetchall()),
            "top_reject_reasons": self._reject_reason_counts(),
            "discovered": self.discovery_counts(),
            "contacts": self.contact_counts(),
            "failed_boards": [
                (r[0], r[1]) for r in c.execute(
                    "SELECT board_key, error FROM collection_runs r WHERE ok = 0 AND started_at = "
                    "(SELECT MAX(started_at) FROM collection_runs WHERE board_key = r.board_key)"
                ).fetchall()
            ],
        }
