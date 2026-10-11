"""Orchestration: collect from every source, score everything, probe for new boards."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from applyflow.collectors import ATS_COLLECTORS, HNWhoIsHiringCollector
from applyflow.config import Profile
from applyflow.http import FetchError, PoliteClient
from applyflow.models import Company, ScoreResult
from applyflow.scoring import score_job
from applyflow.store import Store

Log = Callable[[str], None]


@dataclass
class CollectSummary:
    boards_ok: int = 0
    boards_failed: list[tuple[str, str]] = field(default_factory=list)
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_closed: int = 0


def collect(
    store: Store,
    client: PoliteClient,
    companies: list[Company],
    include_ats: bool = True,
    include_hn: bool = True,
    log: Log = print,
) -> CollectSummary:
    summary = CollectSummary()

    def record(board_key: str, jobs) -> None:
        counts = store.upsert_board(board_key, jobs)
        store.log_run(board_key, True, counts["seen"])
        summary.boards_ok += 1
        summary.jobs_seen += counts["seen"]
        summary.jobs_new += counts["new"]
        summary.jobs_closed += counts["closed"]
        log(f"  {board_key:<45} {counts['seen']:>4} jobs ({counts['new']} new, {counts['closed']} closed)")

    def fail(board_key: str, error: Exception) -> None:
        store.log_run(board_key, False, error=str(error))
        summary.boards_failed.append((board_key, str(error)))
        log(f"  {board_key:<45} FAILED: {error}")

    if include_ats:
        for company in companies:
            board_key = f"{company.ats}:{company.slug}"
            collector = ATS_COLLECTORS[company.ats](client)
            try:
                jobs = collector.fetch(company)
                for job in jobs:
                    job.team_size = company.team_size
                record(board_key, jobs)
            except (FetchError, KeyError, TypeError, ValueError) as exc:
                fail(board_key, exc)

    if include_hn:
        hn = HNWhoIsHiringCollector(client)
        try:
            story = hn.latest_story()
            if story is None:
                log("  hn: no 'Who is hiring?' thread found")
            else:
                story_id = str(story["objectID"])
                log(f"  hn: {story.get('title')}")
                record(f"hn:{story_id}", hn.fetch(story_id=story_id))
        except (FetchError, KeyError, TypeError, ValueError) as exc:
            fail("hn:latest", exc)

    return summary


def boards_to_collect(store: Store, seeds: list[Company]) -> list[Company]:
    """Hand-picked seeds plus every supported board that discovery found, without duplicates."""
    from applyflow.config import SUPPORTED_ATS

    companies = list(seeds)
    seen = {c.board_key for c in seeds}
    for company in store.discovered_boards(SUPPORTED_ATS):
        if company.board_key not in seen:
            seen.add(company.board_key)
            companies.append(company)
    return companies


def score_all(store: Store, profile: Profile) -> list[ScoreResult]:
    results = [score_job(job, profile) for job in store.active_jobs()]
    store.save_scores(results)
    return results


# ---------------------------------------------------------------------------
# Probing: find which ATS (if any) a company's board lives on.
# ---------------------------------------------------------------------------

def slug_candidates(name: str) -> list[str]:
    """'Sarvam AI' -> ['sarvamai', 'sarvam-ai', 'sarvam', ...]"""
    base = name.lower().strip()
    words = re.findall(r"[a-z0-9]+", base)
    if not words:
        return []
    stop = {"inc", "ltd", "llc", "pvt", "private", "limited", "technologies", "technology", "labs", "the"}
    core = [w for w in words if w not in stop] or words
    candidates = [
        "".join(words), "-".join(words), "".join(core), "-".join(core), core[0],
        "".join(core) + "hq", "".join(core) + "inc", "".join(core) + "labs",
    ]
    seen, ordered = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def probe(client: PoliteClient, name: str, slugs: list[str] | None = None) -> list[tuple[Company, int]]:
    """Try each slug on each ATS. Returns (company, job_count) for boards that exist."""
    found = []
    for slug in slugs or slug_candidates(name):
        for ats, collector_cls in ATS_COLLECTORS.items():
            company = Company(name=name, ats=ats, slug=slug)
            try:
                jobs = collector_cls(client).fetch(company)
            except FetchError:
                continue
            except (KeyError, TypeError, ValueError):
                continue
            # Ashby and Workable answer unknown boards with an empty list; only count real boards.
            if jobs or ats not in ("ashby", "workable"):
                found.append((company, len(jobs)))
    return found
