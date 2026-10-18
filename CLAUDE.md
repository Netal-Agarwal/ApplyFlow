# ApplyFlow - context for Claude Code

Read this first. It carries the decisions made while planning the project, so you
don't have to rediscover them.

## What this is

A job-hunt engine for one person: a final-year B.Tech IT student (graduating May
2027) looking for a **6-month internship from January 2027 and/or a new-grad SDE
role from mid-2027**. Strongest fit is backend (Python, FastAPI, Flask, Node, REST,
Postgres/Mongo, AWS Lambda/serverless). Full-stack and AI-application roles are
secondary; ML/data/platform are a small stretch bucket.

Targets: Bangalore, Hyderabad, Delhi/Gurgaon first; remote roles open to India;
onsite abroad only with explicit visa sponsorship.

**Startups first.** They hire freshers more readily and a cold email reaches a
decision-maker. Big companies are kept but mostly filter out (senior roles).

## Phases

1. Discovery & scoring - done (PR #1, #2): collectors, YC discovery, board detection,
   scoring, HTML/CSV report.
2. Contacts & drafting - next: find the right contact per company (founder/CTO for
   small teams, EM/recruiter for larger - see `contact_hint`), research the company,
   draft a personalized email per opening, review/approve screen. An LLM is allowed
   for research/drafting ("pipeline with agentic parts"), never for sending decisions.
3. Sending & follow-ups: Gmail API (OAuth) from the user's **personal Gmail**.
   Ramp from ~10-15/day to 30-40/day max. Randomized gaps, business hours in the
   recipient's timezone, no open-tracking pixels, no attachments (resume link only),
   reply detection stops sequences, 1-2 follow-ups, permanent do-not-contact list.
4. Insights: reply/interview rates by source, tier, template.

Later: FastAPI + SQLAlchemy dashboard (user's own stack) replacing the CLI report;
`store.py` is the only module that should need to change for the DB move.

## Hard rules

- **Human approval before sending**, at least until drafts prove reliable. Then
  loosen (e.g. auto-send tier B and follow-ups), never for tier A.
- **Email hiring people only** (founders, CTOs, EMs, recruiters). No referral asks.
- **Polite collection**: honest User-Agent, ≤1 req/sec/host, backoff on 429/5xx,
  robots.txt for company websites. Never scrape LinkedIn, Wellfound, Naukri,
  Instahyre (ToS). Free sources only - no paid APIs.
- **Personal data stays out of git**: `config/profile.yaml`, `data/`, `output/` are
  gitignored. Commit only `profile.example.yaml` with placeholders.
- Success = replies and interviews, not emails sent.

## Working conventions

- Python 3.10+, deps: requests, PyYAML (pytest for dev). Keep it light.
- Tests are offline (`pytest`), against fixtures in `tests/fixtures/` that mirror
  the real API formats. Add a fixture + test for every behaviour change.
- One step at a time; the user reviews the uncommitted changes, then commits and  pushes.
- Never run git commit, git push, or open PRs. Leave changes uncommitted, summarize them,
  and suggest a commit message; the user commits and pushes.
- The user develops on Windows (cmd / PowerShell). Give Windows commands.
- Every scoring decision appends a reason string - keep it that way; `explain`
  and `stats` depend on it.

## Commands

```
python -m applyflow discover      # YC startups -> detect their boards (40/run)
python -m applyflow run           # collect + score + report
python -m applyflow stats         # tiers, top rejection reasons
python -m applyflow explain "..." # why one job scored what it did
pytest
```

## Calibration backlog

Round 1 (from the first live run, 2026-10-11) is done - 9 fixes, see `tests/test_calibration.py`:
Ashby remote/hybrid, US-only (citizen/clearance/ITAR) roles, freshness, per-city duplicates,
boilerplate ATS emails, Greenhouse board names, mid-level titles, fresher roles reaching A,
HN "(X role, not a Y role)" headers.
Live 2026-10-18: A=2, B=42, skip=64 -> A=4, B=17, skip=26 (~5,400 openings, 34 boards + HN).

Round 2 (2026-10-19) is done - 7 items, see `tests/test_calibration_round2.py`: security and
devrel titles excluded, remote tied to a foreign place / "residents only" is restricted, relocation
without a visa word isn't sponsorship, `fresher_skill_floor` (25/50) for primary-family fresher
roles in preferred cities, vague HN headers ("Multiple ... roles", "(see post)") -> skip. Odoo's
"Visa sponsorship + relocation support" was checked and is a genuine offer.
Live: A=4, B=17, skip=26 -> A=4, B=11, skip=24 (5,394 openings). Then 2x `discover` (+17 boards,
+63 email candidates): 5,538 openings, A=4, B=12, skip=25.

Round 3 (2026-10-19) is done - see `tests/test_calibration_round3.py`: a board guessed from a slug
is only saved as found when the board points back to the company (its website - decisive either
way - or, failing that, its ATS name, or postings mentioning the domain/name; "previously <name>"
covers renames like FamPay -> FamApp); otherwise it's "unverified", not collected, and listed in
`startups_to_email.csv` with the reason. `verify-boards` re-checks saved boards (demoted 9 of 25,
later promoted FamPay back). Discovery is India-only by default (`discovery.yc.regions_any`), queue
ordered India, then `preferred_team_size` 10-300. "solution engineer" excluded.
Live: 5,538 openings, A=4, B=12 -> 5,500, A=4, B=11 (Netomi solution engineer out).

Round 4, seen on the 2026-10-19 round-3 run, roughly in priority order:

1. **The India discovery pool is exhausted.** YC's hiring.json has only 48 India-located companies
   and all were checked in the last 30 days, so both `discover` runs checked 0. Next free sources:
   yc-oss's full company list filtered to India (not just `isHiring`), re-checking "none" companies
   after `recheck_days`, and hand-curated India seeds with Greenhouse/Lever/Ashby/Workable boards.
2. **Name-only verification is weak.** Morph (Greenhouse name), Pillar (Lever page title, 0 postings),
   Raven (Workable name) and Brumby are "found" only because a name matched and the board shows no
   website - exactly the same-name case. greenhouse:morph posts a "Founding Research Scientist" in
   NYC while YC's Morph is morphllm.com in Ohio. Consider a location check or a "found (weak)" status.
3. **Weekday is a recruiting marketplace.** workable:weekday-1 has 1,025 postings for client
   companies, so its B rows ("Nodejs developer" 78, "Backend & AI" 74) aren't Weekday's own roles and
   the "founder or CTO" contact hint is wrong. Flag aggregator boards (postings >> team size).
4. **Old non-India email candidates.** `startups_to_email.csv` has 103 rows but only 42 are in India;
   the rest were discovered under the old "Remote" filter. Filter or drop companies that no longer
   match the discovery filters.
5. **Unspecified-track roles fill B** (10 of 11). Check whether some state a fresher-friendly range
   ("0-2 years") the track detector misses, or whether B should need a fresher signal.
6. **HN "hybrid / remote available" + visa.** Typify "San Francisco (hybrid / remote available)",
   "VISA" is `remote_restricted`; it may deserve the abroad-with-sponsorship path. Low priority.
