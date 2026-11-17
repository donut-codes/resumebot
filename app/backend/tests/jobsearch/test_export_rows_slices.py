"""The tracker export builds its rows in slices and yields between them; the turn log makes its folder once.
Everything is made up."""

import asyncio
import datetime

from app import turn_log
from app.jobsearch import stages
from app.jobsearch.imports import tracker

ROWS = tracker.EXPORT_SLICE * 2 + 7


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, kept, dropped):
        self.answers = [kept, dropped]

    async def execute(self, _statement):
        return _Result(self.answers.pop(0))


def _kept(n):
    status_at = tracker.COLUMNS.index("status")
    rows = []
    for i in range(n):
        values = [f"v{i}-{c}" for c in tracker.COLUMNS]
        values[status_at] = stages.GATED_OUT if i % 2 else next(iter(stages.LABELS))
        rows.append(tuple(values))
    return rows


def test_sliced_rows_equal_the_unsliced_rows_and_the_loop_is_yielded_to():
    kept = _kept(ROWS)
    dropped = [tuple(f"d{i}-{j}" for j in range(8)) for i in range(ROWS)]
    status_at = tracker.COLUMNS.index("status")
    want_kept = []
    for values in kept:
        row = [tracker._cell(v) for v in values]
        row[status_at] = stages.LABELS.get(values[status_at], values[status_at])
        want_kept.append(row)
    want_dropped = [[tracker._cell(v) for v in values] for values in dropped]

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    async def run():
        task = asyncio.create_task(ticker())
        out = await tracker.export_rows(_Session(kept, dropped), "owner")
        task.cancel()
        return out

    got_kept, got_dropped = asyncio.run(run())
    assert got_kept == want_kept
    assert got_dropped == want_dropped
    assert ticks >= 6  # three slices of each sheet, each yielding once


def test_the_turn_log_makes_its_folder_once_and_retries_after_a_failure(tmp_path, monkeypatch):
    folder = tmp_path / "logs"
    now = datetime.datetime(2030, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    log = turn_log.TurnLog(folder, clock=lambda: now)
    calls = []
    real = type(folder).mkdir

    def counting(self, *a, **k):
        calls.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(type(folder), "mkdir", counting)
    log.record(0.1, 0.1)
    log.record(0.2, 0.2)
    assert len(calls) == 1
    assert len((folder / "turn-waits-2030-01-02.log").read_text(encoding="utf-8").splitlines()) == 2

    blocked = tmp_path / "blocked"
    blocked.write_text("a file", encoding="utf-8")
    again = turn_log.TurnLog(blocked, clock=lambda: now)
    calls.clear()
    again.record(0.1, 0.1)
    again.record(0.1, 0.1)
    assert len(calls) == 2  # a failure sets nothing, so each line tries again
