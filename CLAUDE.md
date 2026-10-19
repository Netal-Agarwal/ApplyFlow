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
2. Contacts & drafting:
   - **2a contacts - done (2026-10-19)**, see "Phase 2a notes" below and `tests/test_contacts.py`.
   - **2a.1 trustworthy contacts - done (2026-10-19)**, see "Phase 2a.1 notes" and
     `tests/test_contacts_2a1.py`.
   - **2b drafting - next**: research the company, draft a personalized email per opening
     (and a general "I'd love to work with you" email for `general_outreach` targets), with a
     review/approve screen. An LLM is allowed for research/drafting ("pipeline with agentic
     parts"), never for sending decisions. Candidate phone/LinkedIn/GitHub for signatures are
     in `profile.yaml` (`candidate.*`).
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
python -m applyflow verify-boards # re-check discovered boards belong to their company
python -m applyflow contacts      # who to email at each target (--company NAME, --force)
python -m applyflow explain-contact "..."  # every contact candidate and why the primary won
pytest
```

## Phase 2a notes (contacts)

- Targets: companies with an A/B opening (never `recruiting_marketplaces`, nor postings saying
  "one of our clients" / "our client") + India-located discovered companies with no readable board
  (`general_outreach`). Domain from seeds.yaml `website`, discovery, the HN header URL, or the
  board's own site link; none -> "no_domain" (retried every run, cheap).
- Sources in trust order, stopping once a fit-0 verified/strong contact is found: posting emails
  (verified) -> company site, homepage + 5 likely pages, robots.txt respected (personal email
  verified, careers@/jobs@ fallback; addresses not tied to a named person and not first.last-shaped
  are only fallback) -> YC company page founders (`/companies/<slug>` is allowed by YC's robots.txt;
  only name + title used) -> GitHub org linked from the site (unauthenticated, 60 calls/hour;
  stops at `x-ratelimit-remaining: 0` until reset) -> pattern: a published first.last etc. confirms
  a pattern (strong), else first@domain for a named founder/CTO/EM (guessed). No SMTP probing.
- Ranking: role fit for `contact_hint` first, then confidence (verified > strong > guessed > fallback).
  Top 3 stored in `contacts`; every candidate + reasons in `contact_checks.details` (`explain-contact`).
  Re-checked at most every `contacts.recheck_days` (30). `config/do_not_contact.txt` (gitignored).
- Website "people" are filtered: titles naming another organisation are testimonials/investors
  ("CTO at Globex", "Founder - Heroku"), possessive titles ("Founder's story") aren't people,
  and no first@ guess from an initial ("J Richards").
- Live 2026-10-19: 51 targets (9 role, 42 general outreach). Primary: guessed 42, fallback 1,
  none 4, no_domain 4, verified 0, strong 0. 32 of the guessed names came from YC pages.

## Phase 2a.1 notes (trustworthy contacts)

- **Ranking is deliverability first**: verified > strong > published_inbox > guessed > fallback;
  role fit only breaks ties within a level. At <=50 people (contact_hint "founder or CTO"),
  founders@ / careers@ / jobs@ / hiring@ / talent@ / hr@ and hello@ / team@ / hi@ / contact@ / info@
  are `published_inbox` (founders@ and hiring inboxes first); above 50 they're `fallback`.
  Tool/system addresses (jira@, noreply@, support@, help-*, billing@, privacy@, security@, press@,
  legal@, and "support"/"billing"/... anywhere in the address) are dropped.
- **Posting emails always count** (verified), even with no domain; the domain is then taken from a
  non-freemail posting address (SingleStore: pkhaitan@singlestore.com).
- **MX check** before keeping any contact: DNS-over-HTTPS (Cloudflare JSON, Google as fallback) via
  `PoliteClient.get_api`, cached in `mx_cache` for `contacts.mx_cache_days` (30). No MX / NXDOMAIN /
  null MX -> contacts dropped with a reason; a failed lookup keeps them marked "MX unchecked"
  (failures aren't cached).
- **Guesses carry alternates**: first@ plus first.last@ and firstlast@ when the surname is known (not an
  initial). Guessed / published-inbox people get LinkedIn people-search and Google links in
  contacts.csv and the report - links only, never fetched.
- **Apply-only**: > `contacts.apply_only_team_size` (300) people, or size unknown with a board bigger
  than `large_company_job_count` -> contact_hint "apply via posting", action apply, no contact
  search unless a posting lists an email.
- Live 2026-10-19 (51 targets): role verified 1, published_inbox 1, none 4, apply_only 2,
  no_domain 1; general outreach published_inbox 8, guessed 32, none 1, apply_only 1. MX: 42 domains,
  all with MX, 0 lookup failures. GitHub: 0 emails again (48 Supabase + 19 Odoo members, unauthenticated).

Open points for 2b / later:
1. **Still mostly guessed** (32 of 42 general-outreach primaries). Phase 3 needs bounce handling that
   walks the stored alternates; drafts to a guessed address should say so in the review screen.
2. **GitHub gives nothing unauthenticated** (two live passes, 0 member emails; the 60/hour limit is hit
   on repeated runs). A free token would help, or drop the source.
3. **PostHog has no domain and isn't apply-only**: its seed has no team size and its board has 10
   roles. Add `website:` (and `team_size:` if known) to the seed, as for any seed without one.
4. **Odoo / Supabase / Sarvam / Enveritas: none.** Big or mid companies with unknown size that publish
   no usable address; they're not apply-only because their size is unknown. Adding `team_size:` to
   seeds (or a size source for HN companies) would route the big ones to apply-only.
5. Role targets' `roles` column is captured at lookup time; it refreshes only on a re-check.

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
