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
   - **2b drafts & send-ready report - done (2026-10-19)**, see "Phase 2b notes" and
     `tests/test_drafting.py`. An LLM is allowed for research/drafting, never for sending decisions.
   - **2c outreach pool at scale - done (2026-10-20)**, see "Phase 2c notes" and `tests/test_pool_2c.py`.
3. Sending & follow-ups: Gmail API (OAuth) from the user's **personal Gmail**.
   Ramp from ~10-15/day to 30-40/day max. Randomized gaps, business hours in the
   recipient's timezone, no open-tracking pixels, resume linked (attached only for recruiters/HR or
   when a posting asks for one by email - the 2b `resume_action` rule),
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
python -m applyflow draft-pack    # homepage context, template drafts, packets (output/drafts/packets)
python -m applyflow import-drafts # load output/drafts/written/<id>.md, validated
python -m applyflow queue --date YYYY-MM-DD   # the day's batch (stored); report shows it
python -m applyflow mark-sent <id> | --batch YYYY-MM-DD ; mark-bounced <id>
python -m applyflow add-target "Co" --website co.com [--note] [--email] [--name] [--role]
python -m applyflow pool [--rescore]   # 2c: every source -> deduped pool, hiring-likelihood score, top ~400
python -m applyflow contacts --limit 50 [--retry-unread]   # resumable batches with an ETA
python -m applyflow verify-sources     # every published address must appear on the company's own site
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

## Phase 2b notes (drafts, queue, report)

