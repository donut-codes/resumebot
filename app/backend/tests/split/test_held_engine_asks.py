"""A held engine (the schedule off) still answers the page's asks: its dispatcher takes the topic `ask` and no other.
Every name here is made up."""

import json
from datetime import UTC, datetime

from app import db
from app.jobsearch import facts, main
from app.jobsearch.work import engine_asks, queue
from app.jobsearch.work.queue import handlers, loop, topics
from app.settings import settings
from app.timeutil import iso_utc
from tests.jobsearch.work_queue.support import posted_now
from tests.support import OWNER, pin_clock

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
ID = "h1h2h3"


async def test_a_dispatcher_told_to_take_only_asks_has_that_one_handler():
    held = loop.Dispatcher(OWNER, only=frozenset({topics.ENGINE_ASK}))
    assert set(held.topics()) == {topics.ENGINE_ASK}
    assert held.swept() == (topics.ENGINE_ASK,)


async def test_a_dispatcher_with_no_limit_takes_every_topic_as_before():
    assert set(loop.Dispatcher(OWNER).topics()) == set(handlers.HANDLERS)


async def test_the_engine_starts_the_ask_only_queue_when_held_and_the_whole_queue_when_not(monkeypatch):
    seen: list = []

    async def run(owner=None, only=None):
        seen.append(only)

    monkeypatch.setattr(main.queue_loop, "run", run)
    monkeypatch.setattr(settings, "run_schedule", False)
    await main._start_queue("engine")
    assert main._start_queue("all") is None, "the whole app, held, runs asks on the spot and starts no queue"
    monkeypatch.setattr(settings, "run_schedule", True)
    await main._start_queue("engine")
    await main._start_queue("all")
    assert seen == [frozenset({topics.ENGINE_ASK}), None, None]


async def test_a_held_dispatcher_answers_an_ask_and_leaves_other_posted_work_alone(js, monkeypatch):
    pin_clock(monkeypatch, NOW)
    monkeypatch.setattr(engine_asks, "_asks", {})

    async def rank(session, owner, args):
        return {"ranked": args["n"]}

    engine_asks.register("rank", rank)
    asked = json.dumps({"name": "rank", "args": {"n": 2}, "asked_at": iso_utc(NOW)})
    async with db.session_factory()() as session:
        await facts.put(session, OWNER, f"ask:{ID}", asked, now=NOW)
        queue.post(session, OWNER, topics.ENGINE_ASK, [ID], cause=topics.ASK, now=NOW)
        queue.post(session, OWNER, topics.POSTING_SIFT, [topics.ANY], cause="start", now=NOW)
        await session.commit()
    held = loop.Dispatcher(OWNER, clock=lambda: NOW, only=frozenset({topics.ENGINE_ASK}), gaps=False)
    await held.drain()
    async with db.read_session_factory()() as reader:
        answer = await facts.get(reader, OWNER, f"answer:{ID}")
    assert json.loads(answer) == {"ranked": 2}
    assert await posted_now() == [(topics.POSTING_SIFT, topics.ANY)], "the sift to-do was not taken"
