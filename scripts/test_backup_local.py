"""Tests for backup_local.py: stdlib unittest, run by CI (python -m unittest discover -s scripts)."""

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_local as b  # noqa: E402

REAL = Path.stat


def slow_size_stat(part_suffix: str, stale_reads: int):
    """Path.stat that reports a stale size for the .part file for its first `stale_reads` calls (a lagging virtual drive)."""
    reads = {"n": 0}

    def stat(self, *args, **kwargs):
        result = REAL(self, *args, **kwargs)
        if self.name.endswith(part_suffix):
            reads["n"] += 1
            if reads["n"] <= stale_reads:
                return mock.Mock(st_size=0, st_mode=result.st_mode)
        return result

    return stat


class DriveCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dump = root / "resumebot-20261021-120000.dump"
        self.dump.write_bytes(b"x" * 1000)
        self.folder = root / "drive"
        self.folder.mkdir()

    def test_a_size_that_is_reported_late_still_counts_as_a_whole_copy(self):
        with mock.patch.object(Path, "stat", slow_size_stat(".part", 3)), mock.patch.object(b.time, "sleep"):
            held = b.copy_to(self.folder, self.dump, 14)
        self.assertEqual(held, 1)
        self.assertEqual((self.folder / self.dump.name).read_bytes(), self.dump.read_bytes())

    def test_a_copy_that_really_is_short_is_still_refused(self):
        with mock.patch.object(Path, "stat", slow_size_stat(".part", 10**6)), mock.patch.object(b.time, "sleep"):
            with self.assertRaises(b.base.Stop):
                b.copy_to(self.folder, self.dump, 14)
        self.assertEqual(list(self.folder.iterdir()), [])


