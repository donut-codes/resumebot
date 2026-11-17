"""The runner dispatcher's walk (the work queue spec C4 `ready.<kind>`, inventory B10, phase 4): the pass
that commands an idle lane with the work a session could take (`dispatcher.dispatch_pass`) runs from `ready.<kind>`
to-dos and, as the old loop's quiet pace did, once a minute on the minute tick. A lane that goes idle posts
`dispatch.idle`, which the walk takes too; the scoped walk and the
runner's own side are phase 6. Nothing here reaches the network: the command is recorded, never sent. Everything is
made up."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import facts
from app.jobsearch.models import Todo
from app.jobsearch.work import dispatcher, housekeeping, queue, wake
from app.jobsearch.work.queue import consume, handlers, loop
from app.jobsearch.work.queue import topics as queue_topics
from app.models.agent import AgentConnection
from app.settings import settings
from app.timeutil import iso_utc
from tests.builders import parse_job
from tests.jobsearch.work_queue.support import posted_now
from tests.support import OWNER, empty_the_tables, load_profile

WALK_TOPICS = consume.WALK_TOPICS


@pytest.fixture
async def world(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(settings, "wake_url", "http://127.0.0.1:8651/wake")
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()


@pytest.fixture
def sent(monkeypatch):
    commands: list[tuple[str, str]] = []

    async def command(runner, kind, claim, item, model=None):
        commands.append((runner, kind))
        return True

    monkeypatch.setattr(wake, "command", command)
    return commands


def walk_only() -> loop.Dispatcher:
    table = {t: h for t, h in handlers.HANDLERS.items() if t in WALK_TOPICS}
    return loop.Dispatcher(OWNER, table=table, gaps=False)


def ticks_only() -> loop.Dispatcher:
    table = {t: h for t, h in handlers.HANDLERS.items() if t == queue_topics.TICK_MINUTE}
    return loop.Dispatcher(OWNER, table=table, gaps=False)


async def _idle(*names: str) -> None:
    async with db.session_factory()() as session:
        for name in names:
            await roster.note_runner(session, OWNER, {"runner": name, "state": "idle", "session_id": f"s-{name}"})
        await session.commit()


async def post(topic: str = "ready.parser", cause: str = "test") -> None:
    async with db.session_factory()() as session:
        queue.post(session, OWNER, topic, [""], cause=cause)
        await session.commit()


async def ready_posts(*, due: bool = False) -> list[tuple[str, str]]:
    """The `ready.<kind>` to-dos in the table (with `due`, those that are due now: a claim's own to-do waits for its
    lease to run out, and is not a look owed now)."""
    async with db.session_factory()() as session:
        query = select(Todo.topic, Todo.key).where(Todo.topic.in_(WALK_TOPICS))
        if due:
            query = query.where(Todo.not_before <= datetime.now(UTC))
        return [(topic, key) for topic, key in (await session.execute(query.order_by(Todo.id))).all()]


async def _forget_the_posts() -> None:
    async with db.session_factory()() as session:
        await session.execute(Todo.__table__.delete())
        await session.commit()


# --- the handler is the old pass --------------------------------------------------------------------------------------


async def test_the_queue_commands_the_idle_lane_the_old_pass_commands(world, sent):
    await parse_job(age=timedelta(minutes=2))
    await _idle("session-1")
    assert await dispatcher.dispatch_pass(OWNER) == 1
    old = list(sent)
    assert old == [("session-1", "parser")]

    async with db.engine().begin() as conn:
        await empty_the_tables(conn)
    dispatcher.forget()
    sent.clear()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    await parse_job(age=timedelta(minutes=2))
    await _idle("session-1")
    await post()
    assert await walk_only().drain() == 2, "one run commands the lane; the next finds nothing more and stops"
    assert sent == old


async def test_one_run_takes_the_to_dos_of_every_kind_and_the_walk_is_made_once(world, sent):
    await parse_job()
    await _idle("session-1")
    await _forget_the_posts()
    for topic in ("ready.parser", "ready.checker", "ready.analysis", "ready.browser_reader"):
        await post(topic)
    d = walk_only()
    assert await d.pass_once() == 1, "the kinds' to-dos are one look: the walk reads them all"
    assert sent == [("session-1", "parser")]
    assert await d.drain() == 1, "and the second look, which the first asked for, finds nothing more"
    assert await ready_posts(due=True) == []


async def test_new_work_is_not_made_to_wait_out_the_pace_of_a_pass_that_found_nothing(world, sent):
    await _idle("session-1")
    assert await dispatcher.dispatch_pass(OWNER) == 0, "nothing waits: the pass is quiet for a minute"
    await parse_job()
    await post()
    await walk_only().drain()
    assert sent == [("session-1", "parser")], "work just made ready drops the pace, as `dispatch_now` does"


async def test_work_that_is_gated_is_looked_at_once_and_not_spun_on_and_the_minute_tick_walks_it_when_the_gate_lifts(
        world, sent):
    await parse_job()
    await _idle("session-1")
    async with db.session_factory()() as session:
        session.add(AgentConnection(owner=OWNER, slug=roster.SCOUT, name="Scout", paused=True))
        await session.commit()
    await _forget_the_posts()
    await post()
    d = walk_only()
    assert await d.drain() == 1
    assert sent == [] and await ready_posts() == [], "paused: nothing is commanded, and the to-do is taken"
    assert await d.drain() == 0, "no spin: nothing posted, nothing runs"

    async with db.session_factory()() as session:
        for connection in (await session.scalars(select(AgentConnection))).all():
            connection.paused = False  # a write that posts for the pool and the kinds (`_paused_cleared`)
        await session.commit()
    await _forget_the_posts()  # as if the resume had posted nothing: only the clock is left
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue_topics.TICK_MINUTE, cause="test")
        await session.commit()
    await ticks_only().drain()
    assert sent == [("session-1", "parser")], "the minute tick's walk is the old pace of a minute"


async def test_a_ready_to_do_with_no_wake_address_is_a_cheap_look_that_starts_no_pause(world, sent, monkeypatch):
    monkeypatch.setattr(settings, "wake_url", "")
    await post()
    d = walk_only()
    assert await d.drain() == 1
    assert d.finished == {} and await ready_posts() == []


async def test_a_walk_that_commanded_a_lane_posts_itself_again_for_the_next_idle_one(world, sent):
    first, second = await parse_job(age=timedelta(minutes=2)), await parse_job()
    assert first != second
    await _idle("session-1")
    await post()
    d = walk_only()
    assert await d.pass_once() == 1
    assert sent == [("session-1", "parser")]
    assert ("ready.parser", "") in await posted_now(), "more may wait for the next lane, as the old pass found"
    await _idle("session-2")
    assert await d.drain() >= 1
    assert sent == [("session-1", "parser"), ("session-2", "parser")]


async def test_a_lane_that_goes_idle_while_work_waits_is_commanded_at_once_and_not_at_the_next_minute(world, sent):
    """Found in the review of phase 4: the idle edge posted `dispatch.idle` and nothing took it, so with a job waiting
    and every lane busy the job waited for the minute tick once a lane freed (the old loop looked every 10 seconds)."""
    await parse_job(age=timedelta(minutes=2))
    async with db.session_factory()() as session:
        await roster.note_runner(session, OWNER, {"runner": "session-1", "state": "alive", "session_id": "s-1"},
                                 "analyst")
        await session.commit()
    assert await dispatcher.dispatch_pass(OWNER) == 0, "every lane is busy: nothing to command"
    await _forget_the_posts()
    async with db.session_factory()() as session:
        await roster.note_runner(session, OWNER, {"runner": "session-1", "state": "idle", "session_id": "s-1"})
        await session.commit()
    assert ("dispatch.idle", "session-1") in await posted_now()
    assert await walk_only().drain() >= 1
    assert sent == [("session-1", "parser")], "commanded from the lane's own to-do"
    assert ("dispatch.idle", "session-1") not in await posted_now(), "and the to-do is taken"


def test_the_walk_takes_the_idle_edge_with_the_work_that_is_ready():
    assert queue_topics.DISPATCH_IDLE in WALK_TOPICS
    assert handlers.HANDLERS[queue_topics.DISPATCH_IDLE] is handlers.HANDLERS["ready.parser"]


# --- the minute tick --------------------------------------------------------------------------------------------------


async def test_the_minute_tick_walks_work_no_writer_posted_for_as_the_quiet_pace_did(world, sent):
    await parse_job()
    await _idle("session-1")
    await _forget_the_posts()  # the job was made by a write that posted nothing
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue_topics.TICK_MINUTE, cause="test")
        await session.commit()
    assert await ticks_only().drain() == 1
    assert sent == [("session-1", "parser")]


# --- the health door --------------------------------------------------------------------------------------------------


async def test_the_minute_tick_feeds_the_last_pass_of_housekeeping_and_of_the_dispatcher(world, sent, monkeypatch):
    monkeypatch.setattr(housekeeping, "last_pass", None)
    monkeypatch.setattr(dispatcher, "last_pass", None)
    await _idle("session-1")  # an idle lane is what makes the pass look
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue_topics.TICK_MINUTE, cause="test")
        await session.commit()
    assert await ticks_only().drain() == 1
    assert housekeeping.last_pass is not None and dispatcher.last_pass is not None
    assert datetime.now(UTC) - housekeeping.last_pass < timedelta(minutes=1)


async def test_the_minute_tick_writes_both_last_passes_where_another_session_reads_them(world, sent, monkeypatch):
    monkeypatch.setattr(housekeeping, "last_pass", None)
    monkeypatch.setattr(dispatcher, "last_pass", None)
    await _idle("session-1")  # an idle lane is what makes the pass look
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue_topics.TICK_MINUTE, cause="test")
        await session.commit()
    assert await ticks_only().drain() == 1
    assert housekeeping.last_pass is not None and dispatcher.last_pass is not None
    async with db.session_factory()() as session:
        stamps = await facts.get_many(session, OWNER, "last_pass:")
    assert stamps == {"last_pass:dispatcher": iso_utc(dispatcher.last_pass),
                      "last_pass:housekeeping": iso_utc(housekeeping.last_pass)}
