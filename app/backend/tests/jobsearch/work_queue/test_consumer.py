"""The consumer of the work queue (the work queue spec C2, C5, C8, and the queue service tests of H, built
with the first consumer in phase 2): the dispatcher reads what is due, runs one handler for the to-dos of a topic and
deletes exactly the ids it read. The handlers here are the tests' own; the real ones are in `test_posting_steps.py` and
`test_ticks.py`. Everything is made up."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from app import db
from app.jobsearch.models import Todo
from app.jobsearch.work import queue
from app.jobsearch.work.queue import consume, handlers, loop, service
from tests.jobsearch.work_queue.support import OWNER, posted_now

FIRST, SECOND, THIRD = "test.first", "test.second", "test.third"


class Spy:
    """A handler that records the groups it was handed, and does `work` (a coroutine function of the run) first."""

    def __init__(self, work=None, outcome: handlers.Outcome | None = None) -> None:
        self.calls: list[service.Group] = []
        self.work = work
        self.outcome = outcome or handlers.Outcome()
        self.called = asyncio.Event()

    async def __call__(self, run: handlers.Run) -> handlers.Outcome:
        self.calls.append(run.group)
        self.called.set()
        if self.work is not None:
            await self.work(run)
        return self.outcome


def table(*spies: tuple[str, Spy], gap: float = 0, lane: str = "db") -> dict[str, handlers.Handler]:
    return {topic: handlers.Handler(topic, None, lane, n, timedelta(seconds=gap), spy)
            for n, (topic, spy) in enumerate(spies)}


class Clock:
    """The real clock moved on by `offset`: the to-dos a test posts are stamped by the real one."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


def dispatcher(handlers_: dict, clock: Clock | None = None, *, gaps: bool = False) -> loop.Dispatcher:
    return loop.Dispatcher(OWNER, clock=clock or Clock(), table=handlers_, gaps=gaps)


async def post(topic: str, key: str = "", *, after: timedelta | None = None, cause: str = "test") -> None:
    async with db.session_factory()() as session:
        queue.post(session, OWNER, topic, [key], after=after, cause=cause)
        await session.commit()


async def todos() -> list[Todo]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Todo).order_by(Todo.id))).all())


async def _until_empty(seconds: float = 5) -> None:
    """Wait until the table holds no to-do (a handler that ran has removed its own), without a sleep to guess by."""
    for _ in range(int(seconds / 0.02)):
        if not await posted_now():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"still there after {seconds} seconds: {await posted_now()}")


# --- handed out once, removed exactly ---------------------------------------------------------------------------------


async def test_two_to_dos_for_one_topic_are_handled_once_and_both_removed(js):
    spy = Spy()
    await post(FIRST, "a")
    await post(FIRST, "b")
    await post(FIRST, "a")
    assert await dispatcher(table((FIRST, spy))).drain() == 1
    assert len(spy.calls) == 1 and spy.calls[0].keys == ("a", "b") and len(spy.calls[0].ids) == 3
    assert await posted_now() == []


async def test_a_to_do_posted_while_its_handler_runs_survives_and_runs_it_again(js):
    go = asyncio.Event()

    async def another_writer():
        """A request's own task: not the handler's, so what the handler suppresses does not apply to it."""
        await go.wait()
        await post(FIRST, "late")

    writer = asyncio.create_task(another_writer())

    async def meanwhile(run):
        if len(spy.calls) == 1:
            go.set()
            await writer

    spy = Spy(meanwhile)
    await post(FIRST, "early")
    d = dispatcher(table((FIRST, spy)))
    assert await d.pass_once() == 1
    assert [(t.topic, t.key) for t in await todos()] == [(FIRST, "late")], "only the ids it read were deleted"
    assert await d.drain() == 1
    assert [g.keys for g in spy.calls] == [("early",), ("late",)]
    assert await posted_now() == []


async def test_a_failing_handler_keeps_its_to_do_and_backs_off(js, caplog):
    caplog.set_level(logging.WARNING)

    async def breaks(run):
        raise RuntimeError("the page broke")

    spy = Spy(breaks)
    await post(FIRST)
    clock = Clock()
    d = dispatcher(table((FIRST, spy)), clock)
    assert await d.pass_once() == 1
    (kept,) = await todos()
    assert kept.attempts == 1 and "RuntimeError: the page broke" in kept.last_error
    assert timedelta(seconds=3) < kept.not_before - datetime.now(UTC) <= timedelta(seconds=5), "5 seconds"
    assert await d.pass_once() == 0, "not handed again before its time"
    clock.offset = timedelta(seconds=10)
    assert await d.pass_once() == 1
    (kept,) = await todos()
    assert kept.attempts == 2
    assert timedelta(seconds=35) < kept.not_before - datetime.now(UTC) <= timedelta(seconds=40), "then 30 seconds"
    assert [m for m in caplog.messages if "the handler of test.first failed" in m]
    spy.work = None
    clock.offset = timedelta(minutes=2)
    assert await d.drain() == 1
    assert await posted_now() == [], "a handler that works at last removes it"


