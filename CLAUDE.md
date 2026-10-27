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
   - **2c.1 quality cleanup - done (2026-10-20)**, see "Phase 2c.1 notes" and `tests/test_pool_2c1.py`.
   - **2d email format + bounce-safe batches - done (2026-10-22)**, see "Phase 2d notes" and
     `tests/test_format_2d.py`.
   - **2e refill with safe contacts - done (2026-10-27)**, see "Phase 2e notes" and `tests/test_refill_2e.py`.
   - **2f genuine supply + per-type bounce gate - done (2026-10-28)**, see "Phase 2f notes" and
     `tests/test_supply_2f.py`.
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
python -m applyflow mark-sent <id> | --batch YYYY-MM-DD ; mark-bounced <id> [--stop]
python -m applyflow add-target "Co" --website co.com [--note] [--email] [--name] [--role]
python -m applyflow pool [--rescore]   # 2c: every source -> deduped pool, hiring-likelihood score, top ~400
python -m applyflow pool --reselect    # 2c.1: re-apply large-company / tech-fit rules offline, refill dropped slots
python -m applyflow contacts --limit 50 [--retry-unread]   # resumable batches with an ETA
python -m applyflow verify-sources     # every published address must appear on the company's own site
python -m applyflow hn-archive --from 2026-05 --to 2026-10   # 2e: HN "Who is hiring?" archive -> role targets
python -m applyflow yc-jobs            # 2e: ycombinator.com/companies/<slug>/jobs for every YC pool company
python -m applyflow import-targets     # 2e: config/private/manual_targets.txt (Company | website | role seen | note)
python -m applyflow role-check [--all] # 2e: role match from the ATS board / careers page
python -m applyflow contacts --deep    # 2e: 12 more pages (footer, privacy, terms, team, join-us) for unsafe targets
python -m applyflow pool --reselect --target-size 650   # 2e: a bigger selection for one run
python -m applyflow safe-contacts      # 2e: unsent contacts by type, sending days covered (2f: by sector, 2f caps)
python -m applyflow supply --all       # 2f: fintech lists (RBI / Sahamati), consultancy seeds, GitHub lists, YC fintech
python -m applyflow guess-evidence     # 2f: which stored guesses are backed by evidence (offline)
python -m applyflow classify-published [--apply]   # 2f: "other" published addresses -> hiring / unsendable
python -m applyflow referrals          # 2f: output/referrals.csv (large / apply-only companies with a fit role)
python -m applyflow contacts --deep --shard 0/3     # 2f: new targets too; shards run side by side
python -m applyflow role-check --checked-since 2026-10-27   # 2f: role matches for targets looked up since a day
python -m applyflow queue --date D [--no-fresh]     # 2f: per-type gate + freshness re-check of each source page
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

## Phase 2c.1 notes (quality cleanup, 2026-10-20)

