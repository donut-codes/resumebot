"""Two requests at once, with sessions that really commit: a locked read sees what
the request before it committed, a stop and an answer at the same time can't wait on each other, and a double click is
told from a failure.

Each test here fails without the fix it names. They use the `committed` fixture (a session on a connection of its own).

A request on SQLite waits for its turn to write, not for a row: a write transaction holds the whole database from its
first statement (`process_engine.portable`, and `conftest.py` begins each one `BEGIN IMMEDIATE`). So a request cannot
read a copy that goes stale under it, and there is no row another request holds for `tick` to leave alone or for a lock
order to protect. The tests of those two (Postgres's row locks) went with them; the tests of two requests at the same
time, and of one request behind another, stay."""

import asyncio
import uuid

import pytest
from process_samples import LOOP, engine_for
from sqlalchemy import select
from support import sample_engine

from process_engine import config as engine_config
from process_engine import live
from process_engine.engine import Engine
from process_engine.errors import EngineError, NotWaiting
from process_engine.models import CANCELLED, COMPLETED, WAITING, ProcInstance

ENTITY_ID = uuid.UUID("6f1c2f0e-8b0a-4f43-9c53-0d1d5a0a1b11")


def loop_engine() -> Engine:
    return engine_for(LOOP)


async def wait_until_blocked(task: asyncio.Task) -> None:
    """Give `task`, a request, time to reach the transaction in front of it, and check that it is still waiting there:
    SQLite does not say who waits for its turn (the driver waits, out of sight)."""
    await asyncio.sleep(0.3)
    assert not task.done(), "it should be waiting for the request in front of it"


async def started(committed, engine: Engine, key: str, *args, **kwargs) -> ProcInstance:
    setup = committed()
    instance = await engine.start(setup, key, *args, **kwargs)
    await setup.commit()
    return instance


# --- a locked read is a fresh read ---


async def test_a_cancel_that_read_the_instance_earlier_sees_what_the_answer_committed(committed):
    engine = loop_engine()
    instance = await started(committed, engine, "again")
    one, two = committed(), committed()
    held = await two.get(ProcInstance, instance.id)  # request two has read it, as waiting, and still holds that copy
    assert held.status == WAITING
    await two.commit()  # (its read is over: a request that held the database open would be in front of request one)

    await engine.complete(one, instance.id, "Ask", {"again": False})  # request one answers it
    await one.commit()
    await engine.cancel(two, instance.id, "the household changed its mind")  # and two, which read it earlier, cancels
    await two.commit()

    assert (await committed().get(ProcInstance, instance.id)).status == COMPLETED  # not overwritten with cancelled
    assert held.status == COMPLETED


async def test_an_answer_that_read_the_instance_earlier_is_told_the_instance_has_ended(committed):
    engine = loop_engine()
    instance = await started(committed, engine, "again")
    one, two = committed(), committed()
    held = await two.get(ProcInstance, instance.id)  # read as waiting, and still held
    await two.commit()

    await engine.cancel(one, instance.id, "stopped")
    await one.commit()
    with pytest.raises(NotWaiting, match="already cancelled"):
        await engine.complete(two, instance.id, "Ask", {"again": False})
    assert held.status == CANCELLED  # the copy it read earlier was refreshed by the locked read
    await two.rollback()  # (the refused request lets go of the database, as a route would)
    assert (await committed().get(ProcInstance, instance.id)).status == CANCELLED


async def test_a_cancel_that_waited_behind_the_answer_finds_the_instance_completed(committed):
    """The same two requests with one behind the other: request one answers and has not committed, request two's cancel
    waits for its turn to write, and then reads what one committed."""
    engine = loop_engine()
    instance = await started(committed, engine, "again")
    one, two = committed(), committed()
    await engine.complete(one, instance.id, "Ask", {"again": False})
    cancel = asyncio.create_task(engine.cancel(two, instance.id, "the household changed its mind"))
    await wait_until_blocked(cancel)
    await one.commit()
    await cancel
    await two.commit()

    assert (await committed().get(ProcInstance, instance.id)).status == COMPLETED  # not overwritten with cancelled


async def test_an_answer_that_waited_behind_the_cancel_is_told_the_instance_has_ended(committed):
    engine = loop_engine()
    instance = await started(committed, engine, "again")
    one, two = committed(), committed()
    await engine.cancel(one, instance.id, "stopped")
    answer = asyncio.create_task(engine.complete(two, instance.id, "Ask", {"again": False}))
    await wait_until_blocked(answer)
    await one.commit()
    with pytest.raises(NotWaiting, match="already cancelled"):
        await answer
    await two.rollback()

    assert (await committed().get(ProcInstance, instance.id)).status == CANCELLED


# --- one answer, twice, at the same time ---


async def test_a_double_click_answers_once_and_the_second_is_told_nothing_is_waiting(committed):
    engine = loop_engine()
    instance = await started(committed, engine, "again")

    async def click(session):
        try:
            await engine.complete(session, instance.id, "Ask", {"again": False})
            await session.commit()
            return "answered"
        except NotWaiting:
            await session.rollback()
            return "not waiting"

    results = await asyncio.gather(click(committed()), click(committed()))
    assert sorted(results) == ["answered", "not waiting"]
    assert (await committed().get(ProcInstance, instance.id)).status == COMPLETED


async def test_a_double_click_on_a_live_step_is_not_held_as_a_failure(committed):
    engine = loop_engine()
    told: list[Exception] = []

    async def on_failure(session, entity_id, key, what, exc, subject=None):
        told.append(exc)

    engine_config.configure(on_drive_failure=on_failure)
    instance = await started(committed, engine, "again", {})

    async def click(session):
        async def work():
            return await engine.complete(session, instance.id, "Ask", {"again": False})

        try:
            await live.attempt(session, ENTITY_ID, "again", "take the answer", work)
            await session.commit()
            return "answered"
        except NotWaiting:
            await session.rollback()
            return "not waiting"

    results = await asyncio.gather(click(committed()), click(committed()))
    assert sorted(results) == ["answered", "not waiting"]
    assert told == []  # the loser was not a failure
    assert (await committed().get(ProcInstance, instance.id)).status == COMPLETED


# --- a stop and an answer at once ---


async def test_stopping_a_parent_and_answering_its_sub_process_at_once_do_not_deadlock(committed):
    engine = sample_engine(items=[{"name": "restart a service", "risk": "high"}])
    parent = await started(committed, engine, "intake", message="intake.requested")
    setup = committed()
    await engine.complete(setup, parent.id, "Approve", {"approved": True})
    child = (await setup.scalars(select(ProcInstance).where(ProcInstance.parent_id == parent.id,
                                                            ProcInstance.status == WAITING).limit(1))).one()
    await setup.commit()

    async def answer(session):
        try:
            await engine.complete(session, child.id, "Second_look", {"agrees": True})
            await session.commit()
        except EngineError:  # (stopped first: nothing is waiting any more)
            await session.rollback()

    async def stop(session):
        await engine.cancel(session, parent.id, "stopped")
        await session.commit()

    await asyncio.gather(*(f(committed()) for _ in range(5) for f in (answer, stop)))  # a deadlock would raise here
    check = committed()
    assert (await check.get(ProcInstance, parent.id)).status == CANCELLED
    assert (await check.get(ProcInstance, child.id)).status in (COMPLETED, CANCELLED)
