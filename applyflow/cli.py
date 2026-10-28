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
    python -m applyflow mark-bounced <id> [--stop]   # record a bounce; the next address is suggested (or none)
    python -m applyflow add-target "Co" --website co.com   # a target of your own
    python -m applyflow pool             # Phase 2c: every source -> scored pool, top ~400 selected
    python -m applyflow pool --reselect  # 2c.1: apply large-company / tech-fit rules offline, refill
    python -m applyflow contacts --limit 50   # contacts in resumable batches, with an ETA
    python -m applyflow verify-sources   # every published address must appear on the company's site
    python -m applyflow hn-archive --from 2026-05 --to 2026-10   # 2e: HN "Who is hiring?" archive -> role targets
    python -m applyflow yc-jobs          # 2e: YC company job pages -> role targets
    python -m applyflow import-targets   # 2e: config/private/manual_targets.txt -> manual targets with drafts
    python -m applyflow role-check       # 2e: a role match (board / careers page) for targets without one
    python -m applyflow contacts --deep  # 2e: more pages for targets with no safe contact
    python -m applyflow safe-contacts    # 2e: unsent safe contacts by kind, and sending days covered
    python -m applyflow supply --all     # 2f: fintech lists, consultancies, GitHub lists, YC fintech -> the pool
    python -m applyflow supply --consultancies --saas --fintech-seeds --fintech   # 2g: seed lists (new ones only)
    python -m applyflow guess-evidence   # 2f: which stored guesses are backed by evidence (offline)
    python -m applyflow classify-published [--apply]   # 2f: "other" published addresses: hiring or not
    python -m applyflow referrals        # 2f: output/referrals.csv for large / apply-only companies

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
        from applyflow.outreach import send_records, type_gates
        gates = type_gates(send_records(store), day, profile.outreach)
        from applyflow.outreach import recheck_due
        outreach = outreach_sections(items, batch, day.isoformat(), stored, load_facts(profile).listing(),
                                     "Per-type gate (2f): " + "; ".join(
                                         f"{k.replace('_', ' ')} {g.reason}" for k, g in gates.items()
                                         if k in ("evidence_guess", "general_inbox", "hiring_inbox")) + ".",
                                     recheck=recheck_due(items, day))
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
    names = store.known_first_names()          # 2f: evidence for guesses (a published 'rahul@' shows first@)
    roles = sum(t.kind == "role" for t in targets)
    from applyflow.contacts import pending_targets
    if args.deep:
        batch, pending = deep_targets(store, profile, targets, args.limit)
        print(f"Deep search (2e): {len(pending)} target(s) with no safe contact (none, info@ / contact@ only, guessed "
              f"or an unrecognised address); this batch {len(batch)}.")
    else:
        batch, pending = pending_targets(store, targets, days, args.force, args.limit, args.retry_unread)
    if args.shard:                             # 2f: "i/n" - run n lookups side by side on disjoint targets
        import zlib
        i, n = (int(x) for x in args.shard.split("/"))
        batch = [t for t in batch if zlib.crc32(t.key.encode()) % n == i]
        pending = [t for t in pending if zlib.crc32(t.key.encode()) % n == i]
    skipped = len(targets) - len(pending)
    recent = "" if args.force else f"; {skipped} checked in the last {days} days are skipped"
    print(f"Finding contacts for {len(targets)} targets ({roles} with A/B roles, {len(targets) - roles} general "
          f"outreach){recent}.")
    print(f"This batch: {len(batch)} of {len(pending)} still to look up"
          + (f" (--limit {args.limit}; run again to continue where this stops)" if args.limit else "") + ".")
    done = 0
    from applyflow.contacts import manual_result
    import time
    started = time.monotonic()
    for i, target in enumerate(batch, start=1):
        manual = store.get_manual_target(target.key) if target.kind == "manual" else None
        if manual is not None and manual["email"]:
            store.save_contact_result(manual_result(target, manual))     # given by hand: no lookup
            continue
        result = find_contacts(client, target, profile, dnc, github, log=print, mx=mx, deep=args.deep, names=names)
        if args.deep and any(r.startswith("website (deep): read 0 page(s)") for r in result.reasons) \
                and store.contacts_for(target.key):
            print(f"    kept the stored contacts: the site couldn't be read now")
        else:
            store.save_contact_result(result)
        done += 1
        elapsed = time.monotonic() - started
        per = elapsed / i
        left_batch, left_all = len(batch) - i, len(pending) - i
        print(f"    [{i}/{len(batch)}] {_mmss(elapsed)} elapsed, ~{_mmss(per * left_batch)} left in this batch"
              + (f", ~{_mmss(per * left_all)} for all {left_all} remaining" if left_all != left_batch else ""))
    counts = store.contact_counts()
    remaining = len(pending) - len(batch)
    print(f"\nLooked up {done}, skipped {skipped} checked recently. {github.status()}. {mx.status()}."
          + (f"\n{remaining} target(s) still to look up: run `contacts --limit {args.limit}` again." if remaining else ""))
    print("Primary contacts by confidence: " + ", ".join(
        f"{k}={counts.get(k, 0)}" for k in ("verified", "strong", "published_inbox", "guessed", "fallback", "none", "apply_only", "no_domain")))
    print(f"Written: {write_contacts_csv(store, Path(args.out) / 'contacts.csv')}")
    return 0


