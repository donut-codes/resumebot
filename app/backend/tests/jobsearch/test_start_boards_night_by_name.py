"""The nightly board read is started by name: `boards_night.start_in_background` asks the engine door, and the local
door runs the one task (plan lane `engine-calls-starts-b`). No network, no database, no model calls."""

import asyncio
import uuid

from app.jobsearch.missions import boards_night

#: The real function, taken at import: the autouse fixture `no_background_reads` replaces the name in each test.
REAL_START = boards_night.start_in_background


def test_a_board_read_is_started_by_asking_the_engine_door_by_name(fake_starts):
    run_id = uuid.uuid4()
    REAL_START(run_id, "someone", 7)
    REAL_START(run_id, "someone", 7, True)
    assert fake_starts.starts == [("boards_night", (run_id, "someone", 7, False)),
                                  ("boards_night", (run_id, "someone", 7, True))]
    assert not boards_night._tasks, "with the door recording, nothing runs here"


async def test_the_local_door_runs_the_board_read_once_with_what_it_was_asked(monkeypatch):
    ran = []

    async def finish(entry_id, owner, details_per_night, resume=False):
        ran.append((entry_id, owner, details_per_night, resume))

    monkeypatch.setattr(boards_night, "_finish", finish)
    run_id = uuid.uuid4()
    REAL_START(run_id, "someone", 7, True)
    assert len(boards_night._tasks) == 1, "the task is kept, so `stop_all` can cancel it"
    await asyncio.gather(*list(boards_night._tasks))
    assert ran == [(run_id, "someone", 7, True)]
    assert not boards_night._tasks
