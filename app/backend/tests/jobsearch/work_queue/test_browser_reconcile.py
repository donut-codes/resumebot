"""The browser's queue (the work queue spec C4 `browser.reconcile`, inventory B4, phase 4): making the queue
of browser items match the roles and sites (`browser.queue.refresh`) runs from `browser.reconcile` to-dos, once for all
that are due and never oftener than its 30 second pace, and the lease expiry (`browser.leases.expire`) runs on the
minute tick. Nothing else makes the queue: the hand-outs, the pool's counts, the housekeeping pass and the schedule's
look read it as it is. Everything is made up."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.jobsearch.browser import items as browser_items
from app.jobsearch.browser import leases as browser_leases
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.models import BrowserItem, Opportunity, Todo
from app.jobsearch.pool import core as pool
from app.jobsearch.work import housekeeping, queue
from app.jobsearch.work.queue import consume, handlers, loop
from app.jobsearch.work.queue import topics as queue_topics
from tests.builders import ONE, WORKER, browser_world, make_the_queue, next_item
from tests.jobsearch.work_queue.support import posted_now
from tests.support import DEFAULT_OWNER, empty_the_tables

BROWSER = queue_topics.BROWSER_RECONCILE


def browser_only(clock=None, *extra: str) -> loop.Dispatcher:
    table = {t: h for t, h in handlers.HANDLERS.items() if t in (BROWSER, *extra)}
    return loop.Dispatcher(DEFAULT_OWNER, clock=clock or (lambda: datetime.now(UTC)), table=table, gaps=False)


async def post(key: str = "", cause: str = "test") -> None:
    async with db.session_factory()() as session:
        queue.post(session, DEFAULT_OWNER, BROWSER, [key], cause=cause)
        await session.commit()


async def a_role_to_find(n: int = 1) -> uuid.UUID:
    """A role on deck whose posting is on the board a browser reads and that no one has sourced: a source item."""
    async with db.session_factory()() as session:
        role = Opportunity(
            owner=DEFAULT_OWNER, slug=f"role-{n}", company=f"Initech {n}", title=f"Programs Director {n}",
            match_key=f"initech {n}|programs director {n}", origin="scout", status="second_impression", fit=4,
            url=f"https://{ONE}/jobs/{n}")
        session.add(role)
        await session.commit()
        return role.id


async def items() -> list[tuple[str, str, str]]:
    """The browser's items by kind, domain and state (their targets are ids, made at random)."""
    async with db.session_factory()() as session:
        return sorted((i.kind, i.domain, i.state) for i in (await session.scalars(select(BrowserItem))).all())


async def forget_the_posts() -> None:
    async with db.session_factory()() as session:
        await session.execute(Todo.__table__.delete())
        await session.commit()


# --- the handler is the old refresh -----------------------------------------------------------------------------------


async def test_the_queue_makes_the_items_the_old_refresh_makes(js, monkeypatch):
    await browser_world()
    await a_role_to_find(1)
    await a_role_to_find(2)
    async with db.session_factory()() as session:
        assert await browser_queue.refresh(session, DEFAULT_OWNER) > 0
        await session.commit()
    old = await items()
    assert len(old) >= 2 and all(state == "waiting" for _kind, _domain, state in old)

    async with db.engine().begin() as conn:
        await empty_the_tables(conn)
    await browser_world()
    await a_role_to_find(1)
    await a_role_to_find(2)
    await forget_the_posts()
    await post()
    assert await browser_only().drain() == 1
    assert await items() == old


async def test_many_to_dos_are_one_refresh_and_it_leaves_nothing_to_look_at_again(js, monkeypatch):
    await browser_world()
    await a_role_to_find()
    calls: list[int] = []
    real = browser_queue.refresh

    async def counting(session, owner, now=None):
        calls.append(1)
        return await real(session, owner, now)

    monkeypatch.setattr(browser_queue, "refresh", counting)
    for key in ("", "*", ""):
        await post(key)
    assert await browser_only().drain() == 1
    assert calls == [1], "one refresh for all the to-dos: the to-do says it is time, whatever the pace"
    async with db.session_factory()() as session:
        plan = await browser_queue._plan(session, DEFAULT_OWNER, datetime.now(UTC))
    assert plan is not None and not (plan.adds or plan.fills or plan.reopens or plan.drops)


