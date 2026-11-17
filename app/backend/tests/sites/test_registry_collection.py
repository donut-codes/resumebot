"""`dev.py check` collects the shipped tests and the restricted folder's in one pytest call, and `--shipped-only` leaves
the restricted folder out (the generic-sites design, 3.5 and 3.6). The commands are checked, not run."""

import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
dev = pytest.importorskip("dev")


def _commands(tmp_path, plan, *, restricted: bool):
    tree = dev.Tree(tmp_path)
    if restricted:
        (tree.backend / dev.RESTRICTED_TESTS).mkdir(parents=True)
    calls = []

    def fake_run(cmd, *, cwd=None, env=None, quiet=False):
        calls.append(list(map(str, cmd)))
        return 0

    with mock.patch.object(dev, "run", fake_run), mock.patch.object(dev, "app_python", lambda t: Path("py")):
        dev.check_backend(dev.Gate(), tree, plan)
    return calls


def test_both_trees_go_to_one_pytest_call_and_to_ruff_when_the_restricted_folder_is_there(tmp_path):
    ruff, _, pytest_app, _ = _commands(tmp_path, dev.plan_check(["backend"], []), restricted=True)
    assert ruff[-4:] == ["check", "app", "tests", "restricted_runbooks"]
    assert pytest_app[-2:] == ["tests", dev.RESTRICTED_TESTS]
    assert "-n" in pytest_app and pytest_app.count("pytest") == 1


def test_shipped_only_leaves_the_restricted_folder_out_of_both(tmp_path):
    plan = dev.plan_check(["backend"], [], shipped_only=True)
    ruff, _, pytest_app, _ = _commands(tmp_path, plan, restricted=True)
    assert ruff[-3:] == ["check", "app", "tests"]
    assert dev.RESTRICTED_TESTS not in pytest_app and pytest_app[-1] != "tests"


def test_a_tree_with_no_restricted_folder_runs_as_it_always_did(tmp_path):
    ruff, _, pytest_app, _ = _commands(tmp_path, dev.plan_check(["backend"], []), restricted=False)
    assert ruff[-3:] == ["check", "app", "tests"]
    assert dev.RESTRICTED_TESTS not in pytest_app


def test_a_restricted_test_path_is_a_backend_test_path_unless_shipped_only_was_asked():
    plan = dev.plan_check([], ["app/backend/restricted_runbooks/tests/test_x.py"])
    assert plan.py_paths == ["restricted_runbooks/tests/test_x.py"] and plan.app_tests
    with pytest.raises(dev.Stop, match="restricted"):
        dev.plan_check([], ["restricted_runbooks/tests/test_x.py"], shipped_only=True)
