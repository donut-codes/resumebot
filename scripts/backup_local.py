#!/usr/bin/env python3
"""Back up the local database into the data folder's backups/ folder, a copy of it in a second place when the user
has set one, and the last 14 backups kept in each place. Stdlib only; it shares its helpers with lib/localdata.py.

    SQLite (JOBSEARCH_DATABASE_URL is sqlite+aiosqlite:///...): a copy made by SQLite's own online backup, which is
        consistent while the app is writing (WAL), named resumebot-<date>-<time>.sqlite3.
    PostgreSQL (a postgresql:// address): a `pg_dump -Fc`, named resumebot-<date>-<time>.dump.

    app/backend/.venv/Scripts/python.exe scripts/backup_local.py       one backup now (the release runs it once,
                                                                       before the update)

Run it from the code folder with the app environment's own Python, as above, never `py`: on Windows that is the system
Python, whose SQLite can be one with the WAL-reset bug, and the SQLite copy is made by the Python that runs this (the
safe versions: `sqlite_is_safe` in app/backend/app/setup_cli.py).

The database is the one the data folder's .env names (JOBSEARCH_DATABASE_URL: read here, never printed, and given to
pg_dump through its environment, not its command line). The second place is a folder of the user's own choosing, for a
copy that outlives this disk (another disk, or a folder another program copies off this machine). It is the .env's
RESUMEBOT_BACKUP_DIR, a full path, and there is no default:

    RESUMEBOT_BACKUP_DIR=D:\\Backups\\resumebot

With none set the backup is made, checked and kept in backups/ only, and one line says how to set one.

Every backup is checked before it is kept (a dump: pg_restore reads its table of contents; a SQLite copy: quick_check
and foreign_key_check), is written under a temporary name and moved into place whole, and is readable by this user
only. A second place that is set but is not a full path, is missing or cannot be written is said plainly and is not a
crash: the backup stays in backups/ and the exit status is 1. Only files named resumebot-<date>-<time>.dump or
resumebot-<date>-<time>.sqlite3 are ever deleted, and each kind prunes only its own: an old .dump (the rollback to
PostgreSQL) is never touched by a SQLite backup.

To restore a SQLite backup: stop the app, then
    app/backend/.venv/Scripts/python.exe scripts/restore_local.py <the .sqlite3 backup>
It sets the live database file aside (kept, never deleted) and copies the backup in.

To restore a dump: stop the app, then
    psql ... -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    pg_restore --no-owner --no-acl --single-transaction --dbname=<database> <the dump>

Exit status: 0 backed up (and copied to the second place when one is set), 1 backed up but the copy to the second
place that is set failed, 2 no backup was made.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import localdata as base  # noqa: E402  (in lib/, beside skillmodel)

BACKUP_KEY = "RESUMEBOT_BACKUP_DIR"
KEEP = 14
# A backup's name carries the moment it was made in UTC, marked by the Z ("resumebot-20261108-153000Z.dump"). Backups
# made before that were named by the machine's local clock, with no Z; both are still found and pruned.
DUMP_NAME = re.compile(r"resumebot-\d{8}-\d{6}Z?\.dump")
SQLITE_NAME = re.compile(r"resumebot-\d{8}-\d{6}Z?\.sqlite3")
_STAMP = re.compile(r"resumebot-(\d{8}-\d{6})(Z?)\.")


def stamp_name(kind: str, now: datetime | None = None) -> str:
    """The name of a backup of `kind` ("dump" or "sqlite3") made at `now` (default: now), stamped in UTC."""
    return f"resumebot-{(now or datetime.now(UTC)).astimezone(UTC):%Y%m%d-%H%M%S}Z.{kind}"


def local_instant(naive: datetime) -> datetime:
    """The instant an old, local-clock name means: the naive time read on this machine's clock."""
    return naive.astimezone()


def made_at(name: str) -> datetime:
    """The instant a backup's name says it was made: UTC for a name with the Z, the machine's local clock for an older
    one. Names that carry no stamp sort first."""
    found = _STAMP.match(name)
    if not found:
        return datetime.min.replace(tzinfo=UTC)
    naive = datetime.strptime(found.group(1), "%Y%m%d-%H%M%S")
    return naive.replace(tzinfo=UTC) if found.group(2) else local_instant(naive)


