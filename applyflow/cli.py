"""Command-line interface.

    python -m applyflow run              # collect + score + report (the daily command)
    python -m applyflow collect          # fetch openings from every source
    python -m applyflow score            # (re)score stored openings against your profile
    python -m applyflow report           # write output/opportunities.html and .csv
    python -m applyflow stats            # counts by source and tier, top rejection reasons
    python -m applyflow explain <job>    # why a job got its score (id, or part of a title)
    python -m applyflow probe "Company"  # find a company's ATS board; --add saves it to seeds
    python -m applyflow verify-seeds     # check every seed board still works
    python -m applyflow verify-boards    # check discovered boards belong to their company
    python -m applyflow contacts         # find who to email at each outreach target (Phase 2a)
    python -m applyflow explain-contact <company>   # every candidate and why the primary won
    python -m applyflow discover         # find hiring startups (YC directory) and their job boards
    python -m applyflow draft-pack       # Phase 2b: company context, template drafts, drafting packets
    python -m applyflow import-drafts    # load output/drafts/written/<id>.md (validated)
    python -m applyflow queue --date D   # pick a day's batch (tier 1, 2, then gated tier 3)
    python -m applyflow mark-sent <id>   # record a sent email (or --batch D for a whole day)
    python -m applyflow mark-bounced <id>   # record a bounce; the next address is suggested
    python -m applyflow add-target "Co" --website co.com   # a target of your own

ApplyFlow never sends email: you send from Gmail and record it here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from applyflow import config
from applyflow.http import PoliteClient
from applyflow.discovery import discover_yc, verify_saved_boards
from applyflow.pipeline import boards_to_collect, collect, probe, score_all
from applyflow.report import write_companies_csv, write_contacts_csv, write_csv, write_html
from applyflow.scoring import score_job
from applyflow.store import Store


def _client(profile: config.Profile, min_interval: float) -> PoliteClient:
    return PoliteClient(user_agent=profile.user_agent, min_interval=min_interval)


def cmd_collect(args, profile, store) -> int:
    seeds = config.load_seeds(args.seeds)
    companies = boards_to_collect(store, seeds)
    sources = set(args.source)
    discovered = len(companies) - len(seeds)
    print(
        f"Collecting from {len(seeds)} seed boards + {discovered} discovered boards"
        + (" + HN" if "hn" in sources or "all" in sources else "")
    )
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


def _dnc():
    from applyflow.contacts import DoNotContact
    return DoNotContact.load(config.DO_NOT_CONTACT_PATH)


def cmd_report(args, profile, store) -> int:
    from applyflow.drafting import load_facts
    from applyflow.outreach import parse_day, refresh_drafts, stored_or_preview_batch
    from applyflow.report import outreach_sections, write_outbox_csv
    tiers = ("A",) if args.min_tier == "A" else ("A", "B")
    rows = store.ranked(tiers)
    out = Path(args.out)
    from applyflow.discovery import name_key
    contacts = {name_key(c["company"] or ""): c for c in store.primary_contacts()}
    items = refresh_drafts(store, profile, _dnc())
    outreach = ""
    if items:
        day = parse_day(getattr(args, "date", None))
        batch, stored, gate = stored_or_preview_batch(store, profile, items, day)
        outreach = outreach_sections(items, batch, day.isoformat(), stored, load_facts(profile).listing(),
                                     f"Tier 3 (guessed addresses) {gate.reason}.")
        outbox = write_outbox_csv(items, {it.target_id for it in batch}, out / "outbox.csv")
    html_path = write_html(rows, out / "opportunities.html", store.stats(), contacts, outreach)
    csv_path = write_csv(rows, out / "opportunities.csv")
    print(f"{len(rows)} opportunities written to:\n  {html_path}\n  {csv_path}")
    if items:
        written = sum(it.draft.source == "claude" for it in items)
        print(f"{len(items)} outreach drafts ({written} Claude-written, {len(items) - written} template); "
              f"today's batch: {len(batch)}{'' if stored else ' (preview)'}:\n  {outbox}")
    no_board = store.companies_without_board()
    if no_board:
        path = write_companies_csv(no_board, out / "startups_to_email.csv")
        print(f"{len(no_board)} hiring startups without a readable job board (Phase 2 email candidates):\n  {path}")
    if store.contact_checks():
        print(f"Contacts: {write_contacts_csv(store, out / 'contacts.csv')}")
    return 0


def cmd_discover(args, profile, store) -> int:
    client = _client(profile, args.min_interval)
    print(f"Discovering hiring startups from the YC directory (checking up to {args.limit})...")
    summary = discover_yc(store, client, profile, limit=args.limit)
    print(
        f"\n{summary.candidates} companies match your filters; checked {summary.checked} "
        f"({summary.skipped_recent} checked recently, skipped).\n"
        f"  boards found:      {len(summary.found)}  -> collected on your next `run`\n"
        f"  unverified board:  {len(summary.unverified)}  -> guessed board may be another company's; "
        f"listed in startups_to_email.csv\n"
        f"  unsupported board: {summary.unsupported}  -> listed in startups_to_email.csv\n"
        f"  no board found:    {summary.none}"
    )
    remaining = summary.candidates - summary.checked - summary.skipped_recent
    if remaining > 0:
        print(f"{remaining} companies left to check - run `discover` again to continue.")
    return 0


def cmd_contacts(args, profile, store) -> int:
    from applyflow.contacts import DoNotContact, GitHubBudget, MXChecker, build_targets, find_contacts
    from applyflow.discovery import name_key
    client = _client(profile, args.min_interval)
    targets = build_targets(store, profile, config.load_seeds(args.seeds), client)
    if args.company:
        wanted = name_key(args.company)
        targets = [t for t in targets if wanted and wanted in name_key(t.company)]
        if not targets:
            print(f"No outreach target matching '{args.company}'.")
            return 1
    dnc = DoNotContact.load(config.DO_NOT_CONTACT_PATH)
    github = GitHubBudget()
    mx = MXChecker(client, store, int(profile.contacts["mx_cache_days"]))
    days = int(profile.contacts["recheck_days"])
    roles = sum(t.kind == "role" for t in targets)
    print(f"Finding contacts for {len(targets)} targets ({roles} with A/B roles, {len(targets) - roles} general "
          f"outreach){'' if args.force else f'; companies checked in the last {days} days are skipped'}...")
    done = skipped = 0
    from applyflow.contacts import manual_result
    for target in targets:
        if not args.force and store.contacts_checked_recently(target.key, days):
            skipped += 1
            continue
        manual = store.get_manual_target(target.key) if target.kind == "manual" else None
        if manual is not None and manual["email"]:
            store.save_contact_result(manual_result(target, manual))     # given by hand: no lookup
            continue
        result = find_contacts(client, target, profile, dnc, github, log=print, mx=mx)
        store.save_contact_result(result)
        done += 1
    counts = store.contact_counts()
    print(f"\nLooked up {done}, skipped {skipped} checked recently. {github.status()}. {mx.status()}.")
    print("Primary contacts by confidence: " + ", ".join(
        f"{k}={counts.get(k, 0)}" for k in ("verified", "strong", "published_inbox", "guessed", "fallback", "none", "apply_only", "no_domain")))
    print(f"Written: {write_contacts_csv(store, Path(args.out) / 'contacts.csv')}")
    return 0


def cmd_explain_contact(args, profile, store) -> int:
    checks = store.find_contact_checks(args.company)
    if not checks:
        print(f"No contact lookup for '{args.company}'. Run `contacts --company \"{args.company}\"` first.")
        return 1
    if len(checks) > 1:
        exact = [c for c in checks if c["company"].lower() == args.company.lower()]
        if len(exact) != 1:
            print("Several companies match; be more specific:")
            for c in checks:
                print(f"  {c['company_key']:<36} {c['company']}")
            return 1
        checks = exact
    check = checks[0]
    details = json.loads(check["details"] or "{}")
    if check["kind"] == "role":
        kind = "A/B roles: " + "; ".join(json.loads(check["roles"] or "[]"))
    elif check["kind"] == "manual":
        kind = "added by hand (add-target)"
    else:
        kind = "general outreach (no readable board)"
    print(f"{check['company']} ({check['company_key']}) - {kind}")
    print(f"Domain: {check['domain'] or 'none'} (from {check['domain_source'] or '-'}); "
          f"team size {check['team_size'] or '?'}; looking for: {check['hint']}; checked {check['checked_at'][:10]}")
    print("\nSources:")
    for reason in details.get("reasons", []):
        print(f"  - {reason}")
    print("\nCandidates (best first; fit 0 = the role we're looking for):")
    for i, c in enumerate(details.get("considered", [])):
        mark = "PRIMARY" if i == 0 else ("backup " if i < 3 else "       ")
        who = f"{c['name']} - {c['role']}" if c["name"] else c["role"]
        mx = " (MX unchecked)" if c.get("mx") == "unchecked" else ""
        print(f"  {mark} {c['confidence']:<15} fit {c['fit']}  {c['email']:<38} {who} "
              f"[{c['source']}]{mx} {c['note']}".rstrip())
        print(f"           {c['source_url']}")
        if c.get("alternates"):
            print(f"           if it bounces, try: {', '.join(c['alternates'])}")
    for email, why in details.get("excluded", []):
        print(f"  EXCLUDED {email}: {why}")
    if details.get("why_primary"):
        print(f"\nWhy the primary won: {details['why_primary']}.")
    return 0


def cmd_draft_pack(args, profile, store) -> int:
    from applyflow.outreach import build_items, ensure_context, refresh_drafts, write_packets
    items = build_items(store, profile, _dnc())
    if not items:
        print("No outreach targets yet. Run `contacts` first.")
        return 1
    if not args.no_fetch:
        missing = sum(store.get_context(it.company_key) is None and bool(it.inputs.website) for it in items)
        print(f"Fetching homepage context for {missing} target(s) without it (robots.txt respected)...")
        ensure_context(store, _client(profile, args.min_interval), items, log=print, force=args.refetch)
    items = refresh_drafts(store, profile, _dnc())
    out = Path(args.out)
    paths = write_packets(items, profile, out)
    bad = [it for it in items if it.draft.template_failures]
    written = sum(it.draft.source == "claude" for it in items)
    print(f"\n{len(paths)} packets in {out / 'packets'} (STYLE_GUIDE.md, INDEX.md alongside).")
    print(f"Drafts: {written} Claude-written, {len(items) - written} template; "
          f"{sum(it.status == 'ready' for it in items)} targets ready to send.")
    for it in bad:
        print(f"  template check failed for {it.target_id}: {'; '.join(it.draft.template_failures)}")
    print(f"Write drafts to {out / 'written'}\\<id>.md, then run `import-drafts`.")
    return 0


def cmd_import_drafts(args, profile, store) -> int:
    from applyflow.outreach import import_written
    folder = Path(args.dir)
    if not folder.exists():
        print(f"No folder {folder}.")
        return 1
    results = import_written(store, profile, folder, _dnc())
    ok = [r for r in results if r.ok]
    print(f"Imported {len(results)} file(s): {len(ok)} passed, {len(results) - len(ok)} stay on the template.")
    for r in results:
        if not r.ok:
            print(f"  {r.target_id}: " + "; ".join(r.failures))
    return 0 if len(ok) == len(results) else 1


def cmd_queue(args, profile, store) -> int:
    from applyflow.outreach import OutreachError, make_queue, parse_day
    try:
        day = parse_day(args.date)
    except OutreachError as exc:
        print(f"error: {exc}")
        return 2
    batch = make_queue(store, profile, day, args.size, _dnc())
    print(f"Batch for {batch.day}: {len(batch.items)} email(s)")
    for i, it in enumerate(batch.items, start=1):
        a = it.address
        who = f"{a.name} ({a.role})" if a and a.name else (a.role if a else "")
        state = f"  [sent {it.sent_date}]" if it.status == "sent" else ""
        print(f"  {i:>2}. T{it.send_tier} {it.company:<24} {(it.sent_email or a.email):<34} {who}{state}")
    tier3 = sum(it.send_tier == 3 for it in batch.items)
    print(f"Tier 3 (guessed addresses) {batch.gate.reason}.")
    print(f"  {tier3} in this batch, {batch.tier3_held} held back.")
    if batch.skipped_same_company:
        print(f"{batch.skipped_same_company} skipped: another target for the same company is queued.")
    print("Send them from the report (Open in Gmail), then `mark-sent --batch "
          f"{batch.day}` or `mark-sent <id>` for each.")
    return 0


def cmd_mark_sent(args, profile, store) -> int:
    from applyflow.outreach import OutreachError, mark_batch_sent, mark_sent, parse_day
    try:
        if args.batch:
            marked = mark_batch_sent(store, profile, parse_day(args.batch), _dnc())
            print(f"Marked {len(marked)} sent from the {args.batch} batch:")
            for m in marked:
                print(f"  {m}")
            return 0
        if not args.target:
            print("error: give a target id, or --batch YYYY-MM-DD")
            return 2
        it = mark_sent(store, profile, args.target, parse_day(args.date), _dnc())
    except OutreachError as exc:
        print(f"error: {exc}")
        return 1
    print(f"Marked sent: {it.target_id} -> {it.address.email}")
    return 0


def cmd_mark_bounced(args, profile, store) -> int:
    from applyflow.outreach import OutreachError, mark_bounced, parse_day
    try:
        it = mark_bounced(store, profile, args.target, parse_day(args.date), _dnc())
    except OutreachError as exc:
        print(f"error: {exc}")
        return 1
    print(f"Bounce recorded for {it.target_id} ({it.bounced[-1]}).")
    if it.address:
        print(f"Next address: {it.address.label()} - send tier {it.send_tier}. "
              f"The draft greets '{it.inputs.greeting}'" + (" (template: the written draft no longer fits)"
                                                            if it.draft.failures else "") + ".")
    else:
        print("No more addresses for this company.")
    return 0


def cmd_add_target(args, profile, store) -> int:
    from applyflow.discovery import site_domain
    from applyflow.outreach import OutreachError, add_target
    if not site_domain(args.website):
        print(f"error: '{args.website}' isn't a website domain")
        return 2
    try:
        it = add_target(store, profile, _client(profile, args.min_interval), args.company, args.website,
                        note=args.note or "", email=args.email or "", person=args.person or "",
                        role=args.role or "", dnc=_dnc(), log=print, out_dir=Path(args.out) / "drafts")
    except OutreachError as exc:
        print(f"error: {exc}")
        return 1
    a = it.address
    print(f"Added {it.company} ({it.target_id}): " + (f"{a.label()}, send tier {it.send_tier}" if a else
                                                      f"no contact found ({it.status})"))
    print(f"Template draft stored; packet: {Path(args.out) / 'drafts' / 'packets' / (it.target_id + '.md')}")
    return 0


def cmd_verify_boards(args, profile, store) -> int:
    print("Re-checking that every discovered board belongs to its company...")
    demoted, promoted = verify_saved_boards(store, _client(profile, args.min_interval))
    print(f"\n{len(demoted)} board(s) demoted to unverified; their jobs no longer appear in the report "
          "(re-run `report` or `run` to refresh it).")
    if promoted:
        print(f"{len(promoted)} unverified board(s) now pass and will be collected on the next `run`.")
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
    if s["discovered"]:
        d = s["discovered"]
        print(f"\nDiscovered companies: {sum(d.values())} "
              f"(board found {d.get('found', 0)}, unverified board {d.get('unverified', 0)}, "
              f"unsupported board {d.get('unsupported', 0)}, none {d.get('none', 0)})")
    if s.get("contacts"):
        c = s["contacts"]
        print(f"\nContacts: {sum(c.values())} companies looked up; primary contact " + ", ".join(
            f"{k}={c.get(k, 0)}" for k in ("verified", "strong", "published_inbox", "guessed", "fallback", "none", "apply_only", "no_domain")))
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
    if result.contact_hint:
        print(f"Who to contact: {result.contact_hint}")
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
        p.add_argument("--date", help="day whose batch the report shows (YYYY-MM-DD, default today)")

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
    p = sub.add_parser("contacts", help="find the best person to email at each outreach target (Phase 2a)")
    p.add_argument("--company", help="only targets whose name contains this")
    p.add_argument("--force", action="store_true", help="look up again even if checked recently")
    p.add_argument("--out", default=str(config.OUTPUT_DIR))
    p.set_defaults(func=cmd_contacts)

    p = sub.add_parser("explain-contact", help="every contact candidate for a company, and why the primary won")
    p.add_argument("company")
    p.set_defaults(func=cmd_explain_contact)

    sub.add_parser("verify-boards", help="check discovered boards belong to their company; demote mismatches"
                   ).set_defaults(func=cmd_verify_boards)

    p = sub.add_parser("draft-pack", help="fetch company context, refresh template drafts, write drafting packets")
    p.add_argument("--out", default=str(config.DRAFTS_DIR))
    p.add_argument("--no-fetch", action="store_true", help="don't fetch missing homepage context")
    p.add_argument("--refetch", action="store_true", help="fetch every homepage again")
    p.set_defaults(func=cmd_draft_pack)

    p = sub.add_parser("import-drafts", help="load written drafts (output/drafts/written/<id>.md), validated")
    p.add_argument("--dir", default=str(config.DRAFTS_DIR / "written"))
    p.set_defaults(func=cmd_import_drafts)

    p = sub.add_parser("queue", help="pick a day's send batch (nothing is sent)")
    p.add_argument("--date", help="YYYY-MM-DD (default today)")
    p.add_argument("--size", type=int, help="batch size (default outreach.daily_batch)")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("mark-sent", help="record an email you sent")
    p.add_argument("target", nargs="?", help="target id, e.g. yc-clickpe")
    p.add_argument("--batch", help="mark a whole day's queue (YYYY-MM-DD) as sent")
    p.add_argument("--date", help="send date, YYYY-MM-DD (default today)")
    p.set_defaults(func=cmd_mark_sent)

    p = sub.add_parser("mark-bounced", help="record a bounce; the next address is suggested")
    p.add_argument("target")
    p.add_argument("--date", help="YYYY-MM-DD (default today)")
    p.set_defaults(func=cmd_mark_bounced)

    p = sub.add_parser("add-target", help="add a company of your own as an outreach target")
    p.add_argument("company")
    p.add_argument("--website", required=True, help="the company's domain, e.g. acme.com")
    p.add_argument("--note", help="why it's a target (shown in its packet)")
    p.add_argument("--email", help="address to use (skips the contact search)")
    p.add_argument("--name", dest="person", help="the person's name")
    p.add_argument("--role", help="the person's role, e.g. CTO")
    p.add_argument("--out", default=str(config.OUTPUT_DIR))
    p.set_defaults(func=cmd_add_target)

    p = sub.add_parser("discover", help="find hiring startups and their job boards automatically")
    p.add_argument("--limit", type=int, default=40, help="companies to check this run (default 40)")
    p.set_defaults(func=cmd_discover)
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