def deep_targets(store, profile, targets, limit: int = 0):
    """2e: targets (not queued / sent / locked) whose current address isn't safe - no contact, only info@ /
    contact@, a guess or an unrecognised address - in target order; (this batch, all) like pending_targets."""
    from applyflow.outreach import build_items
    items = {it.company_key: it for it in build_items(store, profile, _dnc())}
    deep_done = {r[0] for r in store.conn.execute(
        "SELECT company_key FROM contact_checks WHERE details LIKE '%website (deep)%'")}
    pending = []
    for t in targets:
        it = items.get(t.key)
        if it is None and t.domain and t.key not in deep_done:
            pending.append(t)                  # 2f: a new target, never looked up: the deep search is its first
            continue
        if it is None or it.protected or t.key in deep_done or not t.domain:
            continue
        if it.status in ("no_contact", "unsendable") or (it.status == "ready" and it.address is not None
                                                          and it.address.kind in ("guessed", "other")):
            pending.append(t)
    return (pending[:limit] if limit else pending), pending


def _mmss(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m" if seconds >= 3600 else f"{seconds // 60}m{seconds % 60:02d}s"


def cmd_pool(args, profile, store) -> int:
    from applyflow.outreach import parse_day
    from applyflow.pool import build_pool, rescore, reselect
    from applyflow.report import write_pool_csv
    day = parse_day(args.date)
    if args.target_size:                      # 2e: a bigger pool for one run, without editing profile.yaml
        profile.pool["target_size"] = args.target_size
    if args.reselect:
        scored, changes = reselect(store, profile, day)
        dropped = [c for c in changes if c.was_selected]
        added = [c for c in changes if not c.was_selected]
        print(f"Re-selected under the current rules (offline): {len(dropped)} dropped, {len(added)} added.")
        for c in dropped:
            print(f"  - {c.company.name:<30} {c.now.why_selected}")
        for c in added:
            print(f"  + {c.company.name:<30} {c.now.score:>3}  {c.company.region:<13} {c.now.why_selected}")
        if added:
            print("Run `contacts --limit 50` for the new targets, then `draft-pack`.")
    elif args.rescore:
        scored = rescore(store, profile, day)
        print(f"Re-scored {len(scored)} pool companies (selection kept).")
    else:
        print("Building the outreach pool from every source (robots.txt respected, 1 request/second/host)...")
        summary = build_pool(store, _client(profile, args.min_interval), profile, day)
        scored = summary.scored
        print("\nCompanies per source (before dedupe):")
        for k, v in summary.per_source.items():
            print(f"  {k:<34} {v:>5}")
        for k, v in summary.notes.items():
            print(f"  note {k}: {v}")
        print(f"Before dedupe: {summary.before_dedupe}; after dedupe by website domain: {summary.after_dedupe}.")
        print(f"Funding signal within 6 months: {summary.funding['6m']}; within 12 months: {summary.funding['12m']}.")
    selected = [s for s in scored if s.selected]
    india = sum(s.company.region == "india" for s in selected)
    print(f"Selected {len(selected)} for outreach ({india} India, {len(selected) - india} remote-global); "
          f"role / manual targets come on top.")
    print("\nTop 20 by hiring likelihood:")
    for s in scored[:20]:
        print(f"  {s.score:>3}  {s.company.name:<28} {s.company.region:<13} {'; '.join(s.reasons)}")
    print(f"\nWritten: {write_pool_csv(scored, Path(args.out) / 'pool.csv')}")
    return 0


def _month(text: str, end: bool = False):
    from datetime import date, timedelta
    y, m = (int(x) for x in text.split("-")[:2])
    if not end:
        return date(y, m, 1)
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))


