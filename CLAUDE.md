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
   - **2g seed lists at scale + reply outcomes - done (2026-10-28)**, see "Phase 2g notes",
     `tests/test_supply_2g.py` and `tests/test_replies_2g.py`.
   - **2h supply refill (wide search, seeds, direct-apply list) - done (2026-11-03)**, see "Phase 2h notes" and
     `tests/test_supply_2h.py`.
3. Sending & follow-ups - engine built 2026-11-01 (see "Phase 3 notes", `tests/test_sending_3.py`) but **not used:
   sending is manual since 2026-11-02** (see "Manual sending notes", `tests/test_manual_3.py`). The original plan:
   Gmail API (OAuth) from the user's **personal Gmail**.
   Ramp from ~10-15/day to 30-40/day max. Randomized gaps, business hours in the
   recipient's timezone, no open-tracking pixels, resume linked (attached only for recruiters/HR or
   when a posting asks for one by email - the 2b `resume_action` rule),
   reply detection stops sequences, 1-2 follow-ups, permanent do-not-contact list.
   **`check-inbox` must classify every reply as human reply / auto-reply / bounce and apply the 2g outcomes
   automatically** (`outreach.mark_replied`: interested / declined_warm + recheck date / declined / auto_ack, the
   replier stored as a verified person; `mark_bounced --stop` for a hard 550 5.1.1 on a guess). Any outcome stops
   follow-ups (`OutreachItem.followups_stopped`); auto_ack counts as delivered.
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
python -m applyflow supply --consultancies --saas --fintech-seeds --fintech   # 2g: seed lists (only seeds not in the pool)
python -m applyflow supply --yc-fintech-india       # 2g: YC fintechs abroad with an office / engineering in India
python -m applyflow mark-replied <email> --outcome {interested,declined_warm,declined,auto_ack} [--recheck D] [--contact "Name <email>"] [--role R] [--keep-followup]
python -m applyflow daily --sent D [--next D] [--skip ID] [--skip-followup ID] [--followups 10] [--force]   # manual: the evening routine
python -m applyflow contacts --wide [--shard 0/4]   # 2h: up to 25 own pages (sitemap, blog, press, careers, PDFs)
python -m applyflow supply --consultancies --saas --fintech-seeds --shard 0/8   # 2h: parallel seed verification
python -m applyflow direct-apply       # 2h: output/direct_apply.csv (fitting roles at large / form-only / bounced companies)
python -m applyflow gmail-login        # 3 (engine, unused): OAuth (gmail.send + gmail.readonly), token in config/private/
python -m applyflow reconcile [--from 2026-10-21 --to 2026-10-29] [--release] [--no-inbox]
python -m applyflow plan [--date D] [--no-fresh]      # 3: next working day: 30 new + follow-ups (<=15) + retries
python -m applyflow send [--dry-run] [--to-self --limit N] [--no-fresh]
python -m applyflow check-inbox [--since D]
python -m applyflow digest [--print] [--date D]
python -m applyflow drop <row|email|target id> [--date D] ; hold <date> [--undo] ; pause ; resume ; summary [--date D]
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

## Phase 2g notes (seed lists at scale, reply outcomes, 2026-10-28)

- **Seed lists** (`supply.collect_seeds`, sources.yaml `consultancies` / `saas` / `fintech_seeds`): 428 consultancies
  (66 from 2f + 362 new: Pune, Bangalore, Hyderabad, Ahmedabad, Noida / Gurgaon, Chennai, Kochi, Jaipur, Indore ...),
  107 mid-size product / SaaS companies (`sector: saas`, source `saas:seed`), 64 fintechs (`fintech:seed`). team_size is
  an approximate headcount from general knowledge (over 300 = hiring inbox only, the 2f large rule). `supply` now only
  verifies seeds whose domain isn't already in the pool. Verification = 2f's `verify_company` plus `seed_fit`: a
  consultancy's homepage must show client software work (`CLIENT_WORK`: clients, product engineering, custom software,
  case studies, offshore ...), a SaaS / fintech homepage a product (`PRODUCT_WORK`). Pool: `saas` selected past
  target_size (`priority_2f`), `SOURCE_PRIORITY` gains saas. Drafts: a SaaS angle line (`SECTOR["saas"]` in drafts_2g.py).