- **Facts**: `candidate.facts` in profile.yaml (from `config/private/Netal_Agarwal_Resume.pdf`, gitignored) is
  the only thing drafts may say about the candidate. Validation enforces it for numbers and named
  technologies (a tech from the company's own text is allowed only in a sentence about them: "your ...").
- **Modules**: `drafting.py` (facts, inputs, templates, validation, packets, Gmail URL) is pure;
  `outreach.py` (items, send tiers, bounces, queue, context fetch, import, add-target) talks to the store.
  Target id = company_key with non-alphanumerics as "-" (`yc-clickpe`, `co-singlestore`, `manual-acme`).
- **Two email types**: role (names the role, one company line, best project by lead_project + JD stack,
  availability, low-pressure ask) and general outreach (what stood out from one-liner / site, contribution,
  "open to a January 2027 intern or 2027 new grad, or a short chat"). Manual targets use general outreach.
  "Hi <First>," for a person, "Hi <Company> team," for an inbox (`display_company` drops ", Inc.").
  Remote-global = role `remote_open`, or a company location that's "Remote" / outside India -> time-zone line.
- **Validation** (`drafting.validate`, also run on templates): 120-160 words between greeting and sign-off,
  exact greeting, exact signature after a sign-off line, resume line matching `resume_action`, no
  brackets/placeholders, role named, time-zone line when remote, no buzzwords, subject <=90 chars and not
  clickbait, numbers/tech from facts. A written draft failing any rule stays on the template with the reasons
  (also when a bounce moves the target to a different person: greeting no longer matches).
- **resume_action**: attach for recruiting / hiring-inbox contacts (hr@, talent@, careers@...) or a posting that
  asks for a resume by email; else link (signature has the resume URL). Gmail compose links can't attach: the
  user attaches the PDF by hand.
- **Send tiers**: 1 = verified or recruiter/HR (published), 2 = published inbox / strong / fallback, 3 = guessed,
  behind the tier-3 gate (see "Phase 2b polish"). One per company (domain), DNC (any of
  the target's addresses or its domain) and already-sent skipped. `queue` stores `send_queue`; re-running a day
  keeps entries already sent. Bounce order: primary, its alternates, then each backup and its alternates.
- **Context**: `company_context` (homepage title, meta description, ~600-char snippet) captured by `read_site`
  during `contacts`, or fetched by `draft-pack` (one page, robots.txt respected; blocked/failed recorded, not
  retried unless `--refetch`). `get_page` now decodes UTF-8 when no charset is declared (was Latin-1 mojibake).
- Live 2026-10-19: 51 targets (9 role, 42 general outreach); ready 42 (T1 3, T2 7, T3 32), no contact 5,
  apply-only 3, no domain 1. Context: 45 ok, 2 robots-blocked (Morphle, Helloverify), 1 HTTP 403 (Enveritas).
  42 Claude-written drafts imported (all pass), 9 templates (the non-send-ready targets). Day-1 batch = 10
  (T1 + T2); the 32 guessed founders wait for the tier-3 gate.

Open points for Phase 3 / later:
1. Only 10 tier-1/2 targets exist: after the first batch, nothing else is sendable until tier 3 unlocks
   (2 days later at the earliest). More published inboxes (or verified contacts) would fill that gap.
2. Written drafts are static text: a contact or role change only shows up as a validation failure (template
   fallback), never as an updated written draft. Re-run `draft-pack` and rewrite those.
3. The fact check covers numbers and named technologies, not every claim; the review screen is still the guard.
4. Gmail compose URLs get long (~2-3 KB); fine for Gmail today, but Phase 3 should send via the API instead.

## Phase 2b polish notes (2026-10-19)

- **Credentials line** in every email, right after the project paragraph: the DRDO (DEAL lab) internship and the
  published patent, in one short sentence. Facts: `experience[0].short` ("DRDO's DEAL lab") and `patent.short`
  ("Android attention management"); `Facts.credentials()` builds the template wording. Validation now fails a
  draft that doesn't mention the internship's org (`org_short`, "DRDO") and the word "patent". The 42 written
  drafts rotate six wordings. The template's old DRDO / patent filler sentences are gone (one DRDO-detail filler
  remains for short drafts).
- **Draft review**: company lines rewritten as own-voice observations rather than close paraphrases of site copy
  (Cardboard, SingleStore, RocketSDR, Landeed, xPay, Infer, Explorex, Rivia, Zuddl, PrimeVault, Optifye, Drip,
  ClickPe, AlgoTest, IMT Care, Swadesh, BusinessOnBot, Kalam, GroMo, Binks, ZiffyHomes, ...); closings vary across
  three low-pressure wordings and never reuse the opening's ask ("considered"). The role template closes with
  "I'd be happy to talk whenever it suits you." The style guide says all three things.
- **Tier-3 gate** (`outreach.tier3` in the profile, `outreach.tier3_gate`), replacing day 8:
  locked until `unlock_after_days` (2) calendar days after the first mark-sent, and only if no bounce has been
  recorded before tier 3 starts; then `start_per_day` (5), counted inside `daily_batch`; `ramp_per_day` (10) from
  `ramp_after_days` (3) after the first tier-3 send; paused whenever bounces / sends >= `max_bounce_rate` (5%),
  resuming when more tier 1-2 sends bring it back under. `queue` and the report print the gate's reason.
  `outreach_events.send_tier` records each send's tier (needed to know when tier 3 started).
- "rails" dropped from the fact-check tech list (false positive on "payment rails"; "ruby on rails" stays).
- Live: 42/42 written drafts pass; 2026-10-20 preview = the same 10 tier-1/2 targets; tier 3 "locked: nothing
  sent yet".

## Phase 2c notes (outreach pool at scale, 2026-10-20)

- **Sources** (`sources.py`, config in `config/sources.yaml`, all via the polite client): (a) yc-oss `all.json`:
  active India companies (any hiring status) + active "Fully Remote" teams of 10-150 (`remote_global`, cap 80);
  (b) portfolio pages through one generic regex collector: Better Capital, Axilor, Prime VP, Antler (India tag,
  `?0b933bfd_page=N`); skipped 100X.VC (404), Blume (no websites), Speciale (robots.txt), Surge/Titan/Kae/Kalaari/
  Stellaris/Nexus/Venture Catalysts (no name+website pairs); (c) Inc42 Funding Galore RSS (`content:encoded`
  carries each week's table; only 24 weeks, May-Oct 2026) - rounds matched by domain/name, else the website from
  `inc42.com/company/<slug>/` (cached in `funding_lookups`), else dropped; Entrackr skipped (prose, no websites);
  (d) GitHub raw files: hemanth/bangalore-startups (`startups.json`), rushout09/ai-startups-india (README table).
  `merge` dedupes by domain (yc > portfolio > funding > list for key/name; fields filled, sources/funding unioned).
- **Hiring likelihood** (`pool.py`, weights in `pool.weights`): funding 6m 25 / 12m 15, team 10-150 20 (2-9 10,
  151-500 8, unknown 6), careers page/board/YC hiring 12, tech fit high 20 / one word 10 / D2C-food-fashion -10,
  preferred city 10, batch/founded <=3y 10 (<=6y 5), India 8, remote-global 0; capped 0-100, every factor a reason.
  Selection: ~400 targets in all; existing contact checks / manual / role targets always kept; not selected:
  >300 people, Series C+ or $50 Mn+ rounds with unknown size ("likely 300+"), recruiting marketplaces, past the
  remote-global cap. `pool --rescore` re-scores (careers pages found by `contacts`) and keeps selections.
- **Contacts**: pool targets join `build_targets` (key `yc:<slug>` or `pool:<domain>`, unknown size -> founder or
  CTO). `contacts --limit N` resumes where it stopped; `--retry-unread` redoes lookups that read no page. Every
  contact stores `proof` (company site | posting | manual | github profile); `verify-sources` re-checks old ones and
  demotes to guessed if the address isn't on the company's domain (2026-10-19: 15 confirmed incl. RocketSDR's
  contact@ on /about, 0 demoted). Inbox preference: founders@/careers@/talent@/hr@/jobs@ > hello@/hi@/team@ >
  info@/contact@ (`inbox_preference`); "join-us@" counts as a hiring inbox.
- **Drafts**: greeting "Hi <First>," (name from the post/page, an HN poster handle - `jobs.poster` - or a
  first.last address), "Hi," for a person whose name isn't known, "Hi <Company> team," only for shared inboxes.
  hello@/hi@/team@/info@/contact@ need an application subject ("Internship / new-grad application – Netal Agarwal
  (Backend|AI|Full-stack|Cloud, Jan 2027)"). Funding may be mentioned only with a sourced signal; round and month
  must match it ("raises the bar" and a company's own "fundraising platform" don't count). Drafts queued before 2c
  (the 10 in the 2026-10-21 batch) are locked to the 2b rules (`draft_rules`), so the scheduled Gmail drafts
  stay valid. `output/drafts/drafts_2c.py` + `write_2c.py` wrote 100 drafts (never touching `protected_2b.txt`).
- **Queue**: order within a tier is role targets, then hiring likelihood, then inbox preference; a target in an
  earlier day's stored batch that isn't marked sent is held for that day (`held_earlier`).
- **HTTP**: a DNS/connection failure on robots.txt is "site unreachable" and not cached (it used to cache
  "Disallow: /" for the run, which hit ~80 lookups during a network drop).
- Live 2026-10-20: before dedupe 2,850 (YC India 157, YC remote 338, Better 183, Axilor 52, Prime 53, Antler 83,
  Bangalore list 1,693, AI list 22, Inc42 271 new + 100 matched); after 2,768 (India 2,430, remote 338); funding
  signal 300 (all within 6 months: the feed only reaches May 2026). Selected 391 pool + role/manual = 401 targets.
  Primary contacts: verified 10, published_inbox 115, guessed 169, fallback 15, none 88, apply_only 3, no_domain 1.
  Ready 309 (T1 50, T2 90, T3 169). Drafts: 142 Claude-written (42 from 2b + 100 new), 259 template, 0 failing.

Open points for Phase 3 / later:
1. **Funding history is short**: Inc42's feed has 24 weeks, so "within 12 months" adds nothing over 6 months yet.
   Keep running `pool` weekly to build history (rounds are re-read each run, not stored), or store rounds.
2. **Funded companies lack size**: 132 selected come from funding news with no headcount; big ones with no
   amount slip through (Darwinbox). A size source (Inc42 profile "employees" field?) would help.
3. **The Bangalore list is old** (1,693 entries, mostly 2014-era): it rarely makes the top 400 (4 selected) but
   inflates the pool; consider dropping it or requiring a working homepage.
4. **88 targets have no contact** (30 sites unreadable: robots.txt, 403, down). They need a manual look or drop.
5. Tier 3 (169 guessed) is still behind the gate; at 20/day the T1-2 supply (140) lasts about 11 days.

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