def kind_of(name: str) -> re.Pattern[str]:
    """The name pattern of the kind of backup `name` is: pruning only ever looks at its own kind."""
    return SQLITE_NAME if name.endswith(".sqlite3") else DUMP_NAME


def second_place(setting: str) -> Path:
    """The folder the setting names. It must be a full path: any other is refused, never guessed at."""
    given = Path(setting)
    if not given.is_absolute():
        raise base.Stop(f"{BACKUP_KEY} must be the full path of a folder (D:\\Backups\\resumebot, say)")
    return given


def listed(folder: Path, pattern: re.Pattern[str] = DUMP_NAME) -> list[Path]:
    return sorted((p for p in folder.iterdir() if pattern.fullmatch(p.name)), key=lambda p: (made_at(p.name), p.name))


def prune(folder: Path, keep: int, pattern: re.Pattern[str] = DUMP_NAME) -> int:
    """Delete all but the newest `keep` backups of one kind in `folder` (oldest first by the moment each name says). Returns how many are left."""
    dumps = listed(folder, pattern)
    for old in dumps[:-keep] if keep else dumps:
        old.unlink()
    return min(len(dumps), keep)


SIZE_SETTLE_SECONDS = 30.0


def size_settles(path: Path, want: int, wait: float = SIZE_SETTLE_SECONDS, sleep: Callable[[float], None] | None = None) -> bool:
    """True when `path` reports `want` bytes, polling for up to `wait` seconds: a cloud program's virtual drive can report
    a new file's size late, so a single look right after the copy can see a stale one."""
    waited = 0.0
    while True:
        if path.stat().st_size == want:
            return True
        if waited >= wait:
            return False
        (sleep or time.sleep)(0.5)
        waited += 0.5


def copy_to(folder: Path, dump: Path, keep: int) -> int:
    """A copy of `dump` in `folder` (made under a temporary name, moved into place when whole), and the folder pruned.
    Raises Stop with a plain reason when the folder is missing or cannot be written. Returns how many dumps it holds."""
    if not folder.is_dir():
        if not folder.parent.is_dir():
            raise base.Stop(f"the folder {folder} is missing, and so is the folder it should be in")
        try:
            folder.mkdir()
        except OSError as error:
            raise base.Stop(f"could not make the folder {folder}: {error.strerror or error}") from None
    part = folder / f"{dump.name}.part"
    try:
        shutil.copyfile(dump, part)
        if not size_settles(part, dump.stat().st_size):
            raise base.Stop(f"the copy in {folder} came out a different size")
        os.replace(part, folder / dump.name)
        return prune(folder, keep, kind_of(dump.name))
    except OSError as error:
        raise base.Stop(f"could not write to {folder}: {error.strerror or error}") from None
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


def copy_sqlite(source: Path, part: Path) -> None:
    """Copy the database file `source` to `part` with SQLite's online backup (consistent while the app writes, WAL
    included), leaving `part` a single file in the rollback-journal mode, so no -wal or -shm lies beside it."""
    if not source.is_file():
        raise base.Stop(f"the database file {source} does not exist")
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        dest = sqlite3.connect(part)
        try:
            src.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")
        finally:
            dest.close()
    finally:
        src.close()


def backup_sqlite(source: Path, backups: Path, keep: int, log: Callable[[str], None]) -> Path:
    """One SQLite backup into `backups`; returns the file. Raises Stop when it cannot be made or fails its checks."""
    name = stamp_name("sqlite3")
    part = backups / f"{name}.part"
    started = time.monotonic()
    try:
        try:
            copy_sqlite(source, part)
        except sqlite3.Error as error:
            raise base.Stop(f"the SQLite backup of {source.name} failed: {error}") from None
        problem = base.check_sqlite_file(part)
        if problem:
            raise base.Stop(f"the copy of {source.name} is not sound ({problem}), so it was not kept")
        base.restrict_to_user(part)
        os.replace(part, backups / name)
    finally:
        part.unlink(missing_ok=True)
    backup = backups / name
    kept = prune(backups, keep, SQLITE_NAME)
    log(f"backup: {name}, {base.megabytes(backup.stat().st_size)}, copied from {source.name} in {time.monotonic() - started:.1f} s")
    log(f"  local: {backups} holds {kept} SQLite backups (the last {keep} are kept)")
    return backup