async def test_a_failing_to_do_logs_at_error_when_its_attempts_reach_three(js, caplog):
    caplog.set_level(logging.ERROR)
    await post(FIRST)
    async with db.session_factory()() as session:
        ids = [t.id for t in (await session.scalars(select(Todo))).all()]
        await service.fail(session, ids, "boom", datetime.now(UTC), attempts=1)
        await session.commit()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "the second failure is quiet"
    async with db.session_factory()() as session:
        await service.fail(session, ids, "boom", datetime.now(UTC), attempts=2)
        await session.commit()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR and "failed 3 times" in r.getMessage()]


def test_the_backoff_is_five_seconds_then_thirty_then_two_minutes_then_ten_then_an_hour_for_ever():
    assert [service.backoff(n) for n in range(7)] == [
        timedelta(seconds=5), timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(hours=1),
        timedelta(hours=1), timedelta(hours=1)]


# --- time -------------------------------------------------------------------------------------------------------------


async def test_a_to_do_due_in_the_future_is_not_handed_before_its_time(js):
    spy = Spy()
    await post(FIRST, after=timedelta(hours=1))
    clock = Clock()
    d = dispatcher(table((FIRST, spy)), clock)
    started = await d.dispatch()
    assert started.tasks == [] and started.wait == loop.IDLE, "nothing is due, and the next is over an idle look away"
    await post(SECOND, after=timedelta(seconds=10))
    d2 = dispatcher(table((SECOND, spy)), clock)
    assert 8 < (await d2.dispatch()).wait.total_seconds() <= 10, "the wait ends when the next to-do comes due"
    clock.offset = timedelta(hours=2)
    assert await d.drain() == 1 and len(spy.calls) == 1


async def test_a_to_do_overdue_at_start_runs_at_start(js):
    long_ago = datetime.now(UTC) - timedelta(days=1)
    async with db.session_factory()() as session:
        await session.execute(insert(Todo).values(owner=OWNER, topic=FIRST, key="", posted_at=long_ago,
                                                  not_before=long_ago, cause="tick"))
        await session.commit()
    spy = Spy()
    assert await dispatcher(table((FIRST, spy))).drain() == 1
    assert len(spy.calls) == 1 and spy.calls[0].waiting_since == long_ago


async def test_a_topic_is_not_started_again_inside_its_pause_and_the_first_run_after_quiet_is_at_once(js):
    spy = Spy()
    clock = Clock()
    d = dispatcher(table((FIRST, spy), gap=30), clock, gaps=True)
    await post(FIRST, "one")
    assert await d.pass_once() == 1, "the first run after a quiet spell is at once"
    await post(FIRST, "two")
    started = await d.dispatch()
    assert started.tasks == [] and 0 < started.wait.total_seconds() <= 30, "its pause has not ended: wait for it"
    clock.offset = timedelta(seconds=31)
    assert await d.pass_once() == 1
    assert [g.keys for g in spy.calls] == [("one",), ("two",)]


async def test_a_run_that_found_nothing_owed_starts_no_pause_and_a_run_that_did_work_does(js):
    spy = Spy(outcome=handlers.Outcome(cheap=True))
    d = dispatcher(table((FIRST, spy), gap=30), Clock(), gaps=True)
    await post(FIRST, "one")
    assert await d.pass_once() == 1
    await post(FIRST, "two")
    assert await d.pass_once() == 1, "the look that did no work did not make the next change wait for it"
    spy.outcome = handlers.Outcome(changed=1)
    await post(FIRST, "three")
    assert await d.pass_once() == 1
    await post(FIRST, "four")
    started = await d.dispatch()
    assert started.tasks == [] and 0 < started.wait.total_seconds() <= 30, "but a run that did work does"


