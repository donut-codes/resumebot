"""The background board sweep is started by name: `sourcing.start_board_sweep` asks the engine door, and the local door
runs one sweep at a time (plan lane `engine-calls-starts-b`). No network, no database, no model calls."""

import asyncio

from app.jobsearch.postings import sourced as sourcing

#: The real function, taken at import: the autouse fixture `no_background_board_sweeps` replaces the module's name in
#: each test.
REAL_START = sourcing.start_board_sweep


def test_the_board_sweep_is_started_by_asking_the_engine_door_by_name(fake_starts, monkeypatch):
    monkeypatch.setattr(sourcing, "_board_sweep", None)
    REAL_START("someone")
    assert fake_starts.starts == [("board_sweep", "someone")]
    assert sourcing._board_sweep is None, "with the door recording, nothing runs here"


async def test_the_local_door_runs_one_sweep_at_a_time(monkeypatch):
    started, release, ran = asyncio.Event(), asyncio.Event(), []

    async def safely(owner):
        ran.append(owner)
        started.set()
        await release.wait()
        return 0

    monkeypatch.setattr(sourcing, "_sweep_boards_safely", safely)
    monkeypatch.setattr(sourcing, "_board_sweep", None)
    REAL_START("someone")
    REAL_START("someone")
    await asyncio.wait_for(started.wait(), 5)
    assert ran == ["someone"], "asked while one runs, a second starts nothing"
    release.set()
    assert sourcing._board_sweep is not None
    await sourcing._board_sweep
    REAL_START("someone")
    assert sourcing._board_sweep is not None
    await sourcing._board_sweep
    assert ran == ["someone", "someone"], "once the first is over, the next ask starts a sweep"
