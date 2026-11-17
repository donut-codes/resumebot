"""Ask the engine and wait for its notice (the one pattern for work the page must not do): a question is a fact and a
to-do; the engine answers it in a fact and tells the page's waiter. Every name here is made up."""

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db, notice, program
from app.jobsearch import facts
from app.jobsearch.models import Todo
from app.jobsearch.work import engine_asks, engine_calls
from app.jobsearch.work.queue import consume, handlers, loop, service, topics
from app.timeutil import iso_utc
from tests.jobsearch.work_queue.support import posted_now
from tests.support import OWNER, pin_clock


def test_the_ask_topic_is_one_the_dispatcher_takes_and_the_janitor_leaves_alone():
    assert topics.ENGINE_ASK == "ask"
    assert topics.ENGINE_ASK in consume.consumed_topics()
    assert consume.OWN_TOPIC_OF[consume.ASKS] == topics.ENGINE_ASK
    assert consume.ASKS in consume.NAMES


def test_no_writer_posts_the_ask_topic_by_itself():
    """Only `engine_calls.ask` posts it, with the ask's id: the tripwire's catch-all must never post it for a table."""
    assert topics.ENGINE_ASK not in topics.WRITE_DRIVEN


def test_the_ask_lane_runs_two_at_once_so_a_slow_ask_holds_no_db_lane():
    assert loop.LANES["ask"] == 2


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
ID = "a1b2c3"


@pytest.fixture(autouse=True)
def _no_registered_asks(monkeypatch):
    """Each test registers its own functions: nothing registered by another test (or by the app) is seen."""
    monkeypatch.setattr(engine_asks, "_asks", {})


def _ask(name: str, args: dict, at: datetime) -> str:
    return json.dumps({"name": name, "args": args, "asked_at": iso_utc(at)})


def _run(keys: list[str]) -> handlers.Run:
    group = service.Group(owner=OWNER, topic=topics.ENGINE_ASK, keys=tuple(keys), ids=(1,), causes=frozenset({"ask"}),
                          attempts=0, waiting_since=NOW)
    return handlers.Run(OWNER, group, NOW, handlers.Tally())


async def _put(key: str, value: str, at: datetime = NOW) -> None:
    async with db.session_factory()() as session:
        await facts.put(session, OWNER, key, value, now=at)
        await session.commit()


async def _get(key: str) -> str | None:
    async with db.read_session_factory()() as reader:
        return await facts.get(reader, OWNER, key)


def _told(monkeypatch) -> list:
    told: list = []
    monkeypatch.setattr(notice, "tell_page", lambda owner, items: told.append((owner, list(items))) or True)
    return told


def test_the_handler_is_on_the_ask_lane_with_no_pause():
    handler = handlers.HANDLERS[topics.ENGINE_ASK]
    assert (handler.name, handler.lane, handler.gap) == (consume.ASKS, "ask", timedelta(0))


async def test_an_ask_is_answered_by_its_registered_function_and_the_page_is_told(js, monkeypatch):
    seen: list = []

    async def rank(session, owner, args):
        seen.append((owner, args))
        return {"ranked": args["n"]}

    engine_asks.register("rank", rank)
    told = _told(monkeypatch)
    waiter = notice.waiters.wait_for("ask", [ID])
    await _put(f"ask:{ID}", _ask("rank", {"n": 3}, NOW))
    outcome = await handlers.engine_ask(_run([ID]))
    assert seen == [(OWNER, {"n": 3})]
    assert json.loads(await _get(f"answer:{ID}")) == {"ranked": 3}
    assert told == [(OWNER, [("ask", ID)])]
    assert await waiter.wait(0.01) is True
    assert outcome.changed == 0 and outcome.cheap is False


async def test_an_ask_that_already_has_an_answer_is_not_answered_again(js, monkeypatch):
    calls: list = []

    async def rank(session, owner, args):
        calls.append(args)
        return {}

    engine_asks.register("rank", rank)
    told = _told(monkeypatch)
    await _put(f"ask:{ID}", _ask("rank", {}, NOW))
    await _put(f"answer:{ID}", "{}")
    outcome = await handlers.engine_ask(_run([ID]))
    assert calls == [] and told == [] and outcome.cheap is True


async def test_a_look_with_no_ask_owed_is_cheap_and_tells_no_one(js, monkeypatch):
    told = _told(monkeypatch)
    outcome = await handlers.engine_ask(_run([topics.ANY]))
    assert outcome.cheap is True and outcome.changed == 0 and told == []


async def test_an_ask_text_that_cannot_be_read_is_left_alone_and_does_not_stop_the_look(js):
    await _put("ask:bad", "this is not json")
    outcome = await handlers.engine_ask(_run([topics.ANY]))
    assert outcome.cheap is True and await _get("answer:bad") is None


