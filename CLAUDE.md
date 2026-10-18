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
-    - One step at a time; the user reviews the uncommitted changes, then commits and pushes.
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

Round 3, seen on the 2026-10-19 runs, roughly in priority order:

1. **Discovery attaches same-name companies' boards.** Slug guessing found `lever:bloom` (a Toronto
   PR agency), `ashby:asimov` (a biotech) and `lever:sapling` (one posting titled "pr9jp") for
   unrelated YC startups. Verify a probed board before saving it, e.g. the posting URLs/text mention
   the company's website domain, or the board's company name matches.
2. **Discovery checks the smallest teams first.** The 80 companies checked were mostly 2-5 people
   in the US; 17 new boards added 1 B row. Prioritise India-located companies and teams of ~10-300.
3. **"Solution engineer" (singular) slips through.** Netomi "Agentic Solution Engineer" (Gurugram)
   is B 58; `title_exclude` only has "solutions engineer".
4. **Unspecified-track roles fill B** (11 of 12). Worth checking whether some state a fresher-friendly
   range ("0-2 years") the track detector misses, or whether B should need a fresher signal.
5. **HN "hybrid / remote available" + visa.** Typify "San Francisco (hybrid / remote available)",
   "VISA" is now `remote_restricted`; it may deserve the abroad-with-sponsorship path. Low priority
   (it was skip before).