def backup_postgres(data_dir: Path, pg_bin: str | None, backups: Path, keep: int, log: Callable[[str], None]) -> Path:
    """One pg_dump into `backups`; returns the file."""
    target = base.load_target(data_dir)
    pg_dump = base.find_pg_tool("pg_dump", pg_bin, data_dir)
    pg_restore = base.find_pg_tool("pg_restore", pg_bin, data_dir)
    if not pg_dump or not pg_restore:
        raise base.Stop("pg_dump and pg_restore were not found (put PostgreSQL's bin folder on the PATH, or pass --pg-bin)")
    name = stamp_name("dump")
    part = backups / f"{name}.part"
    started = time.monotonic()
    try:
        done = base.run([pg_dump, "-Fc", "--no-owner", "--no-acl", f"--file={part}"], env=target.env)
        if done.returncode != 0:
            raise base.Stop(f"pg_dump failed on {target.label}: {base.first_line(done.stderr)}")
        base.restrict_to_user(part)
        listing = base.run([pg_restore, "--list", str(part)])
        if listing.returncode != 0 or not listing.stdout.strip():
            raise base.Stop(f"the dump is not a readable archive: {base.first_line(listing.stderr)}")
        os.replace(part, backups / name)
    finally:
        part.unlink(missing_ok=True)
    dump = backups / name
    kept = prune(backups, keep)
    log(f"backup: {name}, {base.megabytes(dump.stat().st_size)}, dumped from {target.label} in {time.monotonic() - started:.1f} s")
    log(f"  local: {backups} holds {kept} (the last {keep} are kept)")
    return dump


def backup_once(data_dir: Path, pg_bin: str | None, keep: int, log: Callable[[str], None]) -> int:
    """One backup. Returns the exit status: 0, 1 (the second place is set and the copy failed) or 2 (no backup); a plain Stop for what ends it."""
    url = base.database_url(data_dir)
    sqlite_source = base.sqlite_file(url, data_dir) if base.is_sqlite_url(url) else None
    if sqlite_source is None:
        base.load_target(data_dir)  # the plain Stop for a missing address, a remote host or a system database
    backups = data_dir / "backups"
    base.private_dir(backups)
    for stale in backups.glob("*.part"):  # left by a run that was killed
        if time.time() - stale.stat().st_mtime > 3600:
            stale.unlink(missing_ok=True)
    if sqlite_source is not None:
        dump = backup_sqlite(sqlite_source, backups, keep, log)
    else:
        dump = backup_postgres(data_dir, pg_bin, backups, keep, log)

    env_file = data_dir / ".env"
    setting = base.parse_env(env_file.read_bytes().decode("utf-8-sig")).get(BACKUP_KEY, "")
    if not setting:
        log(f"  second copy: none is set up (to keep one in a second place, add {BACKUP_KEY}=<the full path of a folder> to {env_file})")
        log("RESULT: ok")
        return 0
    try:
        folder = second_place(setting)
        held = copy_to(folder, dump, keep)
    except base.Stop as problem:
        log(f"  second copy: NOT COPIED to {Path(setting)}: {problem}")
        log("RESULT: the backup is in the local backups folder only; the second copy was not made")
        return 1
    log(f"  second copy: copied to {folder}, which holds {held}")
    log("RESULT: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backup_local.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", type=int, default=KEEP, help=f"dumps kept in each place (default {KEEP})")
    parser.add_argument("--data-dir", help="the data folder (default: RESUMEBOT_DATA_DIR, else the user's own data folder, the one `resumebot init` makes)")
    parser.add_argument("--pg-bin", help="PostgreSQL's bin folder (pg_dump and pg_restore)")
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be 1 or more")
    data_dir = Path(args.data_dir) if args.data_dir else base.default_data_dir()
    try:
        return backup_once(data_dir, args.pg_bin, args.keep, base.say)
    except base.Stop as stop:
        print(f"Stopped: {stop}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
