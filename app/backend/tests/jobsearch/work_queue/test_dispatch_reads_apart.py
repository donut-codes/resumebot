"""The dispatcher looks at the queue as a reader (no writer turn, no wait behind a writer) and does not read the
to-dos of a topic whose handler is running. The handlers here are the tests' own; everything is made up."""

import asyncio

from app import db
from app.jobsearch.work import queue
from app.jobsearch.work.queue import service
from tests.jobsearch.work_queue.support import OWNER, posted_now
from tests.jobsearch.work_queue.test_consumer import FIRST, SECOND, Spy, dispatcher, post, table


async def test_a_look_does_not_wait_for_the_writer_turn(js):
    await post(FIRST)
    slow = db.session_factory()()
    try:
        queue.post(slow, OWNER, SECOND, ["held"], cause="test")
        await slow.flush()  # holds the writer turn until it commits
        spy = Spy()
        started = await asyncio.wait_for(dispatcher(table((FIRST, spy))).dispatch(), 3)
        assert len(started.tasks) == 1, "the look read the to-do while a writer held the turn"
        await asyncio.gather(*started.tasks)
    finally:
        await slow.rollback()
        await slow.close()
    assert len(spy.calls) == 1


async def test_a_topic_in_flight_is_not_read_and_is_seen_again_when_its_run_ends(js, monkeypatch):
    release = asyncio.Event()

    async def hold(_run):
        await release.wait()

    spy = Spy(hold)
    d = dispatcher(table((FIRST, spy)))
    await post(FIRST, "one")
    first = await d.dispatch()
    await asyncio.wait_for(spy.called.wait(), 3)
    asked: list[set[str]] = []
    real = service.due

    async def spied(session, owner, now, topics):
        asked.append(set(topics))
        return await real(session, owner, now, topics)

    monkeypatch.setattr(service, "due", spied)
    await post(FIRST, "two")
    again = await d.dispatch()
    assert again.tasks == [] and asked == [set()], "due is asked about no topic while FIRST runs"
    release.set()
    await asyncio.gather(*first.tasks)
    release.clear()
    release.set()
    after = await d.dispatch()
    assert asked[-1] == {FIRST} and len(after.tasks) == 1, "the re-look sees the to-do posted meanwhile"
    await asyncio.gather(*after.tasks)
    assert [c.keys for c in spy.calls] == [("one",), ("two",)] and await posted_now() == []
