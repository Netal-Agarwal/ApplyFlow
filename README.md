# ApplyFlow

An agentic pipeline that finds relevant openings, researches target companies, and
drafts personalized outreach to hiring managers and recruiters, with human review
before sending.

ApplyFlow is built around one idea: **a few well-targeted applications beat a mass
blast.** It reads public job boards, scores every opening against your profile,
and tells you what to do about each one: apply, email, or both.

It's **startup-first**: startups hire freshers far more readily than big companies,
and a cold email there reaches someone who decides. ApplyFlow discovers hiring
startups automatically, finds their job boards from their own websites, and ranks
startup openings higher.

## Status

| Phase | What it does | Status |
|---|---|---|
| **1. Discovery & scoring** | Discover hiring startups; collect openings from ATS job boards and HN; filter and rank them against your profile; export a report | ✅ This release |
| 2. Contacts & drafting | Find the right hiring contact; research the company; draft a personalized email per target; send-ready report with today's batch | ✅ |
| 3. Sending & follow-ups | Gmail API sending with ramped daily caps, reply detection, automatic follow-ups, do-not-contact list | Planned |
| 4. Insights | Reply and interview rates by source, tier and template; more sources | Planned |

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/Netal-Agarwal/applyflow.git
cd applyflow
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp config/profile.example.yaml config/profile.yaml   # Windows: copy config\profile.example.yaml config\profile.yaml
# then edit config/profile.yaml (it's gitignored)

python -m applyflow discover       # find hiring startups and their boards (repeat to check more)
python -m applyflow run            # collect + score + report
```

Open `output/opportunities.html` in a browser for the ranked list, or
`output/opportunities.csv` in a spreadsheet. `output/startups_to_email.csv` lists hiring
startups whose job board can't be read automatically - good direct-outreach targets.

**Suggested rhythm:** `discover` a couple of times a week (each run checks 40 new
companies, about 3-5 minutes), and `run` daily (a minute or two).

## Commands

| Command | What it does |
|---|---|
| `python -m applyflow run` | Collect, score and report in one go (the daily command) |
| `python -m applyflow collect [--source ats hn]` | Fetch openings; marks jobs that disappeared as closed |
| `python -m applyflow score` | Re-score everything stored (run after editing your profile; no network needed) |
| `python -m applyflow report [--min-tier A]` | Write the HTML and CSV report |
| `python -m applyflow stats` | Counts by source and tier, plus the top reasons openings were filtered out |
| `python -m applyflow explain "<id or title>"` | Show exactly why one opening got its score |
| `python -m applyflow probe "Company Name" [--add]` | Find which ATS a company's board is on; `--add` saves it to the seed list |
| `python -m applyflow verify-seeds` | Check every seed board still responds |
| `python -m applyflow discover [--limit 40]` | Find hiring startups in the YC directory and detect their job boards |
| `python -m applyflow verify-boards` | Check every discovered board belongs to its company; demote mismatches |
| `python -m applyflow contacts [--company NAME] [--force]` | Find the best person to email at each outreach target; writes `output/contacts.csv` |
| `python -m applyflow explain-contact "<company>"` | Every contact candidate considered, and why the primary won |
| `python -m applyflow draft-pack [--refetch]` | Fetch company homepage context, refresh template drafts, write a drafting packet per target to `output/drafts/packets` |
| `python -m applyflow import-drafts` | Load drafts written to `output/drafts/written/<id>.md`; each is validated, failures stay on the template |
| `python -m applyflow queue --date YYYY-MM-DD [--size 20]` | Pick the day's batch: tier 1 (verified, recruiter/HR), tier 2 (published inbox), tier 3 (guessed) behind a bounce-aware gate (`outreach.tier3`) |
| `python -m applyflow mark-sent <id>` / `--batch YYYY-MM-DD` | Record emails you sent (ApplyFlow never sends) |
| `python -m applyflow mark-bounced <id>` | Record a bounce; the next alternate or backup address is suggested |
| `python -m applyflow add-target "Co" --website co.com [--note] [--email] [--name] [--role]` | Add your own target; finds a contact unless you give an email |

## How it works

```
YC directory ─► discover ─► company websites ─► board found? ─┐
                                                              │
config/seeds.yaml ────────────────────────────────────────────┼─► collectors ─► SQLite ─► scoring ─► report
HN "Who is hiring" ───────────────────────────────────────────┘                             ▲
                                                                               config/profile.yaml
