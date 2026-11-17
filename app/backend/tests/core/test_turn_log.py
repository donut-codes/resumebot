"""The turn-wait log: off unless RESUMEBOT_TURN_LOG=1, one
line per writer turn in the data folder's logs, a summary that reads them back. Everything is made up."""

import asyncio
import datetime
import uuid

import pytest

from app import db, turn_log
from app.errors import Busy
from tests.core.test_sqlite_writer_turn import ADD, engine, quick_engine  # noqa: F401 - fixtures

NOW = datetime.datetime(2030, 1, 2, 3, 4, 5, 678000, tzinfo=datetime.UTC)


def test_a_turn_is_one_line_in_the_file_of_its_utc_day(tmp_path):
    log = turn_log.TurnLog(tmp_path / "logs", clock=lambda: NOW)
    log.record(0.25, 0.5)
    log.record(1.5, None)
    lines = (tmp_path / "logs" / "turn-waits-2030-01-02.log").read_text(encoding="utf-8").splitlines()
    assert lines == ["2030-01-02T03:04:05.678Z wait=0.2500 hold=0.5000", "2030-01-02T03:04:05.678Z wait=1.5000 busy"]


def test_a_failing_write_is_dropped(tmp_path):
    blocked = tmp_path / "logs"
    blocked.write_text("a file, not a folder", encoding="utf-8")
    turn_log.TurnLog(blocked, clock=lambda: NOW).record(0.1, 0.1)  # raises nothing


def test_nothing_is_logged_when_the_variable_is_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RESUMEBOT_TURN_LOG", raising=False)
    before = list(db.turn_ends)
    turn_log.install_from_environment()
    assert db.turn_ends == before


def test_the_variable_turns_it_on_in_the_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RESUMEBOT_TURN_LOG", "1")
    before = list(db.turn_ends)
    turn_log.install_from_environment()
    try:
        assert len(db.turn_ends) == len(before) + 1
        db.turn_ends[-1](0.01, 0.02)
        assert list((tmp_path / "logs").glob("turn-waits-*.log"))
    finally:
        db.turn_ends[:] = before


async def test_a_real_turn_and_a_refused_one_are_logged(tmp_path, quick_engine):  # noqa: F811
    stop = turn_log.install(tmp_path / "logs", clock=lambda: NOW)
    try:
        async def waiting() -> None:
            async with db.session_factory()() as session:
                await session.execute(ADD, {"body": "never"})

        async with db.session_factory()() as holder:
            await holder.execute(ADD, {"body": "held"})
            with pytest.raises(Busy):
                await asyncio.create_task(waiting())
            await holder.commit()
    finally:
        stop()
    assert db.turn_ends == []  # stopping took the watcher out
    lines = (tmp_path / "logs" / "turn-waits-2030-01-02.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0].endswith("busy") and "hold=" in lines[1]
    assert float(lines[0].split()[1].split("=")[1]) >= 0.09  # the refused one waited its limit


def test_the_summary_gives_count_percentiles_over_one_second_busy_and_the_longest_hold():
    lines = [f"t wait={w} hold={h}" for w, h in ((0.1, 0.2), (0.2, 0.9), (2.0, 0.3))] + ["t wait=5.0 busy", "junk"]
    got = turn_log.summarise(lines)
    assert got == {"count": 4, "p50": 0.2, "p99": 5.0, "max": 5.0, "over_1s": 2, "busy": 1, "max_hold": 0.9}


def test_the_command_prints_the_summary_of_its_files(tmp_path, capsys):
    one = tmp_path / "a.log"
    one.write_text("t wait=0.1 hold=0.2\nt wait=0.3 busy\n", encoding="utf-8")
    assert turn_log.main([str(one)]) == 0
    out = capsys.readouterr().out
    assert "count 2" in out and "busy 1" in out and "max hold 0.2000 s" in out


def test_a_slow_turn_names_the_place_that_took_it_and_a_quick_one_does_not(tmp_path):
    log = turn_log.TurnLog(tmp_path / "logs", clock=lambda: NOW)
    db.ending_place = "app.somewhere:do_work:42"
    log.record(0.1, 1.5)
    log.record(1.5, None)
    log.record(0.1, 0.9)
    db.ending_place = None
    lines = (tmp_path / "logs" / "turn-waits-2030-01-02.log").read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith("hold=1.5000 at=app.somewhere:do_work:42")
    assert lines[1].endswith("busy at=app.somewhere:do_work:42")
    assert lines[2].endswith("hold=0.9000")


async def test_the_place_is_the_first_frame_outside_the_database_code(tmp_path, quick_engine):  # noqa: F811
    seen: list[str | None] = []
    db.turn_ends.append(lambda _w, _h: seen.append(db.ending_place))
    try:
        async def takes_the_turn() -> None:
            async with db.session_factory()() as session:
                await session.execute(ADD, {"body": "x"})
                await session.commit()

        await takes_the_turn()
    finally:
        db.turn_ends.pop()
    assert seen and seen[0] is not None and seen[0].endswith(":takes_the_turn:" + seen[0].rsplit(":", 1)[1])
    assert "test_turn_log" in seen[0]


def test_the_summary_still_reads_a_line_that_carries_a_place():
    got = turn_log.summarise(["t wait=0.1 hold=1.5 at=a.b:c:1", "t wait=2.0 busy at=a.b:c:1"])
    assert got["count"] == 2 and got["busy"] == 1 and got["max_hold"] == 1.5


async def test_a_turn_begun_by_the_row_lock_names_the_caller_of_the_lock(js):
    """A long hold must not be logged at `app.jobsearch.as_read:lock`, the helper every pass calls, which names no pass.
    The place skips that file as it skips this one."""
    from app.jobsearch import as_read
    from app.jobsearch.models import Opportunity

    seen: list[str | None] = []
    db.turn_ends.append(lambda _w, _h: seen.append(db.ending_place))
    try:
        async def the_pass_that_locks() -> None:
            async with db.session_factory()() as session:
                await as_read.lock(session, Opportunity, [uuid.UUID(int=1)])  # its first statement begins the turn
                await session.commit()

        await the_pass_that_locks()
    finally:
        db.turn_ends.pop()
    assert seen and seen[0] is not None
    assert ":the_pass_that_locks:" in seen[0] and "as_read" not in seen[0], seen[0]
