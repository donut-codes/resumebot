"""Tests for restore_local.py: stdlib unittest, run by CI (python -m unittest discover -s scripts). Temporary folders only."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_local as b  # noqa: E402
import restore_local as r  # noqa: E402


def make_db(path: Path, rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO item (id) VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()


def count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM item").fetchone()[0]
    finally:
        conn.close()


def aside(folder: Path, suffix: str = "") -> list[Path]:
    return [p for p in folder.iterdir() if ".before-restore-" in p.name and p.name.endswith(suffix)]


class Restore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.live = self.data / "app.db"
        (self.data / ".env").write_text(f"JOBSEARCH_DATABASE_URL=sqlite+aiosqlite:///{self.live.as_posix()}\n", encoding="utf-8")
        make_db(self.live, 3)
        self.backup = Path(self.tmp.name) / "resumebot-20261101-120000.sqlite3"
        make_db(self.backup, 7)
        self.log: list[str] = []
        self.idle = lambda _url: False

    def restore(self, answers=None):
        return r.restore_once(self.data, self.backup, self.log.append, answers or self.idle)

    def test_the_backup_replaces_the_live_file_and_the_old_one_is_kept_aside(self):
        self.assertEqual(self.restore(), 0)
        self.assertEqual(count(self.live), 7)
        kept = aside(self.data)
        self.assertEqual(len(kept), 1)
        self.assertEqual(count(kept[0]), 3)

    def test_the_wal_and_shm_of_the_old_file_are_set_aside_with_it(self):
        (self.data / "app.db-wal").write_bytes(b"w")
        (self.data / "app.db-shm").write_bytes(b"s")
        self.restore()
        self.assertFalse((self.data / "app.db-wal").exists())
        self.assertFalse((self.data / "app.db-shm").exists())
        self.assertEqual(len(aside(self.data, "-wal")), 1)
        self.assertEqual(len(aside(self.data, "-shm")), 1)

    def test_it_refuses_an_app_address_that_is_not_on_this_machine_and_asks_nobody(self):
        (self.data / ".env").write_text(
            f"JOBSEARCH_DATABASE_URL=sqlite+aiosqlite:///{self.live.as_posix()}\n"
            "RESUMEBOT_APP_URL=http://other-host.example:8430\n",
            encoding="utf-8")
        asked: list[str] = []
        with self.assertRaises(r.base.Stop):
            self.restore(lambda url: asked.append(url) or False)
        self.assertEqual(asked, [])
        self.assertEqual(count(self.live), 3)

    def test_it_refuses_while_the_app_answers_and_changes_nothing(self):
        with self.assertRaises(r.base.Stop):
            self.restore(lambda _url: True)
        self.assertEqual(count(self.live), 3)
        self.assertEqual(aside(self.data), [])

    def test_a_backup_that_fails_its_check_changes_nothing(self):
        self.backup.write_bytes(b"this is not a database" * 100)
        with self.assertRaises(r.base.Stop):
            self.restore()
        self.assertEqual(count(self.live), 3)
        self.assertEqual(aside(self.data), [])

    def test_a_missing_backup_file_changes_nothing(self):
        self.backup.unlink()
        with self.assertRaises(r.base.Stop):
            self.restore()
        self.assertEqual(count(self.live), 3)

    def test_it_refuses_a_data_folder_on_postgresql(self):
        (self.data / ".env").write_text("JOBSEARCH_DATABASE_URL=postgresql://u:p@127.0.0.1:5433/resumebot\n", encoding="utf-8")
        with self.assertRaises(r.base.Stop):
            self.restore()
        self.assertEqual(count(self.live), 3)

    def test_it_prints_the_next_steps(self):
        self.restore()
        # One line: the script, run with the app environment's own Python, to start and then to smoke.
        self.assertTrue(any(".venv/Scripts/python.exe app/scripts/local/stack.py" in line and " start " in line
                            and line.rstrip().endswith("smoke") for line in self.log))
        self.assertFalse(any("py -3" in line for line in self.log))

    def test_a_backup_made_by_backup_local_restores_end_to_end(self):
        (self.data / "backups").mkdir()
        made = b.backup_sqlite(self.live, self.data / "backups", 14, lambda _t: None)
        conn = sqlite3.connect(self.live)
        conn.execute("DELETE FROM item")
        conn.commit()
        conn.close()
        r.restore_once(self.data, made, self.log.append, self.idle)
        self.assertEqual(count(self.live), 3)

    def test_main_returns_two_when_it_stops(self):
        self.assertEqual(r.main([str(self.backup.with_name("nope.sqlite3")), "--data-dir", str(self.data)]), 2)


if __name__ == "__main__":
    unittest.main()
