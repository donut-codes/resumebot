"""`resumebot copy-to-sqlite --from <postgres-url> --to <file>`: copy the old Postgres into a new SQLite file."""

import argparse
import asyncio
import sys
from pathlib import Path

from app.migrate import copier
from app.migrate.plan import CopyError

EXIT_NOT_VERIFIED = 1


def main(argv: list[str]) -> int:
    """Copy the old Postgres database into a new SQLite file and check the copy (never writes to the old one)."""
    parser = argparse.ArgumentParser(prog="resumebot copy-to-sqlite", description=main.__doc__)
    parser.add_argument("--from", dest="source", required=True, metavar="POSTGRES_URL",
                        help="the old database, as postgresql://user:password@host:port/name")
    parser.add_argument("--to", dest="target", required=True, metavar="FILE",
                        help="the new SQLite file: it must not exist yet")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0) if isinstance(stop.code, int) else 2
    try:
        report = asyncio.run(copier.copy(args.source, Path(args.target), say=lambda text: print(text, flush=True)))
    except CopyError as exc:
        print(f"resumebot copy-to-sqlite: {exc}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    for one in report.verified.tables:
        left = f", {one.dropped} left out" if one.dropped else ""
        print(f"  {one.table:<28} {one.source:>10} rows -> {one.target:>10}  ({one.compared} compared{left})")
    print(f"noise copies left behind: {len(report.left.dropped)} raw copies and "
          f"{report.left.cleansed_dropped} cleansed copies; {len(report.left.held)} newest copies kept so postings "
          "read the same")
    print(f"postings pointed at their current copy: {report.repointed}; roles whose stored order was written: "
          f"{report.settled}; {report.seconds:.0f} seconds")
    if not report.ok:
        for problem in report.verified.problems:
            print(f"  NOT VERIFIED: {problem}", file=sys.stderr)
        print(f"the copy at {args.target} did not check out: do not use it", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    print(f"copied and checked: {args.target}")
    return 0
