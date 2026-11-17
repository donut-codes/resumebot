#!/usr/bin/env python3
"""Restore the local SQLite database from a backup made by backup_local.py. Stdlib only.

    app/backend/.venv/Scripts/python.exe scripts/restore_local.py <backup file> [--data-dir DIR]

Run it from the code folder with the app environment's own Python, as above, never `py`: on Windows that is the system
Python, whose SQLite can be one with the WAL-reset bug (the safe versions: `sqlite_is_safe` in
app/backend/app/setup_cli.py).

It refuses while the app answers (stop it first: `stack.py stop`), checks the backup (quick_check and
foreign_key_check), sets the live database file and its -wal and -shm files aside beside it (named
<file>.before-restore-<date>-<time>*, kept, never deleted), copies the backup in, and prints the next steps. It never
touches PostgreSQL, and it only works on a data folder whose .env names a sqlite+aiosqlite database.

Exit status: 0 restored, 2 nothing was changed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import localdata as base  # noqa: E402

SIDECARS = ("-wal", "-shm")


def restore_once(data_dir: Path, backup: Path, log: Callable[[str], None], app_answers: Callable[[str], bool] = base.app_answers) -> int:
    url = base.database_url(data_dir)
    if not base.is_sqlite_url(url):
        raise base.Stop("the database in .env is not a SQLite database: this script restores SQLite only")
    live = base.sqlite_file(url, data_dir)
    address = base.app_url(data_dir)
    if app_answers(address):
        raise base.Stop(f"the app answers at {address}: stop it first (stack.py stop), then restore")
    if not backup.is_file():
        raise base.Stop(f"{backup} is not a file")
    if backup.resolve() == live.resolve():
        raise base.Stop("the backup is the live database file itself")
    problem = base.check_sqlite_file(backup)
    if problem:
        raise base.Stop(f"the backup is not sound ({problem}), so nothing was changed")
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    live.parent.mkdir(parents=True, exist_ok=True)
    part = live.with_name(live.name + ".restore-part")
    try:
        shutil.copyfile(backup, part)  # copied first: a failure here leaves the live file where it was
        for suffix in ("", *SIDECARS):
            current = Path(str(live) + suffix)
            if current.exists():
                aside = Path(f"{live}.before-restore-{stamp}{suffix}")
                os.replace(current, aside)
                log(f"set aside: {aside.name}")
        os.replace(part, live)
    finally:
        part.unlink(missing_ok=True)
    base.restrict_to_user(live)
    log(f"restored: {live.name} from {backup.name} ({base.megabytes(live.stat().st_size)})")
    log("next: from the code folder, with the app environment's own Python:  app/backend/.venv/Scripts/python.exe "
        f'app/scripts/local/stack.py --data "{data_dir}" start  and then the same line ending in smoke')
    log("the file that was set aside is kept; delete it yourself once the restored app looks right")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="restore_local.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("backup", help="a resumebot-<date>-<time>.sqlite3 file made by backup_local.py")
    parser.add_argument("--data-dir", help="the data folder (default: RESUMEBOT_DATA_DIR, else the user's own data folder, the one `resumebot init` makes)")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else base.default_data_dir()
    try:
        return restore_once(data_dir, Path(args.backup), base.say)
    except base.Stop as stop:
        print(f"Stopped: {stop}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
