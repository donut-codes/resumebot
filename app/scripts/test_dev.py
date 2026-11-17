"""Tests of dev.py, the local development flow. Standard library only, as dev.py is; `dev.py check` runs them
(`python -m unittest discover -s app/scripts -p "test_*.py"`).

Nothing here installs anything, reaches the network or touches a checkout of the repository: environments are faked
and a worktree is removed from a throwaway git repository.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import dev

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


@contextlib.contextmanager
def scratch():
    """A temporary folder that is removed afterwards, read-only git files and all."""
    folder = Path(tempfile.mkdtemp(prefix="resumebot-dev-test-"))
    try:
        yield folder
    finally:
        dev.remove_tree(folder)


@contextlib.contextmanager
def quiet():
    """Capture what dev.py says, so a test run stays readable. Yields (stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def dead_process_id() -> int:
    """The id of a process that has finished."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def sleeper() -> subprocess.Popen:
    """A process that sleeps, in a group of its own (as a preview is), to be stopped by the test."""
    flags: dict = {"start_new_session": True}
    if os.name == "nt":
        flags = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **flags)


def end(proc: subprocess.Popen) -> None:
    """Make sure a process a test started is gone, and collected."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    for pipe in (proc.stdin, proc.stdout):
        if pipe:
            pipe.close()


# --- the check's plan and steps -----------------------------------------------------------------------------------


class PlanCheckTests(unittest.TestCase):
    def test_no_options_and_no_paths_runs_every_side(self):
        plan = dev.plan_check([], [])
        self.assertTrue(all([plan.backend, plan.runner, plan.frontend, plan.shell, plan.scripts]))
        self.assertTrue(plan.app_tests and plan.engine_tests)

    def test_a_side_runs_alone(self):
        plan = dev.plan_check(["backend"], [])
        self.assertTrue(plan.backend and plan.app_tests and plan.engine_tests)
        self.assertFalse(plan.frontend or plan.runner or plan.shell or plan.scripts)

    def test_a_backend_path_narrows_the_run_to_that_file_and_leaves_the_rest_out(self):
        plan = dev.plan_check([], ["app/backend/tests\\jobsearch/test_gates.py"])
        self.assertEqual(plan.py_paths, ["tests/jobsearch/test_gates.py"])
        self.assertTrue(plan.backend and plan.app_tests)
        self.assertFalse(plan.engine_tests or plan.frontend or plan.runner or plan.shell)

    def test_a_page_path_runs_only_the_page_and_keeps_the_path_for_vitest(self):
        plan = dev.plan_check([], ["src/App.test.tsx", "packages/process-view/src/x.test.ts"])
        self.assertEqual(plan.js_paths, ["src/App.test.tsx", "packages/process-view/src/x.test.ts"])
        self.assertTrue(plan.frontend)
        self.assertFalse(plan.backend)

    def test_the_engine_path_is_the_packages_own_tests_only(self):
        plan = dev.plan_check([], ["packages/process-engine"])
        self.assertTrue(plan.backend and plan.engine_tests)
        self.assertFalse(plan.app_tests)

    def test_paths_of_both_sides_run_both(self):
        plan = dev.plan_check([], ["tests/a.py", "src/b.test.ts"])
        self.assertTrue(plan.backend and plan.frontend)

    def test_the_runner_has_no_paths(self):
        with self.assertRaisesRegex(dev.Stop, "drop --runner"):
            dev.plan_check(["runner"], ["tests/a.py"])

    def test_a_path_for_a_side_that_was_switched_off_is_refused_not_dropped(self):
        with self.assertRaisesRegex(dev.Stop, "drop the flag"):
            dev.plan_check(["frontend"], ["tests/a.py"])

    def test_a_path_of_no_known_kind_is_refused(self):
        with self.assertRaisesRegex(dev.Stop, "neither a backend tests/ path"):
            dev.plan_check([], ["docs/a.md"])


class GateTests(unittest.TestCase):
    def test_every_step_runs_and_the_summary_says_what_passed_failed_and_was_skipped(self):
        gate = dev.Gate()
        with quiet():
            gate.step("one", lambda: 0)
            gate.step("two", lambda: 1)
            gate.step("three", lambda: (_ for _ in ()).throw(dev.Stop("plain sentence")))
            gate.step("four", lambda: 1 / 0)  # a bug in a step is one failed step
            gate.skip("five", "no reason")
        self.assertTrue(gate.failed)
        self.assertEqual([r.status for r in gate.results], ["ok", "FAILED", "FAILED", "FAILED", "skipped"])
        text = gate.summary()
        for line in (
            r"ok\s+one", r"FAILED\s+two", r"FAILED\s+three", r"FAILED\s+four", r"skipped\s+five\s+0s\s+\(no reason\)",
        ):
            self.assertRegex(text, line)

    def test_a_gate_with_only_passes_has_not_failed(self):
        gate = dev.Gate()
        with quiet():
            gate.step("one", lambda: 0)
            gate.skip("two", "x")
        self.assertFalse(gate.failed)


class CheckBackendTests(unittest.TestCase):
    """What `check --backend` runs and with what: the commands, the environment, and that no test is left out."""

    def run_backend(self, environ, plan=None):
        calls = []

        def fake_run(cmd, *, cwd=None, env=None, quiet=False):
            calls.append((list(map(str, cmd)), cwd, env))
            return 0

        gate = dev.Gate()
        tree = dev.Tree(Path("repo"))
        with mock.patch.dict(os.environ, environ, clear=False), mock.patch.object(dev, "run", fake_run), \
                mock.patch.object(dev, "app_python", lambda t: Path("py")), quiet():
            dev.check_backend(gate, tree, plan or dev.plan_check(["backend"], []))
        return gate, calls

    def test_pytest_runs_every_test_on_workers_with_no_database_to_name_and_no_plugin_to_leave_tests_out(self):
        gate, calls = self.run_backend({})
        self.assertFalse(gate.failed)
        names = [r.name for r in gate.results]
        self.assertEqual(names, [
            "backend: ruff", "backend: basedpyright", "backend: pytest", "backend: the process engine's own tests",
        ])
        ruff, pyright, pytest_app, pytest_engine = (c[0] for c in calls)
        self.assertEqual(ruff[1:], ["-m", "ruff", "check", "app", "tests"])
        self.assertEqual(pyright[1:4], ["-m", "basedpyright", "--pythonpath"])
        self.assertEqual(pyright[4], "py")
        self.assertEqual(pytest_app[pytest_app.index("-n") + 1], str(dev.worker_count()))
        self.assertEqual(pytest_app[pytest_app.index("--dist") + 1], "worksteal", "no worker waits out another's block")
        self.assertIn("--durations=15", pytest_app)
        self.assertNotIn("-n", pytest_engine)
        for command in (pytest_app, pytest_engine):
            self.assertEqual(command.count("-p"), 1, "only the cache provider is switched off: no test is left out")
            self.assertNotIn("pytest_no_database", " ".join(command))
        self.assertEqual({r.note for r in gate.results}, {""}, "no step has a test it left out to say")

    def test_an_environment_that_cannot_be_built_is_tried_once_not_once_for_each_step(self):
        calls = []

        def failing(tree):
            calls.append(tree)
            raise dev.Stop("no network")

        gate = dev.Gate()
        with mock.patch.object(dev, "app_python", failing), quiet():
            dev.check_backend(gate, dev.Tree(Path("repo")), dev.plan_check(["backend"], []))
        self.assertEqual(len(calls), 1)
        self.assertEqual([r.status for r in gate.results], ["FAILED"] * 4)

    def test_once_gives_every_caller_the_same_answer_or_the_same_stop(self):
        calls = []
        counted = dev.once(lambda: calls.append(1) or "value")
        self.assertEqual((counted(), counted()), ("value", "value"))
        self.assertEqual(len(calls), 1)

        def broken():
            calls.append(2)
            raise dev.Stop("plain sentence")

        refused = dev.once(broken)
        for _ in range(2):
            with self.assertRaisesRegex(dev.Stop, "plain sentence"):
                refused()
        self.assertEqual(calls, [1, 2])

    def test_the_tests_run_in_their_own_folders_with_the_app_and_the_engine_on_the_path(self):
        _, calls = self.run_backend({})
        tree = dev.Tree(Path("repo"))
        self.assertEqual(calls[2][1], tree.backend)
        self.assertEqual(calls[3][1], tree.engine)
        paths = calls[2][2]["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(paths[:3], [str(tree.backend), str(tree.engine / "src"), str(tree.scripts)])
        self.assertEqual(calls[2][2]["PYTHONUTF8"], "1")

    def test_a_narrow_run_names_the_files_to_pytest_and_skips_the_lint_when_only_the_engine_is_named(self):
        plan = dev.plan_check([], ["tests/jobsearch/test_gates.py"])
        _, calls = self.run_backend({}, plan)
        self.assertEqual(calls[2][0][-1], "tests/jobsearch/test_gates.py")
        self.assertEqual(len(calls), 3)  # no engine suite
        _, engine_calls = self.run_backend({}, dev.plan_check([], ["packages/process-engine"]))
        self.assertEqual(len(engine_calls), 1)

class CheckOtherSidesTests(unittest.TestCase):
    def test_the_page_is_typechecked_linted_and_tested_and_vitest_gets_the_paths(self):
        calls = []
        tree = dev.Tree(Path("repo"))
        gate = dev.Gate()
        with mock.patch.object(dev, "run", lambda cmd, **kw: calls.append((list(map(str, cmd)), kw.get("cwd"))) or 0), \
                mock.patch.object(dev, "ensure_frontend", lambda t: "npm"), \
                mock.patch.object(dev, "ensure_node_modules", lambda folder: None), quiet():
            dev.check_frontend(gate, tree, dev.plan_check(["frontend"], []))
            dev.check_frontend(gate, tree, dev.plan_check([], ["src/App.test.tsx"]))
        self.assertEqual(calls[0][0], ["npm", "run", "typecheck"])
        self.assertEqual(calls[1][0], ["npm", "--prefix", "lint", "run", "lint"])
        self.assertEqual(calls[2][0], ["npm", "test"])
        self.assertEqual(calls[5][0], ["npm", "test", "--", "src/App.test.tsx"])
        self.assertTrue(all(cwd == tree.frontend for _, cwd in calls))

    def test_the_scripts_stage_also_runs_the_repository_level_scripts_tests(self):
        calls = []
        tree = dev.Tree(Path("repo"))
        gate = dev.Gate()

        def fake_run(cmd, **kw):
            calls.append((list(map(str, cmd)), kw.get("cwd")))
            return 0

        with mock.patch.object(dev, "run", fake_run), mock.patch.object(dev, "app_python", lambda t: "python"), quiet():
            dev.check_scripts(gate, tree)
        discovered = [cmd[cmd.index("-s") + 1] for cmd, _ in calls if "unittest" in cmd]
        self.assertEqual(discovered, [str(tree.scripts), str(tree.root / "scripts")])
        self.assertEqual(len(gate.results), 3)

    def test_the_scouts_node_tests_run_on_every_system(self):
        tree = dev.Tree(Path("repo"))
        for platform in ("linux", "win32", "darwin"):
            calls = []

            def fake_run(cmd, *, calls=calls, **kwargs):
                calls.append(list(map(str, cmd)))
                return 0

            gate = dev.Gate()
            with mock.patch.object(sys, "platform", platform), mock.patch.object(dev, "run", fake_run), \
                    mock.patch.object(dev, "tool", lambda name, hint: name), quiet():
                dev.check_runner(gate, tree)
            self.assertEqual(calls, [["node", "--test"]], platform)
            self.assertEqual(gate.results[0].status, "ok", platform)

    def test_shellcheck_findings_are_compared_with_the_baseline_by_file_rule_and_line_text(self):
        report = (
            "scripts/a.sh:3:1: warning: Use x [SC2086]\n"
            "scripts/a.sh:9:5: note: Not following [SC1091]\n"
            "scripts/a.sh:12:1: style: not a finding line\n"
        )
        lines = {("scripts/a.sh", 3): "   echo $x  ", ("scripts/a.sh", 9): ". ./b.sh"}
        entries = dev.shellcheck_entries(report, lambda file, number: lines.get((file, number), ""))
        self.assertEqual(entries, ["scripts/a.sh | SC1091 | . ./b.sh", "scripts/a.sh | SC2086 | echo $x"])

    def test_a_finding_that_is_not_in_the_baseline_is_new_and_a_repeat_of_an_old_one_is_new_too(self):
        baseline = ["a | SC1 | x", "a | SC2 | y"]
        self.assertEqual(dev.new_findings(["a | SC1 | x", "a | SC2 | y"], baseline), ([], []))
        twice = ["a | SC1 | x", "a | SC1 | x", "a | SC2 | y"]
        self.assertEqual(dev.new_findings(twice, baseline), (["a | SC1 | x"], []))
        self.assertEqual(dev.new_findings(["a | SC1 | x"], baseline), ([], ["a | SC2 | y"]))
        added = ["a | SC3 | z", "a | SC1 | x", "a | SC2 | y"]
        self.assertEqual(dev.new_findings(added, baseline), (["a | SC3 | z"], []))

    def test_every_baseline_entry_names_a_script_that_exists(self):
        for line in (HERE / "shellcheck-baseline.txt").read_text(encoding="utf-8").splitlines():
            if line.strip():
                self.assertTrue((HERE.parent / line.split(" | ")[0]).is_file(), line)


# --- the environments ---------------------------------------------------------------------------------------------


class WorkerCountTests(unittest.TestCase):
    def test_one_worker_for_each_core_at_most_eight(self):
        self.assertEqual([dev.worker_count(c) for c in (1, 2, 4, 8, 16, 32)], [1, 2, 4, 8, 8, 8])

    def test_this_machine_gets_a_count_in_range(self):
        self.assertTrue(1 <= dev.worker_count() <= 8)


class EnvironmentTests(unittest.TestCase):
    def test_the_cache_folder_follows_the_platform(self):
        with scratch() as root, mock.patch.dict(os.environ, {}, clear=False):
            for key in ("RESUMEBOT_DEV_HOME", "LOCALAPPDATA", "XDG_CACHE_HOME"):
                os.environ.pop(key, None)
            os.environ["LOCALAPPDATA"] = str(root / "local")
            with mock.patch.object(sys, "platform", "win32"):
                self.assertEqual(dev.dev_home(), root / "local" / "resumebot-dev")
            with mock.patch.object(sys, "platform", "darwin"):
                self.assertEqual(dev.dev_home(), Path.home() / "Library" / "Caches" / "resumebot-dev")
            with mock.patch.object(sys, "platform", "linux"):
                self.assertEqual(dev.dev_home(), Path.home() / ".cache" / "resumebot-dev")
                os.environ["XDG_CACHE_HOME"] = str(root / "xdg")
                self.assertEqual(dev.dev_home(), root / "xdg" / "resumebot-dev")
            os.environ["RESUMEBOT_DEV_HOME"] = str(root / "mine")
            with mock.patch.object(sys, "platform", "win32"):
                self.assertEqual(dev.dev_home(), root / "mine")

    def test_the_hash_ignores_the_line_endings_a_checkout_was_given(self):
        self.assertEqual(dev.sha256_of("a\r\nb\r\n", "c"), dev.sha256_of("a\nb\n", "c"))
        self.assertNotEqual(dev.sha256_of("a", "b"), dev.sha256_of("ab", ""))

    def test_the_lock_stamp_ignores_the_apps_version_and_nothing_else(self):
        with scratch() as root:
            def lockfile(version, dependency):
                path = root / "package-lock.json"
                path.write_text(json.dumps({
                    "name": "x", "version": version, "lockfileVersion": 3,
                    "packages": {"": {"name": "x", "version": version}, "node_modules/a": {"version": dependency}},
                }), encoding="utf-8")
                return dev.lock_stamp(path)

            self.assertEqual(lockfile("1.0.0", "2.0.0"), lockfile("9.9.9", "2.0.0"))
            self.assertNotEqual(lockfile("1.0.0", "2.0.0"), lockfile("1.0.0", "2.0.1"))

    def test_the_node_versions_the_page_runs_on(self):
        for version, supported in (
            ((22, 12, 0), False), ((22, 22, 1), False), ((22, 22, 2), True), ((23, 0, 0), False), ((24, 14, 9), False),
            ((24, 15, 0), True), ((24, 19, 0), True), ((25, 1, 0), False), ((26, 0, 0), True), ((20, 19, 0), False),
        ):
            self.assertEqual(dev.node_supported(version), supported, version)

    def test_an_environment_is_built_once_however_many_runs_ask_for_it(self):
        built = []

        def fake_build(path, install, what):
            time.sleep(0.3)
            built.append(path)
            path.mkdir(parents=True)
            (path / "ready").write_text("ok", encoding="utf-8")

        with scratch() as root:
            target = root / "venvs" / "abc"
            threads = [threading.Thread(target=lambda: dev.environment(target, [], "test")) for _ in range(4)]
            with mock.patch.object(dev, "build_environment", fake_build), \
                    mock.patch.object(dev, "environment_works", lambda path: (path / "ready").exists()), quiet():
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
            self.assertEqual(built, [target])

    def test_cached_environments_nothing_has_used_for_a_month_are_removed(self):
        with scratch() as parent:
            old_time = time.time() - 40 * 86400
            for name in ("stale", "kept", "fresh"):
                (parent / name).mkdir()
                (parent / name / "ready").write_text("ok", encoding="utf-8")
            for name in ("stale", "kept"):
                os.utime(parent / name / "ready", (old_time, old_time))
            (parent / "stale.lock").write_bytes(b"")
            (parent / "half-built").mkdir()  # no ready file: another run may be building it
            dev.prune_environments(parent, keep=parent / "kept")
            self.assertEqual(sorted(p.name for p in parent.iterdir()), ["fresh", "half-built", "kept", "stale.lock"])

    @unittest.skipUnless(sys.version_info[:2] == dev.PYTHON, "needs Python 3.12 to run")
    def test_the_running_python_is_the_one_used_when_it_is_3_12(self):
        with mock.patch.object(dev.shutil, "which", return_value=None):  # no uv on this machine
            self.assertEqual(dev.find_python(), sys.executable)

    def test_uvs_own_python_3_12_is_tried_before_any_other(self):
        with scratch() as root:
            managed = root / "managed-python.exe"
            managed.write_bytes(b"")
            asked = []

            def fake_output(cmd):
                asked.append([str(part) for part in cmd])
                return f"{managed}\n"

            with mock.patch.object(dev.shutil, "which", return_value="uv-path"), \
                    mock.patch.object(dev, "output", fake_output):
                self.assertEqual(dev.find_python(), str(managed))
            self.assertEqual(asked, [["uv-path", "python", "find", "--managed-python", "3.12"]])

    def test_when_uv_has_no_python_3_12_the_lookup_falls_back_to_the_running_one(self):
        with mock.patch.object(dev.shutil, "which", return_value="uv-path"), \
                mock.patch.object(dev, "output", return_value=""), \
                mock.patch.object(dev.sys, "version_info", (3, 12, 1, "final", 0)), \
                mock.patch.object(dev.sys, "executable", "the-running-python"):
            self.assertEqual(dev.find_python(), "the-running-python")

    def test_the_environment_keys_follow_the_lock_file(self):
        with scratch() as root:
            tree = dev.Tree(root)
            tree.backend.mkdir(parents=True)
            seen = []

            def fake_environment(path, install, what):
                seen.append((path, install))
                return Path("py")

            with mock.patch.dict(os.environ, {"RESUMEBOT_DEV_HOME": str(root / "home")}), \
                    mock.patch.object(dev, "environment", fake_environment):
                tree.lock.write_bytes(b"a==1 --hash=sha256:x\n")
                dev.app_python(tree)
                tree.lock.write_bytes(b"a==2 --hash=sha256:y\n")
                dev.app_python(tree)
                tree.lock.write_bytes(b"a==2 --hash=sha256:y\r\n")  # the same lock, saved with other line endings
                dev.app_python(tree)
            (first, install), (second, _), (third, _) = seen
            self.assertNotEqual(first, second)
            self.assertEqual(second, third)
            self.assertEqual(first.parent, root / "home" / "venvs")
            self.assertEqual(install[:4], ["--only-binary=:all:", "--require-hashes", "--no-deps", "-r"])


class FileHelperTests(unittest.TestCase):
    def test_a_link_reaches_the_folder_and_removing_it_leaves_the_folder(self):
        with scratch() as root:
            target = root / "target"
            target.mkdir()
            (target / "a.txt").write_text("a", encoding="utf-8")
            link = root / "link"
            with quiet():
                dev.link_directory(link, target)
                self.assertTrue(dev.is_link(link))
                self.assertEqual((link / "a.txt").read_text(encoding="utf-8"), "a")
                dev.link_directory(link, target)  # again: nothing to do
                dev.remove_tree(link)
            self.assertFalse(link.exists())
            self.assertEqual((target / "a.txt").read_text(encoding="utf-8"), "a")

    def test_a_worktrees_node_modules_link_is_removed_and_its_target_is_left_whole(self):
        with scratch() as root:
            modules = root / "elsewhere" / "node_modules"
            modules.mkdir(parents=True)
            (modules / "pkg.js").write_text("x", encoding="utf-8")
            tree = root / "wt"
            (tree / "app").mkdir(parents=True)
            with quiet():
                dev.link_directory(tree / "app" / "node_modules", modules)
            self.assertTrue(dev.is_link(tree / "app" / "node_modules"))
            dev.unlink_worktree(tree)
            self.assertFalse(os.path.lexists(tree / "app" / "node_modules"))
            self.assertEqual((modules / "pkg.js").read_text(encoding="utf-8"), "x")
            dev.unlink_worktree(tree)  # nothing left to remove, and no error

    def test_removing_a_worktree_with_the_link_in_it_leaves_no_folder_and_the_target_whole(self):
        with scratch() as root:
            repo = root / "repo"
            subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)
            (repo / "app").mkdir()
            (repo / "app" / "f.txt").write_text("f", encoding="utf-8")
            for args in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "c"]):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            tree = root / "wt"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree)], check=True)
            modules = root / "modules"
            modules.mkdir()
            (modules / "pkg.js").write_text("x", encoding="utf-8")
            with quiet():
                dev.link_directory(tree / "app" / "node_modules", modules)
                dev.remove_worktree(repo, tree)
            self.assertFalse(tree.exists())
            self.assertEqual((modules / "pkg.js").read_text(encoding="utf-8"), "x")
            listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
            self.assertNotIn("wt", listed)

    def test_a_link_to_somewhere_else_is_replaced_and_a_real_folder_in_the_way_is_refused(self):
        with scratch() as root:
            one, two = root / "one", root / "two"
            one.mkdir()
            two.mkdir()
            (two / "b.txt").write_text("b", encoding="utf-8")
            link = root / "link"
            with quiet():
                dev.link_directory(link, one)
                dev.link_directory(link, two)
                self.assertEqual((link / "b.txt").read_text(encoding="utf-8"), "b")
                dev.remove_tree(link)
                link.mkdir()
                with self.assertRaisesRegex(dev.Stop, "in the way"):
                    dev.link_directory(link, one)

    def test_a_folder_with_read_only_files_is_removed(self):
        with scratch() as root:
            folder = root / "objects"
            folder.mkdir()
            read_only = folder / "pack"
            read_only.write_text("x", encoding="utf-8")
            os.chmod(read_only, 0o444)
            dev.remove_tree(folder)
            self.assertFalse(folder.exists())

    def test_a_folder_deeper_than_windows_allows_is_removed(self):
        with scratch() as root:
            deepest = root
            for _ in range(12):
                deepest = deepest / ("d" * 25)
            os.makedirs(dev.long_path(deepest))
            (dev.long_path(deepest) / "file.txt").write_text("x", encoding="utf-8")
            self.assertGreater(len(str(deepest / "file.txt")), 270)
            dev.remove_tree(root / ("d" * 25))
            self.assertFalse((root / ("d" * 25)).exists())

    def test_this_process_is_alive_and_a_finished_one_is_not(self):
        self.assertTrue(dev.pid_alive(os.getpid()))
        self.assertFalse(dev.pid_alive(dead_process_id()))
        self.assertFalse(dev.pid_alive(0))

    def test_a_child_that_has_ended_is_not_alive_even_before_it_is_collected(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.monotonic() + 30
            while dev.pid_alive(proc.pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(dev.pid_alive(proc.pid))
        finally:
            end(proc)

    def test_a_process_is_stopped_whole(self):
        proc = sleeper()
        try:
            self.assertTrue(dev.pid_alive(proc.pid))
            dev.stop_process_tree(proc.pid)
            proc.wait(timeout=20)
            self.assertFalse(dev.pid_alive(proc.pid))
        finally:
            end(proc)


HOLD_THE_LOCK = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import dev
with dev.exclusive(Path(sys.argv[2]), "the thing"):
    print("held", flush=True)
    sys.stdin.readline()  # until the test says to let go (or dies)
print("released", flush=True)
"""


class ExclusiveTests(unittest.TestCase):
    def holder(self, path):
        """Another process that holds the lock until it is ended. Returns it once it says it has the lock. It runs on
        the base interpreter, not `sys.executable`: in a virtual environment on Windows that is a launcher that starts
        the real Python as a child, so `proc.pid` would name the launcher, not the process holding the lock, and ending
        the launcher would leave the lock held."""
        proc = subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", HOLD_THE_LOCK, str(HERE), str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def release(self, proc):
        """Have the holder let go of the lock and say so, then wait for it. A killed process is finished before the
        operating system has closed its files (Windows keeps them open for a moment), so a test that removes the
        folder the lock is in must not rely on a kill."""
        proc.stdin.write("\n")
        proc.stdin.close()
        self.assertEqual(proc.stdout.readline().strip(), "released")
        proc.wait()

    def test_nobody_else_can_take_the_lock_while_it_is_held_and_anybody_can_after(self):
        with scratch() as root:
            path = root / "locks" / "a.lock"
            with dev.exclusive(path, "the thing"):
                other = os.fdopen(os.open(path, os.O_RDWR), "r+b")
                try:
                    self.assertFalse(dev.take_lock(other))
                finally:
                    other.close()
            with dev.exclusive(path, "the thing", wait=1):
                pass

    def test_a_second_holder_waits_for_the_first(self):
        with scratch() as root:
            path = root / "a.lock"
            order = []

            def first():
                with dev.exclusive(path, "the thing"):
                    order.append("first in")
                    time.sleep(0.5)
                    order.append("first out")

            thread = threading.Thread(target=first)
            with quiet():
                thread.start()
                while not order:
                    time.sleep(0.01)
                with dev.exclusive(path, "the thing", wait=20):
                    order.append("second in")
                thread.join()
            self.assertEqual(order, ["first in", "first out", "second in"])

    def test_many_at_once_go_in_one_at_a_time(self):
        with scratch() as root:
            path = root / "a.lock"
            inside, most, mistakes = [0], [0], []

            def work():
                try:
                    with dev.exclusive(path, "the thing", wait=60):
                        inside[0] += 1
                        most[0] = max(most[0], inside[0])
                        time.sleep(0.02)
                        inside[0] -= 1
                except Exception as exc:  # a thread's mistake must show in the test, not only on the console
                    mistakes.append(repr(exc))

            threads = [threading.Thread(target=work) for _ in range(8)]
            with quiet():
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(60)
            self.assertEqual((most[0], mistakes), (1, []))

    def test_a_lock_a_crashed_run_left_behind_does_not_block_anyone(self):
        with scratch() as root:
            path = root / "a.lock"
            path.write_bytes(b"\0" * dev.LOCK_NOTE_OFFSET + f"{dead_process_id()}\n".encode())
            with dev.exclusive(path, "the thing", wait=5):
                pass

    def test_a_holder_in_another_process_is_waited_for_named_and_given_up_on(self):
        with scratch() as root:
            path = root / "a.lock"
            proc = self.holder(path)
            try:
                with quiet() as (out, _), self.assertRaisesRegex(dev.Stop, rf"is held by process {proc.pid}"):
                    with dev.exclusive(path, "the thing", wait=0.6):
                        self.fail("should not get in")
                self.assertIn(f"busy (process {proc.pid})", out.getvalue())
                self.release(proc)
            finally:
                end(proc)

    def test_a_holder_that_dies_lets_go_of_the_lock(self):
        with scratch() as root:
            path = root / "a.lock"
            proc = self.holder(path)
            try:
                proc.kill()
                proc.wait()
                with dev.exclusive(path, "the thing", wait=10):
                    pass
            finally:
                end(proc)


# --- lock and the files the flow depends on -----------------------------------------------------------------------


def read_lock(name):
    """{package: (version, marker, number of hashes)} of a lock file."""
    packages = {}
    current = None
    for line in (REPO / "app" / "backend" / name).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ ;]+)(?: ; (.*?))?(?: \\)?$", line)
        if match:
            current = match.group(1).lower()
            packages[current] = (match.group(2), match.group(3) or "", 0)
        elif line.strip().startswith("--hash=") and current:
            version, marker, hashes = packages[current]
            packages[current] = (version, marker, hashes + 1)
    return packages


class LockFileTests(unittest.TestCase):
    """The lock files are universal, so a native install on Windows asks for nothing that only Linux has."""

    def test_the_linux_only_packages_carry_a_linux_marker(self):
        for name in ("requirements.lock", "requirements-dev.lock"):
            packages = read_lock(name)
            for package in ("uvloop", "httptools", "watchfiles", "websockets"):
                self.assertIn("sys_platform == 'linux'", packages[package][1], f"{package} in {name}")

    def test_windows_gets_what_it_needs_and_nothing_else_does(self):
        for name in ("requirements.lock", "requirements-dev.lock"):
            packages = read_lock(name)
            for package in ("pywin32", "colorama"):
                self.assertEqual(packages[package][1], "sys_platform == 'win32'", f"{package} in {name}")

    def test_every_package_is_pinned_and_has_its_hashes(self):
        for name in ("requirements.lock", "requirements-dev.lock"):
            for package, (version, _, hashes) in read_lock(name).items():
                self.assertTrue(version, package)
                self.assertGreater(hashes, 0, f"{package} in {name} has no hash")

    def test_the_test_lock_holds_the_run_lock_at_the_same_versions(self):
        run, test = read_lock("requirements.lock"), read_lock("requirements-dev.lock")
        for package, (version, marker, _) in run.items():
            self.assertEqual(test[package][:2], (version, marker), package)
        self.assertIn("pytest", test)
        self.assertNotIn("pytest", run)

    def test_the_header_names_the_command_that_makes_the_files(self):
        for name in ("requirements.lock", "requirements-dev.lock"):
            lines = (REPO / "app" / "backend" / name).read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[1], f"#    {dev.LOCK_COMMAND}")

    def test_the_resumebot_command_is_an_entry_point_of_the_package(self):
        text = (REPO / "app" / "backend" / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(text, r'(?m)^\[project\.scripts\]\nresumebot = "app\.main_cli:main"')


class FrontendLinkTests(unittest.TestCase):
    def test_the_frontend_gets_its_link_so_the_vendored_package_finds_its_imports(self):
        with scratch() as root:
            tree = dev.Tree(root)
            (tree.frontend / "node_modules").mkdir(parents=True)
            with mock.patch.object(dev, "npm_executable", lambda: "npm"), \
                    mock.patch.object(dev, "ensure_node_modules", lambda folder: None), quiet():
                self.assertEqual(dev.ensure_frontend(tree), "npm")
                self.assertTrue(dev.is_link(tree.app / "node_modules"))
                linked = Path(os.path.realpath(tree.app / "node_modules"))
                self.assertEqual(linked, (tree.frontend / "node_modules").resolve())
                dev.remove_tree(tree.app / "node_modules")


# --- shots --------------------------------------------------------------------------------------------------------


class ShotsTests(unittest.TestCase):
    def test_without_playwright_it_stops_and_says_how_to_install_it_with_the_owners_yes(self):
        with scratch() as root, mock.patch.object(dev, "shots_folder", lambda: root / "shots"), \
                mock.patch.object(dev, "tool", lambda name, hint: name), \
                mock.patch.object(dev, "run", side_effect=AssertionError("nothing runs")):
            with self.assertRaises(dev.Stop) as caught:
                dev.shots(["#/board"], str(root / "out"))
        message = str(caught.exception)
        self.assertIn("with the owner's yes for the download", message)
        self.assertIn("npm install --prefix", message)
        self.assertIn("playwright@1.48.0 pixelmatch@5.3.0 pngjs@7.0.0", message)
        self.assertIn("install chromium", message)

    def run_shots(self, root, environ=None):
        folder = root / "shots"
        for package in ("playwright", "pixelmatch", "pngjs"):
            (folder / "node_modules" / package).mkdir(parents=True)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((list(map(str, cmd)), kwargs))
            return 0

        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("SHOTS_BASE", "SHOTS_SCHEME", "SHOTS_FIRST_SCREEN"):
                os.environ.pop(key, None)
            os.environ.update(environ or {})
            with mock.patch.object(dev, "shots_folder", lambda: folder), \
                    mock.patch.object(dev, "tool", lambda name, hint: name), \
                    mock.patch.object(dev, "run", fake_run), quiet():
                self.assertEqual(dev.shots(["#/board", "#/inbox"], str(root / "out")), 0)
        return folder, calls[0]

    def test_with_playwright_the_script_runs_beside_its_modules_against_the_local_instance(self):
        with scratch() as root:
            folder, (command, kwargs) = self.run_shots(root)
            self.assertEqual(command[0], "node")
            self.assertEqual(Path(command[1]), folder / "shots.mjs")
            self.assertEqual(command[2], "http://localhost:8430")
            self.assertEqual(Path(command[3]), (root / "out").resolve())
            self.assertEqual(command[4:], ["#/board", "#/inbox"])
            self.assertEqual(kwargs["cwd"], folder)
            # a run against the running app is the last release
            self.assertEqual(kwargs["env"]["SHOTS_UPDATE_LAST"], "1")
            self.assertEqual(kwargs["env"]["SHOTS_SCHEME"], "light")
            self.assertEqual(Path(kwargs["env"]["SHOTS_LAST"]), folder / "last")
            copied, original = folder / "shots.mjs", HERE / "shots.mjs"
            self.assertEqual(copied.read_text(encoding="utf-8"), original.read_text(encoding="utf-8"))
            self.assertTrue((root / "out").is_dir())

    def test_another_address_and_the_night_scheme_come_from_the_environment(self):
        with scratch() as root:
            environ = {"SHOTS_BASE": "http://localhost:9", "SHOTS_SCHEME": "dark"}
            _, (command, kwargs) = self.run_shots(root, environ=environ)
            self.assertEqual(command[2], "http://localhost:9")
            self.assertEqual(kwargs["env"]["SHOTS_UPDATE_LAST"], "0")
            self.assertEqual(kwargs["env"]["SHOTS_SCHEME"], "dark")


# --- the command line and the files around it ---------------------------------------------------------------------


class CopierRunTests(unittest.TestCase):
    """The copier's tests need a Postgres server: `check` leaves them out by name, `check --copier` is their run."""

    def test_check_leaves_the_copier_tests_out_by_name_and_runs_everything_else(self):
        _, calls = CheckBackendTests().run_backend({})
        pytest_app = calls[2][0]
        self.assertEqual(
            dev.COPIER_TESTS, ("tests/migrate/test_copy_from_postgres.py", "tests/migrate/test_drop_noise.py"))
        self.assertEqual(
            [a for a in pytest_app if a.startswith("--ignore")], [f"--ignore={path}" for path in dev.COPIER_TESTS],
            "only the copier's modules are left out")

    def test_the_copier_run_names_only_its_modules_and_needs_a_postgres_url(self):
        calls = []

        def fake_run(cmd, *, cwd=None, env=None, quiet=False):
            calls.append((list(map(str, cmd)), cwd))
            return 0

        tree = dev.Tree(Path("repo"))
        with mock.patch.object(dev, "run", fake_run), mock.patch.object(dev, "app_python", lambda t: Path("py")), \
                quiet():
            code = dev.check_copier(tree, {"DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:5999/x"})
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(tuple(calls[0][0][-len(dev.COPIER_TESTS):]), dev.COPIER_TESTS)
        self.assertNotIn("-n", calls[0][0])
        self.assertEqual(calls[0][1], tree.backend)

    def test_without_a_postgres_url_it_fails_loudly_and_starts_nothing(self):
        for environ in ({}, {"DATABASE_URL": ""}, {"DATABASE_URL": "sqlite+aiosqlite:///x.db"}):
            with self.subTest(environ), mock.patch.object(dev, "run") as ran, \
                    mock.patch.object(dev.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(dev.Stop, "needs DATABASE_URL to name a running Postgres"):
                    dev.check_copier(dev.Tree(Path("repo")), environ)
                ran.assert_not_called()
                popen.assert_not_called()

    def test_the_command_line_exits_non_zero_with_a_plain_message_and_takes_no_other_options(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            with quiet() as (out, err):
                self.assertEqual(dev.main(["check", "--copier"]), 1)
            self.assertIn("DATABASE_URL", err.getvalue())
        with quiet() as (out, err):
            self.assertEqual(dev.main(["check", "--copier", "--backend"]), 1)
        self.assertIn("alone", err.getvalue())
        self.assertTrue(dev.parser().parse_args(["check", "--copier"]).copier)


class CommandLineTests(unittest.TestCase):
    def test_every_command_the_flow_promises_exists(self):
        top = dev.parser()
        commands = (["check"], ["shots", "#/board"], ["lock"])
        for argv in commands:
            self.assertEqual(top.parse_args(argv).command, argv[0])

    def test_check_takes_the_old_gates_options_and_the_runners_two_names(self):
        top = dev.parser()
        args = top.parse_args(["check", "--backend", "--scout", "tests/a.py"])
        self.assertTrue(args.backend and args.runner)
        self.assertTrue(top.parse_args(["check", "--runner"]).runner)
        self.assertEqual(args.paths, ["tests/a.py"])

    def test_a_mistake_in_the_options_is_one_sentence_and_status_1(self):
        with quiet() as (out, err):
            self.assertEqual(dev.main(["check", "--runner", "tests/a.py"]), 1)
        self.assertIn("drop --runner", err.getvalue())


class SidesRunAtOnceTests(unittest.TestCase):
    """`check` runs its sides (backend, runner, frontend, shell, scripts) at the same time, with one summary."""

    def fake_sides(self, fail=None, error=None):
        """Stand-ins for the five sides. Each waits at a barrier that only opens when all five are running, so a check
        that ran them one after another would time the barrier out and fail."""
        barrier = threading.Barrier(5, timeout=20)
        names = {"check_backend": 2, "check_runner": 1, "check_frontend": 3, "check_shell": 1, "check_scripts": 3}
        patches = []
        for function, steps in names.items():
            def side(gate, *args, function=function, steps=steps):
                barrier.wait()
                if error == function:
                    raise RuntimeError("a bug in this script")
                for number in range(steps):
                    gate.step(f"{function} {number}", lambda: 1 if fail == function else 0)

            patches.append(mock.patch.object(dev, function, side))
        return patches

    def run_check(self, **kwargs):
        patches = self.fake_sides(**kwargs)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with quiet() as (out, err):
                code = dev.check(dev.Tree(Path("repo")), [], [])
        return code, out.getvalue()

    def test_every_side_runs_while_the_others_run_and_the_summary_keeps_their_order(self):
        code, out = self.run_check()
        self.assertEqual(code, 0)
        listed = [" ".join(line.split()[1:3]) for line in out.splitlines() if line.startswith("    ok")]
        self.assertEqual(listed, [
            "check_backend 0", "check_backend 1", "check_runner 0", "check_frontend 0", "check_frontend 1",
            "check_frontend 2", "check_shell 0", "check_scripts 0", "check_scripts 1", "check_scripts 2"])

    def test_a_failed_side_fails_the_check_and_does_not_stop_the_others(self):
        code, out = self.run_check(fail="check_runner")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("FAILED"), 1)
        self.assertEqual(out.count("    ok"), 9)

    def test_a_bug_in_one_side_is_one_failed_step_and_the_others_still_finish(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, out = self.run_check(error="check_shell")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("FAILED"), 1)
        self.assertEqual(out.count("    ok"), 9)

    def test_a_side_run_beside_others_prints_each_line_of_its_commands_with_its_name(self):
        seen = io.StringIO()
        command = [sys.executable, "-c", "print('one'); print('two')"]

        def work():
            dev._LINES.prefix = "[side] "
            try:
                self.assertEqual(dev.run(command), 0)
            finally:
                dev._LINES.prefix = ""

        with contextlib.redirect_stdout(seen):
            thread = threading.Thread(target=work)
            thread.start()
            thread.join()
        self.assertEqual(seen.getvalue().splitlines(), ["[side] one", "[side] two"])


class RepositoryFileTests(unittest.TestCase):
    def test_the_workflow_runs_the_check_on_windows_and_no_longer_the_container_gate(self):
        text = (REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8")
        self.assertIn("windows-latest", text)
        for dearer in ("macos-latest",):
            self.assertNotIn(dearer, text)  # macOS costs ten times a Linux minute
        self.assertIn("app/scripts/dev.py check", text)
        self.assertIn("scripts/check_migrations.py", text)

    def test_the_scripts_name_no_ones_machine(self):
        for name in ("dev.py", "test_dev.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?<!\d)(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+(?:\.\d+)?", name)
            self.assertNotRegex(text, r"[A-Za-z]:[\\/]+Users[\\/]|(?<![\w.])/(?:Users|home|root)/", name)


if __name__ == "__main__":
    unittest.main()
