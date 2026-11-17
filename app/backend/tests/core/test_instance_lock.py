"""One app server per database (`app.instance_lock`, taken first in `main.lifespan`). The lock is an operating system
lock, and the system keeps two handles on one file apart even inside one process, so a second `hold` here is honestly a
second copy; one test uses a real second process for the claim that a crash leaves no lock behind."""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app import db, instance_lock
from app.jobsearch import main
from app.jobsearch.loops import startup
from app.settings import settings


def _url(folder: Path, name: str = "app.db") -> str:
    return db.sqlite_url(folder / name)


def test_a_second_copy_on_the_same_database_is_refused_and_told_who_has_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app.jobsearch.main:app", "--port", "8449"])
    with instance_lock.hold(_url(tmp_path)):
        with pytest.raises(instance_lock.AlreadyRunning) as refused:
            with instance_lock.hold(_url(tmp_path)):
                pytest.fail("the second copy got in")
    text = str(refused.value)
    assert "already running on this database" in text
    assert "Stop that copy first" in text
    assert text.count("pid") == 1, "the holder's pid is named"
    assert "http://127.0.0.1:8449" in text, "and its address"


def test_the_database_can_be_taken_again_once_the_first_copy_has_let_go(tmp_path):
    with instance_lock.hold(_url(tmp_path)):
        pass
    with instance_lock.hold(_url(tmp_path)):
        pass


def test_a_copy_that_is_refused_does_not_let_go_of_the_first_copys_lock(tmp_path):
    with instance_lock.hold(_url(tmp_path)):
        with pytest.raises(instance_lock.AlreadyRunning), instance_lock.hold(_url(tmp_path)):
            pass
        with pytest.raises(instance_lock.AlreadyRunning), instance_lock.hold(_url(tmp_path)):
            pass


def test_two_different_databases_do_not_block_each_other(tmp_path):
    with instance_lock.hold(_url(tmp_path, "one.db")), instance_lock.hold(_url(tmp_path, "two.db")):
        pass


def test_the_same_file_by_another_spelling_is_the_same_database(tmp_path):
    (tmp_path / "sub").mkdir()
    with instance_lock.hold(_url(tmp_path)):
        with pytest.raises(instance_lock.AlreadyRunning), instance_lock.hold(_url(tmp_path / "sub" / "..")):
            pass


def test_a_database_that_is_no_file_is_not_locked():
    with instance_lock.hold("sqlite+aiosqlite://"), instance_lock.hold("sqlite+aiosqlite://"):
        pass


def test_a_copy_that_dies_leaves_no_lock_behind(tmp_path):
    """The system frees the lock when the holder ends, however it ends: nothing stale to clear by hand."""
    url = _url(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(Path(instance_lock.__file__).parents[1])!r})
            from app import instance_lock
            with instance_lock.hold({url!r}):
                print("held", os.getpid(), flush=True)
                sys.stdin.read()
            """)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    pid = ""
    try:
        word, pid = holder.stdout.readline().split()
        assert word == "held"
        with pytest.raises(instance_lock.AlreadyRunning) as refused:
            with instance_lock.hold(url):
                pass
        assert f"pid {pid}" in str(refused.value)
    finally:
        # Ended outright, as a crash would: the holder's own pid (on Windows the venv's launcher is the parent of it).
        if pid:
            os.kill(int(pid), signal.SIGTERM)
        holder.wait()
    with instance_lock.hold(url):
        pass


def _raise(refused):
    raise refused


def test_a_refused_start_says_why_once_and_ends_non_zero_without_a_traceback(tmp_path, monkeypatch, capsys):
    ended: list[int] = []

    def ends(code):
        ended.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(main.os, "_exit", ends)
    with pytest.raises(SystemExit):
        main._stop_refused(instance_lock.AlreadyRunning("The app is already running on this database."))
    assert ended == [1]
    assert capsys.readouterr().err == "The app is already running on this database.\n"


async def test_a_refused_start_runs_no_start_up_step_and_opens_no_database(tmp_path, monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(settings, "database_url", _url(tmp_path))
    monkeypatch.setattr(startup, "configure_logging", lambda: ran.append("logging"))
    monkeypatch.setattr(db, "configure", lambda url=None: ran.append("configure"))
    monkeypatch.setattr(db, "init_schema", lambda: ran.append("schema"))
    monkeypatch.setattr(main, "_stop_refused", _raise)  # the real one ends the process
    with instance_lock.hold(_url(tmp_path)):
        with pytest.raises(instance_lock.AlreadyRunning):
            async with main.lifespan(main.app):
                ran.append("served")
    assert ran == []
    assert not (tmp_path / "app.db").exists(), "the database file was not created by the refused copy"
