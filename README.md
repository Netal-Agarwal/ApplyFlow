# ApplyFlow

An agentic pipeline that finds relevant openings, researches target companies, and
drafts personalized outreach to hiring managers and recruiters, with human review
before sending.

ApplyFlow is built around one idea: **a few well-targeted applications beat a mass
blast.** It reads public job boards, scores every opening against your profile,
and tells you what to do about each one: apply, email, or both.

## Status

| Phase | What it does | Status |
|---|---|---|
| **1. Discovery & scoring** | Collect openings from ATS job boards and HN, filter and rank them against your profile, export a report | ✅ This release |
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

cp config/profile.example.yaml config/profile.yaml   # then edit it (gitignored)

python -m applyflow run            # collect + score + report
```

Open `output/opportunities.html` in a browser for the ranked list, or
`output/opportunities.csv` in a spreadsheet.

A full run makes roughly one request per company board plus a few for HN, at most
one request per second per host, so it takes a minute or two.

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

## How it works

```
config/seeds.yaml ─┐
                   ├─► collectors ─► SQLite ─► scoring ─► report (HTML/CSV)
HN "Who is hiring" ┘   (Greenhouse,             ▲
                        Lever, Ashby, HN)       │
                                       config/profile.yaml
```

### Sources

| Source | Why | How |
|---|---|---|
| Greenhouse, Lever, Ashby | A large share of startups and tech companies host their job board on one of these; each exposes a free public JSON API | One request per company in `config/seeds.yaml` |
| Hacker News "Who is hiring?" | Monthly thread; strong for remote-global roles, and many posts invite applicants to email directly | Free HN Algolia API; top-level comments only |

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

```
score = family weight × (skills 0-50 + location 0-20 + track 0-20 + experience 0-10)
```

Tier A ≥ 70, tier B ≥ 45 (configurable). Each kept opening also gets an **action**:

- **Apply** - large companies (big job boards) and sponsored relocation roles: their formal process is the way in.
- **Email** - HN posts that ask applicants to email someone.
- **Apply + email** - smaller companies: apply, then send a short note to the hiring manager (Phase 2 drafts it).

### Tuning it

Everything lives in `config/profile.yaml`: role families and their patterns, excluded
title terms, experience limit, locations, skill weights and tier thresholds. Edit it,
run `python -m applyflow score` (no re-download needed), and use `explain` on any
opening that landed in the wrong place.

### Adding companies

```bash
python -m applyflow probe "Company Name" --add
```

This tries likely board slugs on Greenhouse, Lever and Ashby and adds the best match to
`config/seeds.yaml`. If it finds nothing, the company may use a different ATS, or pass a
slug you've seen in their careers URL: `probe "Acme" --slug acmehq`.

## Ground rules

- **Personal data stays local.** `config/profile.yaml`, the database (`data/`) and reports
  (`output/`) are gitignored.
- **Polite collection.** Honest User-Agent, at most one request per second per host,
  backoff on rate limits, public APIs only.
- **A human approves outreach.** Nothing will be sent without review (Phases 2-3).

## Development

```bash
pytest                 # or: python -m unittest discover -s tests -t .
```

Tests run offline against recorded-format fixtures in `tests/fixtures/`.

Project layout:

```
applyflow/
  collectors/        greenhouse.py, lever.py, ashby.py, hn.py
  cli.py             command-line entry point
  config.py          profile and seed loading
  http.py            polite HTTP client (rate limits, retries)
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