async def test_an_ask_nobody_registered_raises_so_the_dispatcher_keeps_its_to_do_and_backs_off(js):
    await _put(f"ask:{ID}", _ask("nobody", {}, NOW))
    with pytest.raises(LookupError):
        await handlers.engine_ask(_run([ID]))
    assert await _get(f"answer:{ID}") is None


async def test_one_function_that_raises_does_not_stop_the_other_asks_of_the_look(js):
    async def boom(session, owner, args):
        raise RuntimeError("this one breaks")

    async def fine(session, owner, args):
        return {"ok": True}

    engine_asks.register("boom", boom)
    engine_asks.register("fine", fine)
    await _put("ask:one", _ask("boom", {}, NOW))
    await _put("ask:two", _ask("fine", {}, NOW))
    with pytest.raises(RuntimeError):
        await handlers.engine_ask(_run([topics.ANY]))
    assert json.loads(await _get("answer:two")) == {"ok": True}
    assert await _get("answer:one") is None


async def test_the_start_up_sweep_answers_an_ask_left_unanswered_but_not_an_old_or_answered_one(js, monkeypatch):
    pin_clock(monkeypatch, NOW)
    calls: list = []

    async def rank(session, owner, args):
        calls.append(args["n"])
        return {"ranked": args["n"]}

    engine_asks.register("rank", rank)
    await _put("ask:left", _ask("rank", {"n": 1}, NOW - timedelta(hours=3)))
    await _put("ask:ancient", _ask("rank", {"n": 2}, NOW - timedelta(days=2)))
    await _put("ask:done", _ask("rank", {"n": 3}, NOW - timedelta(hours=1)))
    await _put("answer:done", "{}")
    table = {topics.ENGINE_ASK: handlers.HANDLERS[topics.ENGINE_ASK]}
    dispatcher = loop.Dispatcher(OWNER, clock=lambda: NOW, table=table, gaps=False)
    await dispatcher.start_up()
    assert await posted_now() == [(topics.ENGINE_ASK, topics.ANY)]
    assert await dispatcher.drain() == 1
    assert calls == [1]
    assert json.loads(await _get("answer:left")) == {"ranked": 1}
    assert await _get("answer:ancient") is None
    assert await posted_now() == []


@pytest.fixture
def pinned(monkeypatch):
    """The app's clock says NOW and the next ask gets the id ID, so a test can name the rows the ask wrote."""
    pin_clock(monkeypatch, NOW)
    monkeypatch.setattr(engine_calls, "_new_id", lambda: ID)


async def test_the_page_holds_no_transaction_during_the_wait_and_another_writer_commits_meanwhile(js, pinned,
                                                                                                    monkeypatch):
    rung = asyncio.Event()
    open_when_rung: list[bool] = []
    async with db.session_factory()() as session:

        def ring() -> None:
            open_when_rung.append(session.in_transaction())
            if not session.in_transaction():
                rung.set()  # a commit's own hook may nudge too, inside the commit: only the ask's own ring counts

        monkeypatch.setattr(engine_calls, "nudge", ring)
        waiting = asyncio.create_task(engine_calls.ask(session, OWNER, "rank", {"n": 1}, 10))
        await asyncio.wait_for(rung.wait(), 5)
        async with db.session_factory()() as other:
            await facts.put(other, OWNER, "night_left", "x", now=NOW)
            await other.commit()  # the turn is free: with the ask holding it this would end in Busy
        notice.waiters.notify("ask", [ID])
        assert await asyncio.wait_for(waiting, 5) is None  # woken, and the engine wrote no answer
    assert open_when_rung[-1] is False
    assert await _get("night_left") == "x"


async def test_a_notice_that_comes_before_the_wait_starts_returns_at_once(js, pinned, monkeypatch):
    await _put(f"answer:{ID}", json.dumps({"ranked": 2}))
    monkeypatch.setattr(engine_calls, "nudge", lambda: notice.waiters.notify("ask", [ID]))
    async with db.session_factory()() as session:
        got = await asyncio.wait_for(engine_calls.ask(session, OWNER, "rank", {}, 30), 5)  # 30 s asked, 5 s allowed
    assert got == {"ranked": 2}


async def test_a_lost_notice_costs_nothing_the_answer_is_read_when_the_wait_ends(js, pinned, monkeypatch):
    await _put(f"answer:{ID}", json.dumps({"ranked": 5}))
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)  # the engine answered and its notice never came
    async with db.session_factory()() as session:
        got = await engine_calls.ask(session, OWNER, "rank", {}, 0.01)
    assert got == {"ranked": 5}


