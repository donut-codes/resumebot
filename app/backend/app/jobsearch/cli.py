"""Load the owner's own data into the job-search database, and run a few one-off repairs. Run from the app folder
(the files never leave the machine and are never copied into the repo):

    python -m app.jobsearch.cli import-tracker <path to tracker.xlsx>
    python -m app.jobsearch.cli load-profile <path to profile.json> --by import --why "loaded from a profile file"
    python -m app.jobsearch.cli import-workspace <data folder> [--dry-run]
    python -m app.jobsearch.cli backfill-posted-dates [--dry-run]
    python -m app.jobsearch.cli import-employer-list FILE.csv [--dry-run]
    python -m app.jobsearch.cli read-board-text [--role ROLE_ID ...] [--n 30] [--dry-run]
    python -m app.jobsearch.cli reextract-posting-header [--dry-run]
    python -m app.jobsearch.cli send-back-ungraded [--dry-run]
    python -m app.jobsearch.cli clear-stale-go-skips [--dry-run]
    python -m app.jobsearch.cli save-packet-files [--dry-run]
    python -m app.jobsearch.cli fill-work-modes [--dry-run]
    python -m app.jobsearch.cli structure-postings [--n 50] [--dry-run]
    python -m app.jobsearch.cli recleanse
    python -m app.jobsearch.cli heal-embedded-copies
    python -m app.jobsearch.cli heal-counted-places
    python -m app.jobsearch.cli tag-live-roles
    python -m app.jobsearch.cli relearn-site example
    python -m app.jobsearch.cli relearn-browser-sites

The rest of the owner's work (`requeue`, `regate`, `recipe.record`, `recipe.retire`, `cost.report`,
`reanalysis.compare` and the others) is an action of the action door (`owner_actions/mcp.py`, `owner_actions/`: PRD path
50),
run in the running app's own process (its fetcher, its host stops, its one-run-at-a-time rules), with a dry run when
asked, and logged in Activity. This command line does not repeat it: a second process would have a fetcher and a copy of
the host stops of its own.

The tracker import only reads the spreadsheet, and is safe to run again: a role from an earlier import that the app has
not done anything with since is brought up to date with the file, and one it has (or one its own search found) stays as
the app has it (tracker.py). `-` in place of its path reads the spreadsheet from standard input, so it can be piped in
and never written to a disk. After it, employer records that are one employer are merged and no other pair is asked
about (`employer_pairs.settle`), and then the catch-up starts (catch_up.py).
`load-profile` reads a JSON file holding any of the profile's parts (`criteria`, `gates`, `scoring`, `settings`) and
saves each as a new version; a part that didn't change is left alone. `-` reads standard input. `import-workspace` reads
an earlier workspace folder (the master resume, form answers and writing rules: imports/workspace.py) and
saves what it finds the same way; --dry-run only says what it found. `reextract-posting-header`, `backfill-posted-dates`
and `fill-work-modes` are one-off repairs kept until their live dry runs count nothing (postings/text.py,
link_work_mode.py); --dry-run only counts. `send-back-ungraded` is the same for the roles graded on nothing
(postings/ungraded.py): the owner runs it once after the release, since a repair done once is a command and never a step
of every start (loops/startup.py); its --dry-run reads and changes nothing. Output is counts and versions, never the
contents.

`import-employer-list` keeps the employer names and websites of a CSV file the owner has (a heading row with a name
column and a website column, and a verdict column when the file has one: a row whose verdict says to drop is left out)
as a lookup for the scout (employers/employer_list.py, docs/PRD.md 28.3): when the scout is handed an employer whose job
board the app does not know, the hand-over carries the list's website for it as a first clue, marked as the owner's and
unchecked. It makes no employer and researches nothing. Loading a file of the same name again replaces its rows
(another file's rows stay); --dry-run reads the file and the database and says how many rows would be kept and how many
of the app's employers, and of those that wait for the scout, the list names, and saves nothing. The file is the
owner's and is never copied; the output is counts only.

`read-board-text` reads the whole description of a board's posting from its own page in the app's Chrome, one page
every 30 seconds on the app's one clock for that board's host (shared with the browser's queue), and keeps it as a job
board's copy (the board reader's capture module, docs/PRD.md 48.14): for the roles named with `--role`, else the `--n`
most recent roles that already hold the employer's own text. It starts only with the owner's robots-exempt read
setting on and only when the app's record does not say the board's host is stopped or resting; it ends at a check or a
refusal (which stops the host for the queue too) or at slow-downs (which rest it); --dry-run says which postings it
would read and makes no request. Each page it keeps settles and ranks its role again, and a text that becomes a role's
only text is the copy its gates read. The app's own night reader keeps its own clock and its "one reader at a time"
inside the app's process only: run it when the night's read is not running.
"""

