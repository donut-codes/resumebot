"""The pull (the work queue spec C4 `pool.owed`, inventory B7, phase 4): starting the next job for a free
pool session (`session_kinds.pull`: the Haiku ranking, the deck's Sonnet reads, the employers' research) runs from
`pool.owed` to-dos and the minute tick's nudge instead of every housekeeping pass. The handler is the old pull itself,
once, whole form; it is asked when something changes that the pull reads, and every minute while a free session has
asked lately (what only the clock changes: the hours, the day's room, the gap between searches). Everything is made
up."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, select

from app import db
from app.jobsearch.models import Opportunity, Todo
from app.jobsearch.work import queue, session_kinds
from app.jobsearch.work.queue import consume, handlers, loop
from app.jobsearch.work.queue import topics as queue_topics
from tests.builders import analysis_setup
from tests.jobsearch.work_queue.support import posted, posted_now
from tests.support import OWNER, empty_the_tables, load_profile

POOL = queue_topics.POOL_OWED


def pool_only(clock=None) -> loop.Dispatcher:
    table = {t: h for t, h in handlers.HANDLERS.items() if t == POOL}
    return loop.Dispatcher(OWNER, clock=clock or (lambda: datetime.now(UTC)), table=table, gaps=False)


def ticks_only() -> loop.Dispatcher:
    table = {t: h for t, h in handlers.HANDLERS.items() if t == queue_topics.TICK_MINUTE}
    return loop.Dispatcher(OWNER, table=table, gaps=False)


def a_free_session_asks(now: datetime | None = None) -> None:
    """What `session_kinds.hand_out` notes when a free pool session asks and finds nothing queued. A test that goes on
    to count from the ask gives its own `now`: a second read of the clock is a later moment."""
    session_kinds._pool_asked[OWNER] = now or datetime.now(UTC)


async def post(topic: str = POOL, key: str = "", cause: str = "test") -> None:
    async with db.session_factory()() as session:
        queue.post(session, OWNER, topic, [key], cause=cause)
        await session.commit()


async def forget_the_posts() -> None:
    async with db.session_factory()() as session:
        await session.execute(Todo.__table__.delete())
        await session.commit()


async def pool_posts() -> list[tuple[str, str]]:
    return [(topic, key) for topic, key in await posted_now() if topic == POOL]


async def waiting() -> list[str]:
    """The roles put in front of the analyst."""
    async with db.session_factory()() as session:
        return sorted((await session.scalars(select(Opportunity.company).where(
            Opportunity.analysis_state == "waiting"))).all())


# --- the handler is the old pull ------------------------------------------------------------------------------------


async def test_the_queue_starts_the_job_the_old_pull_starts(js, monkeypatch):
    """The same data through the old pass and through the queue's drain: one role in front of the analyst, the same
    one."""
    await analysis_setup()
    a_free_session_asks()
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 1
        await session.commit()
    old = await waiting()
    assert len(old) == 1

    async with db.engine().begin() as conn:
        await empty_the_tables(conn)
    session_kinds.forget()
    await analysis_setup()
    a_free_session_asks()
    await post()
    assert await pool_only().drain() == 1
    assert await waiting() == old
    assert await pool_posts() == [], "its own writes do not ask it again"


async def test_many_to_dos_are_one_pull(js, monkeypatch):
    await analysis_setup()
    a_free_session_asks()
    for key in ("", "*", ""):
        await post(key=key)
    calls: list[int] = []
    real = session_kinds.pull

    async def counting(session, owner):
        calls.append(1)
        return await real(session, owner)

    monkeypatch.setattr(session_kinds, "pull", counting)
    assert await pool_only().drain() == 1
    assert len(calls) == 1 and len(await waiting()) == 1


async def test_a_to_do_with_no_free_session_asking_reads_nothing_and_starts_no_pause(js):
    """Nothing is pulled for a session that is not there, so the look costs no statement but the queue's own, and the
    change that comes a moment later is not made to wait out a pause."""
    await analysis_setup()
    await post()
    statements: list[str] = []

    def noted(_conn, _cursor, statement, *_rest) -> None:
        if not statement.startswith("BEGIN"):  # SQLite's writer turn is taken by a statement of its own
            statements.append(statement)

    d = pool_only()
    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        assert await d.drain() == 1
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    assert all("js_todos" in s for s in statements), [s[:80] for s in statements if "js_todos" not in s]
    assert d.finished == {}, "a look that did no work starts no pause"
    assert await waiting() == [] and await pool_posts() == []


async def test_a_pull_that_starts_nothing_because_a_job_waits_already_starts_nothing_more(js):
    await analysis_setup()
    a_free_session_asks()
    await post()
    d = pool_only()
    await d.drain()
    assert len(await waiting()) == 1
    await post()
    await d.drain()
    assert len(await waiting()) == 1, "one ahead: what was started is queued now"


# --- who asks it ----------------------------------------------------------------------------------------------------


async def test_the_minute_tick_asks_the_pool_while_a_free_session_asked_lately_and_not_otherwise(js):
    await post(queue_topics.TICK_MINUTE)
    assert await ticks_only().drain() == 1
    assert (POOL, "") not in await posted_now(), "no free session asked: nothing to start for one"

    a_free_session_asks()
    async with db.session_factory()() as session:
        for row in (await session.scalars(select(Todo).where(Todo.topic == queue_topics.TICK_MINUTE))).all():
            row.not_before = datetime.now(UTC)  # the next tick is due
        await session.commit()
    assert await ticks_only().drain() == 1
    assert (POOL, "") in await posted_now()


async def test_the_nudge_is_made_once_in_five_minutes_and_the_first_is_at_once(js):
    """The nudge is for what only time changes (the hours, the day's room, the gap between searches, Claude's usage
    limit), none of which a writer can post, so five minutes late is the most it costs; a writer's own to-do is at
    once. Found in the review of phase 4: a nudge every minute was a pull every minute, 21% of today's quiet hour."""
    now = datetime.now(UTC)
    a_free_session_asks(now)  # the ask and the nudges are counted from one moment
    assert await handlers.nudge_pool(OWNER, now)
    assert not await handlers.nudge_pool(OWNER, now + timedelta(minutes=1))
    assert not await handlers.nudge_pool(OWNER, now + session_kinds.NUDGE_EVERY - timedelta(seconds=1))
    assert await handlers.nudge_pool(OWNER, now + session_kinds.NUDGE_EVERY)
    assert session_kinds.NUDGE_EVERY == session_kinds.POOL_ASK_STANDS, "once for each time an ask stands"