- **TLS**: when the bare domain fails certificate verification (incomplete chain on the apex), `verify_company` retries
  once on `https://www.<domain>` with verification on (`www_retry`); still failing -> dropped. Never disable TLS checks.
  2026-10-28: 3 sites recovered, 10 still failed and were dropped.
- **Fintech lists**: FACE members (faceofindia.org/members, `kind: sahamati`, same logo-link parser; `_SKIP_LINK` drops
  the association's own links and its Zoho form): 84 listed, 58 verified (44 new). Skipped: Payments Council of India
  (members page rendered by JavaScript), DLAI (404), Sahamati FIU / FIP pages (names only, mostly banks / NBFCs).
  `supply --yc-fintech-india` (`india_engineering`): a YC fintech headquartered abroad counts when its own homepage /
  careers / about / contact page ties an office / engineering / team word to an Indian city (customers or prices in
  India don't count): 86 checked, 1 (Gerald; its only contact is a pure guess).
- **Careers-page roles** (`refill.careers_page_roles`): closed roles, "Join our ... community", hubs, docs / portal /
  developer-experience links, hire-us pages ("Onboard a Pre-Vetted Developer", "Dedicated Developer"), lines starting
  with an employee's name ("Alok Kumar SDE - I"), and stacks not in the facts (Salesforce, .NET, Flutter, React Native,
  iOS / Android, PHP, WordPress, Magento, Shopify, SAP, Odoo, ServiceNow, Unity, Drupal, Laravel, AEM, CPQ, Svelte,
  UiPath, Liferay, Angular, Vue, MERN) are not openings; requisition codes and trailing cities are stripped
  (`clean_role_title`); the title named in the email is the best fit (Python / backend, then fresher words), not the
  first listed. A careers-page "yes" turns a general target into a role email, so this matters.
- **Evidence fixes**: section headings aren't people ("Customer Testimonials" was a CEO at Synoverge: `_NAME_STOP`), and
  role inboxes aren't first names (enterprise@haptik.ai backed three founder guesses: `NOT_NAMES`). Guesses whose
  evidence is a first-name address (founder-style, rahul@) rank above those backed only by a staff first.last address
  (`OutreachItem.guess_rank`, `evidence_kind`); `guess_outcomes` (printed by `safe-contacts`) tracks evidence-guess
  sends / bounces / human replies by address format (first@ / first.last@ / other) and by evidence kind.
- **Batch order** (tiers 1-2): named hiring people and founders@ first, then hiring inboxes at established companies
  (50+ people or consultancy / fintech / SaaS: 20/20 delivered on 2026-10-28), then by bounce risk.
- **Replies** (`mark-replied`, `outreach.mark_replied`): `replied` events with `outcome` (interested / declined_warm /
  declined / auto_ack), `recheck_date` and `note` (columns added to outreach_events). Any outcome stops follow-ups
  (`followups_stopped`); auto_ack counts as delivered; declined_warm needs `--recheck` and shows in the report's
  **Recheck** list from that date (`recheck_due`, `report.recheck_section`). `--contact "Name <email>"` (same domain as
  the sent address) stores the replier as a verified named person, source / proof `reply`; `save_contact_result` keeps
  reply contacts across re-lookups. The target is found by the sent address or, failing that, its domain.
- **Stopped targets** reopen only through a verified named person or a published hiring inbox (`REOPEN_KINDS`), never
  another guess or the stopped address's alternates. The 6 earlier stopped targets have none, so they stay stopped.
- Live 2026-10-28: 28 Oct batch marked sent (30 sent, 1 bounced: Decentro's evidence guess rohit.taneja@, staff
  first.last evidence; gate 1 bounce, still capped at 10). Replies: HydraDB declined_warm (Nishkarsh Srivastava,
  recheck 2026-12-01), nuVizz declined_warm (Naresh Kumar, HR, recheck 2027-01-05), Robosoft auto_ack (stays on
  referrals.csv). Pool 3146 -> 3552 (406 new: consultancy 250, SaaS 73, FACE 44, fintech seeds 39). A DNS outage
  during the first supply run dropped 259 seeds; the second run redid every seed not yet in the pool.
  **29 Oct batch stored**: 11 named people + 19 hiring inboxes, 25 consultancy / 1 SaaS / 1 fintech / 3 startup,
  all Claude drafts, all fresh. New sendable since 2g: 80 (67 uncapped: 8 people, 59 hiring inboxes; 5 evidence
  guesses, 8 hello@), 27 of them in the 29 Oct batch. Drafts: 76 written by `output/drafts/drafts_2g.py` +
  `openings_2g.json`, all passing. IDfy / OkCredit guesses held back again (excluded_contacts).
  After the 29 Oct batch: 129 sendable (55 hiring inboxes, 7 evidence guesses, 67 hello@) = 16 days under the caps,
  only 2 of them full 30/day.

Open points:
1. **The 150 goal was not met (67 uncapped).** Consultancy yield at scale is ~20% of verified (50 / 250), not 2f's
   33% (the first 66 seeds were the best-known firms); SaaS 10%, fintech 10-14%. Of 379 2g targets: 141 have only pure
   pattern guesses, 79 are large with no hiring inbox on 12-13 of their own pages (ATS forms), 43 no contact, 38 only
   info@ / contact@ / legal-page addresses. Levers: another seed round (~10-20% yield), or a rule decision (hello@ at
   consultancies of 50+ people; evidence from a staff address for a named founder).
2. Seed team sizes are estimates; a wrong one moves a company across the 300 line (hiring inbox only).
3. 8 hello@ addresses at consultancy / SaaS / fintech companies have no draft: the batch rule only sends hello@ at
   startups of 50 or fewer.
4. Careers-page role matching is still a text heuristic; check role emails' titles in the review screen.

## Phase 3 notes (Gmail sending engine, built 2026-11-01)

- **Modules**: `gmail.py` (OAuth via google-auth / google-auth-oauthlib, REST through `AuthorizedSession` - no Google
  API client library; MIME; message parsing; error classes), `followups.py`, `inbox.py` (check-inbox, reconcile),
  `sending.py` (window, lock, brakes, plan, send, drop / hold, digest, review page, summary). New tables in
  `store.py`: `gmail_messages` (every email of ours with Gmail / thread / Message-ID, source engine | reconcile),
  `send_plan` (a day's rows: new | followup | retry, status planned | sending | sent | failed | dropped | deferred |
  moved | held), `inbox_messages`, `brakes`, `plan_holds`, `app_state`; `outreach_events.keep_followup`.
  Follow-up sends are `followup` events, soft-bounce resends `retried`: neither counts in `send_records` / gates.
- **Private files** (gitignored via `config/private/`): `gmail_credentials.json` (Desktop OAuth client),
  `gmail_token.json`, the resume PDF (`outreach.sending.resume_pdf`, default the one `*Resume*.pdf` there), `PAUSE`.
- **plan**: `make_queue` unchanged except `carry` (rows deferred / held / left planned on an earlier day go first,
  their old row becomes `moved`) and `skip` (rows dropped in review). Short of 30 -> "short by N" (stored in
  `app_state` as `short:<day>` for the digest). Follow-ups and retries fill up to `daily_ceiling` (45). Writes
  `output/review/plan_<day>.html` and `output/digest/digest_<day>.txt`.
- **send**: rows go `planned -> sending` (attempt counted) before the API call and `sent` right after (gmail id,
  thread id, Message-ID fetched for threading; `sent` event with kind, batch_date = day). A row left `sending` by a
  crash is looked up in Sent (recipient + subject) before anything else: found -> recorded, else back to planned.
  `data/send.lock` (O_EXCL, stale after 5 h). Window (`SendWindow`): waits for 09:00; a run starting by 09:15 is on
  time and stops at 10:45; later is a late start and may send until 13:00; starting after 13:00 defers all. Gaps:
  random 2-4 min, shortened (never below 2) to finish by 10:30 (12:45 late). Deferred new rows leave that day's
  `send_queue` (else `held_earlier` would trap them). Before each new email: still ready, same address, DNC,
  validated draft, the 2f freshness check. Resume: `attach` drafts get the PDF and must not contain the resume
  link; `link` drafts must not say "attached". `--to-self` sends to the account with "[TEST] ", ignores the window,
  marks nothing; `--dry-run` sends nothing and changes nothing (both use the next planned day if today has none).
- **Brakes**: PAUSE flag; `auth` brake (401 / invalid_grant / missing token) until `gmail-login` clears it; `abuse`
  brake (403, 429 after back-off, any quota / rate-limit / abuse wording) and `bounces` brake (`bounce_brake` 3 hard
  bounces received that IST day) for that day. Any brake defers the rest of the day's rows. 5xx after back-off
  (5 / 20 / 60 s) stops the run leaving rows planned (rerun continues). `resume` clears PAUSE + today's abuse / bounce
  brakes.
- **check-inbox** (`after:<last check> -in:sent -in:drafts -in:chats`, each message once via `inbox_messages`):
  DSN (mailer-daemon / postmaster / multipart/report) -> Status 5.x.x or "address not found" wording = hard
  (bounced + stopped events, skipped if already recorded); 4.x.x / "(Delay)" / Action: delayed = soft (resent once
  the next working day as a `retry` row; the follow-up waits for it and then replies to the retry); no status =
  `bounce_unclear` (digest only). Auto-reply: Auto-Submitted != no, X-Autoreply / X-Autorespond, Precedence
  auto_reply / bulk / junk, no-reply senders, automated wording; a subject alone ("Thank you for your email") isn't
  enough, so "Thanks for reaching out, no openings" stays human. "A person will get back to you" without ATS signs
  (conditional "if your profile matches", portals, do-not-reply) -> `keep_followup`. Human -> `needs_action`
  outcome (new constant, accepted by `mark_replied`), replier stored as a reply contact; the digest lists it until
  `mark-replied` records the real outcome. Matching: our thread, else a sent address / its domain or a parent
  domain (careers.robo.example -> robo.example); mailings (List-Unsubscribe / List-Id) are `other`. Drive access
  requests (drive-shares-*noreply@google.com) are listed. Nothing is ever replied to.
- **Follow-ups** (`due_followups`): due `followup_after_working_days` (5) Mon-Fri after the delivered message's
  IST date, never before `followups_not_before` (2026-11-02); oldest first, `followups_per_day` (15); one per
  target (a sent / sending / dropped follow-up row or message ends it). Not for: bounced, stopped, DNC, any human
  outcome (interested / declined_warm / declined / needs_action) at that company (company_id), ATS auto_ack, or a
  target with no Gmail record (needs `reconcile`). `OutreachItem.followups_stopped` is now False for auto_ack with
  keep_followup. Text: 3 variants by crc32(target id); greeting / company / role (role emails) / the original's
  first observation sentence (full, cut at its first clause, or none) to land in 50-70 words, padded with "Thank
  you for your time either way." if short; variant 1 says "my email from last week" only when the delivered message
  went out 5-9 calendar days before the follow-up day (`earlier_phrase`), else "my earlier email";
  `validate_followup` (Re: subject, greeting, 50-70 words, exactly one question, no pushy wording, no placeholders,
  "last week" only at 5-9 days, phone / LinkedIn / GitHub in the signature, no attachment).
  Threaded: threadId + In-Reply-To / References = the original's Message-ID; no attachment; resume link signature.
- **reconcile**: every `send_queue` row in the date range without a `new` Gmail record: `in:sent to:<addr>` from 2
  days before to 12 after the batch date; exact subject wins, else a single non-"Re:" message to that address
  ("subject differs"). Unmarked rows are marked sent on the real IST send date (batch_date kept); already-marked
  rows keep their date (a difference is noted). Not found -> listed; `--release` removes unmarked ones from their
  old batch so `plan` can pick them. Then one inbox scan from the start date.
- **Digest** (`build_digest`): brakes ("run gmail-login"), held day, short by N, the plan's rows (company, address,
  name, kind, new / follow-up / retry), human replies needing action with Gmail thread links and a snippet, bounces /
  auto-replies / Drive requests since the last digest (`app_state.last_digest`), the Recheck list, supply
  (`supply_summary`: days at 30/day and full days). `digest` emails it to the authorised account.
