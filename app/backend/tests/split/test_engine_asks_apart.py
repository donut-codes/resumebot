"""A slow ask (a re-gate takes minutes) does not hold the page's other asks: the dispatcher runs each ask apart, one run
answers up to two at once, an ask is answered once, and two re-gates for one owner still wait for each other on the
engine's named lock. Every name is made up; waits are events, not sleeps."""

import asyncio
import json
from datetime import UTC, datetime

from app import db
from app.jobsearch import facts
from app.jobsearch.gating import regate
from app.jobsearch.work import engine_asks, queue
from app.jobsearch.work.queue import loop, topics
from tests.support import OWNER

SLOW, QUICK = "s1s2s3", "q1q2q3"


def _now() -> datetime:
    return datetime.now(UTC)


async def _ask(ask_id: str, name: str) -> None:
    text = json.dumps({"name": name, "args": {}, "asked_at": _now().isoformat()})
    async with db.session_factory()() as session:
        await facts.put(session, OWNER, f"ask:{ask_id}", text, now=_now())
        queue.post(session, OWNER, topics.ENGINE_ASK, [ask_id], cause=topics.ASK, now=_now())
        await session.commit()


async def _answer(ask_id: str) -> str | None:
    async with db.read_session_factory()() as reader:
        return await facts.get(reader, OWNER, f"answer:{ask_id}")


def _registered(monkeypatch, slow_started: asyncio.Event, release: asyncio.Event, calls: list[str]) -> None:
    monkeypatch.setattr(engine_asks, "_asks", {})

    async def slow(session, owner, args):
        calls.append("slow")
        slow_started.set()
        await release.wait()
        return {"done": "slow"}

    async def quick(session, owner, args):
        calls.append("quick")
        return {"done": "quick"}

    engine_asks.register("test.slow", slow)
    engine_asks.register("test.quick", quick)


async def test_quick_ask_not_held_behind_slow_ask(js, monkeypatch):
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    _registered(monkeypatch, started, release, calls)
    d = loop.Dispatcher(OWNER, only=frozenset({topics.ENGINE_ASK}), gaps=False)
    await _ask(SLOW, "test.slow")
    first = await d.dispatch()
    await asyncio.wait_for(started.wait(), 5)
    await _ask(QUICK, "test.quick")
    second = await d.dispatch()
    assert len(second.tasks) == 1, "the quick ask is started while the slow one runs"
    await asyncio.wait_for(asyncio.gather(*second.tasks), 5)
    assert json.loads(await _answer(QUICK) or "null") == {"done": "quick"}
    assert await _answer(SLOW) is None, "the slow ask is still waiting"
    release.set()
    await asyncio.wait_for(asyncio.gather(*first.tasks), 5)
    assert json.loads(await _answer(SLOW) or "null") == {"done": "slow"}
    assert d.in_flight == {}


async def test_one_run_of_several_asks_does_not_hold_the_quick_one_behind_the_slow_one(js, monkeypatch):
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    _registered(monkeypatch, started, release, calls)
    await _ask(SLOW, "test.slow")
    await _ask(QUICK, "test.quick")
    run = asyncio.create_task(engine_asks.answer_due(OWNER, [topics.ANY], _now()))
    await asyncio.wait_for(started.wait(), 5)
    for _ in range(200):
        if await _answer(QUICK) is not None:
            break
        await asyncio.sleep(0)
    assert await _answer(QUICK) is not None, "answered while the slow ask still waits"
    release.set()
    assert await asyncio.wait_for(run, 5) == 2


async def test_each_ask_answered_once(js, monkeypatch):
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    _registered(monkeypatch, started, release, calls)
    await _ask(SLOW, "test.slow")
    await _ask(QUICK, "test.quick")
    star = asyncio.create_task(engine_asks.answer_due(OWNER, [topics.ANY], _now()))
    await asyncio.wait_for(started.wait(), 5)
    again = await engine_asks.answer_due(OWNER, [SLOW], _now())  # the ask's own to-do, while the sweep runs it
    assert again == 0, "an ask being answered is left to the run that has it"
    release.set()
    await asyncio.wait_for(star, 5)
    assert sorted(calls) == ["quick", "slow"]
    assert engine_asks._answering == set()


async def test_two_regates_for_one_owner_stay_serial(js):
    inside, leave, order = asyncio.Event(), asyncio.Event(), []

    async def regating(name: str, hold: bool) -> None:
        async with db.session_factory()() as session:
            async with regate._one_at_a_time(session, OWNER):
                order.append(f"{name} in")
                if hold:
                    inside.set()
                    await leave.wait()
                order.append(f"{name} out")

    first = asyncio.create_task(regating("first", True))
    await asyncio.wait_for(inside.wait(), 5)
    second = asyncio.create_task(regating("second", False))
    for _ in range(50):
        await asyncio.sleep(0)
    assert order == ["first in"], "the second waits on the owner's lock"
    leave.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert order == ["first in", "first out", "second in", "second out"]