def cmd_hn_archive(args, profile, store) -> int:
    from applyflow.refill import existing_targets, collect_hn_archive
    client = _client(profile, args.min_interval)
    start, end = _month(args.start), _month(args.end, end=True)
    print(f"Reading HN 'Who is hiring?' threads {start} to {end} (HN Algolia API)...")
    stats = collect_hn_archive(store, client, profile, start, end, existing_targets(store, _dnc()), log=print)
    score_all(store, profile)
    print(f"\n{stats['posts']} posts in {len(stats['threads'])} thread(s); kept {stats['kept']} "
          f"({stats['new']} new companies, {stats['existing']} already targets).")
    for why, n in sorted(stats["skipped"].items(), key=lambda kv: -kv[1])[:12]:
        print(f"  skipped {n:>4}  {why}")
    print("Openings re-scored. Run `contacts --limit 50` for the new role targets, then `draft-pack`.")
    return 0


def cmd_yc_jobs(args, profile, store) -> int:
    from applyflow.refill import collect_yc_jobs, existing_targets
    rows = store.conn.execute("SELECT key, name FROM pool_companies WHERE key LIKE 'yc:%' ORDER BY "
                              "CASE region WHEN 'india' THEN 0 ELSE 1 END, rank").fetchall()
    slugs = [(r["key"][3:], r["name"]) for r in rows]
    if args.limit:
        slugs = slugs[:args.limit]
    print(f"Reading {len(slugs)} YC company job pages (robots.txt allows /companies/<slug>)...")
    stats = collect_yc_jobs(store, _client(profile, args.min_interval), profile, slugs, existing_targets(store, _dnc()),
                            log=print)
    score_all(store, profile)
    print(f"\n{stats['companies']} pages read ({stats['failed']} failed); {stats['with_postings']} list postings "
          f"({stats['postings']} in all); {stats['kept']} fit at {stats['matched_companies']} companies.")
    for why, n in sorted(stats["skipped"].items(), key=lambda kv: -kv[1])[:10]:
        print(f"  not kept {n:>4}  {why}")
    if stats["new_grads_ok"]:
        print("Roles marked 'new grads OK':")
        for company, title, salary in stats["new_grads_ok"]:
            print(f"  {company:<26} {title:<44} {salary}")
    return 0


def cmd_import_targets(args, profile, store) -> int:
    from applyflow.refill import import_targets
    from applyflow.outreach import KIND_LABELS
    path = Path(args.file)
    results, errors = import_targets(store, profile, _client(profile, args.min_interval), path, _dnc(), log=print,
                                     out_dir=Path(args.out) / "drafts")
    for e in errors:
        print(f"  {e}")
    for r in results:
        if r.error:
            print(f"  line {r.line.line}: {r.line.company}: {r.error}")
            continue
        role = r.role.get("title") or ""
        print(f"  {r.line.company:<26} {r.status:<10} {r.address or '-':<32} {KIND_LABELS.get(r.kind, r.kind):<22} "
              f"tech fit {r.tech_fit}; role match {r.role.get('match', 'unknown')}" + (f": {role}" if role else ""))
    if results:
        print(f"Packets in {Path(args.out) / 'drafts' / 'packets'}; write drafts, then `import-drafts`.")
    return 0