async def test_a_topic_that_is_running_is_not_started_again_and_its_to_dos_wait_for_its_end(js):
    release = asyncio.Event()

    async def slow(run):
        await release.wait()

    spy = Spy(slow)
    d = dispatcher(table((FIRST, spy)))
    await post(FIRST, "one")
    first = await d.dispatch()
    assert len(first.tasks) == 1
    await post(FIRST, "two")
    assert (await d.dispatch()).tasks == [], "one look at a time for a topic"
    release.set()
    await asyncio.gather(*first.tasks)
    assert await d.drain() == 1 and [g.keys for g in spy.calls] == [("one",), ("two",)]


async def test_two_handlers_of_one_lane_run_at_once_and_a_third_waits_for_one_of_them(js):
    release = asyncio.Event()
    running = 0
    most = 0

    async def slow(run):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await release.wait()
        running -= 1

    spies = [(topic, Spy(slow)) for topic in (FIRST, SECOND, THIRD)]
    d = dispatcher(table(*spies))
    for topic in (FIRST, SECOND, THIRD):
        await post(topic)
    started = await d.dispatch()
    assert len(started.tasks) == 3
    await asyncio.sleep(0.1)
    assert most == 2, "the db lane takes two at once"
    release.set()
    await asyncio.gather(*started.tasks)
    assert await posted_now() == []


async def test_the_handlers_go_in_the_order_of_their_priority_not_of_their_arrival(js):
    order: list[str] = []

    async def note(run):
        order.append(run.group.topic)

    d = dispatcher(table((FIRST, Spy(note)), (SECOND, Spy(note)), (THIRD, Spy(note))))
    for topic in (THIRD, SECOND, FIRST):
        await post(topic)
    started = await d.dispatch()
    await asyncio.gather(*started.tasks)
    assert order == [FIRST, SECOND, THIRD]


async def test_a_to_do_of_a_topic_nobody_takes_is_not_read_and_stays(js):
    spy = Spy()
    await post(queue.ROLE_SETTLE, "a-role")
    await post(FIRST)
    assert await dispatcher(table((FIRST, spy))).drain() == 1
    assert [(t.topic, t.key) for t in await todos()] == [(queue.ROLE_SETTLE, "a-role")]


# --- no harm ----------------------------------------------------------------------------------------------------------


async def test_a_handler_cut_off_before_its_to_do_was_removed_repeats_harmlessly(js):
    state: dict[str, object] = {"runs": 0}

    async def sets_the_data(run):
        state["runs"] = int(state["runs"]) + 1  # type: ignore[call-overload]
        state["value"] = "settled"  # by the data, not by the to-do: the same whenever it runs
        if state["runs"] == 1:
            await asyncio.sleep(30)  # the app stops here, before the to-do is removed

    spy = Spy(sets_the_data)
    await post(FIRST)
    cut = asyncio.create_task(dispatcher(table((FIRST, spy))).pass_once())
    await asyncio.wait_for(spy.called.wait(), 5)
    cut.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cut
    assert len(await todos()) == 1, "the to-do is still there"
    assert await dispatcher(table((FIRST, spy))).drain() == 1
    assert state == {"runs": 2, "value": "settled"} and await posted_now() == []


async def test_a_post_made_while_a_slow_writer_holds_the_turn_waits_for_it_and_both_are_handled_in_id_order(js):
    """There is one writer at a time, so a lower id can never commit after a higher one: a post made while another
    writer holds the turn waits, and once the slow writer has committed both to-dos are there, the slow one first."""
    spy = Spy()
    slow = db.session_factory()()
    try:
        queue.post(slow, OWNER, FIRST, ["slow"], cause="test")
        await slow.flush()  # its row has the lower id, and holds the writer turn until it commits
        fast = asyncio.create_task(post(FIRST, "fast"))
        await asyncio.sleep(0.2)
        assert not fast.done(), "the fast post waits for the turn"
        await slow.commit()
    finally:
        await slow.close()
    await asyncio.wait_for(fast, 3)
    d = dispatcher(table((FIRST, spy)))
    handled = await d.drain()
    assert handled >= 1
    keys = [key for call in spy.calls for key in call.keys]
    ids = [i for call in spy.calls for i in call.ids]
    assert keys == ["slow", "fast"] and ids == sorted(ids), "each handled once, the lower id first"
    assert await posted_now() == []


async def test_a_second_dispatcher_handling_the_same_to_do_does_no_harm(js):
    state = {"runs": 0, "value": None}

    async def sets_the_data(run):
        state["runs"] += 1
        state["value"] = "settled"
        await asyncio.sleep(0.05)

    spy = Spy(sets_the_data)
    await post(FIRST)
    results = await asyncio.gather(dispatcher(table((FIRST, spy))).drain(), dispatcher(table((FIRST, spy))).drain())
    assert 1 <= sum(results) <= 2 and state["value"] == "settled"
    assert await posted_now() == []