def make_sqlite_data_dir(root: Path, rows: int = 5, extra_env: str = "") -> tuple[Path, Path]:
    """A data folder whose .env names a WAL-mode SQLite file; returns (data folder, database file)."""
    data = root / "data"
    data.mkdir()
    db = data / "app.db"
    (data / ".env").write_text(f"JOBSEARCH_DATABASE_URL=sqlite+aiosqlite:///{db.as_posix()}\n{extra_env}", encoding="utf-8")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))")
    conn.executemany("INSERT INTO parent (id) VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()
    return data, db


class SqliteBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log: list[str] = []

    def test_a_sqlite_url_is_backed_up_without_pg_dump_even_while_a_writer_holds_the_database_open(self):
        data, db = make_sqlite_data_dir(self.root)
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute("INSERT INTO parent (id) VALUES (100)")
        writer.commit()  # lives in the -wal file only: a plain file copy would miss it
        with mock.patch.object(b.base, "find_pg_tool", side_effect=AssertionError("pg_dump must not be looked for")):
            status = b.backup_once(data, None, 14, self.log.append)
        self.assertEqual(status, 0)  # no RESUMEBOT_BACKUP_DIR: a good backup, with no second copy to make
        made = b.listed(data / "backups", b.SQLITE_NAME)
        self.assertEqual(len(made), 1)
        copy = sqlite3.connect(made[0])
        try:
            self.assertEqual(copy.execute("SELECT count(*) FROM parent").fetchone()[0], 6)
            self.assertEqual(copy.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            copy.close()
        self.assertEqual([p.name for p in (data / "backups").iterdir()], [made[0].name])  # no .part, -wal or -shm left

    def test_a_sqlite_backup_is_copied_to_the_drive_folder_and_exits_zero(self):
        drive = self.root / "drive"
        drive.mkdir()
        data, _ = make_sqlite_data_dir(self.root, extra_env=f"RESUMEBOT_BACKUP_DIR={drive.as_posix()}\n")
        self.assertEqual(b.backup_once(data, None, 14, self.log.append), 0)
        self.assertEqual([p.suffix for p in drive.iterdir()], [".sqlite3"])

    def test_pruning_a_sqlite_backup_never_touches_old_postgres_dumps(self):
        data, _ = make_sqlite_data_dir(self.root)
        backups = data / "backups"
        backups.mkdir()
        old = [backups / f"resumebot-2026102{i}-120000.dump" for i in range(5)]
        older_sqlite = [backups / f"resumebot-2026102{i}-120000.sqlite3" for i in range(3)]
        for path in old + older_sqlite:
            path.write_bytes(b"x")
        b.backup_once(data, None, 1, self.log.append)
        self.assertTrue(all(p.exists() for p in old))
        self.assertEqual(len(b.listed(backups, b.SQLITE_NAME)), 1)

    def test_pruning_a_postgres_dump_never_touches_sqlite_backups(self):
        folder = self.root / "f"
        folder.mkdir()
        sqlite_backups = [folder / f"resumebot-2026102{i}-120000.sqlite3" for i in range(3)]
        for path in sqlite_backups + [folder / f"resumebot-2026102{i}-120000.dump" for i in range(3)]:
            path.write_bytes(b"x")
        self.assertEqual(b.prune(folder, 1), 1)
        self.assertTrue(all(p.exists() for p in sqlite_backups))

    def test_a_copy_that_fails_the_integrity_check_is_not_kept(self):
        data, _ = make_sqlite_data_dir(self.root)
        with mock.patch.object(b.base, "check_sqlite_file", return_value="quick_check: broken"):
            with self.assertRaises(b.base.Stop):
                b.backup_once(data, None, 14, self.log.append)
        self.assertEqual(list((data / "backups").iterdir()), [])

    def test_a_copy_with_a_row_pointing_at_a_missing_parent_is_not_kept(self):
        data, db = make_sqlite_data_dir(self.root)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")  # foreign keys are off on this connection
        conn.commit()
        conn.close()
        with self.assertRaises(b.base.Stop) as raised:
            b.backup_once(data, None, 14, self.log.append)
        self.assertIn("foreign_key_check", str(raised.exception))
        self.assertEqual(list((data / "backups").iterdir()), [])

    def test_a_missing_database_file_stops_with_exit_status_two(self):
        data, db = make_sqlite_data_dir(self.root)
        for leftover in data.glob("app.db*"):
            leftover.unlink()
        self.assertEqual(b.main(["--data-dir", str(data)]), 2)


class PostgresBackup(unittest.TestCase):
    def test_a_postgresql_url_still_runs_pg_dump_into_a_dump_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / ".env").write_text("JOBSEARCH_DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:5433/resumebot\n", encoding="utf-8")
            calls = []

            def run(cmd, **kwargs):
                calls.append(Path(cmd[0]).name)
                if Path(cmd[0]).name == "pg_dump":
                    out = next(a for a in cmd if a.startswith("--file=")).split("=", 1)[1]
                    Path(out).write_bytes(b"dump")
                return mock.Mock(returncode=0, stdout=b"toc", stderr=b"")

            with mock.patch.object(b.base, "find_pg_tool", side_effect=lambda name, *a: name), \
                    mock.patch.object(b.base, "run", side_effect=run), mock.patch.object(b.base, "restrict_to_user"):
                status = b.backup_once(data, None, 14, lambda _text: None)
            self.assertEqual(status, 0)  # no RESUMEBOT_BACKUP_DIR: a good backup, with no second copy to make
            self.assertEqual(calls, ["pg_dump", "pg_restore"])
            self.assertEqual(len(b.listed(data / "backups")), 1)

    def test_a_missing_env_stops_with_exit_status_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(b.main(["--data-dir", tmp]), 2)


class BackupNames(unittest.TestCase):
    """A name is stamped in UTC, so a change of the machine's clock or zone cannot put a newer backup before
    an older one; names made before that (local clock, no Z) are still found and pruned."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        # A machine at UTC+13 with no daylight time: old names mean 13 hours earlier in UTC.
        zone = timezone(timedelta(hours=13))
        patch = mock.patch.object(b, "local_instant", lambda naive: naive.replace(tzinfo=zone))
        patch.start()
        self.addCleanup(patch.stop)

    def touch(self, *names):
        for name in names:
            (self.folder / name).write_bytes(b"x")

    def left(self):
        return sorted(p.name for p in self.folder.iterdir())

    def test_a_name_is_the_utc_moment_with_a_z(self):
        zone = timezone(timedelta(hours=13))
        self.assertEqual(b.stamp_name("dump", datetime(2026, 11, 8, 4, 30, 5, tzinfo=zone)), "resumebot-20261107-153005Z.dump")
        self.assertEqual(b.stamp_name("sqlite3", datetime(2026, 11, 8, 15, 30, 5, tzinfo=UTC)), "resumebot-20261108-153005Z.sqlite3")
        self.assertTrue(b.DUMP_NAME.fullmatch(b.stamp_name("dump")))
        self.assertTrue(b.SQLITE_NAME.fullmatch(b.stamp_name("sqlite3")))

    def test_old_and_new_names_are_both_found(self):
        self.touch("resumebot-20261021-120000.dump", "resumebot-20261022-120000Z.dump", "resumebot-20261021-120000.sqlite3", "notes.txt")
        self.assertEqual([p.name for p in b.listed(self.folder)], ["resumebot-20261021-120000.dump", "resumebot-20261022-120000Z.dump"])
        self.assertEqual([p.name for p in b.listed(self.folder, b.SQLITE_NAME)], ["resumebot-20261021-120000.sqlite3"])

    def test_pruning_orders_old_local_names_and_new_utc_names_by_the_moment_each_means(self):
        # 20261108-100000 on the old local clock (UTC+13) is 21:00 UTC on the 7th: older than 20261108-000000Z, though
        # its digits are larger. Sorted by digits the newest backup would have been the one pruned.
        old, new = "resumebot-20261108-100000.dump", "resumebot-20261108-000000Z.dump"
        self.touch(old, new)
        self.assertEqual(b.prune(self.folder, 1), 1)
        self.assertEqual(self.left(), [new])

    def test_pruning_keeps_the_newest_of_each_kind_across_both_naming_styles(self):
        self.touch("resumebot-20261101-120000.dump", "resumebot-20261102-120000Z.dump", "resumebot-20261103-120000Z.dump",
                   "resumebot-20261101-120000.sqlite3", "resumebot-20261102-120000Z.sqlite3")
        b.prune(self.folder, 2, b.kind_of("x.dump"))
        b.prune(self.folder, 1, b.kind_of("x.sqlite3"))
        self.assertEqual(self.left(), ["resumebot-20261102-120000Z.dump", "resumebot-20261102-120000Z.sqlite3",
                                       "resumebot-20261103-120000Z.dump"])

    def test_two_backups_in_the_hour_the_clocks_go_back_get_different_names(self):
        first = b.stamp_name("dump", datetime(2026, 11, 1, 5, 50, tzinfo=UTC))  # 01:50 EDT in a zone that falls back
        second = b.stamp_name("dump", datetime(2026, 11, 1, 6, 10, tzinfo=UTC))  # 01:10 EST, twenty minutes later
        self.assertLess(first, second)


if __name__ == "__main__":
    unittest.main()