import argparse
import asyncio
import io
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app import db
from app.jobsearch.gating import regate
from app.jobsearch.imports import bring_in, tracker
from app.jobsearch.imports import workspace as workspace_import
from app.jobsearch.missions import catch_up
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.store import ALL_KINDS as KINDS
from app.jobsearch.work import wake
from app.settings import settings
from app.sites import registry


def tracker_source(arg: str):
    """The tracker to read: the path given, or for `-` the spreadsheet's bytes on standard input (a workbook is a zip
    file and needs to be read back, so a pipe is read whole into memory first)."""
    return io.BytesIO(sys.stdin.buffer.read()) if arg == "-" else Path(arg)


async def _import_tracker(args) -> int:
    source = tracker_source(args.xlsx)
    await db.init_schema()
    async with db.session_factory()() as session:
        try:
            # The import, the employers settled and checked for duplicates, and the catch-up over what nobody
            # researched (imports/bring_in.py, the same steps the owner door's `tracker.import` runs). The import is
            # saved only if the catch-up can start, so running it again after a fix starts it.
            brought = await bring_in.tracker_file(session, settings.owner, source)
        except (tracker.TrackerError, catch_up.CatchUpError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        await session.commit()
    for note in brought.notes:
        print(f"note: {note}", file=sys.stderr)
    print("tracker import: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in brought.counts.items()))
    if brought.started is not None:
        print(brought.started.words)
        for key, reason in brought.started.knocks:  # the scout, once the work is saved
            await wake.knock(key, reason)
    return 0


async def _load_profile(args) -> int:
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    try:
        parts = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"error: not JSON ({exc.msg}, line {exc.lineno})", file=sys.stderr)
        return 1
    unknown = sorted(set(parts) - set(KINDS)) if isinstance(parts, dict) else ["(not an object)"]
    if unknown:
        print(f"error: the file holds {', '.join(unknown)}; the parts are {', '.join(KINDS)}", file=sys.stderr)
        return 1
    await db.init_schema()
    async with db.session_factory()() as session:
        try:
            saved = await profile_store.save_parts(session, settings.owner, parts, by=args.by, why=args.why)
        except profile_store.ProfileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        said = None
        if saved.get("gates"):  # a new Hard pass checks the whole log and every role again, as a save in the app does
            said = (await regate.run(
                session, settings.owner, cause=f"Hard pass version {saved['gates']} was loaded")).said
        await session.commit()
    for kind, version in saved.items():
        print(f"{kind}: " + (f"saved as version {version}" if version else "unchanged"))
    if said:
        print(said)
    return 0


async def _import_workspace(args) -> int:
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"error: no folder {folder}", file=sys.stderr)
        return 1
    docs, report = workspace_import.read_workspace(folder)
    for line in report:
        print(f"  {line}")
    if args.dry_run or not docs:
        print("dry run: nothing saved" if args.dry_run else "nothing to import")
        return 0
    await db.init_schema()
    async with db.session_factory()() as session:
        try:
            saved = {kind: await profile_store.save(session, settings.owner, kind, data, by=args.by, why=args.why)
                     for kind, data in docs.items()}
        except profile_store.ProfileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        await session.commit()
    for kind, version in saved.items():
        print(f"{kind}: " + (f"saved as version {version}" if version else "unchanged"))
    return 0


async def _structure_postings(args) -> int:
    """Ask for the shadow structuring of up to N roles' posting texts (posting_structure.py): a waiting parser job for
    each text version with no current record, taken by the pool whenever a session is free. Nothing reads the records.
    `--dry-run` counts and changes nothing."""
    from app.jobsearch.jobs import posting_structure

    async with db.session_factory()() as session:
        count = await posting_structure.queue(session, settings.owner, args.n, dry_run=args.dry_run)
        await session.commit()
    print(f"{count} posting texts {'would be' if args.dry_run else 'are'} waiting for a session")
    return 0


