"""The live counts, the stream's hello and the turn poll only read, so they ask for no writer's turn (the owner,
2026-11-10: reading must not lock everything). Each opened a writer's session, which takes the file's one turn at its
first statement, a read too, and so queued behind any save. Synthetic roles only."""

import json
from datetime import date

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.loops import schedule
from app.jobsearch.models import Opportunity, Todo
from app.jobsearch.pipeline import live
from tests.support import OWNER


@pytest.fixture
async def role(js):
    async with db.session_factory()() as session:
        session.add(Opportunity(owner=OWNER, slug="a", company="Co a", title="Role a", status=stages.FIRST_IMPRESSION,
                                origin="scout", match_key="co a|role a", found=date.today()))
        await session.commit()


@pytest.fixture
def turns():
    """The writer's turns asked for while a test runs (the seconds each waited)."""
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    yield asked
    db.turn_watchers.remove(asked.append)


async def test_opening_a_stream_asks_for_no_turn(role, turns):
    async def disconnected() -> bool:
        return True

    stream = live.stream(OWNER, disconnected)
    try:
        hello = await anext(stream)
    finally:
        await stream.aclose()
    assert json.loads(hello.split("data: ", 1)[1])["counts"]["lanes"][stages.FIRST_IMPRESSION] == 1
    assert turns == [], "the hello's counts are read on a reader"


async def test_a_counts_push_asks_for_no_turn(role, turns, monkeypatch):
    monkeypatch.setattr(live, "COUNTS_AFTER_SECONDS", 0)
    sub = live.broker.subscribe(OWNER, frozenset({"counts"}))
    try:
        await live.broker._send_counts(OWNER)  # pyright: ignore[reportPrivateUsage]
        sent = []
        while not sub.queue.empty():
            sent.append(sub.queue.get_nowait())
    finally:
        live.broker.unsubscribe(sub)
    assert [kind for _, kind, _ in sent] == ["counts"]
    assert sent[0][2]["lanes"][stages.FIRST_IMPRESSION] == 1
    assert turns == [], "the pushed counts are read on a reader"


async def test_the_turn_poll_asks_for_no_turn(js, turns):
    async with db.session_factory()() as session:
        todo = Todo(owner=OWNER, topic="deck.dirty", key="")
        session.add(todo)
        await session.commit()
        ids = [todo.id]
    turns.clear()
    assert await schedule.turn_waiting(ids) is True
    assert turns == [], "whether the turn has answered is read on a reader"