- **Scheduling**: `scripts/schedule_tasks.ps1` (not run by Claude): `\ApplyFlow\` tasks via cmd.exe with
  PYTHONIOENCODING=utf-8, appending to `data/logs/<command>.log`: plan+digest 20:00 Sun-Thu, send 09:00 Mon-Fri
  (WakeToRun, StartWhenAvailable, 4 h limit, IgnoreNew), check-inbox 11:00 / 15:00 / 19:45 daily.
  `scripts/remove_tasks.ps1` removes them. Times are the PC clock: it must be on IST.
- **Hand-sent days** (`sending.hand_sent_day`): a day with `send_queue` rows and no `send_plan` rows was stored by
  `queue` and is sent by hand: `plan` refuses it (`HandSentDay`), `send` sends nothing that day, and `queue` refuses
  a day `plan` already holds. Its rows stay out of later picks (`held_earlier`) until `reconcile` marks them sent on
  their real dates. `reconcile --to` defaults to today. **2026-11-02 is a hand-sent day** (engine live from 3 Nov):
  30 hiring inboxes (20 consultancy, 6 SaaS, 4 fintech; 8 role emails; all attach the PDF), `output/batch_2026-11-02.md`;
  DB backup before it `data/applyflow.pre-1102-queue.db`. Appinventiv was switched from the careers-page "AI/ML
  Engineer" role email to general outreach (role_matches careers -> "no", reviewed; new draft leads with CareerForge
  AI + Serverless FileFinder; old file kept as `output/drafts/written/pool-appinventiv-com.role-2g.bak`; backup
  `data/applyflow.pre-1102-appinventiv.db`). A later `role-check --all` could flip it back.
- **Recorded 2026-11-01**: Accelerize 360 and Codelattice marked sent 2026-10-29 (their 29 Oct batch rows were
  unmarked), then Accelerize declined_warm (Priyanka Kapoor <pkapoor@accelerize360.com>, HR, recheck 2027-01-05)
  and Codelattice auto_ack with keep_followup. DB backup before Phase 3: `data/applyflow.pre3.db`.
- Smoke run on a copy of the live DB (2026-11-01): `plan --date 2026-11-02 --no-fresh` -> 30 new (all hiring
  inboxes), 0 follow-ups (107 sent originals need `reconcile` first); supply 99 sendable = 15 days, 1 full.

Open points:
1. **Not live yet**: gmail-login, reconcile, the to-self test and scheduling are for the user to run.
2. Soft bounces: Gmail itself retries 4.x.x for ~2 days; our one resend the next working day may duplicate a late
   delivery. Revisit with real data.
3. The resume link is a Drive link: Drive access requests in the digest mean it isn't shared "anyone with the link".
4. Follow-up variants share wording across all targets; replies by variant (Phase 4) would show which works.

## Manual sending notes (decided 2026-11-02)

- **Sending stays manual**: the user schedules each batch in Gmail. The Phase 3 engine code stays, but **don't use
  `plan` / `send` / `reconcile` / `check-inbox` / `digest`**. Batches are stored with `queue` (or `daily`), replies and
  bounces recorded by hand with `mark-replied` / `mark-bounced --stop`.
- **`daily --sent D`** (`applyflow/manual.py`, the evening routine, after the day's outcomes are recorded): backs up the
  DB (`data/applyflow.pre-daily-<D>.db`, SQLite backup API); `--skip ID` takes an unsent row out of D's batch (back
  into the pool); marks D's batch sent (`mark_batch_sent`) and D's follow-up checklist (`app_state` `followups:<D>`,
  minus `--skip-followup`) as `followup` events; checks the all-type daily brake on D (bounced events dated D vs
  sends dated D); tripped -> no next batch unless `--force`; else `make_queue` for the next working day (freshness on),
  `output/batch_<next>.md` (table with Attach PDF?, evidence lines, rule problems, full drafts),
  `output/followups_<next>.md` and the HTML report. Idempotent: rerunning marks nothing twice.
- **Manual follow-ups** (`due_followups(..., manual=True)`, `manual_original`): with no Gmail record the original is
  the sent event + its draft subject; same Phase 3 eligibility, variants, validator and "last week" rule; checklist
  capped by `--followups` (10), oldest first. Also (both modes): no follow-up to a target with any `stopped` event
  (SingleStore was stopped by hand on 23 Oct without a bounce) or to an info@ / contact@ address; a `followup` event
  counts as "already followed up". Sent by hand as replies in the original thread, no attachment.
- **Gate change (2026-11-02)**: hiring inboxes pause when **more than 10% of the last 50** hiring-inbox sends bounced
  (`type_gates.hiring_window` 50, `hiring_max_bounce_rate` 0.10; was 3 of 20). The all-type daily brake
  (`sending.day_brake`, used by `daily` and the engine's check-inbox) trips at **4 hard bounces, or more than 12% of
  that day's sends** (`sending.bounce_brake` 4, `bounce_brake_rate` 0.12).
- **Recorded 2026-11-02** (backup `data/applyflow.pre-1102-manual.db`): 29 Oct batch marked sent on 29 Oct (28 + the
  2 already marked), 2 Nov batch on 2 Nov (30); smallcase interested (Thirunarayan Sharma
  <thirunarayan.sharma@smallcase.com>, stored as a verified person; pre-screening questions answered); Hevo Data
  careers@ and Pyramidion careers@ hard bounces (5.1.1) with `--stop`; Capital Numbers auto_ack from noreply@ with
  keep_followup, info@capitalnumbers.com in excluded_contacts. Daily brake on 2 Nov: 2 of 30, not tripped.
- **3 Nov batch** (`daily --sent 2026-11-02`): 25 hiring inboxes + 5 evidence guesses (Fello, 10decoders, Ready,
  Binks, PaletteHQ); 17 startup, 7 consultancy, 6 fintech; all general; 25 attach the PDF; 30 fresh, 0 problems.
  Follow-up checklist: 10 (21-22 Oct originals; "my earlier email" wording).

- **Company fit (2026-11-03)**, `outreach.india_gap` / `tech_gap` -> `OutreachItem.fit_hold`, enforced by
  `BatchRules` (`outreach.require_company_fit`, default on) and left out of `supply_summary`: (1) an India team or
  remote-India hiring - an Indian city / "India" in the locations, an India-region pool company or manual target, or a
  role open to India; remote-global companies with no India location fail (Ready, PaletteHQ, Canix, Firstbase,
  Flockjay, SpruceID); (2) a software / tech company - the 2c.1 non-software words plus consumer goods
  (`NON_TECH_EXTRA`: apparel, fashion, cosmetics ...) in the name / one-liner / tags / homepage title and description,
  cleared by a product phrase (`software_product` + `TECH_PRODUCT_EXTRA`: AI-powered, automation, agents, vision AI
  ...); consultancy / fintech / SaaS seeds are exempt (vetted at verification) except for (3) a homepage saying it
  has shut down (Neuphony: "will not be operational"). Removed from sendable inventory: Canix, Firstbase.io,
  Flockjay, SpruceID (hello@), PaletteHQ (guess), Neuphony, Silae (now a clothing store) - plus 11 pure guesses (tech)
  and 61 (India). False positives fixed while tuning: Datafoundry (AI for pharma / cosmetics), Lifespark (medtech
  wearable), October (coding agents), Optifye (vision AI).
- **3 Nov batch fixes (2026-11-03, backup `data/applyflow.pre-1103-fixes.db`)**: Ready (ready.net) and Binks
  (getbinks.com) added to `never_cold_email` by domain; PaletteHQ and Neuphony dropped by the checks. 10decoders:
  thomas@10decoders.com is founder & CTO **Edrin Thomas**'s own address ("Who you'll talk to: Thomas, CTO ... Book a
  meeting -> thomas@" on the homepage), so it's now his verified contact (rank 0); the `edrin@` guess (and alternates)
  and the `dan@` guess are in excluded_contacts - both rested on thomas@ read as a *first-name* address, but it's a
  surname (open point: `evidence_for_guess` can't tell a surname-format address from a first-name one). SuperKalam
  declined_warm (recheck 2027-01-05; they pointed to a React Native internship). The 3 Nov batch was rebuilt with
  `queue` (rows cleared first: a queued-but-unsent row keeps its queued address): 1 named founder / CTO, 23 hiring
  inboxes, 3 evidence guesses (Fello, Spyne, FORTYTWO42), 3 hello@; follow-up checklist 10 (Rivvun AI replaced
  SuperKalam).

## Phase 2h notes (supply refill A + C + D, 2026-11-03)

- **A. Wide search** (`contacts --wide`, `contacts.read_site(wide=True)`, `cli.wide_targets`): unprotected targets with a
  named founder / CTO / eng lead / recruiter but no sendable address (not large, not fit_hold, never wide-searched),
  up to `WIDE_PAGES` (25) of the site's own pages: the deep pages, then `sitemap_entries` (robots.txt `Sitemap:` lines
  via `PoliteClient.sitemaps`, else /sitemap.xml, /sitemap_index.xml, /wp-sitemap.xml; an index is followed into <= 4
  child sitemaps, pages / careers / press first, then posts; product / tag / author / image sitemaps skipped) and
  homepage links, by kind (`wide_pages`: people pages, careers detail pages, press / news / events, the blog index
  then its newest posts by lastmod, each capped), plus <= `WIDE_PDFS` (2) linked PDFs (`PoliteClient.get_bytes`,
  `pdf_text`: Flate streams' literal strings, no PDF library). A blog / press index with no sitemap is followed into
  its first 3 items. **People named in blog posts, press items or PDFs are never taken as the team** (customers,
  guests); their addresses still pass the usual legal-page, mockup (4+ unnamed) and role-word rules. A wide lookup
  that finds nothing sendable keeps the stored contacts (`cli.wide_improved`; a failed YC page had dropped GoSats'
  founder guess) and is recorded as `app_state` `wide:<key>`.
- **Surname addresses** (`contacts.surname_owner`): `thomas@` is Edrin Thomas's own address (verified) when the
  surname belongs to one named person only, it isn't any named person's first name, and the publishing page names
  that person (the address itself aside). A surname-shaped address is never first-name evidence
  (`evidence_for_guess(..., surnames=)`). Published addresses whose owner only the YC page names (`rahul@` for founder
  Rahul) are matched after the YC step too (pattern or surname).
- **Evidence rule (2026-11-03)**: a guess is sendable (`evidence_guess`) only with **first-name evidence** (a bare
  first-name address like rahul@ on the same domain); the person's own published address is a verified person anyway.
  A guess backed only by a staff first.last address is `unsendable` (`outreach.STAFF_EVIDENCE_REASON` in the status
  note and kind_reason). Outcomes when decided: first-name evidence 6 sent / 0 bounced; staff first.last 4 sent /
  2 bounced (Decentro, Spyne); 0 human replies either way.
- **C. Seeds**: two rounds in sources.yaml (`# Phase 2h` blocks in consultancies / saas / fintech_seeds: 545 new lines,
  deduped against existing seeds and the pool; Pune, Ahmedabad, Kochi, Jaipur, Indore, Chandigarh / Mohali,
  Coimbatore, Bhubaneswar, Kolkata, Noida first). `supply --shard i/n` verifies seeds in parallel; `supply` re-verifies
  every seed not in the pool, so old drops are retried (Successive Digital passed this time). **`extend_pool` now
  holds `data/pool.lock` (`supply.PoolLock`)**: `save_pool` replaces the whole table, and two shards finishing together
  lost 12 companies (re-added by rerunning that shard). Pool 3552 -> 3925 (373 new: consultancy 245, SaaS 97, fintech
  31); 0 of them held by the India / tech checks (verification already needs an India signal and client-work / product
  words). Seed drops over all attempts: homepage failed 425, no India signal 366, no client-work words 54, no product
  words 15, no MX 15.