async def _recleanse(args) -> int:
    """Make the cleansed copy of every posting whose newest raw copy has none at this cleaner's version, from the raw
    copies alone: no fetch, no model; each reader's parse is run again on the raw bytes, and a row that cannot be (the
    browser's and scout's reports, a tracker row, a legacy text) starts from the parse stored with it
    (raw_copies.recleanse, reparse.py). Counts only; nothing reads the copies."""
    from app.jobsearch.postings import cleanse, raw_copies

    async with db.session_factory()() as session:
        counts = await raw_copies.recleanse(session, settings.owner)
        await session.commit()
    print(f"cleaner version {cleanse.CLEANER_VERSION}: {counts['rebuilt']} of {counts['postings']} postings rebuilt "
          f"from raw; {counts['reparsed']} raw rows parsed again from their bytes")
    if counts["stored"]:
        left = ", ".join(f"{n} {reader}" for reader, n in sorted(counts["stored"].items()))
        print(f"not parsed again, started from the stored parse: {left}")
    return 0


async def _heal_embedded_copies(args) -> int:
    """Make again, once, the current cleansed copies that still hold embedded data (a picture written into the text as
    a `data:` address), which the cleaner has dropped since 2026-11-10 without a new cleaner number: only the copies
    whose text comes out different, each from its raw copies with no fetch and no model, its role settled and ranked
    again (raw_copies.heal_embedded). Run it with the app stopped; a second run makes nothing. Counts only."""
    from app.jobsearch.postings import raw_copies

    async with db.session_factory()() as session:
        counts = await raw_copies.heal_embedded(session, settings.owner)
        await session.commit()
    print(f"{counts['looked_at']} current copies hold 'data:': {counts['rebuilt']} made again without its embedded "
          "data")
    return 0


async def _heal_counted_places(args) -> int:
    """Make again, once, the current copies that state only a count of places ("2 Locations") while the posting's kept
    raw rows name the towns (raw_copies.heal_counted_places): no fetch, no model, no raw row touched, each role settled
    again. Run it with the app stopped; a second run makes nothing. Counts only."""
    from app.jobsearch.postings import raw_copies

    async with db.session_factory()() as session:
        counts = await raw_copies.heal_counted_places(session, settings.owner)
        await session.commit()
    print(f"{counts['looked_at']} current copies state only a count of places: {counts['rebuilt']} made again with "
          "the towns the postings named")
    return 0


async def _tag_live_roles(args) -> int:
    """Give the live roles that have no answer the free title match, once (role_match.code_pass): no model, no job
    opened (the app's minute tick opens the Haiku job after the start). Run it with the app stopped; a second run
    matches none. Counts only."""
    from app.jobsearch.work import role_match

    async with db.session_factory()() as session:
        looked, matched = await role_match.code_pass(session, settings.owner)
        await session.commit()
    print(f"{looked} live roles had no answer: {matched} matched by title, {looked - matched} left for the Haiku job")
    return 0


async def _backfill_posted_dates(args) -> int:
    """Fill the posted day of roles that have none from their postings and stored texts
    (repairs.backfill_posted). A dry run changes nothing."""
    from app.jobsearch.postings import repairs

    if not args.dry_run:
        await db.init_schema()
    async with db.session_factory()() as session:
        result = await repairs.backfill_posted(session, settings.owner, dry_run=args.dry_run)
        if not args.dry_run:
            await session.commit()
    verb = "would give" if args.dry_run else "gave"
    print(f"{result.undated} roles have no posted day: {verb} {result.filled} of them one "
          f"(and {result.postings} of their postings), {result.undated - result.filled} stay dated by when they "
          "were found")
    return 0


async def _import_employer_list(args) -> int:
    """Keep the names and websites of the owner's employer list as the scout's lookup (employer_list.replace), and say
    how the list meets the app's employers (employer_list.matches). A dry run saves nothing. Counts only: no row is
    printed."""
    from app.jobsearch.employers import employer_list

    path = Path(args.csv)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: the file could not be read ({getattr(exc, 'strerror', None) or type(exc).__name__})",
              file=sys.stderr)
        return 1
    if not args.dry_run:
        await db.init_schema()
    async with db.session_factory()() as session:
        try:
            brought = await bring_in.employer_list_file(session, settings.owner, path.name, text,
                                                        dry_run=args.dry_run)
        except employer_list.ListError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not args.dry_run:
            await session.commit()
    read, found = brought.read, brought.found
    verb = "would keep" if args.dry_run else "kept"
    print(f"employer list: {read.rows} rows read, {verb} {len(read.entries)} ({read.dropped} marked drop, "
          f"{read.unusable} with no usable name or website, {read.repeats} repeats); {found.employers} of the app's "
          f"{found.of} employers are in it ({found.waiting} of the {brought.waiting} that wait for the scout to find a "
          f"board), and {found.rows} of the kept rows name one of them" + ("; nothing saved (dry run)" if args.dry_run
                                                                          else ""))
    return 0