# --- what a handler's own writes post ---------------------------------------------------------------------------------


async def test_a_handlers_own_writes_do_not_post_its_topic_again_but_every_other_topic_still_posts(js):
    async def writes(run):
        async with db.session_factory()() as session:
            queue.post(session, OWNER, FIRST, ["again"], cause="test:its own write")
            queue.post(session, OWNER, FIRST, ["later"], after=timedelta(days=3), cause="test:a retry")
            queue.post(session, OWNER, SECOND, ["glue"], cause="test:the follow-on")
            await session.commit()

    await post(FIRST)
    assert await dispatcher(table((FIRST, Spy(writes)))).drain() == 1
    assert sorted((t.topic, t.key) for t in await todos()) == [(FIRST, "later"), (SECOND, "glue")]


async def test_a_handler_that_stopped_short_posts_itself_again_and_a_tick_posts_its_next_time(js):
    spy = Spy(outcome=handlers.Outcome(more=True))
    await post(FIRST)
    d = dispatcher(table((FIRST, spy)))
    assert await d.pass_once() == 1
    (again,) = await todos()
    assert (again.topic, again.key, again.cause) == (FIRST, "", "more")
    spy.outcome = handlers.Outcome(again=timedelta(minutes=1))
    assert await d.pass_once() == 1
    (tick,) = await todos()
    assert tick.cause == "tick" and timedelta(seconds=55) < tick.not_before - datetime.now(UTC) <= timedelta(minutes=1)
    assert await d.pass_once() == 0, "not before it comes due"


def _swept_run(late_cause: str | None) -> tuple[Spy, dict[str, handlers.Handler]]:
    """A named handler that finds one change, and (with `late_cause`) a writer in a task of its own that posts a to-do
    of the handler's topic under that cause while the handler runs."""
    go = asyncio.Event()

    async def another_writer():
        """A request's own task, begun outside the handler: what the handler suppresses does not apply to it."""
        await go.wait()
        await post(FIRST, "late", cause=late_cause or "")

    writer = asyncio.create_task(another_writer()) if late_cause is not None else None

    async def meanwhile(run):
        if writer is not None and len(spy.calls) == 1:
            go.set()
            await writer

    spy = Spy(meanwhile, handlers.Outcome(changed=1))
    return spy, {FIRST: handlers.Handler(FIRST, "first", "db", 0, timedelta(0), spy)}


async def test_a_swept_run_is_not_a_catch_when_a_writer_posted_for_its_topic_while_it_ran(js, caplog):
    caplog.set_level(logging.WARNING)
    _spy, handlers_ = _swept_run("hook:Opportunity.status")
    await post(FIRST, queue.ANY, cause="sweep")
    d = dispatcher(handlers_)
    assert await d.pass_once() == 1
    assert [(t.key, t.cause) for t in await todos()] == [("late", "hook:Opportunity.status")], "the late one stays"
    assert d.tally.caught == {} and not [m for m in caplog.messages if "sweep_caught" in m]


async def test_a_swept_run_that_finds_work_no_writer_posted_for_is_still_a_catch(js, caplog):
    caplog.set_level(logging.WARNING)
    _spy, handlers_ = _swept_run(None)
    await post(FIRST, queue.ANY, cause="sweep")
    d = dispatcher(handlers_)
    assert await d.pass_once() == 1
    assert d.tally.caught == {FIRST: 1}
    assert [m for m in caplog.messages if "sweep_caught topic=test.first n=1" in m]


async def test_a_swept_run_is_still_a_catch_when_only_the_dispatcher_s_own_to_do_came_while_it_ran(js, caplog):
    caplog.set_level(logging.WARNING)
    _spy, handlers_ = _swept_run("sweep")
    await post(FIRST, queue.ANY, cause="sweep")
    d = dispatcher(handlers_)
    assert await d.pass_once() == 1
    assert d.tally.caught == {FIRST: 1} and [m for m in caplog.messages if "sweep_caught topic=test.first n=1" in m]


async def test_the_dispatcher_waits_at_most_an_idle_look_and_a_missed_wake_costs_no_more(js):
    d = dispatcher(table((FIRST, Spy())))
    assert (await d.dispatch()).wait == loop.IDLE == timedelta(seconds=30)


# --- the wake ---------------------------------------------------------------------------------------------------------