async def test_a_refresh_says_how_much_it_changed_and_a_paused_mission_changes_nothing(js):
    await browser_world(resumed=False)
    await a_role_to_find()
    async with db.session_factory()() as session:
        assert await browser_queue.refresh(session, DEFAULT_OWNER) == 0, "the mission is paused"
    assert await items() == []
    async with db.session_factory()() as session:
        from app.jobsearch.missions import core as missions
        from app.jobsearch.missions import record

        await missions.resume(session, await missions.get(session, DEFAULT_OWNER, record.BROWSER), by="Tester")
        await session.commit()
    async with db.session_factory()() as session:
        changed = await browser_queue.refresh(session, DEFAULT_OWNER)
        await session.commit()
    assert changed == len(await items()) >= 1


async def test_a_pass_with_nothing_to_change_changes_nothing_and_says_so(js):
    await browser_world()
    await a_role_to_find()
    async with db.session_factory()() as session:
        await browser_queue.refresh(session, DEFAULT_OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        assert await browser_queue.refresh(session, DEFAULT_OWNER) == 0


async def test_a_second_refresh_leaves_a_waiting_starred_roles_item_as_it_is(js):
    """A starred role's item is stamped with the time it was made (the newest goes first). A refresh that finds it
    still waiting, first as before, keeps that stamp: it rewrote it at every run, and so always said it had changed
    something (the hourly sweep then logged a catch each time)."""
    await browser_world()
    async with db.session_factory()() as session:  # starred, with the board's link and no source: a source item
        session.add(Opportunity(
            owner=DEFAULT_OWNER, slug="starred", company="Initech", title="Programs Director",
            match_key="initech|programs director", origin="scout", status="second_impression",
            url=f"https://www.{ONE}/jobs/view/1001", sourced_at=None, favorited_at=datetime.now(UTC)))
        await session.commit()

    async def rows() -> list[tuple]:
        async with db.session_factory()() as session:
            found = (await session.scalars(select(BrowserItem).order_by(BrowserItem.id))).all()
            return [(i.id, i.kind, i.state, i.priority, i.order_at, i.link, i.next_at) for i in found]

    async with db.session_factory()() as session:
        assert await browser_queue.refresh(session, DEFAULT_OWNER) > 0
        await session.commit()
    before = await rows()
    starred = [row for row in before if row[1] == "source"]
    assert [(state, priority) for _id, _kind, state, priority, *_ in starred] == [("waiting", 0)]
    assert starred[0][4] is not None, "the starred role's item is stamped"
    async with db.session_factory()() as session:
        assert await browser_queue.refresh(session, DEFAULT_OWNER, datetime.now(UTC) + timedelta(minutes=5)) == 0
        await session.commit()
    assert await rows() == before


async def test_the_whole_pass_the_watch_and_the_page_counts_do_not_make_the_queue(js):
    """Only `browser.reconcile` (and the start, and the owner's Run now) makes the queue. The housekeeping pass, the
    schedule's look at the queue and the Agents page's counts used to make it on their way (the owner, 2026-11-10: the
    page program runs no work)."""
    await browser_world()
    await a_role_to_find()
    async with db.session_factory()() as session:
        await housekeeping.settle(session, DEFAULT_OWNER)
        await browser_queue.watch(session, DEFAULT_OWNER)
        await pool._browser_waiting(session, DEFAULT_OWNER)
        await pool.browser_unblocks(session, DEFAULT_OWNER, pool.RESEARCHER, set())
        await session.commit()
    assert await items() == [], "none of them makes an item"
    assert await make_the_queue() >= 1, "the queue is made when it is asked for"
    assert len(await items()) >= 1


async def test_the_hand_outs_make_no_queue(js):
    """`next_item` and `code_take` hand out what is queued and make nothing: `browser.reconcile` makes the queue (the
    owner, 2026-11-10: the page program runs no work, and a hand-out is a read of the queue)."""
    await browser_world(extra_sites=False)
    await a_role_to_find()
    got = await next_item(WORKER)
    assert got.get("stop") is True and not got.get("item"), "nothing is queued, so the session is told to stop"
    async with db.session_factory()() as session:
        taken = await browser_items.code_take(session, DEFAULT_OWNER, "code-test0001", datetime.now(UTC))
        await session.commit()
    assert taken is None
    assert await items() == [], "and neither call made an item"


async def test_a_refresh_is_never_held_back_by_the_one_before_it(js, monkeypatch):
    """The 30 second throttle is gone: a refresh made at once after another still looks at the roles and sites."""
    await browser_world()
    looked: list[int] = []
    real = browser_queue._plan

    async def counting(session, owner, now):
        looked.append(1)
        return await real(session, owner, now)

    monkeypatch.setattr(browser_queue, "_plan", counting)
    for _ in range(2):
        async with db.session_factory()() as session:
            await browser_queue.refresh(session, DEFAULT_OWNER)
            await session.commit()
    assert looked == [1, 1], "the second refresh looks again at once"


# --- the leases, on the minute tick -----------------------------------------------------------------------------------


async def _a_lease_that_ran_out() -> uuid.UUID:
    await browser_world()
    await a_role_to_find()
    async with db.session_factory()() as session:
        await browser_queue.refresh(session, DEFAULT_OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        item = (await session.scalars(select(BrowserItem).where(BrowserItem.kind == "read"))).first()
        assert item is not None
        item.state, item.leased_by = "leased", "browser_reader-aaaa1111"
        item.lease_until = datetime.now(UTC) - browser_leases.LEASE
        await session.commit()
        return item.id


async def _tick() -> None:
    table = {t: h for t, h in handlers.HANDLERS.items() if t == queue_topics.TICK_MINUTE}
    d = loop.Dispatcher(DEFAULT_OWNER, table=table, gaps=False)
    async with db.session_factory()() as session:
        queue.post(session, DEFAULT_OWNER, queue_topics.TICK_MINUTE, cause="test")
        await session.commit()
    assert await d.drain() == 1


async def _state(item_id: uuid.UUID) -> str:
    async with db.session_factory()() as session:
        return (await session.get(BrowserItem, item_id)).state


async def test_the_minute_tick_gives_back_the_items_whose_lease_ran_out(js, monkeypatch):
    item_id = await _a_lease_that_ran_out()
    await _tick()
    assert await _state(item_id) == "waiting"
    assert ("ready.browser_reader", "") in await posted_now(), "and the walk is told it is work for a session"


# --- who asks it, and how often ---------------------------------------------------------------------------------------


def test_the_refresh_keeps_the_pace_it_always_had():
    handler = handlers.HANDLERS[BROWSER]
    assert handler.gap == timedelta(seconds=30), "no more often than the loop it replaces"
    assert handler.name == consume.BROWSER and handler.lane == "db"


async def test_the_sweep_and_the_start_ask_for_the_whole_queue(js):
    d = loop.Dispatcher(DEFAULT_OWNER, gaps=False)
    assert BROWSER in d.swept()
    await d.start_up()
    assert (BROWSER, "*") in await posted_now()


async def test_a_refresh_only_the_sweep_asked_for_that_changes_something_is_a_catch(js, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    await browser_world()
    await a_role_to_find()
    await forget_the_posts()  # the role was made by a write that posted nothing
    await post("*", cause="sweep")
    d = browser_only()
    await d.drain()
    assert d.tally.caught[BROWSER] >= 1
    assert [m for m in caplog.messages if "sweep_caught topic=browser.reconcile" in m]


def test_the_janitor_leaves_the_browser_to_the_dispatcher():
    assert BROWSER in consume.consumed_topics()


async def test_a_refresh_that_fails_keeps_its_to_do_and_backs_off(js, monkeypatch):
    await browser_world()

    async def broken(session, owner, now=None):
        raise RuntimeError("the roles could not be read")

    monkeypatch.setattr(browser_queue, "refresh", broken)
    await forget_the_posts()
    await post()
    d = browser_only()
    assert await d.pass_once() == 1
    (kept,) = [t for t in await posted_now() if t == (BROWSER, "")]
    async with db.session_factory()() as session:
        row = (await session.scalars(select(Todo).where(Todo.topic == BROWSER))).one()
    assert kept == (BROWSER, "") and row.attempts == 1 and row.not_before > datetime.now(UTC)
    assert timedelta(seconds=1) < row.not_before - datetime.now(UTC) <= timedelta(seconds=6)
