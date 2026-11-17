"""The local instance's logs are set aside while it runs, on the rule its start uses (app/scripts/local/stack.py
`rotate`: over the limit, three kept, `<name>.1` the newest).

The rule ran only at a start and a stop, so an app that ran for days wrote one app.log without limit. Now the
supervisor looks at the logs as it runs (`roll`), and a process whose output is one of them starts a fresh file once
its own was set aside (`follow`): its lines go to the file set aside until it does, so none is lost and no write
waits for the move. On Windows a file can be renamed under the process writing it only when it was opened to allow
that (`open_log`). The launcher is not in the backend's image, so these skip there. Everything is made up; the limit
is passed in and nothing waits."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

STACK = Path(__file__).resolve().parents[3] / "scripts" / "local" / "stack.py"

pytestmark = pytest.mark.skipif(not STACK.exists(), reason="the launcher is not in this image")

LIMIT, KEEP = 10, 3


@pytest.fixture(scope="module")
def stack():
    spec = importlib.util.spec_from_file_location("resumebot_stack_for_log_rotation", STACK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def held(stack, tmp_path):
    """app.log, and the descriptor a running app holds as its output; closed at the end."""
    log = tmp_path / "app.log"
    out = stack.open_log(log)
    yield log, out
    os.close(out)


def names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


def test_a_log_written_past_the_limit_while_running_is_set_aside_and_three_old_ones_stay(stack, tmp_path, held):
    log, out = held
    for run in range(1, 6):
        os.write(out, b"run %d, a line past the limit\n" % run)
        stack.roll(tmp_path, limit=LIMIT, keep=KEEP)
        assert not log.exists(), "set aside though the app still holds it open"
        os.write(out, b"run %d, before the app looked\n" % run)
        assert stack.follow(log, (out,)), "the app starts a fresh log"
    os.write(out, b"the newest line\n")
    assert names(tmp_path) == ["app.log", "app.log.1", "app.log.2", "app.log.3"]
    assert log.read_bytes() == b"the newest line\n"
    kept = [(tmp_path / f"app.log.{number}").read_bytes() for number in (1, 2, 3)]
    assert kept == [b"run %d, a line past the limit\nrun %d, before the app looked\n" % (run, run) for run in (5, 4, 3)]


def test_a_log_under_the_limit_stays_and_the_app_goes_on_writing_it(stack, tmp_path, held):
    log, out = held
    os.write(out, b"short\n")
    stack.roll(tmp_path, limit=LIMIT, keep=KEEP)
    assert not stack.follow(log, (out,)), "its log is still its log"
    os.write(out, b"more\n")
    assert names(tmp_path) == ["app.log"]
    assert log.read_bytes() == b"short\nmore\n", "byte for byte, each write at the end"


def test_the_lanes_logs_are_set_aside_too_and_nothing_else_in_the_folder_is_touched(stack, tmp_path):
    long = "a line past the limit\n"
    for name in ("lanes.log", "runs-writer.log", "nightly-check-2026-01-02.md", "db-snapshot-20260102-030405.txt"):
        (tmp_path / name).write_text(long, encoding="utf-8")
    stack.roll(tmp_path, limit=LIMIT, keep=KEEP)
    # The lanes open, write and close for each line, so their next line starts the fresh file by itself.
    assert names(tmp_path) == ["db-snapshot-20260102-030405.txt", "lanes.log.1", "nightly-check-2026-01-02.md",
                               "runs-writer.log.1"]


def test_a_log_that_cannot_be_moved_is_left_with_its_older_ones_and_set_aside_at_a_later_look(
        stack, tmp_path, monkeypatch):
    log = tmp_path / "app.log"
    log.write_text("a line past the limit\n", encoding="utf-8")
    for number in (1, 2, 3):
        (tmp_path / f"app.log.{number}").write_text(f"old {number}", encoding="utf-8")
    other = tmp_path / "lanes.log"
    other.write_text("a line past the limit\n", encoding="utf-8")
    move = Path.replace

    def refused(self, target):  # what Windows answers while something holds the file open and forbids the move
        if self == log:
            raise PermissionError(13, "held open", str(self))
        return move(self, target)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "replace", refused)
        with pytest.raises(PermissionError):
            stack.rotate(log, limit=LIMIT, keep=KEEP)
        stack.roll(tmp_path, limit=LIMIT, keep=KEEP)  # a look while running goes on to the other logs
    assert [(tmp_path / f"app.log.{number}").read_text(encoding="utf-8") for number in (1, 2, 3)] == [
        "old 1", "old 2", "old 3"], "no older log was moved or lost for a move that did not happen"
    assert log.exists() and not other.exists() and (tmp_path / "lanes.log.1").exists()
    stack.roll(tmp_path, limit=LIMIT, keep=KEEP)
    assert (tmp_path / "app.log.1").read_text(encoding="utf-8") == "a line past the limit\n"
    assert (tmp_path / "app.log.3").read_text(encoding="utf-8") == "old 2"


def test_a_pass_that_was_cut_short_is_finished_by_the_next_one(stack, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("new", encoding="utf-8")  # under the limit
    (tmp_path / "app.log.0").write_text("moved, then the process was ended", encoding="utf-8")
    (tmp_path / "app.log.1").write_text("old 1", encoding="utf-8")
    stack.rotate(log, limit=LIMIT, keep=KEEP)
    assert names(tmp_path) == ["app.log", "app.log.1", "app.log.2"]
    assert (tmp_path / "app.log.1").read_text(encoding="utf-8") == "moved, then the process was ended"
    assert (tmp_path / "app.log.2").read_text(encoding="utf-8") == "old 1"


def test_only_the_streams_that_write_the_log_follow_it(stack, tmp_path, held):
    log, out = held
    elsewhere = os.open(tmp_path / "elsewhere.txt", os.O_WRONLY | os.O_CREAT)
    try:
        assert stack.writing(log, (out, elsewhere)) == (out,)
    finally:
        os.close(elsewhere)


def test_the_app_looks_for_its_log_every_few_seconds_until_it_ends(stack, tmp_path, held):
    log, out = held
    waited: list[float] = []

    def wait(seconds: float) -> bool:
        """The time between two looks: the log is set aside during the first, the app ends in the third."""
        waited.append(seconds)
        if len(waited) == 1:
            os.write(out, b"a line past the limit\n")
            stack.roll(tmp_path, limit=LIMIT, keep=KEEP)
        if len(waited) == 2:
            os.write(out, b"after the look\n")
        return len(waited) == 3

    stack.keep_following(log, (out,), wait)
    assert waited == [stack.FOLLOW_EVERY] * 3
    assert (tmp_path / "app.log.1").read_bytes() == b"a line past the limit\n"
    assert log.read_bytes() == b"after the look\n"


def test_a_look_that_fails_does_not_end_the_looking(stack, tmp_path, held, monkeypatch):
    log, out = held
    looks = iter([OSError("the disk is full"), None])

    def follow(path, fds):
        found = next(looks)
        if found:
            raise found

    monkeypatch.setattr(stack, "follow", follow)
    turns = iter([False, False, True])
    stack.keep_following(log, (out,), lambda seconds: next(turns))
    assert next(looks, "both looks were made") == "both looks were made"


def test_a_program_is_started_as_the_server_was_through_the_launcher_that_follows_its_log(stack, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("", encoding="utf-8")
    cfg = stack.Config(tmp_path, {})
    command = stack.program_command(cfg, "page", "app.jobsearch.page:app", cfg.port)
    assert command[:4] == [str(cfg.python), str(STACK), "serve", str(cfg.logs / "page.log")]
    server = ["app.jobsearch.page:app", "--host", "127.0.0.1", "--port", "8430", "--no-server-header"]
    assert command[4:] == server
    ran: list[tuple] = []
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stack.runpy, "run_module",
                        lambda name, **how: ran.append((name, how, list(sys.argv), sys.path[0])))
    assert stack.main(command[2:]) == 0
    conf = cfg.logs / "page.logconfig.json"  # the app's one log format, handed to uvicorn (app/log_setup.py)
    assert ran == [("uvicorn", {"run_name": "__main__", "alter_sys": True},
                    ["uvicorn", *server, "--log-config", str(conf)], str(tmp_path))], (
        "what `python -m uvicorn` with the same words and the log config runs, from the folder it is started in")
    assert "app.log_setup.LogFormatter" in conf.read_text(encoding="utf-8")