- **Careers-page roles**: `_NOT_A_ROLE` also skips navigation / marketing lines ("Find a Developer", "Developer
  Resources", "Your AI-native product engineer", "AI platforms & developer tools", "Developer Console") and SharePoint,
  blockchain / web3, Dynamics 365, HubSpot, WSO2, hybrid, React / Next.js titles; a title asking for more years than
  `max_years_required` ("Python Developer (3-6 Years)") is skipped (`_TITLE_YEARS`). 15 stored matches were set to
  "no" by review (notes say "2h review"); Siddhatech / Technostacks titles tidied.
- **D. Direct apply** (`direct-apply`, `applyflow/direct_apply.py` -> output/direct_apply.csv): companies with status
  apply_only, large with no hiring inbox, or stopped / bounced (Hevo, Pyramidion, Electric Pe); fit_hold left out.
  Roles from the company's own ATS board (every posting scored) or careers page (titles that fit; each title's own
  detail page read when linked for location, years, posted date; a bare title only with an intern / fresher /
  trainee / SDE I word); stored openings too. A role fits when its family is backend / cloud / full-stack / generic
  SDE, it's an internship or new-grad role or states no experience (tier skip allowed, rejected never), and it's in
  India or remote open to India (unknown location only at an India company, noted). The page read as a careers page
  must have a careers / jobs path (`CAREERS_PATH`; Skill-lync's alumni page was taken for one). Sorted by tier,
  fresher track, family, then newest. Live: 160 companies, 13 rows; most large Indian companies' careers pages are
  rendered by JavaScript (Darwinbox, Keka, Workday) and can't be read.
- **`mark_batch_sent`** marks a (target, address) once: Castler's careers@ bounced, hr@ reopened the target (2g
  REOPEN_KINDS), and `daily` marked careers@ sent a second time (the duplicate event was removed).