def cmd_role_check(args, profile, store) -> int:
    from applyflow.outreach import build_items
    from applyflow.refill import check_roles
    have = store.role_matches()
    since = {r[0] for r in store.conn.execute("SELECT company_key FROM contact_checks WHERE checked_at >= ?",
                                              (args.checked_since,))} if args.checked_since else None
    keys = [it.company_key for it in build_items(store, profile, _dnc())
            if it.status in ("ready", "apply_only") and not it.protected and it.company_key not in have
            and (it.company_key.startswith(("pool:", "manual:")) or args.all or since is not None)
            and (since is None or it.company_key in since)]
    if args.limit:
        keys = keys[:args.limit]
    print(f"Checking {len(keys)} target(s) for an open role that fits (ATS board, else careers page)...")
    counts = check_roles(store, _client(profile, args.min_interval), profile, keys, log=print)
    print("Role matches: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def cmd_supply(args, profile, store) -> int:
    """2f: fintech lists, product-engineering consultancies, GitHub lists and India-friendly YC fintechs -> the
    stored pool (verified on their own sites), then the selection is redone offline."""
    from applyflow.contacts import MXChecker
    from applyflow.outreach import parse_day
    from applyflow.discovery import site_domain
    from applyflow.sources import PoolCompany, collect_list, company_website
    from applyflow import supply
    day = parse_day(args.date)
    if args.target_size:
        profile.pool["target_size"] = args.target_size
    client = _client(profile, args.min_interval)
    mx = MXChecker(client, store, int(profile.contacts["mx_cache_days"]))
    sources = config.load_sources()
    found: list[PoolCompany] = []
    report: list[str] = []
    want = lambda flag: args.all or getattr(args, flag)
    if want("fintech"):
        from applyflow.refill import resolve_domain
        resolve = lambda name: resolve_domain(client, mx, name, "fintech payments lending")[0]
        for cfg in sources.get("fintech_lists") or []:
            print(f"{cfg.get('label', cfg['id'])}...")
            got, stats = supply.collect_fintech(client, mx, cfg, profile, log=print, resolve=resolve)
            found += got
            report.append(f"fintech:{cfg['id']}: {stats.get('note', '')}; dropped {len(stats.get('dropped', []))}")
    in_pool = {json.loads(r["data"]).get("domain", "") for r in store.pool_rows()}
    for flag, sector in (("consultancies", "consultancy"), ("saas", "saas"), ("fintech_seeds", "fintech")):
        if not want(flag):
            continue
        source, cfg_key = supply.SEED_SECTORS[sector][:2]
        seeds = sources.get(cfg_key) or []
        fresh = [s for s in seeds if site_domain(company_website(str(s["website"]))) not in in_pool]   # 2g
        print(f"{cfg_key} seeds ({len(seeds)}; {len(seeds) - len(fresh)} already in the pool, kept as they are)...")
        got, stats = supply.collect_seeds(client, mx, fresh, profile, sector, log=print)
        found += got
        report.append(f"{source}: {stats['note']} ({len(seeds) - len(fresh)} already in the pool)")
        for d in stats["dropped"]:
            print(f"  dropped {d}")
    if want("lists"):
        for cfg in sources.get("github_lists") or []:
            got, note = collect_list(client, cfg, print)
            found += got
            report.append(f"list:{cfg['id']}: {note}")
    if want("yc_fintech"):
        rows = [PoolCompany.from_json(json.loads(r["data"])) for r in store.pool_rows()]
        cands = [c for c in rows if c.region != "india" and "yc" in c.sources and not c.india_friendly
                 and (c.sector or supply.pool_sector(c)) == "fintech"]
        print(f"Remote YC fintechs: checking {len(cands)} jobs pages for a posting open to India...")
        friendly = 0
        for c in cands:
            why = supply.india_friendly(client, profile, c)
            if why:
                c.india_friendly, c.sector = why, "fintech"
                found.append(c)
                friendly += 1
                print(f"  {c.name:<28} {why}")
        report.append(f"yc fintech (remote): {len(cands)} checked, {friendly} India-friendly")
    if want("yc_fintech_india"):                   # 2g: India engineering on the company's own site
        rows = [PoolCompany.from_json(json.loads(r["data"])) for r in store.pool_rows()]
        cands = [c for c in rows if c.region != "india" and "yc" in c.sources and not c.india_friendly
                 and (c.sector or supply.pool_sector(c)) == "fintech" and c.website]
        print(f"YC fintechs abroad: checking {len(cands)} sites for an office / engineering team in India...")
        hits = 0
        for c in cands:
            why = supply.india_engineering(client, c.website)
            if why:
                c.india_friendly, c.sector = f"India engineering on its site ({why})", "fintech"
                found.append(c)
                hits += 1
                print(f"  {c.name:<28} {why}")
        report.append(f"yc fintech (India engineering): {len(cands)} checked, {hits} with an India office / team")
    scored, changes, new = supply.extend_pool(store, profile, found, day, log=print)
    added = [c for c in changes if c.now.selected and not c.was_selected]
    print("\n".join(report))
    print(f"Newly selected: {len(added)}")
    for c in added:
        print(f"  + {c.company.name:<30} {c.company.sector or '-':<12} {c.now.why_selected}")
    print("Next: `contacts --deep --limit 50` (new targets), `role-check --checked-since <today>`, `draft-pack`.")
    return 0


def cmd_guess_evidence(args, profile, store) -> int:
    from applyflow.supply import backfill_evidence
    counts = backfill_evidence(store, log=print)
    print(f"{counts['guesses']} stored guesses of unprotected targets checked; {counts['evidence']} backed by evidence "
          f"({counts['moved']} moved to the address format the evidence shows). The rest stay pure pattern guesses "
          "(never sent).")
    return 0


def cmd_classify_published(args, profile, store) -> int:
    from applyflow.outreach import build_items
    from applyflow.supply import classify_other_addresses
    rows = classify_other_addresses(store, build_items(store, profile, _dnc()), apply=args.apply)
    for r in rows:
        print(f"  {r.company:<26} {r.email:<36} {r.kind:<13} {r.reason}")
    n = sum(r.kind != "unsendable" for r in rows)
    print(f"{len(rows)} 'other' published address(es): {n} sendable, {len(rows) - n} unsendable"
          + (" (recorded in excluded_contacts)" if args.apply else " (dry run: --apply records the unsendable ones)"))
    return 0


def cmd_referrals(args, profile, store) -> int:
    from applyflow.outreach import build_items
    from applyflow.supply import referral_rows, write_referrals
    rows = referral_rows(store, profile, build_items(store, profile, _dnc()))
    path = write_referrals(rows, Path(args.out) / "referrals.csv")
    print(f"{len(rows)} referral row(s) written to {path} (send the LinkedIn notes yourself).")
    return 0


def cmd_safe_contacts(args, profile, store) -> int:
    from applyflow.outreach import KIND_LABELS, build_items, mark_queued
    from applyflow.refill import safe_summary
    items = build_items(store, profile, _dnc())
    mark_queued(store, items)
    s = safe_summary(items, int(profile.outreach["daily_batch"]), int(profile.outreach["general_inbox_per_day"]),
                     args.guessed_per_day)
    print("Ready, unsent targets by contact kind:")
    for k, n in sorted(s["by_kind"].items(), key=lambda kv: -kv[1]):
        print(f"  {KIND_LABELS.get(k, k):<28} {n:>4}")
    print(f"Safe (verified person, founders@, hiring inbox): {s['safe']}; hello@ / hi@ / team@: {s['general']}; "
          f"guessed: {s['guessed']} ({s['queued']} of all these already in a stored batch).")
    print(f"At {profile.outreach['daily_batch']}/day with <= {profile.outreach['general_inbox_per_day']} hello@ and "
          f"<= {args.guessed_per_day} guessed a day: {s['days']} sending day(s).")
    statuses: dict[str, int] = {}
    for it in items:
        statuses[it.status] = statuses.get(it.status, 0) + 1
    print("All targets by status: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))
    from applyflow.supply import supply_summary
    t = supply_summary(items, profile.outreach, int(profile.outreach["daily_batch"]))
    print(f"2f sendable (not in a stored batch): {t['sendable']}; by sector: "
          + ", ".join(f"{k or '-'}={v}" for k, v in sorted(t["by_sector"].items()))
          + f". At {profile.outreach['daily_batch']}/day with the 2f caps: {t['days']} day(s), "
            f"{t['full_days']} of them full.")
    from applyflow.outreach import guess_outcomes
    labels = {"first_name": "first-name address (founder-style)", "staff_first_last": "staff first.last address"}
    for axis, title in (("format", "address format"), ("evidence", "evidence kind")):
        rows = guess_outcomes(store)[axis]
        if rows:
            print(f"Evidence-guess outcomes by {title} (2g): " + "; ".join(
                f"{labels.get(k, k)} sent {v['sent']}, bounced {v['bounced']}, replied {v['replied']}"
                for k, v in sorted(rows.items())))
    return 0


def cmd_verify_sources(args, profile, store) -> int:
    from applyflow.contacts import verify_sources
    print("Checking that every published / verified address appears on the company's own site...")
    results = verify_sources(store, _client(profile, args.min_interval), profile, log=print)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("\n" + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for r in results:
        if r.status == "demoted":
            print(f"  DEMOTED {r.company}: {r.email} - {r.note}")
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
    from applyflow.outreach import Freshness
    fresh = None if args.no_fresh else Freshness(_client(profile, args.min_interval), store, day, log=print)
    if fresh is not None:
        print("Freshness check (2f): re-reading each picked address's source page...")
    batch = make_queue(store, profile, day, args.size, _dnc(), fresh=fresh)
    print(f"Batch for {batch.day}: {len(batch.items)} email(s)")
    for i, it in enumerate(batch.items, start=1):
        a = it.address
        who = f"{a.name} ({a.role})" if a and a.name else (a.role if a else "")
        state = f"  [sent {it.sent_date}]" if it.status == "sent" else ""
        print(f"  {i:>2}. T{it.send_tier} {it.company:<24} {(it.sent_email or a.email):<34} {who}{state}")
    from applyflow.outreach import KIND_LABELS
    print("By contact type (2f per-type gate):")
    for why, n in batch.held_by_kind.items():
        if why not in batch.gates:
            print(f"  held: {n} - {why}")
    for kind, g in batch.gates.items():
        if kind.startswith("_"):
            continue
        n = sum(1 for it in batch.items if it.address and it.address.kind == kind)
        held = batch.held_by_kind.get(kind, 0)
        if n or held or g.state == "paused":
            print(f"  {KIND_LABELS.get(kind, kind):<44} {n:>2} in batch, {held:>3} held - {g.reason}")
    if fresh is not None:
        counts = {s: sum(c.status == s for c in batch.fresh) for s in ("ok", "gone", "unchecked")}
        print(f"Freshness: {counts['ok']} still on their page, {counts['gone']} dropped (excluded_contacts), "
              f"{counts['unchecked']} kept unchecked (page unreadable now).")
    if batch.held_earlier:
        print(f"{batch.held_earlier} ready target(s) left out: already in an earlier day's stored queue, not yet "
              "marked sent.")
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
        it = mark_bounced(store, profile, args.target, parse_day(args.date), _dnc(), stop=args.stop)
    except OutreachError as exc:
        print(f"error: {exc}")
        return 1
    print(f"Bounce recorded for {it.target_id} ({it.bounced[-1]}).")
    if it.status == "stopped":
        print("Its alternates and backups are on hold (--stop): it won't be queued again.")
    elif it.address:
        print(f"Next address: {it.address.label()} - send tier {it.send_tier}. "
              f"The draft greets '{it.inputs.greeting}'" + (" (template: the written draft no longer fits)"
                                                            if it.draft.failures else "") + ".")
    else:
        print("No more addresses for this company.")
    return 0


def cmd_mark_replied(args, profile, store) -> int:
    from applyflow.outreach import OUTCOME_LABELS, OutreachError, mark_replied, parse_day
    try:
        it = mark_replied(store, profile, args.email, args.outcome, parse_day(args.date), args.recheck or "",
                          args.contact or "", args.role or "", _dnc())
    except OutreachError as exc:
        print(f"error: {exc}")
        return 1
    print(f"Reply recorded for {it.target_id} ({it.sent_email}): {OUTCOME_LABELS[it.outcome]}"
          + (f", recheck {it.recheck}" if it.recheck else "") + "; follow-ups stopped."
          + (f" Stored {it.replied_by} as a verified named person (source: the reply)." if it.replied_by else ""))
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
    p.add_argument("--limit", type=int, default=0, help="look up at most N targets this run (resumable)")
    p.add_argument("--retry-unread", action="store_true",
                   help="also redo lookups that read no page of the site (network drop, failing site)")
    p.add_argument("--deep", action="store_true",
                   help="2e: read more pages (team, about-us, careers, jobs, join, contact, privacy, terms, footer "
                        "links) for targets with no safe contact (2f: and new targets never looked up)")
    p.add_argument("--shard", help="2f: i/n - only targets whose key hashes to i of n (parallel runs)")
    p.add_argument("--out", default=str(config.OUTPUT_DIR))
    p.set_defaults(func=cmd_contacts)

    p = sub.add_parser("pool", help="Phase 2c: collect companies from every source, score hiring likelihood, select ~400")
    p.add_argument("--rescore", action="store_true", help="score the stored pool again without fetching")
    p.add_argument("--reselect", action="store_true",
                   help="2c.1: re-score and re-select the stored pool under the current rules (large companies, "
                        "tech-fit floor), refilling dropped slots; queued / sent / locked targets are kept")
    p.add_argument("--date", help="score as of this day (YYYY-MM-DD, default today)")
    p.add_argument("--target-size", type=int, default=0,
                   help="2e: targets to select this run (default pool.target_size, 400)")
    p.add_argument("--out", default=str(config.OUTPUT_DIR))
    p.set_defaults(func=cmd_pool)

    p = sub.add_parser("verify-sources", help="confirm each published address on the company's site; demote if not")
    p.set_defaults(func=cmd_verify_sources)

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
    p.add_argument("--no-fresh", action="store_true",
                   help="2f: skip the pre-send freshness check (re-reading each address's source page)")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("mark-sent", help="record an email you sent")
    p.add_argument("target", nargs="?", help="target id, e.g. yc-clickpe")
    p.add_argument("--batch", help="mark a whole day's queue (YYYY-MM-DD) as sent")
    p.add_argument("--date", help="send date, YYYY-MM-DD (default today)")
    p.set_defaults(func=cmd_mark_sent)

    p = sub.add_parser("mark-bounced", help="record a bounce; the next address is suggested")
    p.add_argument("target")
    p.add_argument("--date", help="YYYY-MM-DD (default today)")
    p.add_argument("--stop", action="store_true", help="don't move on to the target's alternates or backups")
    p.set_defaults(func=cmd_mark_bounced)

    p = sub.add_parser("mark-replied", help="2g: record a reply (stops follow-ups; declined_warm comes back on its date)")
    p.add_argument("email", help="the address the email went to, or the replier's address on the same domain")
    p.add_argument("--outcome", required=True, choices=["interested", "declined_warm", "declined", "auto_ack"])
    p.add_argument("--recheck", help="YYYY-MM-DD: list the target again in the report's Recheck list (declined_warm)")
    p.add_argument("--contact", help='"Name <email>": the person who replied, stored as a verified named person')
    p.add_argument("--role", help="the replier's role, if known (e.g. HR, Founder)")
    p.add_argument("--date", help="YYYY-MM-DD the reply came (default today)")
    p.set_defaults(func=cmd_mark_replied)

    p = sub.add_parser("add-target", help="add a company of your own as an outreach target")
    p.add_argument("company")
    p.add_argument("--website", required=True, help="the company's domain, e.g. acme.com")
    p.add_argument("--note", help="why it's a target (shown in its packet)")
    p.add_argument("--email", help="address to use (skips the contact search)")
    p.add_argument("--name", dest="person", help="the person's name")
    p.add_argument("--role", help="the person's role, e.g. CTO")
    p.add_argument("--out", default=str(config.OUTPUT_DIR))
    p.set_defaults(func=cmd_add_target)

    p = sub.add_parser("hn-archive", help="2e: HN 'Who is hiring?' threads -> role targets with posting emails")
    p.add_argument("--from", dest="start", default="2026-05", help="first month, YYYY-MM (default 2026-05)")
    p.add_argument("--to", dest="end", default="2026-10", help="last month, YYYY-MM (default 2026-10)")
    p.set_defaults(func=cmd_hn_archive)

    p = sub.add_parser("yc-jobs", help="2e: YC company job pages -> role targets")
    p.add_argument("--limit", type=int, default=0, help="read at most N companies' pages")
    p.set_defaults(func=cmd_yc_jobs)

    p = sub.add_parser("import-targets", help="2e: companies you found yourself (Company | website | role | note)")
    p.add_argument("file", nargs="?", default=str(config.CONFIG_DIR / "private" / "manual_targets.txt"))
    p.add_argument("--out", default=str(config.OUTPUT_DIR))
    p.set_defaults(func=cmd_import_targets)

    p = sub.add_parser("role-check", help="2e: look for an open role that fits at targets without a role match")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--all", action="store_true", help="every ready target, not only pool / manual ones")
    p.add_argument("--checked-since", help="2f: only targets looked up on or after this day (YYYY-MM-DD)")
    p.set_defaults(func=cmd_role_check)

    p = sub.add_parser("safe-contacts", help="2e: unsent safe contacts by kind and the sending days they cover")
    p.add_argument("--guessed-per-day", type=int, default=7)
    p.set_defaults(func=cmd_safe_contacts)

    p = sub.add_parser("supply", help="2f: fintech lists, consultancies, GitHub lists, YC fintech -> the pool")
    p.add_argument("--all", action="store_true", help="every 2f source")
    p.add_argument("--fintech", action="store_true", help="fintech_lists in sources.yaml (RBI, Sahamati)")
    p.add_argument("--consultancies", action="store_true", help="the consultancy seed list in sources.yaml")
    p.add_argument("--saas", action="store_true", help="2g: the mid-size product / SaaS seed list in sources.yaml")
    p.add_argument("--fintech-seeds", dest="fintech_seeds", action="store_true",
                   help="2g: the fintech seed list in sources.yaml")
    p.add_argument("--lists", action="store_true", help="refresh the GitHub lists in sources.yaml")
    p.add_argument("--yc-fintech", dest="yc_fintech", action="store_true",
                   help="remote YC fintechs with a posting open to India")
    p.add_argument("--yc-fintech-india", dest="yc_fintech_india", action="store_true",
                   help="2g: YC fintechs headquartered abroad whose own site shows an office / engineering in India")
    p.add_argument("--date", help="as of this day (YYYY-MM-DD, default today)")
    p.add_argument("--target-size", type=int, default=0, help="targets to select this run (default pool.target_size)")
    p.set_defaults(func=cmd_supply)

    sub.add_parser("guess-evidence", help="2f: mark stored guesses that evidence backs (offline)"
                   ).set_defaults(func=cmd_guess_evidence)
    p = sub.add_parser("classify-published", help="2f: classify 'other' published addresses (hiring or not)")
    p.add_argument("--apply", action="store_true", help="record the unsendable ones in excluded_contacts")
    p.set_defaults(func=cmd_classify_published)
    p = sub.add_parser("referrals", help="2f: output/referrals.csv for large / apply-only companies with a fit role")
    p.add_argument("--out", default=str(config.OUTPUT_DIR))
    p.set_defaults(func=cmd_referrals)

    p = sub.add_parser("discover", help="find hiring startups and their job boards automatically")
    p.add_argument("--limit", type=int, default=40, help="companies to check this run (default 40)")
    p.set_defaults(func=cmd_discover)
    return parser


def main(argv: list[str] | None = None) -> int:
    # 2f: a company name the Windows console can't encode (cp1252) killed a long `contacts` run mid-way.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
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
