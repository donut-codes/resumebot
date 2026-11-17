"""The minute tick tells the page when 'is an agent ready' changed with the clock (`handlers.tell_ready`): one
`changed{ready}` item when the answer differs from the last one seen, none otherwise, and no writer's turn for the
look. Everything is made up."""

import logging
from datetime import timedelta

import pytest

from app import db
from app.jobsearch.pipeline import live_sink
from app.jobsearch.work import dispatcher, job_queue
from app.jobsearch.work.queue import handlers, service, topics
from tests.support import NOON, OWNER

READY = (True, None)
PEAK = (False, "It is Claude's peak hours.")
ITEM = ("changed", "ready", ("ready",))


class Heard:
    """Stands in for the sink in use: it keeps each call of `announce(owner, items)`, as `live_sink.Sink` takes it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list]] = []

    def announce(self, owner: str, items: list) -> None:
        self.calls.append((owner, list(items)))


class Answers:
    """Stands in for `job_queue.ready(session, owner, now=None)`: it answers the next of its list each time."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)

    async def __call__(self, session, owner, now=None):
        return self.answers.pop(0)


@pytest.fixture
def heard(js, monkeypatch):
    """What the engine's sink was told, with nothing remembered of an earlier look."""
    sink = Heard()
    monkeypatch.setattr(live_sink, "_sink", sink)
    monkeypatch.setattr(handlers, "_told_ready", {})
    return sink


async def test_the_first_look_and_a_look_that_finds_the_same_tell_nothing(heard, monkeypatch):
    monkeypatch.setattr(job_queue, "ready", Answers(READY, READY, READY))
    assert [await handlers.tell_ready(OWNER) for _ in range(3)] == [False, False, False]
    assert heard.calls == []


async def test_a_look_that_finds_a_different_answer_tells_once_and_only_then(heard, monkeypatch):
    monkeypatch.setattr(job_queue, "ready", Answers(READY, PEAK, PEAK, READY))
    told = [await handlers.tell_ready(OWNER) for _ in range(4)]
    assert told == [False, True, False, True]
    assert heard.calls == [(OWNER, [ITEM]), (OWNER, [ITEM])]


async def test_a_new_reason_for_the_same_no_is_a_change(heard, monkeypatch):
    monkeypatch.setattr(job_queue, "ready", Answers(PEAK, (False, "No agent has checked in yet.")))
    assert [await handlers.tell_ready(OWNER) for _ in range(2)] == [False, True]


async def test_a_look_that_fails_is_logged_and_tells_nothing(heard, monkeypatch, caplog):
    async def broken(session, owner, now=None):
        raise RuntimeError("no room")

    monkeypatch.setattr(job_queue, "ready", broken)
    caplog.set_level(logging.WARNING)
    assert await handlers.tell_ready(OWNER) is False
    assert heard.calls == []
    assert "whether an agent is ready failed" in caplog.text


async def test_the_look_takes_no_writers_turn(heard):
    asked: list[float] = []

    def watch(waited: float) -> None:
        asked.append(waited)

    db.turn_watchers.append(watch)
    try:
        assert [await handlers.tell_ready(OWNER) for _ in range(2)] == [False, False]  # the real `job_queue.ready`
    finally:
        db.turn_watchers.remove(watch)
    assert asked == []
    assert heard.calls == []
    assert handlers._told_ready[OWNER] == (False, "No agent has checked in yet."), "a real answer was read and kept"


async def test_the_minute_tick_looks_after_it_has_stamped_the_passes(monkeypatch):
    order: list[str] = []

    def stub(name: str, result=None):
        async def run(*args, **kwargs):
            order.append(name)
            return result

        return run

    monkeypatch.setattr(handlers, "_housekeeping", stub("housekeeping"))
    monkeypatch.setattr(handlers, "probe", stub("probe", []))
    monkeypatch.setattr(handlers, "nudge_pool", stub("nudge_pool", False))
    monkeypatch.setattr(dispatcher, "dispatch_now", stub("dispatch_now", 0))
    monkeypatch.setattr(handlers, "stamp_passes", stub("stamp_passes"))
    monkeypatch.setattr(handlers, "tell_ready", stub("tell_ready", False))
    group = service.Group(owner=OWNER, topic=topics.TICK_MINUTE, keys=(topics.ANY,), ids=(),
                          causes=frozenset({"sweep"}), attempts=0, waiting_since=NOON - timedelta(minutes=1))
    outcome = await handlers.tick_minute(handlers.Run(OWNER, group, NOON, handlers.Tally()))
    assert order == ["housekeeping", "probe", "nudge_pool", "dispatch_now", "stamp_passes", "tell_ready"]
    assert outcome.again == handlers.MINUTE_EVERY