async def test_with_no_engine_the_ask_returns_none_after_its_time_and_the_fact_and_the_to_do_are_kept(js, pinned,
                                                                                                        monkeypatch):
    rung: list[bool] = []
    monkeypatch.setattr(engine_calls, "nudge", lambda: rung.append(True))
    async with db.session_factory()() as session:
        got = await engine_calls.ask(session, OWNER, "rank", {"n": 2}, 0.01)
    assert got is None and rung
    assert json.loads(await _get(f"ask:{ID}")) == {"name": "rank", "args": {"n": 2}, "asked_at": iso_utc(NOW)}
    assert await _get(f"answer:{ID}") is None
    async with db.read_session_factory()() as reader:
        todos = (await reader.execute(select(Todo.owner, Todo.topic, Todo.key, Todo.cause))).all()
    assert [tuple(row) for row in todos] == [(OWNER, "ask", ID, "ask")]
    assert notice.waiters.notify("ask", [ID]) == 0, "the waiter was removed when the wait ended"


async def test_an_ask_is_answered_end_to_end_by_the_engines_handler_and_the_notice_wakes_the_wait(js, pinned,
                                                                                                   monkeypatch):
    async def rank(session, owner, args):
        return {"ranked": args["n"]}

    engine_asks.register("rank", rank)
    table = {topics.ENGINE_ASK: handlers.HANDLERS[topics.ENGINE_ASK]}
    dispatcher = loop.Dispatcher(OWNER, clock=lambda: NOW, table=table, gaps=False)
    running: list[asyncio.Task] = []
    monkeypatch.setattr(program, "PROGRAM", "page")  # a page asks; in the whole app the ask runs on the spot

    def nudge() -> None:
        if not running:  # the engine looks once; a second nudge of the same ask changes nothing
            running.append(asyncio.get_running_loop().create_task(dispatcher.drain()))

    monkeypatch.setattr(engine_calls, "nudge", nudge)
    async with db.session_factory()() as session:
        got = await asyncio.wait_for(engine_calls.ask(session, OWNER, "rank", {"n": 4}, 30), 10)
    await asyncio.gather(*running)
    assert got == {"ranked": 4}
    assert await posted_now() == []


# --- an ask to an engine that is not running ------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_nothing_listens_is_false_when_the_signal_is_off(monkeypatch):
    monkeypatch.setattr(notice, "_injected", None)
    assert engine_calls._nothing_listens() is False


def test_nothing_listens_is_true_for_a_free_engine_port_and_false_for_one_in_use(monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(notice, "_injected", (_free_port(), listener.getsockname()[1]))
        assert engine_calls._nothing_listens() is False
    monkeypatch.setattr(notice, "_injected", (_free_port(), _free_port()))
    assert engine_calls._nothing_listens() is True


def _spy_on_the_wait(monkeypatch) -> list:
    """Every wait the ask makes is noted (the time it was asked for) and ends at once."""
    waited: list = []
    real = notice.waiters.wait_for

    def spy(kind, ids):
        waiter = real(kind, ids)
        inner = waiter.wait

        async def wait(timeout):
            waited.append(timeout)
            return await inner(0.01)

        waiter.wait = wait
        return waiter

    monkeypatch.setattr(notice.waiters, "wait_for", spy)
    return waited


async def test_with_nothing_on_the_engines_port_the_ask_does_not_wait_and_is_kept(js, pinned, monkeypatch):
    monkeypatch.setattr(notice, "_injected", (_free_port(), _free_port()))
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    waited = _spy_on_the_wait(monkeypatch)
    async with db.session_factory()() as session:
        got = await asyncio.wait_for(engine_calls.ask(session, OWNER, "rank", {"n": 2}, 30), 5)
    assert got is None and waited == [], "it did not wait"
    assert json.loads(await _get(f"ask:{ID}")) == {"name": "rank", "args": {"n": 2}, "asked_at": iso_utc(NOW)}
    assert await _get(f"answer:{ID}") is None
    async with db.read_session_factory()() as reader:
        todos = (await reader.execute(select(Todo.owner, Todo.topic, Todo.key, Todo.cause))).all()
    assert [tuple(row) for row in todos] == [(OWNER, "ask", ID, "ask")]
    assert notice.waiters.notify("ask", [ID]) == 0, "the waiter was removed"


async def test_with_a_listener_on_the_engines_port_the_ask_waits_as_it_did(js, pinned, monkeypatch):
    await _put(f"answer:{ID}", json.dumps({"ranked": 7}))
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    waited = _spy_on_the_wait(monkeypatch)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(notice, "_injected", (_free_port(), listener.getsockname()[1]))
        async with db.session_factory()() as session:
            got = await asyncio.wait_for(engine_calls.ask(session, OWNER, "rank", {}, 30), 5)
    assert waited == [30], "it waited the time it was given"
    assert got == {"ranked": 7}