async def _read_board_text(args) -> int:
    """Read the whole description of a board's postings from their own pages in the app's Chrome and keep each as a job
    board's copy (capture.run). With `--role` the roles named, else the `--n` most recent roles that already hold the
    employer's own text, and every role that needs the board's copy because its employer's text is out of reach
    (the board reader's copy module: it is given the copy when its page is read). A role's own link on that board
    (the tracker's import) is read as well as the nightly search's postings. Only with the owner's robots-exempt
    setting on; a dry run reads the database alone."""
    capture, feed = registry.board_reader("capture"), registry.board_reader("guest")
    if capture is None or feed is None:
        print("error: this install has no reader of a job board's pages", file=sys.stderr)
        return 1
    try:
        named = [uuid.UUID(role) for role in args.role]
    except ValueError:
        print("error: --role takes a role's id", file=sys.stderr)
        return 1
    async with db.session_factory()() as session:
        planned = await capture.plan(session, settings.owner, named, args.n)
        on = await feed.is_on(session, settings.owner)
        resting = await capture.resting(session, settings.owner, datetime.now(UTC))
    holding = sum(1 for target in planned.targets if target.employers_own)
    needing = sum(1 for target in planned.targets if target.needs_copy)
    needs = f"; {needing} of them roles that need the board's copy of the posting" if needing else ""
    print(f"{len(planned.targets)} board postings to read, one for each of {len(planned.targets)} roles "
          f"({holding} of them roles that hold the employer's own text{needs}); {planned.already} roles have all "
          f"their board postings read already, {planned.no_posting} named roles have none")
    if args.dry_run:
        notes = ([] if on else ["the board read is off"]) + ([resting] if resting else [])
        print("dry run: nothing read" + "".join(f"; {note}, so a read would not start" for note in notes))
        return 0
    if not on:
        print("error: the board read is off: resumebot doesn't read it unless it is switched on by hand",
              file=sys.stderr)
        return 1
    result = await capture.run(settings.owner, planned.targets)
    kept = result.outcomes
    print(f"read {result.read} pages in {result.requests} requests ({result.backoffs} back-offs): "
          f"{kept[capture.STORED]} kept as the posting's text, {kept[capture.RAW_ONLY]} kept in the raw copy only, "
          f"{kept[capture.GONE]} gone, {kept[capture.NO_TEXT]} with no whole text"
          + (f"; {result.copies} roles given the board's copy as their text" if result.copies else ""))
    signed_out = "a page asked for a sign-in: the app's Chrome is not signed in to the board"
    why = result.stopped or result.unreachable or (signed_out if result.signed_out else "")
    if why:
        print(f"ended early: {why}", file=sys.stderr)
    return 1 if why else 0


async def _reextract_posting_header(args) -> int:
    """Read again the roles whose text was read as a page, to keep the posting's header (repairs.
    reextract_posting_header). A dry run reads the pages, politely, and changes nothing."""
    from app.jobsearch.postings import repairs

    if not args.dry_run:
        await db.init_schema()
    async with db.session_factory()() as session:
        result = await repairs.reextract_posting_header(session, settings.owner, dry_run=args.dry_run)
        if not args.dry_run:
            await session.commit()
    verb = "would get" if args.dry_run else "got"
    print(f"{result.candidates} roles hold a text read from a page: {verb} only the posting's header in front "
          f"(no research again, the code-decided parts re-run) {result.header_only}, {verb} a changed description "
          f"and research again {result.reanalysed}, keep their text {result.kept}")
    return 0


