"""The engine's side of the bell (`work/bell.py`): a whitelisted name runs the real call, anything else runs nothing.
The engine functions are replaced by recorders. Everything is made up."""

import asyncio
import uuid

import pytest

from app import notice
from app.jobsearch.loops import schedule
from app.jobsearch.work import bell, dispatcher, engine_calls, wake
from app.jobsearch.work.queue import consume
from app.sites import wake_ops
from app.sites.agent_calls import Wake


@pytest.fixture
def knocked(monkeypatch):
    """What `wake.knock` was asked, as (id, reason)."""
    seen: list[tuple[str, str]] = []

    async def knock(run_id, reason=wake.SEARCH_STARTED):
        seen.append((run_id, reason))
        return True

    monkeypatch.setattr(wake, "knock", knock)
    return seen


async def test_look_wakes_the_dispatchers_waiters():
    event = asyncio.Event()
    consume.listen(event)
    try:
        await bell.handle("look", {})
        assert event.is_set()
    finally:
        consume.forget(event)


async def test_a_dispatch_bell_runs_one_pass_of_the_dispatcher(monkeypatch):
    seen = []

    async def dispatch_now(owner=None):
        seen.append(owner)
        return 1

    monkeypatch.setattr(dispatcher, "dispatch_now", dispatch_now)
    await bell.handle("dispatch", {})
    assert seen == [None]


async def test_a_knock_calls_wake_knock_with_the_id_and_the_reason(knocked):
    await bell.handle("knock", {"id": "r1", "reason": "gaps_asked"})
    assert knocked == [("r1", "gaps_asked")]


@pytest.mark.parametrize("fields", [
    {}, {"id": "r1"}, {"reason": "gaps_asked"}, {"id": "r1", "reason": "nonsense"},
    {"id": "r1", "reason": ["gaps_asked"]}, {"id": 7, "reason": "gaps_asked"}, {"id": "", "reason": "gaps_asked"},
])
async def test_a_knock_with_a_missing_or_unknown_part_knocks_nobody(knocked, fields):
    await bell.handle("knock", fields)
    assert knocked == []


async def test_knocks_call_the_schedules_send_knocks_with_the_pairs(monkeypatch):
    seen = []

    async def send_knocks(pairs):
        seen.append(pairs)

    monkeypatch.setattr(schedule, "send_knocks", send_knocks)
    await bell.handle("knocks", {"pairs": [["k1", "search_started"], ["k2", "gaps_asked"]]})
    assert seen == [[("k1", "search_started"), ("k2", "gaps_asked")]]


@pytest.mark.parametrize("fields", [
    {}, {"pairs": []}, {"pairs": "k1"}, {"pairs": [["k1", "bogus"]]},
    {"pairs": [["k1", "search_started"], ["k2", "bogus"]]}, {"pairs": [["k1"]]}, {"pairs": [["", "search_started"]]},
    {"pairs": [[f"k{n}", "search_started"] for n in range(engine_calls.KNOCKS_PER_BELL + 1)]},
])
async def test_knocks_that_are_not_what_the_page_rings_run_nothing(monkeypatch, fields):
    seen = []

    async def send_knocks(pairs):
        seen.append(pairs)

    monkeypatch.setattr(schedule, "send_knocks", send_knocks)
    await bell.handle("knocks", fields)
    assert seen == []


async def test_wakes_call_wake_ops_send_all_with_a_wake_for_each_project(monkeypatch):
    seen = []

    async def send_all(wakes):
        seen.append(list(wakes))
        return []

    monkeypatch.setattr(wake_ops, "send_all", send_all)
    ids = [str(uuid.UUID(int=1)), str(uuid.UUID(int=2))]
    await bell.handle("wakes", {"ids": ids})
    assert [project_id for project_id, _ in seen[0]] == ids
    assert all(one is not None for _, one in seen[0])


@pytest.mark.parametrize("fields", [
    {}, {"ids": []}, {"ids": "x"}, {"ids": ["not-a-uuid"]}, {"ids": [7]},
    {"ids": [str(uuid.UUID(int=n)) for n in range(1, engine_calls.WAKES_PER_BELL + 2)]},
])
async def test_wakes_that_are_not_what_the_page_rings_run_nothing(monkeypatch, fields):
    seen = []

    async def send_all(wakes):
        seen.append(list(wakes))
        return []

    monkeypatch.setattr(wake_ops, "send_all", send_all)
    await bell.handle("wakes", fields)
    assert seen == []


@pytest.mark.parametrize("name", ["explode", "", "look ", "LOOK", "start:nothing", "start:", "knock:r1"])
async def test_a_name_that_is_not_on_the_list_is_ignored(knocked, name):
    await bell.handle(name, {"id": "r1", "reason": "gaps_asked"})
    assert knocked == []


async def test_a_bell_with_fields_that_are_not_a_dict_does_not_raise(knocked):
    await bell.handle("knock", None)
    await bell.handle("knock", "r1")  # type: ignore[arg-type]
    assert knocked == []


async def test_a_call_that_fails_is_logged_and_does_not_raise(monkeypatch, caplog):
    async def boom(run_id, reason=wake.SEARCH_STARTED):
        raise RuntimeError("the scout is down")

    monkeypatch.setattr(wake, "knock", boom)
    await bell.handle("knock", {"id": "r1", "reason": "gaps_asked"})
    assert "bell 'knock' failed" in caplog.text


def test_the_bell_names_are_exactly_these():
    assert set(bell.HANDLERS) == {"look", "dispatch", "knock", "knocks", "wakes"}


async def test_every_bell_the_page_rings_has_a_handler_in_the_engine(monkeypatch):
    rung: list[str] = []
    monkeypatch.setattr(notice, "ring_engine", lambda kind, **fields: rung.append(kind))
    remote = engine_calls.Remote()
    await remote.knock("r", "gaps_asked")
    await remote.dispatch_now()
    await remote.knocks([("k", "search_started")])
    await remote.send_wakes([("p", Wake("r", "m"))])
    remote.nudge()
    assert set(rung) == set(bell.HANDLERS)
