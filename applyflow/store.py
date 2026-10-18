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
    board_status  TEXT NOT NULL,               -- found | unsupported | none | error
    checked_at    TEXT NOT NULL,
    added_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(board_status);

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
        """Add columns introduced after a database was first created."""
        for table, column, decl in (("jobs", "team_size", "INTEGER"), ("scores", "contact_hint", "TEXT"),
                                     ("scores", "merged_locations", "TEXT")):
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
                                      emails, board_size, team_size, first_seen, last_seen, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET
                        company=excluded.company, title=excluded.title, url=excluded.url,
                        apply_url=excluded.apply_url, description=excluded.description,
                        locations=excluded.locations, remote=excluded.remote,
                        employment_type=excluded.employment_type, department=excluded.department,
                        posted_at=excluded.posted_at, emails=excluded.emails,
                        board_size=excluded.board_size, board_key=excluded.board_key,
                        team_size=excluded.team_size,
                        last_seen=excluded.last_seen, active=1
                    """,
                    (
                        job.id, job.source, job.external_id, job.company, board_key, job.title, job.url,
                        job.apply_url, job.description, json.dumps(job.locations),
                        None if job.remote is None else int(job.remote), job.employment_type,
                        job.department, job.posted_at, json.dumps(job.emails), job.board_size,
                        job.team_size, now, now,
                    ),
                )
            placeholders = ",".join("?" for _ in seen_ids) or "''"
            closed = self.conn.execute(
                f"UPDATE jobs SET active = 0 WHERE board_key = ? AND active = 1 AND id NOT IN ({placeholders})",
                (board_key, *seen_ids),
            ).rowcount
        return {"seen": len(jobs), "new": new, "closed": closed}

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
            ORDER BY s.score DESC, j.company, j.title
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
                                       ats, slug, board_status, checked_at, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    name=excluded.name, website=excluded.website, team_size=excluded.team_size,
                    description=excluded.description, locations=excluded.locations, batch=excluded.batch,
                    ats=excluded.ats, slug=excluded.slug, board_status=excluded.board_status,
                    checked_at=excluded.checked_at
                """,
                (c.key, c.name, c.website, c.source, c.team_size, c.description, c.locations, c.batch,
                 c.ats, c.slug, c.board_status, now, now),
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

    def companies_without_board(self) -> list[sqlite3.Row]:
        """Hiring companies whose board we can't read: candidates for direct outreach in Phase 2."""
        return self.conn.execute(
            "SELECT * FROM companies WHERE board_status IN ('unsupported', 'none') "
            "ORDER BY CASE WHEN lower(locations) LIKE '%india%' THEN 0 ELSE 1 END, "
            "COALESCE(team_size, 100000), name"
        ).fetchall()

    def discovery_counts(self) -> dict[str, int]:
        return dict(self.conn.execute(
            "SELECT board_status, COUNT(*) FROM companies GROUP BY board_status").fetchall())

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
            "failed_boards": [
                (r[0], r[1]) for r in c.execute(
                    "SELECT board_key, error FROM collection_runs r WHERE ok = 0 AND started_at = "
                    "(SELECT MAX(started_at) FROM collection_runs WHERE board_key = r.board_key)"
                ).fetchall()
            ],
        }
