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
| 2. Contacts & drafting | Find the right hiring contact; research the company; draft a personalized email per opening; review/approve screen | Planned |
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
| YC company directory | Thousands of startups with headcount, location and a "hiring" flag | [yc-oss export](https://yc-oss.github.io/api/) of YC's public directory; filtered to active, hiring, India/remote |
| Hacker News "Who is hiring?" | Monthly thread; strong for remote-global roles, and many posts invite applicants to email directly | Free HN Algolia API; top-level comments only |

### Startup discovery

`discover` takes hiring YC companies that match your filters (Indian and smallest
teams first), then for each one:

1. Reads its homepage and careers page (respecting `robots.txt`) looking for links to a
   job board. Greenhouse, Lever, Ashby and Workable boards get collected from then on.
2. If none is linked, tries a few slugs from its domain and name on each supported board.
3. If it uses a board without a public API (Keka, Darwinbox, Zoho Recruit, Freshteam,
   Wellfound, ...) or none at all, it's saved to `startups_to_email.csv` for direct outreach.

Results are stored, so each company is re-checked at most every 30 days.

Deliberately **not** scraped: LinkedIn, Wellfound, Naukri, Instahyre. Their terms
forbid it and they actively block scrapers.

### Scoring

Each opening goes through these checks in order. Anything rejected records why, so
`explain` and `stats` can show it.

1. **Title filters** - excluded terms (senior, lead, SDE-2, frontend, android, ...) reject the role.
2. **Role family** - backend, cloud, full-stack, AI-application, ML, data, platform, or generic SDE.
   Families are tiered primary / secondary / stretch, which weights the final score.
3. **Track** - internship, new-grad, or unspecified (unspecified scores lower: many are secretly mid-level).
4. **Experience** - "3+ years of experience" style requirements above your limit reject the role.
5. **Location** - preferred Indian cities > remote open to India > other Indian cities > remote (region unclear).
   Remote roles restricted to other regions are rejected; onsite roles abroad are kept only when the
   posting signals visa sponsorship or relocation support.
6. **Skills** - weighted keyword match against your resume; skills you don't have yet pull the score down.
7. **Startup bonus** - companies of up to 300 people (or with small job boards when headcount is unknown) get +6.

```
score = family weight × (skills 0-50 + location 0-20 + track 0-20 + experience 0-10 + startup bonus)
```

Tier A ≥ 70, tier B ≥ 45 (configurable). Each kept opening also gets an **action**:

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
