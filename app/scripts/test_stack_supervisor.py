"""Tests of what the supervisor in local/stack.py writes to supervisor.log: a time with its offset, which program and
pid, and what a restart says. Standard library only. The clock and the children are made up; nothing is started."""

import contextlib
import datetime as dt
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "local"))

import stack

SECRET = "zq-secret-7d41-never-in-a-log"
OFFSET_TIME = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d"


class Kid:
    """A made-up child: up from its start until it dies; the clock is the test's own."""

    def __init__(self, name: str, world: dict, log: Path | None = None) -> None:
        self.name, self.world, self.log = name, world, log
        self.proc = None
        self.started, self.delay = 0.0, 5.0
        self.up, self.starts = False, 0

    def start(self) -> None:
        self.starts += 1
        self.proc = SimpleNamespace(pid=100 + self.starts, returncode=None)
        self.started, self.up = self.world["now"], True

    def alive(self) -> bool:
        return self.up

    def die(self, code: int = 1) -> None:
        self.up = False
        self.proc.returncode = code

    def finish(self, grace: float) -> bool:
        self.up = False
        self.proc.returncode = -1
        return True


def all_well(url):
    return {"status": "ok"}


class SupervisorLines(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = Path(self.folder.name)
        env = {"JOBSEARCH_DATABASE_URL": f"sqlite+aiosqlite:///{(self.data / 'a.db').as_posix()}"}
        (self.data / ".env").write_text("", encoding="utf-8")
        self.config = stack.Config(self.data, env)
        self.config.logs.mkdir(parents=True, exist_ok=True)
        zone = dt.timezone(dt.timedelta(hours=-5))
        patch = mock.patch.object(stack, "wall_clock", lambda: dt.datetime(2026, 11, 16, 9, 30, 5, tzinfo=zone))
        patch.start()
        self.addCleanup(patch.stop)
        self.world = {"now": 0.0}

    def look(self, keepers, t, ask=all_well) -> str:
        self.world["now"] = t
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stack.tend(self.config, keepers, t, ask)
        return out.getvalue()

    def test_a_supervisor_line_has_a_time_with_offset_the_program_and_the_pid(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stack.supervisor_say("stopping")
        self.assertEqual(out.getvalue(), f"2026-11-16T09:30:05-05:00 supervisor {os.getpid()} stopping\n")

    def test_the_shared_say_stays_plain(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stack.say("set up")
        self.assertEqual(out.getvalue(), "==> set up\n")

    def test_a_restart_names_the_child_the_old_pid_and_exit_code_the_new_pid_and_why(self) -> None:
        engine = Kid("engine", self.world)
        keepers = [stack.Keeper(engine)]
        self.look(keepers, 0)
        engine.die(3)
        self.look(keepers, 100)
        out = self.look(keepers, 105)
        line = [x for x in out.splitlines() if "restarted" in x][0]
        self.assertRegex(line, OFFSET_TIME)
        for part in ("engine", "old pid 101", "exit 3", "new pid 102", "exited"):
            self.assertIn(part, line)

    def test_a_restart_after_a_failed_health_check_says_so(self) -> None:
        page = Kid("page", self.world, self.config.logs / "page.log")
        watch = stack.Watch(every=1, fails=2, grace=0)
        keepers = [stack.Keeper(page, "http://127.0.0.1:1/page", watch)]
        text = ""
        for t in (0, 1, 2, 3, 7, 8):
            text += self.look(keepers, t, lambda url, t=t: None if t else {"status": "ok"})
        line = [x for x in text.splitlines() if "restarted" in x][0]
        for part in ("page", "old pid 101", "new pid 102", "health check failed"):
            self.assertIn(part, line)

    def test_a_child_that_ends_within_ten_seconds_gets_the_end_of_its_own_log_under_the_line(self) -> None:
        log = self.config.logs / "engine.log"
        log.write_text("".join(f"line {n}\n" for n in range(1, 9)), encoding="utf-8")
        engine = Kid("engine", self.world, log)
        keepers = [stack.Keeper(engine)]
        self.look(keepers, 0)
        engine.die()
        self.look(keepers, 4)
        out = self.look(keepers, 9)
        lines = out.splitlines()
        at = [i for i, x in enumerate(lines) if "restarted" in x][0]
        self.assertEqual([x.strip() for x in lines[at + 1:at + 6]], [f"line {n}" for n in range(4, 9)])
        self.assertNotIn("line 3", out)

    def test_a_child_that_ran_longer_gets_no_log_copied(self) -> None:
        log = self.config.logs / "page.log"
        log.write_text("an old line\n", encoding="utf-8")
        page = Kid("page", self.world, log)
        keepers = [stack.Keeper(page)]
        self.look(keepers, 0)
        page.die()
        self.look(keepers, 30)
        self.assertNotIn("an old line", self.look(keepers, 35))

    def test_the_lanes_log_is_never_copied_and_a_secret_in_it_never_reaches_the_supervisor_log(self) -> None:
        log = self.config.logs / "lanes.log"
        log.write_text(f"claude said {SECRET}\n", encoding="utf-8")
        lanes = Kid("lanes", self.world, log)
        keepers = [stack.Keeper(lanes)]
        self.look(keepers, 0)
        lanes.die()
        text = self.look(keepers, 2) + self.look(keepers, 7)
        self.assertIn("restarted", text)
        self.assertNotIn(SECRET, text)


class ChildOutput(unittest.TestCase):
    def test_a_started_child_with_a_log_has_its_stdout_and_stderr_in_that_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            log = folder / "logs" / "lanes-stdout.log"
            code = "import sys; print('to-stdout'); print('to-stderr', file=sys.stderr); sys.exit(3)"
            kid = stack.Child("lanes", [sys.executable, "-c", code], folder, dict(os.environ), log, folder / "run")
            kid.start()
            kid.proc.wait(timeout=30)
            text = log.read_text(encoding="utf-8")
            self.assertIn("to-stdout", text)
            self.assertIn("to-stderr", text)

    def test_the_lanes_are_started_with_a_log_file_in_logs(self) -> None:
        source = Path(stack.__file__).read_text(encoding="utf-8")
        self.assertRegex(source, r'(?s)Child\("lanes",.{0,200}config\.logs / "lanes-stdout\.log"')


if __name__ == "__main__":
    unittest.main()
