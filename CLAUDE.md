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
- One PR per step; the user reviews and merges.
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

## Calibration backlog (from the first live run, 2026-10-11)

First live run: 4,543 openings from 25 sources -> A=0, B=37, skip=57, rejected=4,449.
The B list (`output/opportunities.csv`) exposed these problems, roughly in priority order.
Fix each with a test, then re-run live and compare `stats` before/after.

1. **Bogus "remote" for Ashby jobs.** Notion "SF, California" and Cohere
   "Canada | Dubai | London | US" were classed `remote_unclear` and kept. Cause:
   `isRemote: true` on hybrid roles. Trust remote only when `workplaceType` is
   "Remote" (or a location string says remote); otherwise these are
   `abroad_onsite` and need sponsorship.
2. **Palantir US-government roles kept as "sponsored".** ~20 of 37 B rows. Likely a
   false-positive sponsorship match in boilerplate. Add negative signals: "U.S.
   citizen", "US person", "security clearance", "ITAR", "must be eligible for
   clearance". Log which phrase matched in the reason string.
3. **Stale evergreen postings.** Rows dated 2014, 2020, 2021. Add a freshness
   factor: penalize postings older than ~120 days, reject older than ~365 unless the
   board re-published them.
4. **Duplicates across locations.** Same company + title appears 2-3x (one per
   city). Group by (company, normalized title), merge locations, keep best score.
5. **Boilerplate emails.** `hr@rubrik.com` comes from an accommodations/EEO
   paragraph. For ATS sources, drop emails near words like accommodation,
   disability, EEO, privacy, benefits - or keep ATS emails only when the text
   invites applications. HN emails are fine. This matters for Phase 2.
6. **Company names from the ATS**: "Rubrik Job Board" - prefer the seed/discovered
   name over Greenhouse's `company_name`.
7. **Mid-level titles slip through**: add "intermediate", "mid-level", "mid level",
   "experienced" to `title_exclude` (GitLab "Intermediate Backend Engineer").
8. **Nothing reaches tier A.** India internships (Rubrik winter intern 49, Stripe
   intern Bengaluru 62) should be top-tier. Ideas: when track is internship/new_grad,
   unstated experience is expected - score it 10/10, not 4/10; lower
   `skill_score_cap` (30 -> ~22) because real JDs mention fewer of the listed
   skills than fixtures; re-check tiers on real data afterwards. Target: genuine
   India/remote fresher roles in A, US-only and evergreen noise out of A/B.
9. **HN title parsing**: "Product Engineer (Product Manager role, not a Full-stack
   role)" was kept as fullstack. Low priority.