```

### Sources

| Source | Why | How |
|---|---|---|
| Greenhouse, Lever, Ashby, Workable | Most startups host their job board on one of these; each exposes a free public JSON API | One request per board (seeds + discovered) |
| YC company directory | Thousands of startups with headcount, location and a "hiring" flag | [yc-oss export](https://yc-oss.github.io/api/) of YC's public directory; filtered to active, hiring, located in India |
| Hacker News "Who is hiring?" | Monthly thread; strong for remote-global roles, and many posts invite applicants to email directly | Free HN Algolia API; top-level comments only |

### Startup discovery

`discover` takes hiring YC companies located in India (`discovery.yc.regions_any`; "Remote"
alone doesn't count, since US startups use it for US-remote), teams of 10-300 people first
(`preferred_team_size`), then for each one:

1. Reads its homepage and careers page (respecting `robots.txt`) looking for links to a
   job board. Greenhouse, Lever, Ashby and Workable boards get collected from then on.
2. If none is linked, tries a few slugs from its domain and name on each supported board -
   but slugs like "bloom" are shared by many companies, so a guessed board is only trusted
   when it points back to the company (the website it links to, or its name). Otherwise it's
   saved as **unverified** with the reason, and not collected.
3. If it uses a board without a public API (Keka, Darwinbox, Zoho Recruit, Freshteam,
   Wellfound, ...), an unverified board, or none at all, it's saved to `startups_to_email.csv`
   for direct outreach.

`verify-boards` re-runs that check on every saved board: mismatches are demoted (their jobs
leave the report) and unverified boards that now pass are promoted.

Results are stored, so each company is re-checked at most every 30 days.

### Contacts (Phase 2a)

`contacts` picks a person to email at every company with a tier A/B opening (recruiting
marketplaces excluded - apply through their posting) and at India startups without a readable
board. In order of trust: emails the posting publishes for applicants, the company's own site
(a few pages, `robots.txt` respected), the founders on its YC page, its GitHub organisation, and
finally an email pattern (`first.last@...`) confirmed by a published address - or, failing that,
a clearly labelled `first@domain` guess. Each contact gets a confidence (verified, strong,
guessed, fallback) and its source URL; the person is chosen by role fit (founder/CTO at small
teams, engineering manager or recruiter at bigger ones). Mail servers are never probed and
nothing is sent. Addresses in `config/do_not_contact.txt` (see the `.example`) are never used.

Ranking puts deliverability first: verified > strong > a published inbox (founders@, careers@,
hello@ ... at teams of 50 or fewer) > guessed. Every domain is checked for MX records (DNS over
HTTPS, cached for 30 days) and contacts on domains that can't receive mail are dropped. Guessed
addresses come with two alternates to try if the first bounces, plus LinkedIn and Google search
links to check the person by hand. Companies over 300 people are apply-only.

Deliberately **not** scraped: LinkedIn, Wellfound, Naukri, Instahyre. Their terms
forbid it and they actively block scrapers.

### Scoring

Each opening goes through these checks in order. Anything rejected records why, so
`explain` and `stats` can show it.

1. **Title filters** - excluded terms (senior, lead, SDE-2, frontend, android, security, devrel, ...)
   reject the role. HN posts whose header names no specific engineering role are left as skip.
2. **Role family** - backend, cloud, full-stack, AI-application, ML, data, platform, or generic SDE.
   Families are tiered primary / secondary / stretch, which weights the final score.
3. **Track** - internship, new-grad, or unspecified (unspecified scores lower: many are secretly mid-level).
4. **Experience** - "3+ years of experience" style requirements above your limit reject the role.
   Unstated experience is expected for internships and new-grad roles; elsewhere it scores lower.
5. **Freshness** - postings older than a year are rejected as evergreen; older than ~4 months lose 10 points.
6. **Location** - preferred Indian cities > remote open to India > other Indian cities > remote (region unclear).
   Remote roles restricted to other regions are rejected - including remote tied to a foreign place
   ("Remote - Ireland", "Remote, San Francisco, CA", "residents only"). Onsite roles abroad are kept
   only when the posting offers visa sponsorship (relocation help alone doesn't count), and never when
   it asks for US citizenship, a security clearance or ITAR status.
7. **Skills** - weighted keyword match against your resume; skills you don't have yet pull the score
   down, except for internships and new-grad roles. Fresher roles in a core family and a preferred
   city get at least 25/50 (`fresher_skill_floor`), since intern JDs often name few skills.
8. **Startup bonus** - companies of up to 300 people (or with small job boards when headcount is unknown) get +6.

```
score = family weight × (skills 0-50 + location 0-20 + track 0-20 + experience 0-10 + startup bonus
                        - stale penalty)
```

Tier A ≥ 70 (internship and new-grad roles only; others top out at B), tier B ≥ 45 (configurable).
The same opening posted once per city is merged into one row listing every location. Each kept opening also gets an **action**:

- **Apply** - large companies (big job boards) and sponsored relocation roles: their formal process is the way in.
- **Email** - HN posts that ask applicants to email someone.
- **Apply + email** - smaller companies: apply, then send a short note to the hiring manager (Phase 2 drafts it).

And a **contact hint** for Phase 2: founder or CTO at teams up to 50, engineering manager
or tech lead up to 300, recruiter or engineering manager beyond that.

### Tuning it

Everything lives in `config/profile.yaml`: role families and their patterns, excluded
title terms, experience limit, locations, skill weights and tier thresholds. Edit it,
run `python -m applyflow score` (no re-download needed), and use `explain` on any
opening that landed in the wrong place.

### Adding companies

```bash
python -m applyflow probe "Company Name" --add
```

This tries likely board slugs on Greenhouse, Lever, Ashby and Workable and adds the best match to
`config/seeds.yaml`. If it finds nothing, the company may use a different ATS, or pass a
slug you've seen in their careers URL: `probe "Acme" --slug acmehq`.

## Ground rules

- **Personal data stays local.** `config/profile.yaml`, the database (`data/`) and reports
  (`output/`) are gitignored.
- **Polite collection.** Honest User-Agent, at most one request per second per host,
  backoff on rate limits. Public job-board APIs only; company websites are read only
  where `robots.txt` allows.
- **A human approves outreach.** Nothing will be sent without review (Phases 2-3).

## Development

```bash
pytest                 # or: python -m unittest discover -s tests -t .
```

Tests run offline against recorded-format fixtures in `tests/fixtures/`.

Project layout:

```
applyflow/
  collectors/        greenhouse.py, lever.py, ashby.py, workable.py, hn.py
  cli.py             command-line entry point
  config.py          profile and seed loading
  detect.py          find a company's job board from its website
  discovery.py       YC directory discovery
  http.py            polite HTTP client (rate limits, retries, robots.txt)
  models.py          Job, Company, ScoreResult
  pipeline.py        collect / score / probe orchestration
  report.py          HTML and CSV export
  scoring.py         filters, classification and scoring
  store.py           SQLite storage
config/
  profile.example.yaml
  seeds.yaml
tests/
```