async def test_a_commit_that_posted_wakes_the_dispatcher_and_a_rollback_does_not(js):
    event = asyncio.Event()
    consume.listen(event)
    try:
        async with db.session_factory()() as session:
            queue.post(session, OWNER, FIRST, cause="test")
            await session.rollback()
        assert not event.is_set()
        async with db.session_factory()() as session:
            queue.post(session, OWNER, FIRST, cause="test")
            await session.flush()
            assert not event.is_set(), "a flush is not the commit"
            await session.commit()
        assert event.is_set()
    finally:
        consume.forget(event)


async def test_a_to_do_posted_in_a_savepoint_wakes_at_the_commit_and_not_at_the_savepoint_s_release(js):
    """SQLAlchemy runs `after_commit` for a savepoint's release too, when the row is not yet visible to the dispatcher's
    own connection: a wake spent there would leave the real commit waking nothing, and the to-do would wait for the idle
    look. The writers use savepoints everywhere (`actions`, `gate_facts`, `catch_up`, `records`)."""
    event = asyncio.Event()
    consume.listen(event)
    try:
        async with db.session_factory()() as session:
            async with session.begin_nested():
                queue.post(session, OWNER, FIRST, cause="test")
            assert not event.is_set(), "a savepoint's release is not the commit"
            async with session.begin_nested():
                async with session.begin_nested():
                    queue.post(session, OWNER, FIRST, ["deeper"], cause="test")
            assert not event.is_set(), "nor is the release of one inside another"
            await session.commit()
        assert event.is_set(), "the commit that made the rows visible wakes the dispatcher"
    finally:
        consume.forget(event)


async def test_a_savepoint_that_is_released_and_a_transaction_that_rolls_back_wake_nothing(js):
    event = asyncio.Event()
    consume.listen(event)
    try:
        async with db.session_factory()() as session:
            async with session.begin_nested():
                queue.post(session, OWNER, FIRST, cause="test")
            await session.rollback()
        assert not event.is_set()
    finally:
        consume.forget(event)


async def test_a_commit_that_posted_nothing_wakes_nothing(js):
    event = asyncio.Event()
    consume.listen(event)
    try:
        async with db.session_factory()() as session:
            await session.scalars(select(Todo))
            await session.commit()
        assert not event.is_set()
    finally:
        consume.forget(event)


async def test_the_loop_wakes_at_a_commit_and_runs_the_handler_long_before_the_idle_look(js):
    spy = Spy()
    d = dispatcher(table((FIRST, spy)))
    task = asyncio.create_task(d.run())
    try:
        await asyncio.sleep(0.2)  # the loop has looked once and is waiting
        await post(FIRST)
        await asyncio.wait_for(spy.called.wait(), 5)
        await _until_empty()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_the_loop_survives_a_look_that_fails(js, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    spy = Spy()
    d = dispatcher(table((FIRST, spy)))
    real = service.due
    asks = {"n": 0}

    async def flaky(*args, **kwargs):
        asks["n"] += 1
        if asks["n"] == 1:
            raise RuntimeError("the database blinked")
        return await real(*args, **kwargs)

    monkeypatch.setattr(service, "due", flaky)
    task = asyncio.create_task(d.run())
    try:
        await asyncio.sleep(0.2)
        await post(FIRST)
        await asyncio.wait_for(spy.called.wait(), 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [m for m in caplog.messages if "the look at what is due failed" in m]


# --- start ------------------------------------------------------------------------------------------------------------


async def test_a_start_posts_each_topic_once_and_makes_the_ticks_when_none_is_there(js):
    table_ = {
        topic: handlers.Handler(topic, name, "db", n, timedelta(0), Spy())
        for n, (topic, name) in enumerate(((queue.TICK_MINUTE, None), (queue.TICK_HOUR, None),
                                           (queue.POSTING_SIFT, "sift"), (queue.POSTING_ROLES, "roles")))}
    d = loop.Dispatcher(OWNER, table=table_)
    await d.start_up()
    await d.start_up()  # a second start: the topics are posted again, the ticks are not
    rows = sorted((t.topic, t.cause) for t in await todos())
    assert rows == [("posting.roles", "start"), ("posting.roles", "start"), ("posting.sift", "start"),
                    ("posting.sift", "start"), ("tick.hour", "start"), ("tick.minute", "start")]


def test_the_pause_of_the_posting_steps_is_the_pause_of_the_loop_they_were_steps_of():
    assert handlers.TURN_GAP == timedelta(seconds=30)
    assert {h.gap for t, h in handlers.HANDLERS.items() if t in consume.TOPIC_OF.values()} == {handlers.TURN_GAP}