async def test_the_first_ask_of_a_free_session_after_a_quiet_spell_asks_the_pull_and_a_standing_ask_does_not(js):
    """What the old pull took from the ask itself (it looked every ten seconds while one stood): a free session that
    asks when none had asked within `POOL_ASK_STANDS` (the peak hours ended, the pause lifted, the first start) is the
    change that makes the pull look, and posts for it; a lane asking every two minutes afterwards posts nothing."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        assert not session_kinds.pool_asked(OWNER)
        await session_kinds.hand_out(session, OWNER, "session-1")
        assert (POOL, "") in await posted(session), "the first ask"
        await session.execute(Todo.__table__.delete())
        await session_kinds.hand_out(session, OWNER, "session-1")
        assert (POOL, "") not in await posted(session), "the ask stands: the nudge is the clock's"
        session_kinds._pool_asked[OWNER] = datetime.now(UTC) - session_kinds.POOL_ASK_STANDS - timedelta(seconds=5)
        await session_kinds.hand_out(session, OWNER, "session-1")
        assert (POOL, "") in await posted(session), "an ask that had lapsed is a first one again"
        await session.rollback()


async def test_an_ask_stands_for_five_minutes_as_the_old_pull_held_it(js):
    now = datetime.now(UTC)
    assert not session_kinds.pool_asked(OWNER)
    session_kinds._pool_asked[OWNER] = now - session_kinds.POOL_ASK_STANDS + timedelta(seconds=5)
    assert session_kinds.pool_asked(OWNER)
    session_kinds._pool_asked[OWNER] = now - session_kinds.POOL_ASK_STANDS - timedelta(seconds=5)
    assert not session_kinds.pool_asked(OWNER)


async def test_the_sweep_and_the_start_ask_the_pull_for_everything(js):
    d = loop.Dispatcher(OWNER, gaps=False)
    assert POOL in d.swept()
    await d.start_up()
    assert (POOL, "*") in await posted_now()


async def test_a_pull_only_the_sweep_asked_for_that_starts_a_job_is_a_catch(js, caplog):
    caplog.set_level(logging.WARNING)
    await analysis_setup()
    a_free_session_asks()
    await forget_the_posts()  # what the setup's writes posted is not what asked: only the sweep did
    await post(key="*", cause="sweep")
    d = pool_only()
    await d.drain()
    assert d.tally.caught == {POOL: 1}
    assert [m for m in caplog.messages if "sweep_caught topic=pool.owed n=1" in m]


# --- the pause, and who leaves it to the queue ------------------------------------------------------------------------


def test_the_pull_keeps_the_pause_of_the_loop_it_replaced():
    assert handlers.HANDLERS[POOL].gap == timedelta(seconds=10), "no more often than the loop it replaces"
    assert handlers.HANDLERS[POOL].name == consume.PULL and handlers.HANDLERS[POOL].lane == "db"


def test_the_janitor_leaves_the_pull_to_the_dispatcher():
    assert POOL in consume.consumed_topics()