- **Review exclusions** (excluded_contacts, "2h review"): harsh@okcredit.in (held as in 2f / 2g), hello@chainrisk.io
  (the domain is now an unrelated site), Saturo (NetSuite-only), Inno Valley Works (Salesforce-only), Dynamic Dreamz
  (Shopify-only), Nimblechapps (now operations consulting), Abhyaz (Zoho-only; one address was parsed as
  "-recruitment@"). Shapecrunch (3D-printed insoles) and Staqu (dropped by the tech-fit floor) have no draft.
- **Drafts**: `output/drafts/drafts_2h.py` + `openings_2h.json` (76 company lines; drafts_2g format, sector lines,
  ongoing wording; resume attached for hiring inboxes, linked otherwise via resume_action); all passing. hello@ at
  consultancy / SaaS / fintech companies (21) get no draft: the batch rule sends hello@ only at startups of <= 50.
- **Recorded 2026-11-03** (backup `data/applyflow.pre-1103-evening.db`): Castler careers@, Spyne sanjay.kumar@
  (evidence guess, 5.1.1), Peoplebox hello@ (Google Group) bounced with `--stop`; InCred auto_ack; Wobot and Fission
  Labs declined_warm (recheck 2027-01-05; Akanksha Mahesh, HR, stored as a verified person). `daily --sent 2026-11-03`:
  brake not tripped (3 of 30); **4 Nov batch**: 18 named people + 12 hiring inboxes, 0 guesses, all Claude drafts,
  all fresh; follow-up checklist 10.
- Live 2026-11-03: **84 new sendable contacts** (72 of the goal kinds: 19 named people, 48 hiring inboxes, 5 evidence
  guesses; plus 12 hello@), 70 of the goal kinds with a validated Claude draft. A (wide, 364 + 159 targets): 12 (9
  named, 2 evidence, 1 hello@). C (seeds): 72 (10 named, 48 hiring inboxes, 3 evidence, 11 hello@). After the 4 Nov
  batch: 113 sendable (36 hiring inboxes, 5 evidence guesses, 1 named, 71 hello@) = about 1 full 30/day day.

Open points:
1. **The 150 goal was not met (72 goal-kind contacts).** The wide search yields ~2-4% of pattern-only targets (small
   Indian sites rarely publish a personal address anywhere); seed yield is ~20% of verified. Another seed round gives
   ~15-20 per 100 verified; the bigger lever is a rule decision (hello@ at consultancies of 50+ people).
2. Castler is reopened through hr@castler.com after careers@ bounced (the 2g reopen rule), although the outcome was
   recorded with `--stop`; decide whether a mailbox-level NDR should stop the whole domain.
3. direct_apply.csv is thin (13 rows): JS-rendered careers sites can't be read; 40 companies show no careers page.

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