async def _send_back_ungraded(args) -> int:
    """Send back the roles whose research graded nothing of a text that is not their posting (ungraded.
    send_back_ungraded). A dry run only counts them."""
    from app.jobsearch.postings import ungraded

    if not args.dry_run:
        await db.init_schema()
    async with db.session_factory()() as session:
        result = await ungraded.send_back_ungraded(session, settings.owner, dry_run=args.dry_run)
        if not args.dry_run:
            await session.commit()
    verb = "would send" if args.dry_run else "sent"
    where = ", ".join(f"{n} in {stage}" for stage, n in sorted(result.by_stage.items())) or "none"
    print(f"{verb} {result.roles} roles back for the posting's text: {where}")
    return 0


async def _save_packet_files(args) -> int:
    """Save again, once, the files of every ready packet whose last save to the packet folder failed (packets.
    save_failed_files). A dry run only counts them."""
    from app.jobsearch.writing import packet_files, packets

    if not packet_files.enabled():
        print("no packet folder set")
        return 0
    async with db.session_factory()() as session:
        tried, again = await packets.save_failed_files(session, settings.owner, dry_run=args.dry_run)
    print(f"would save {tried} packets" if args.dry_run else f"saved {tried} packets ({again} failed again)")
    return 0


async def _clear_stale_go_skips(args) -> int:
    """Clear the Launch skip reasons that are no longer true (packets.stale_go_skips): (a) on a role not in Ready
    to Launch, (b) a packet-failure reason on a role whose packet is ready. A dry run only counts them."""
    from app.jobsearch.writing import packets

    if not args.dry_run:
        await db.init_schema()
    async with db.session_factory()() as session:
        left, fixed = await packets.stale_go_skips(session, settings.owner)
        if not args.dry_run:
            for role in [*left, *fixed]:
                role.go_skip = None
            await session.commit()
    verb = "would clear" if args.dry_run else "cleared"
    print(f"{verb} {len(left) + len(fixed)} roles (a: {len(left)}, b: {len(fixed)})")
    return 0


async def _fill_work_modes(args) -> int:
    """Read again the roles that hold a job system link and no work mode, and fill it in (link_work_mode.py). A dry run
    only counts them."""
    from app.jobsearch.gating import link_work_mode

    if not args.dry_run:
        await db.init_schema()
    async with db.session_factory()() as session:
        result = await link_work_mode.run(session, settings.owner, dry_run=args.dry_run)
        if not args.dry_run:
            await session.commit()
    if args.dry_run:
        print(f"would read {result.roles} roles that hold a job system link and no work mode")
    else:
        print(f"read {result.roles} roles: {result.filled} got a work mode, {result.regated} changed at the gates")
    return 0


async def _relearn(args) -> int:
    """Put a site read in the browser back in the learn queue (browser_reader.relearn)."""
    from app.jobsearch.browser import reader as browser_reader

    await db.init_schema()
    async with db.session_factory()() as session:
        try:
            done = await browser_reader.relearn(session, settings.owner, args.site)
        except browser_reader.BrowserError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        await session.commit()
    for name, what in done:
        print(f"{name}: {what}")
    if not done:
        print(f"no site of yours matches {args.site!r}")
    return 0 if done else 1


