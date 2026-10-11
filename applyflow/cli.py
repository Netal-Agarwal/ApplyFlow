"""Command-line interface.

    python -m applyflow run              # collect + score + report (the daily command)
    python -m applyflow collect          # fetch openings from every source
    python -m applyflow score            # (re)score stored openings against your profile
    python -m applyflow report           # write output/opportunities.html and .csv
    python -m applyflow stats            # counts by source and tier, top rejection reasons
    python -m applyflow explain <job>    # why a job got its score (id, or part of a title)
    python -m applyflow probe "Company"  # find a company's ATS board; --add saves it to seeds
    python -m applyflow verify-seeds     # check every seed board still works
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from applyflow import config
from applyflow.http import PoliteClient
from applyflow.pipeline import collect, probe, score_all
from applyflow.report import write_csv, write_html
from applyflow.scoring import score_job
from applyflow.store import Store


def _client(profile: config.Profile, min_interval: float) -> PoliteClient:
    return PoliteClient(user_agent=profile.user_agent, min_interval=min_interval)


def cmd_collect(args, profile, store) -> int:
    companies = config.load_seeds(args.seeds)
    sources = set(args.source)
    print(f"Collecting from {len(companies)} seed boards" + (" + HN" if "hn" in sources or "all" in sources else ""))
    summary = collect(
        store,
        _client(profile, args.min_interval),
        companies,
        include_ats="ats" in sources or "all" in sources,
        include_hn="hn" in sources or "all" in sources,
    )
    print(
        f"\nDone: {summary.boards_ok} boards ok, {len(summary.boards_failed)} failed; "
        f"{summary.jobs_seen} openings ({summary.jobs_new} new, {summary.jobs_closed} closed)."
    )
    if summary.boards_failed:
        if all("HTTP 4" in err for _, err in summary.boards_failed):
            print("Failed boards usually moved or closed. Check them with `python -m applyflow verify-seeds`.")
        else:
            print("Some requests didn't get through - check your internet connection and try again.")
    return 0


def cmd_score(args, profile, store) -> int:
    results = score_all(store, profile)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.tier] = counts.get(r.tier, 0) + 1
    print(
        f"Scored {len(results)} openings: "
        f"A={counts.get('A', 0)}  B={counts.get('B', 0)}  "
        f"skip={counts.get('skip', 0)}  rejected={counts.get('rejected', 0)}"
    )
    return 0


def cmd_report(args, profile, store) -> int:
    tiers = ("A",) if args.min_tier == "A" else ("A", "B")
    rows = store.ranked(tiers)
    out = Path(args.out)
    html_path = write_html(rows, out / "opportunities.html", store.stats())
    csv_path = write_csv(rows, out / "opportunities.csv")
    print(f"{len(rows)} opportunities written to:\n  {html_path}\n  {csv_path}")
    return 0


def cmd_run(args, profile, store) -> int:
    cmd_collect(args, profile, store)
    print()
    cmd_score(args, profile, store)
    print()
    return cmd_report(args, profile, store)


def cmd_stats(args, profile, store) -> int:
    s = store.stats()
    print(f"Active openings: {s['active_jobs']}")
    print("By source: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_source"].items())))
    print("By tier:   " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_tier"].items())))
    if s["top_reject_reasons"]:
        print("\nTop reasons openings were filtered out:")
        for reason, n in s["top_reject_reasons"]:
            print(f"  {n:>5}  {reason}")
    if s["failed_boards"]:
        print("\nBoards failing on their latest run:")
        for board, err in s["failed_boards"]:
            print(f"  {board}: {err}")
    return 0


def cmd_explain(args, profile, store) -> int:
    job = store.get_job(args.job)
    if job is None:
        matches = store.find_jobs(args.job)
        if not matches:
            print(f"No job matching '{args.job}'.")
            return 1
        if len(matches) > 1:
            print("Several jobs match; pass one of these ids:")
            for m in matches:
                print(f"  {m.id:<40} {m.company} - {m.title}")
            return 1
        job = matches[0]
    result = score_job(job, profile)     # fresh, so profile edits show immediately
    print(f"{job.company} - {job.title}\n{job.url}\n")
    print(f"Tier {result.tier}, score {result.score}" + (f", action: {result.action}" if result.action else ""))
    for reason in result.reasons:
        print(f"  - {reason}")
    return 0


def cmd_probe(args, profile, store) -> int:
    client = _client(profile, args.min_interval)
    print(f"Probing Greenhouse, Lever and Ashby for '{args.name}'...")
    found = probe(client, args.name, args.slug or None)
    if not found:
        print("No public board found. Try passing likely slugs: probe \"Name\" --slug acme --slug acmehq")
        return 1
    for company, n in found:
        print(f"  {company.ats:<11} {company.slug:<30} {n} open jobs")
    if args.add:
        company, _ = max(found, key=lambda pair: pair[1])
        added = config.append_seed(company, args.seeds)
        print(f"{'Added' if added else 'Already in seeds:'} {company.ats}:{company.slug}")
    return 0


def cmd_verify_seeds(args, profile, store) -> int:
    from applyflow.collectors import ATS_COLLECTORS
    from applyflow.http import FetchError

    client = _client(profile, args.min_interval)
    bad = 0
    for company in config.load_seeds(args.seeds):
        try:
            jobs = ATS_COLLECTORS[company.ats](client).fetch(company)
            status = f"ok ({len(jobs)} jobs)"
        except FetchError as exc:
            status = f"FAILED ({exc.status or exc})"
            bad += 1
        print(f"  {company.name:<20} {company.ats}:{company.slug:<35} {status}")
    print(f"\n{bad} board(s) need attention." if bad else "\nAll seed boards respond.")
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="applyflow", description="Find and rank openings that fit your profile.")
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--profile", default=None, help="profile YAML (default: config/profile.yaml)")
    parser.add_argument("--seeds", type=Path, default=config.SEEDS_PATH, help="seed companies YAML")
    parser.add_argument("--min-interval", type=float, default=1.0,
                        help="minimum seconds between requests to the same host (default 1.0)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_collect_opts(p):
        p.add_argument("--source", nargs="+", choices=["all", "ats", "hn"], default=["all"])

    def add_report_opts(p):
        p.add_argument("--out", default=str(config.OUTPUT_DIR))
        p.add_argument("--min-tier", choices=["A", "B"], default="B")

    p = sub.add_parser("collect", help="fetch openings from every source")
    add_collect_opts(p)
    p.set_defaults(func=cmd_collect)

    sub.add_parser("score", help="score stored openings").set_defaults(func=cmd_score)

    p = sub.add_parser("report", help="write the HTML and CSV report")
    add_report_opts(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("run", help="collect + score + report")
    add_collect_opts(p)
    add_report_opts(p)
    p.set_defaults(func=cmd_run)

    sub.add_parser("stats", help="counts and top rejection reasons").set_defaults(func=cmd_stats)

    p = sub.add_parser("explain", help="show why a job got its score")
    p.add_argument("job", help="job id (e.g. greenhouse:12345) or part of a title/company")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("probe", help="find a company's ATS board")
    p.add_argument("name")
    p.add_argument("--slug", action="append", help="slug to try (repeatable)")
    p.add_argument("--add", action="store_true", help="add the best match to seeds.yaml")
    p.set_defaults(func=cmd_probe)

    sub.add_parser("verify-seeds", help="check every seed board still responds").set_defaults(func=cmd_verify_seeds)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        profile = config.load_profile(Path(args.profile) if args.profile else None)
    except (OSError, config.ConfigError) as exc:
        print(f"error: couldn't load profile: {exc}", file=sys.stderr)
        return 2
    store = Store(args.db)
    try:
        return args.func(args, profile, store)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
