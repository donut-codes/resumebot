"""Tests of what local/stack.py's `setup` runs. Standard library only. Nothing here makes an environment, installs a
package or reaches the network: the steps are recorded, never run."""

import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "local"))

import stack


class SetupLeavesTheCommand(unittest.TestCase):
    def steps(self, folder: Path) -> list[list[str]]:
        """The commands one `setup` hands to run_step, in order, for a data folder and an environment under `folder`."""
        config = stack.Config(folder / "data", {"RESUMEBOT_VENV": str(folder / "venv")})
        ran: list[list[str]] = []
        purelib = folder / "purelib"
        purelib.mkdir()
        found = SimpleNamespace(stdout=f"{purelib}\n", returncode=0)
        with mock.patch.object(stack, "run_step", lambda command, cwd=None, label="", env=None: ran.append(command)), \
                mock.patch.object(stack, "only_me", lambda path: None), \
                mock.patch.object(stack, "taken_ports", lambda config: []), \
                mock.patch.object(stack, "say", lambda text: None), \
                mock.patch.object(subprocess, "run", lambda *a, **k: found):
            stack.setup(config, dev=False, page=False)
        return ran

    def test_setup_installs_the_resumebot_command_after_the_locked_packages_and_resolves_nothing_for_it(self):
        with tempfile.TemporaryDirectory() as folder:
            ran = self.steps(Path(folder))
        pip = [step for step in ran if step[1:4] == ["-m", "pip", "install"]]
        self.assertEqual(len(pip), 2, ran)
        locked, command = pip
        self.assertIn("--require-hashes", locked)
        self.assertEqual(command[0], locked[0])  # the same environment's own pip
        self.assertIn("--no-deps", command)
        self.assertEqual(command[-2:], ["-e", str(stack.BACKEND)])

    def test_what_the_command_step_leaves_in_the_code_folder_is_ignored_by_git(self):
        ignored = (stack.APP.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("*.egg-info/", ignored)



class TheWordsNameTheSafePython(unittest.TestCase):
    """`py -3.12` is the system Python on Windows, and its SQLite can be one the project refuses for the data file."""

    def test_setup_on_an_older_python_names_uvs_own_python_and_never_the_py_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            config = stack.Config(Path(folder) / "data", {})
            with mock.patch.object(stack.sys, "version_info", (3, 11, 0)), self.assertRaises(stack.Stop) as stopped:
                stack.setup(config, dev=False, page=False)
            self.assertFalse((Path(folder) / "data").exists())
        said = str(stopped.exception)
        self.assertIn("uv python find --managed-python 3.12", said)
        self.assertNotIn("py -3", said)

    def test_the_help_names_the_environments_python_and_uvs_and_never_the_py_launcher(self):
        said = stack.__doc__
        self.assertNotIn("py -3", said)
        self.assertIn("app/backend/.venv/Scripts/python.exe", said)
        self.assertIn("uv python find --managed-python 3.12", said)
        self.assertNotRegex(said, r"stack\.py\w")


class PortsAreFree(unittest.TestCase):
    """`start` tests every port the install will use (`setup` binds none), on loopback, with ports these tests bind
    themselves."""

    def setUp(self):
        self.sockets: list[socket.socket] = []
        self.addCleanup(self.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        # The holder lookup runs netstat and tasklist (about a second); only the naming test wants the real shape.
        patched = mock.patch.object(stack, "holder", lambda port, udp: None)
        patched.start()
        self.addCleanup(patched.stop)

    def close(self):
        for held in self.sockets:
            held.close()

    def free_block(self) -> tuple[int, int]:
        """A page port and a lane port whose every port (page, engine, UDP twins, five lanes) is free right now."""
        for _ in range(200):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                page = probe.getsockname()[1]
            lane = page + 1000 if page + 1000 <= 65500 else page - 1000
            if page > 65400:
                continue
            if self.can_bind(page, lane):
                return page, lane
        self.fail("no free block of ports")

    @staticmethod
    def can_bind(page: int, lane: int) -> bool:
        """Every port of the block binds now (a bind is quick; the connect probe waits out a refused connection)."""
        wanted = [(page, True), (page, False), (page + stack.ENGINE_OFFSET, True), (page + stack.ENGINE_OFFSET, False)]
        wanted += [(lane + index, False) for index in range(stack.LANE_COUNT)]
        for port, udp in wanted:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    return False
        return True

    def config(self, page: int, lane: int) -> "stack.Config":
        return stack.Config(self.data, {"RESUMEBOT_APP_URL": f"http://127.0.0.1:{page}", "SCOUT_PORT": str(lane)})

    def hold(self, port: int, udp: bool = False) -> None:
        held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
        held.bind(("127.0.0.1", port))
        if not udp:
            held.listen()
        self.sockets.append(held)

    def test_setup_binds_no_port_so_a_taken_one_does_not_stop_it_and_start_says_it_instead(self):
        page, lane = self.free_block()
        self.hold(page)
        self.hold(lane + 2)
        self.hold(page + stack.ENGINE_OFFSET, udp=True)
        config = self.config(page, lane)
        config.environ["RESUMEBOT_VENV"] = str(Path(self.tmp.name) / "venv")
        purelib = Path(self.tmp.name) / "purelib"
        purelib.mkdir()
        found = SimpleNamespace(stdout=f"{purelib}\n", returncode=0)
        with mock.patch.object(stack, "run_step", lambda command, cwd=None, label="", env=None: None), \
                mock.patch.object(stack, "only_me", lambda path: None), \
                mock.patch.object(stack, "say", lambda text: None), \
                mock.patch.object(subprocess, "run", lambda *a, **k: found):
            stack.setup(config, dev=False, page=False)  # raises nothing
        said = " ".join(stack.problems(config, hold=False))
        self.assertIn(f"port {page}", said)
        self.assertIn(f"port {lane + 2}", said)
        self.assertIn(f"UDP port {page + stack.ENGINE_OFFSET}", said)

    def test_the_sentence_names_what_holds_the_port(self):
        page, lane = self.free_block()
        self.hold(page)
        with mock.patch.object(stack, "holder", lambda port, udp: "python.exe (pid 4242)"):
            found = stack.taken_ports(self.config(page, lane))
        mine = [line for line in found if f"port {page} " in line]  # other test processes may hold ports nearby
        self.assertEqual(len(mine), 1)
        self.assertIn("python.exe (pid 4242)", mine[0])

    def test_every_taken_port_is_listed(self):
        page, lane = self.free_block()
        self.hold(page)
        self.hold(lane)
        found = stack.taken_ports(self.config(page, lane))
        for port in (page, lane):  # other test processes may hold ports nearby, so no count
            self.assertTrue(any(f"port {port} " in line for line in found), found)

    def test_start_lists_a_held_page_port(self):
        page, lane = self.free_block()
        self.hold(page)
        found = stack.problems(self.config(page, lane), hold=True)
        self.assertTrue(any(f"port {page}" in line and "RESUMEBOT_APP_URL" in line for line in found), found)

    def test_start_hold_starts_no_lanes_and_does_not_check_their_ports(self):
        page, lane = self.free_block()
        self.hold(lane)
        config = self.config(page, lane)
        held = stack.problems(config, hold=True)
        self.assertFalse(any(f"port {lane}" in line for line in held), held)
        full = stack.problems(config, hold=False)
        self.assertTrue(any(f"port {lane}" in line and "SCOUT_PORT" in line for line in full), full)

    def test_a_port_past_65535_is_a_problem(self):
        config = stack.Config(self.data, {"RESUMEBOT_APP_URL": "http://127.0.0.1:65500"})
        found = stack.taken_ports(config)
        self.assertTrue(any("65535" in line for line in found), found)

    def test_postgres_port_is_listed_only_when_the_stack_runs_its_postgres(self):
        env = {"RESUMEBOT_PG_PORT": "5999"}
        own = stack.Config(self.data, {**env, "JOBSEARCH_DATABASE_URL": "postgresql://x"})
        other = stack.Config(self.data, {**env, "JOBSEARCH_DATABASE_URL": "sqlite+aiosqlite:///x"})
        self.assertIn(5999, [use.port for use in stack.install_ports(own)])
        self.assertNotIn(5999, [use.port for use in stack.install_ports(other)])

    def test_the_default_postgres_port_is_the_one_pg_py_uses(self):
        import pg
        self.assertEqual(stack.DEFAULT_PG_PORT, pg.DEFAULT_PORT)


if __name__ == "__main__":
    unittest.main()