async def _relearn_browser_sites(args) -> int:
    """Put every site read in the browser back in the Pathfinder's queue (browser_reader.relearn_all)."""
    from app.jobsearch.browser import reader as browser_reader

    await db.init_schema()
    async with db.session_factory()() as session:
        done = await browser_reader.relearn_all(session, settings.owner)
        await session.commit()
    for name, what in done:
        print(f"{name}: {what}")
    if not done:
        print("no site of yours is read in your browser")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobsearch", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("import-tracker", help="read the tracker spreadsheet into the roles table")
    t.add_argument("xlsx", help="the spreadsheet, or - for standard input")
    p = sub.add_parser("load-profile", help="save profile parts from a JSON file as new versions")
    p.add_argument("file", help="the JSON file, or - for standard input")
    p.add_argument("--by", default="import", help="who is saving it (shown in the history)")
    p.add_argument("--why", default="", help="why (shown in the history)")
    w = sub.add_parser("import-workspace", help="read a workspace folder's resume, form answers and writing rules")
    w.add_argument("folder")
    w.add_argument("--dry-run", action="store_true", help="say what was found, save nothing")
    w.add_argument("--by", default="import")
    w.add_argument("--why", default="imported from an earlier workspace folder")
    fw = sub.add_parser("fill-work-modes", help="read again the roles added by a job system's link that have no work "
                                                "mode, fill it in and check the gates again")
    fw.add_argument("--dry-run", action="store_true", help="count the roles, change nothing")
    bp = sub.add_parser("backfill-posted-dates", help="give a posted day to the roles with none, from the posted date "
                                                      "their postings hold or their stored text says")
    bp.add_argument("--dry-run", action="store_true", help="count the roles, read and change nothing")
    el = sub.add_parser("import-employer-list", help="keep the employer names and websites of a CSV file as the "
                                                     "scout's first clue to an employer's site (makes no employer)")
    el.add_argument("csv", help="the owner's file: a name column, a website column, and a verdict column if it has one")
    el.add_argument("--dry-run", action="store_true", help="count what would be kept and matched, save nothing")
    rh = sub.add_parser("reextract-posting-header", help="read again the roles whose text was read as a page, so it "
                                                         "starts at the posting's title and keeps its header lines")
    rh.add_argument("--dry-run", action="store_true", help="read the pages, count what would happen, change nothing")
    rt = sub.add_parser("read-board-text", help="read a board's postings' whole descriptions from their own pages in "
                                                 "the app's Chrome, one page every 30 seconds on the app's clock for "
                                                 "that board, and keep them")
    rt.add_argument("--role", action="append", default=[], help="a role's id (repeat for more); else the most recent "
                                                                "roles that hold the employer's own text")
    rt.add_argument("--n", type=int, default=30, help="how many roles when none is named (default 30)")
    rt.add_argument("--dry-run", action="store_true", help="say which postings would be read, make no request")
    sb = sub.add_parser("send-back-ungraded", help="send back for the posting's text the roles whose research graded "
                                                   "nothing of a text that is not their posting")
    sb.add_argument("--dry-run", action="store_true", help="count the roles, change nothing")
    cs = sub.add_parser("clear-stale-go-skips", help="clear the Launch skip reasons that stopped being true (a role "
                                                     "that left Ready to Launch; a packet that is ready now)")
    cs.add_argument("--dry-run", action="store_true", help="count the roles, change nothing")
    sf = sub.add_parser("save-packet-files", help="save again the files of every ready packet whose last save to the "
                                                  "packet folder failed")
    sf.add_argument("--dry-run", action="store_true", help="count the packets, write nothing")
    sp = sub.add_parser("structure-postings", help="ask for the shadow structuring of posting texts, one waiting "
                                                   "parser job each (nothing reads the records)")
    sp.add_argument("--n", type=int, default=50, help="how many posting texts (default 50)")
    sp.add_argument("--dry-run", action="store_true", help="count them, change nothing")
    sub.add_parser("recleanse", help="rebuild cleansed copies from the raw copies at the cleaner's current "
                                     "version, with no fetch")
    sub.add_parser("heal-embedded-copies", help="make again, once, the current cleansed copies that still hold "
                                                "embedded data (run with the app stopped)")
    sub.add_parser("heal-counted-places", help="make again, once, the current copies that state only a count of "
                                               "places while the raw rows name the towns (run with the app stopped)")
    sub.add_parser("tag-live-roles", help="give the live roles that have no answer the free title match to a target "
                                          "role, once (no model; run with the app stopped)")
    rl = sub.add_parser("relearn-site", help="put a site read in your browser back in the learn queue")
    rl.add_argument("site", help="part of the site's address or name, such as example")
    sub.add_parser("relearn-browser-sites",
                   help="put every site read in your browser back in the Pathfinder's queue, so each gets a runbook")
    args = parser.parse_args(argv)
    run = {"relearn-site": _relearn, "relearn-browser-sites": _relearn_browser_sites,
           "import-tracker": _import_tracker, "load-profile": _load_profile,
           "import-workspace": _import_workspace, "structure-postings": _structure_postings,
           "recleanse": _recleanse, "heal-embedded-copies": _heal_embedded_copies,
           "heal-counted-places": _heal_counted_places, "tag-live-roles": _tag_live_roles,
           "fill-work-modes": _fill_work_modes,
           "backfill-posted-dates": _backfill_posted_dates, "import-employer-list": _import_employer_list,
           "reextract-posting-header": _reextract_posting_header,
           "send-back-ungraded": _send_back_ungraded,
           "clear-stale-go-skips": _clear_stale_go_skips, "save-packet-files": _save_packet_files,
           "read-board-text": _read_board_text}
    return asyncio.run(run[args.cmd](args))


if __name__ == "__main__":
    raise SystemExit(main())