- **Large companies** (`pool.large_reason`): unknown team size + a round of `pool.large_company.min_series` (C) or
  later (`late_round`: "Pre-Series C" isn't late) or one round >= `min_amount_mn` ($50 Mn), or a profile-level
  `never_cold_email` match (names or domains, any size; starts with Darwinbox) -> large: not selected, hint
  "apply via posting", and `build_items` gives an already looked-up target status `apply_only`. Fixed a 2c bug:
  `_amount_mn` read "$250K" as $250 Mn, so 49 seed-stage companies were wrongly "likely 300+" (18 now selected).
- **Product fit** (`pool.product_fit`, non-YC pool companies only): an investment-firm name (Capital / Ventures /
  Partners ...) with investing words on the site, or a `non_software` phrase (biofuel, solar, hardware, logistics,
  fleet, wholesale, nbfc, wealth managers, services company, consulting, construction, hydrogen, synthetic biology,
  ...) in the name / sector / one-liner / homepage fails unless the homepage or one-liner has a `software_product`
  phrase (saas, api, sdk, dashboard, no-code, book a demo, integrate, ...). Funding-news-only companies must also
  reach `funding_only_min_tech` (20) tech-fit points; an Inc42 software subsector (Application Layer, Horizontal
  SaaS, Fintech SaaS, Investment Tech, Digital Learning Platform, ...) counts as one more product word. Lists and
  thresholds in `DEFAULT_POOL` (config.py), overridable in profile.yaml `pool:`.
- **Re-selection** (`pool --reselect`, offline): already looked-up general-outreach targets stay in only while they
  pass; **protected** targets (`store.protected_target_ids`: any send_queue row, sent/bounced event, or draft_rules
  lock) are never dropped. Slots refill by hiring likelihood; ties go YC > portfolio > funding > list (`rank_key`),
  so refills don't pick 2014-era Bangalore-list names by alphabet. Dropped pool targets get status `dropped`
  (never queued; the report tag's tooltip shows why). Homepages only arrive with `contacts`, so loop
  `contacts --limit 50` -> `pool --reselect` until "0 dropped" (took 6 passes).
- Bug-report inboxes (reportbug@, bugs@, bugreport-*) are tool addresses now (M2P had reportbug@ as its contact).
- Live 2026-10-20: 69 net dropped from the 2c selection (42 non-software, 25 funding-only below the floor, 1
  investment firm, Darwinbox), 69 refilled (all India; remote-global stays at the 80 cap); 391 pool + role/manual
  targets. Of the 2026-10-22 preview's named companies, BiofuelCircle, Oorjaa, Ecofy, Nexedge Capital, Aeron
  Systems, Hang Ten, Fairdeal.Market are dropped; Innefu and Kapture CX kept. Ready 306 (T1 33, T2 96, T3 177).
  Drafts: 194 Claude-written (75 new via `output/drafts/drafts_2c1.py` + `write_2c1.py`: 25 new tier 1-2 targets
  and the top 50 tier 3), 287 template; all written drafts of ready targets pass (23 failing belong to dropped /
  apply-only targets). The 2026-10-21 stored batch (10) and its 2b-locked drafts were not touched.

Open points:
1. **The India pool past rank ~180 is thin**: refills at score 34 are portfolio companies with no one-liner and
   unknown size (some still weak: Cancer Clinic, Pepper Farms, The Wedding Company). A size source and a second
   funding feed would help more than further keyword tuning.
2. **Keyword product fit is coarse**: Detect (industrial CV software, "hardware" in its one-liner) and Rovia-style
   consumer fintech apps are borderline. Override by hand with `never_cold_email` or by editing the lists.
3. Tier 1 shrank from 50 to 33 (most dropped preview companies were careers@ inboxes); T1-2 supply is ~129, about
   6-7 days at 20/day before tier 3 must be open.

## Phase 2d notes (email format and bounce-safe batches, 2026-10-22)

- **Middle section** of every email: the two projects that best match the company (`choose_projects`: posting
  stack + lead project for roles, else project keywords in the company's text; focus word breaks ties), one
  sentence each (`projects[].short` in the facts), the credentials line, then "Day to day I work in ..." with 4-7
  facts skills (`choose_skills`: posting, company text, the team's usual stack, the lead project's stack; no
  React / JavaScript unless full-stack or named by the company). 160-200 words.
- **Closing ask** (general outreach): the availability sentence names both roles, so the ask only points back at
  them ("Would either of these fit <Company>'s plans? ..."); `CLOSING_ASKS` rotate by target id. A draft whose ask
  repeats "January 2027 intern" / "2027 new grad" after the availability sentence fails (`restated_roles`).
- **Resume, never both**: attach -> "I've attached my resume." and no Resume: line in the signature
  (`Facts.signature(resume=False)`); link -> the link stays and the body never says "attached".
- **Validation** (2d only): exactly two named projects, a skills line of facts skills only, credentials line,
  no restated roles in the ask, no resume link when attaching. `tests/test_format_2d.py`.
- **Legacy rules**: targets queued or sent when 2d arrived are locked to "2c" in `draft_rules` (once, by a store
  migration; 2026-10-21's 2b locks stay); they keep the one-project, 120-160-word format and their drafts.
- **Bounces**: `mark-bounced <id> --stop` records the bounce plus a `stopped` event: the target gets status
  "stopped" and its alternates / backups are never queued. Used for IOMETE (careers@) and Per Diem (jobs@).
- **Queue order** (`OutreachItem.bounce_rank`): tiers 1 and 2 together, lowest bounce risk first - a verified
  named person (stored name or a first.last address) 0, hiring inbox (careers@ / jobs@ / talent@ / hr@ / work@
  ...) 1, founders@ 2, hello@ / hi@ / team@ 3, any other address 4, info@ / contact@ 5; then role targets, hiring
  likelihood, inbox preference. Tier 3 still comes after, behind its gate. `daily_batch` is 30.
- **Drafts**: `output/drafts/drafts_2d.py` rewrote all 159 unsent written drafts of sendable targets (each
  company's reviewed opening kept; the two projects picked per company in `PAIRS`, overriding the default order
  where another fits better, e.g. Peoplebox leads with CareerForge AI); all pass. The old files are in
  `output/drafts/written_pre2d/`. Dropped / apply-only targets were skipped.
- Live 2026-10-22: 30 sent, 4 bounced (13%) -> tier 3 paused (and it can't unlock anyway: bounces were recorded
  before it started). 2026-10-23 batch: 30 (2 verified people, 7 careers@, 5 founders@, 16 hello@ / team@).
  Ready drafts: 159 Claude-written + 119 templates, 0 failing.

Open points:
1. **Tier 3 can never open under the current gate** (bounces before unlock keep it locked; 4/30 is far above
   5%). The 169-177 guessed founders need a decision: a different gate, or verifying addresses another way.
2. **Hiring inboxes bounce too**: both 2026-10-22 bounces were careers@ / jobs@ (published on the site, MX ok).
   The bounce_rank order assumes hiring inboxes are safer than founders@ - revisit with more send data.
3. 119 ready targets (e.g. Maya Labs, MixRank in the 2026-10-23 batch) still go out on the template.

## Phase 2e notes (refill with safe contacts, 2026-10-23 .. 27)

- **Safe** = verified named person, founders@, hiring inbox (careers@ / jobs@ / talent@ / hr@ / work@ / join-us@ ...)
  (`outreach.contact_kind`, `SAFE_KINDS`). hello@ / hi@ / team@ are capped at `outreach.general_inbox_per_day` (5) in
  `pick_batch`; **info@ / contact@ are never sent to** (status "unsendable"). Also unsendable: an unnamed personal
  address seen only on a privacy / terms / legal page (grievance or privacy officers: `legal_page_person`), and any
  address in the `excluded_contacts` table (reviewed out: sales desk, mockup names, wrong fit; survives re-lookups).
  Role-word locals (customer-success@, partner.institute@) aren't people. A page naming 4+ unnamed personal
  addresses is a mockup or directory (`MOCKUP_ADDRESSES`; Intryc's ticket mockup had 10). Queued / sent / locked
  targets keep their addresses.
- **One company once** (`mark_duplicates`): a ready target whose domain (else name) already has a sent, stopped,
  exhausted or protected target - or a better ready one - becomes "duplicate".
- **Sources** (`refill.py`), each records a `role_matches` row (yes / no / unknown + title, url, experience, salary):
  1. HN archive (`hn-archive`): threads via Algolia `numericFilters`; a post is kept when not filled (`is_filled`),
     a header role passes the title + family filters (`best_header_role`: plurals, "(Rust, MySQL)" kept), score_job
     isn't rejected, the location is India / remote_open, and `extract_contact_emails` finds an address. Newest post
     per company; contacted / DNC companies skipped (`Existing`). Jobs stored with `upsert_jobs` (never closes
     others). Live: 6 threads, 1,478 posts, kept 4 (1,075 no fitting header role, 286 abroad / restricted, 45 years).
     Usable: Logen.io (queued) and CyberAtlas (its post asks for a Finder query and an "HN Software Engineer"
     subject: do it by hand). Proxybase and ArtCraft need Rust: excluded.
  2. YC job pages (`yc-jobs`): the /jobs page's data-page JSON (title, location, type, minExperience, salary,
     skills), the job page fetched for keepers; eng roles, intern / new grad / no stated experience, India or
     remote-India ("IN" -> India). Board key `ycjobs:<slug>`, target key `yc:<slug>`. Live: 495 pages, 148 with
     postings, 602 postings, 3 fit at 2 companies (Cardboard x2 "new grads OK"; ClickPe intern, listed 3+ years ago:
     deactivated as stale). Most postings are non-engineering (359), senior (129) or "1+ years" (56).
  3. Entrackr (`kind: entrackr` in sources.yaml): weekly reports found in Fri-Sun daily sitemaps; `report_text` +
     `parse_entrackr_deals` read the prose; `data/entrackr_deals.json` (reviewed by hand: 169 deals in 12 reports)
     wins. Websites from `domain_candidates` + `homepage_matches` (names the company and shows an India or sector
     signal, so circle.com isn't "Circle") + MX. Live: 108 matched pool companies, 21 resolved, 40 dropped; after the
     2c.1 rules 4 new targets (Disha, Piston, Firi, Swish). Inc42 refreshed (472 rounds, 24 weeks). Companies funded
     in the last `pool.recent_funding_weeks` (12) are selected past `target_size` when they pass the rules.
  4. `import-targets`: config/private/manual_targets.txt, "Company | website | role seen | note"; contact lookup,
     tech fit, role match (the role seen; else YC jobs page, ATS board, careers page), packet + draft (a role email
     when a role is seen or found). The file is created with a header and 2 commented examples; empty so far.
- **Deep search** (`contacts --deep`, `deep_pages`): footer links, then /team /about-us /careers /jobs /join /join-us
  /contact /privacy /terms ..., 12 pages; only targets with no safe contact, never protected ones; a site that reads
  0 pages keeps its stored contacts. Founders' own addresses often sit on privacy pages (grievance officer): kept when
  the name matches; unnamed ones there are unsendable.
- **Email regex fix**: `text._EMAIL_RE` / `_OBFUSCATED_EMAIL_RES` are anchored at a token start and length-capped;
  unanchored, they took minutes on alphabetalabs.com (long unbroken text) and stalled the deep search.
- **Resume update (2026-10-26)**: facts re-read from the new resume (skills + HTML/CSS, automated API testing, Apache
  Spark, Hadoop, XGBoost, NLP, GitHub; - Node.js, Express.js, microservices, MySQL, system design; Judicial AI System
  on Spark / Hadoop over "1,500+ court judgments"; project dates; DRDO across 8 scenarios; patent application number).
  67 unsent drafts had "legal case files" -> "court judgments"; no draft started failing.
- Live 2026-10-27: targets 481 -> 730 (`pool --reselect --target-size 650`). Safe unsent contacts: 1 before, 29 now
  (25 newly found: 12 by normal lookups of new targets, 13 by the deep search, 2 HN posts; 4 already stored but not
  sendable before), 28 of them in the stored **2026-10-27 batch** (7 verified people, 17 hiring inboxes, 4 founders@;
  Claude drafts from `output/drafts/drafts_2e.py` + `openings_2e.json`). After it: 1 safe, 41 hello@, 30 other
  published, 228 guessed. Sent so far 60, bounced 6 (hello@ 3/19, hiring inbox 2/24, info@ / contact@ 1/2, verified
  people 0/8, founders@ 0/7): tier 3 is paused (10% >= 5%).

Open points:
1. **Safe supply is exhausted** after the 27 Oct batch (the next day has 1 safe + 5 hello@). Indian startups rarely
   publish founders@ / careers@; the remaining lever is the 228 guessed founders (34 with a same-format published
   address on their domain), which needs a tier-3 decision: the gate can't reopen while the bounce rate is 10%.
2. hello@ bounced 3 of 19 (16%): keep it capped; consider skipping hello@ at very small companies.
3. YC "1+ years" roles (56) are excluded by the intern / new grad / no-experience rule; the profile's
   max_years_required is 1, so loosening that rule would add a few YC role targets (Infinity, AlgoTest, Apollo).
4. Careers-page role matching is a text heuristic ("AI/ML Engineer Engineering"); 18 of 26 funded targets came back
   unknown.

## Phase 2f notes (genuine supply + per-type bounce gate, 2026-10-27 .. 28)

- **Per-type bounce gate** (`outreach.type_gates`, config `outreach.type_gates`), used by `queue` instead of the overall
  5% tier-3 gate (`tier3_gate` stays for the old `pick_batch` path only): verified named people and founders@ always
  open; hiring inboxes paused while 3+ of their last 20 sends bounced; evidence-backed guesses (10/day) and hello@ /
  hi@ / hey@ / team@ (5/day) paused on the day after one with 2+ of their bounces (and that day itself, once recorded);
  pure pattern guesses and unsendable addresses closed. `mark-sent` records each send's kind (`outreach_events.kind`);
  older sends are classified from the stored contact (`send_records`). Order within a batch: tier 1-2 by bounce risk,
  then companies with a hiring signal (role match, funding in 6 months, 20+ people), then evidence guesses, hello@
  last. Batch rules (`BatchRules`, profile): `require_written_draft` (only validated Claude drafts are queued) and
  `type_gates.general_inbox_max_team` 50 (hello@ only at small startups).
- **Contact kinds**: `contact_kind` adds `evidence_guess`; an inferred address (`guessed` or `strong`) is a pure guess
  (never sent) unless it carries evidence. A website address is a `verified_person` only with a name and a hiring role
  (founder / CTO / eng lead / recruiter); unnamed first.last addresses ("person at the company") are not. "Other"
  published addresses go through `classify_published`: hiring words -> hiring inbox (hrindia@, people@), hey@ ->
  general inbox, support / sales / ops / billing / legal / press / general-contact words, the company's own name or a
  lone unconfirmed first name -> unsendable (recorded in `excluded_contacts` by `classify-published --apply`).
  HIRING_INBOXES gained campus@, freshers@, recruiter(s)@, people@ and suffixed forms (`_HIRING_SUFFIXED`).
- **Evidence-backed guesses** (`contacts.evidence_for_guess`, stored in `contacts.evidence`): a founder / CTO / eng lead
  named on a cited page (own site or YC page), MX ok, and a published personal address on the same domain in the same
  format whose first part is a real first name (`Store.known_first_names`: ~600 names ApplyFlow has seen; so vendor@,
  complaints@, dmca@ never count). The guess moves to that format's address. Live lookups apply it; `guess-evidence`
  backfills offline. 24 stored guesses pass of 606.
- **Large companies** (>300 people, or large by the 2c.1 funding rule): selected in the pool as "hiring inbox only"
  targets (outside target_size); `large_inbox_search` reads careers / jobs / contact / join pages and keeps only hiring
  inboxes published there (pages inviting applications rank first); never named employees or guesses. `build_items`
  enforces it (`OutreachItem.large`). Huge IT-services firms (`pool.it_services`) are added to `never_cold_email`.
  Manual targets whose note says "large" (or whose pool record is large) get the same treatment.
- **Ongoing projects**: `Project.when` / `.ongoing` (dates ending "Present"); `drafting.ongoing_failures` fails "I built
  / I've developed ... <ongoing project>" (rule set "2f"; drafts queued or sent before 2f are locked to "2e" by a store
  migration). 102 unsent unlocked drafts rewritten (`output/drafts/fix_ongoing_2f.py`).
- **Sources** (`applyflow/supply.py`, `supply` command, config in sources.yaml): RBI lists (`parse_rbi_entities`;
  www.rbi.org.in answers HTTP 418 on robots.txt to our User-Agent, so the collector skips it); Sahamati AA / TSP member
  pages (47 listed, 32 verified); 66 hand-written consultancy seeds (54 verified; sector consultancy is exempt from the
  services words but needs one tech word on its homepage, `consultancy_fit`); rshetye/AwesomeIndiaStartups (BSD-3,
  `header_table`, 283 companies, selected via `pool.priority_lists`). `verify_company`: the homepage names it (name
  minus "Software / Technologies ..." too), India signal on the homepage or a contact / about page, MX. Every pool
  company carries `sector` (fintech / consultancy, else startup / other on the item: `supply.sector_for`).
  Remote YC fintechs: 86 jobs pages checked, 0 with a posting open to India. Six manual imports: none had a usable
  address (Razorpay, Porter, Emergent: no hiring inbox on their own pages -> referrals only).
- **Freshness check** (`outreach.Freshness`, on by default in `queue`, `--no-fresh` to skip): each picked address's
  source page is re-read (robots.txt respected) before the batch is stored; for an evidence guess both the published
  same-format address and the person's name (YC pages: in the embedded JSON). Gone (or 404 / 410) -> dropped into
  `excluded_contacts` with the date and URL, and the next target is picked; an unreadable page keeps it ("unchecked").
  **Phase 3's sending engine must run the same check shortly before each send.**
- **Queued addresses**: a target queued but not yet sent keeps the address it was queued with in `build_items` (the
  2f filters had made the report fall back to a stored primary - info@ / an unnamed staff address - for two 28 Oct rows;
  the 27 Oct report was checked: 0 of 28 mismatches).
- **Bounces 2026-10-27**: careers@digitalpaani.com (mailto button "Send Email" on /about-us) and careers@electricpe.com
  ("Careers Open roles and applications careers@electricpe.com" on /contact-us) - both really published, still on the
  page, address doesn't exist (550 5.1.1); recorded with `--stop` (their only other addresses are contact@).
- **Referrals**: `referrals` -> output/referrals.csv (large / apply-only companies with a fitting role, LinkedIn note
  <= 300 chars). 6 rows.
- **Misc**: the CLI prints with `errors="replace"` (a cp1252 console killed a contacts shard on a company name);
  `contacts --shard i/n` runs lookups in parallel; `contacts --deep` also takes never-looked-up targets; careers-page
  role lines with "careers / hire / ROR / golang / .net" are skipped.
- Live 2026-10-28: 75 new sendable contacts (7 verified people, 38 hiring inboxes, 11 evidence guesses, 19 hello@;
  41 startup, 22 consultancy, 12 fintech). **28 Oct batch stored**: 3 named founders / CTOs, 20 hiring inboxes,
  7 evidence guesses (fresh-checked, all validated Claude drafts). After it: 4 verified, 20 hiring inboxes,
  4 evidence guesses, 60 hello@ (only some at small startups with drafts) - about one more full day, then ~5/day.
  Bounces so far: hiring inbox 4/41, hello@ 3/19, verified people 0/15, founders@ 0/11, info@/contact@ 1/2.

Open points:
1. **30/day isn't sustainable** under these rules: Indian startups rarely publish hiring inboxes or personal
   addresses, and pure guesses stay unsendable. Real levers: a new source with published addresses, or loosening a
   rule (e.g. evidence from any published personal address, hello@ cap).
2. Hiring inboxes are at 2 of their last 20 bounced; one more bounce pauses them.
3. IDfy and OkCredit evidence guesses were held back by hand as likely >300 people with no headcount on file; a size
   source would let the large-company rule catch them.
4. Careers-page role titles are still a heuristic (fixed by hand: Thoughtworks "Developer careers", Ideas2IT "Hire
   Data Engineer", RailsFactory "ROR Fullstack Engineer").

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
