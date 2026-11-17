"""Go begins its task through the engine door by name (`engine_calls.start("go", owner)`), not with a `create_task` of
its own (`browser/go.py`). Where the local door is installed Go runs as it always did; a door that only records starts
runs nothing."""

import asyncio

from app.jobsearch.browser import go as apply_go
from tests.support import OWNER


async def test_the_local_door_runs_go_once_for_the_owner(monkeypatch):
    calls: list[str] = []

    async def fake_go(owner, **kw):
        calls.append(owner)

    monkeypatch.setattr(apply_go, "go", fake_go)
    await apply_go.start(OWNER)
    await asyncio.wait_for(apply_go._tasks[OWNER], 5)
    assert calls == [OWNER]


async def test_a_recording_door_gets_one_start_named_go_and_runs_nothing(monkeypatch, fake_starts):
    calls: list[str] = []

    async def fake_go(owner, **kw):
        calls.append(owner)

    monkeypatch.setattr(apply_go, "go", fake_go)
    answer = await apply_go.start(OWNER)
    await asyncio.sleep(0)
    assert fake_starts.starts == [("go", OWNER)]
    assert calls == [] and OWNER not in apply_go._tasks
    assert answer["phase"] == "checking" and answer["done"] is False
