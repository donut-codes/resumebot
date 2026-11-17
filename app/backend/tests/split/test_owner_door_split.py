"""The owner door's writes between the two programs: the engine takes a call the page
wrote as a `pending` row, checks it in its writer's turn, runs it, and tells the page when it ends. Every action here
is made up; no network, no model calls, no sleeping."""

import asyncio
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import db, notice, program
from app.errors import NotFound
from app.jobsearch import facts
from app.jobsearch.models import ActionRun, Todo
from app.jobsearch.owner_actions import REGISTRY, Action, asked, runner
from app.jobsearch.owner_actions import mcp as owner_mcp
from app.jobsearch.owner_actions.registry import Args, Ctx, Outcome
from app.jobsearch.work import engine_asks, engine_calls
from app.jobsearch.work.queue import handlers, loop, topics
from app.settings import settings
from tests.support import OWNER, pin_clock

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


class NoArgs(Args):
    pass


@pytest.fixture(autouse=True)
def the_owner_and_the_clock(monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    pin_clock(monkeypatch, NOW)


@pytest.fixture
def actions(monkeypatch):
    """Five made-up write actions: `test.say` (done at once), `test.fails` (refused), `test.once` (waits for
    the gate, one at a time), `test.follow` (goes on after it ends, and is brought up to date by its follow) and
    `test.write` (saves a fact). `entered` is set when `test.once` has begun, `gate` lets it end."""
    entered, gate = asyncio.Event(), asyncio.Event()

    async def say(ctx: Ctx, args: NoArgs) -> Outcome:
        return Outcome({"done": 1}, did="said something", would="say something")

    async def fails(ctx: Ctx, args: NoArgs) -> Outcome:
        raise NotFound("made-up refusal")

    async def wait(ctx: Ctx, args: NoArgs) -> Outcome:
        entered.set()
        await gate.wait()
        return Outcome({"done": 2}, did="waited", would="wait")

    async def start(ctx: Ctx, args: NoArgs) -> Outcome:
        return Outcome({"started": 1}, did="started something", would="start something", follow=True)

    async def follow(session, row) -> None:
        row.progress = "3 of 4"

    async def write(ctx: Ctx, args: NoArgs) -> Outcome:
        await facts.put(ctx.session, ctx.owner, "made-up", "x")
        return Outcome({}, did="wrote", would="write")

    for name, run, flags in (("test.say", say, {}), ("test.fails", fails, {}),
                             ("test.once", wait, {"one_at_a_time": True}), ("test.follow", start, {"follow": follow}),
                             ("test.write", write, {})):
        monkeypatch.setitem(REGISTRY, name, Action(name, name, NoArgs, run, writes=True, **flags))
    return SimpleNamespace(entered=entered, gate=gate)


async def _pending(name: str, args: dict | None = None, *, dry_run: bool = False, state: str = "pending") -> uuid.UUID:
    """A job's row as the page writes it (or, with `state`, as the engine left it); its id."""
    async with db.session_factory()() as session:
        row = ActionRun(owner=OWNER, action=name, arguments=args or {}, dry_run=dry_run, by="claude", state=state,
                        started=NOW)
        session.add(row)
        await session.flush()
        job_id = row.id
        await session.commit()
    return job_id


async def _state(job_id: uuid.UUID) -> str | None:
    async with db.read_session_factory()() as reader:
        return await reader.scalar(select(ActionRun.state).where(ActionRun.id == job_id))


async def _row(job_id: uuid.UUID) -> ActionRun:
    async with db.read_session_factory()() as reader:
        row = await reader.get(ActionRun, job_id)
        assert row is not None
        return row


async def _fact(key: str) -> str | None:
    async with db.read_session_factory()() as reader:
        return await facts.get(reader, OWNER, key)


async def test_begin_takes_a_pending_row_once_and_the_job_runs(js, actions):
    job_id = await _pending("test.say")
    assert await runner.begin(OWNER, job_id) == {"started": True}
    await runner.settle()
    row = await _row(job_id)
    assert row.state == "done" and row.result["said"] == "Said something."
    assert await runner.begin(OWNER, job_id) == {"started": False}, "the row is not pending any more: nothing starts"
    await runner.settle()
    assert not runner._tasks


async def test_begin_of_another_owners_row_or_of_no_row_starts_nothing(js, actions):
    job_id = await _pending("test.say")
    assert await runner.begin("someone-else", job_id) == {"started": False}
    assert await runner.begin(OWNER, uuid.UUID(int=77)) == {"started": False}
    assert await _state(job_id) == "pending" and not runner._tasks


@pytest.mark.parametrize(("name", "args", "status", "words"), [
    ("test.nowhere", {}, 404, "there is no action test.nowhere"),
    ("test.say", {"extra": 1}, 422, "does not take"),
])
async def test_begin_refuses_an_unknown_action_and_bad_arguments_and_keeps_no_row(
        js, actions, name, args, status, words):
    job_id = await _pending(name, args)
    got = await runner.begin(OWNER, job_id)
    assert got["refused"]["status"] == status and words in got["refused"]["reason"]
    assert await _state(job_id) is None and not runner._tasks


async def test_begin_refuses_a_second_of_a_one_at_a_time_action_naming_the_holder_and_keeps_no_row(js, actions):
    holder = await _pending("test.once", state="running")
    second = await _pending("test.once")
    got = await runner.begin(OWNER, second)
    assert got["refused"]["status"] == 409
    assert got["refused"]["reason"].startswith(f"test.once is running already (job {holder}, started ")
    assert await _state(second) is None and await _state(holder) == "running" and not runner._tasks


async def test_begin_lets_a_dry_run_through_and_the_dry_run_says_it_would_be_refused(js, actions):
    await _pending("test.once", state="running")
    dry = await _pending("test.once", dry_run=True)
    assert await runner.begin(OWNER, dry) == {"started": True}
    await runner.settle()
    row = await _row(dry)
    assert row.state == "done" and row.result["would_be_refused"].startswith("test.once is running already (job ")


@pytest.mark.parametrize("args", [{"job": "nope"}, {}, {"job": str(uuid.UUID(int=5))}])
async def test_the_ask_for_a_row_that_cannot_be_found_answers_not_started(js, actions, args):
    await engine_asks.answer(OWNER, "g1", {"name": asked.ASK_DO, "args": args})
    assert json.loads(await _fact("answer:g1")) == {"started": False}


async def test_the_ask_for_a_pending_row_begins_it_and_saves_the_answer(js, actions):
    job_id = await _pending("test.say")
    await engine_asks.answer(OWNER, "a1", {"name": asked.ASK_DO, "args": {"job": str(job_id)}})
    assert json.loads(await _fact("answer:a1")) == {"started": True}
    await runner.settle()
    assert await _state(job_id) == "done"


@pytest.mark.parametrize(("name", "dry_run", "state"), [
    ("test.say", False, "done"), ("test.say", True, "done"), ("test.fails", False, "failed")])
async def test_the_end_of_a_job_tells_the_page_once_after_the_row_is_saved_and_wakes_the_waiter(
        js, actions, monkeypatch, name, dry_run, state):
    told: list = []
    seen: list[str] = []

    def tell(owner, items):
        other = sqlite3.connect(js.url.database, timeout=5)  # what any other program would read at this moment
        try:
            seen.append(other.execute("select state from js_action_runs").fetchone()[0])
        finally:
            other.close()
        told.append((owner, list(items)))
        return True

    monkeypatch.setattr(notice, "tell_page", tell)
    job_id = await _pending(name, dry_run=dry_run)
    waiter = notice.waiters.wait_for("action_run", [str(job_id)])
    await runner.begin(OWNER, job_id)
    await runner.settle()
    assert told == [(OWNER, [("action_run", str(job_id))])], "once"
    assert seen == [state], "the row was already saved when the page was told"
    assert await waiter.wait(0.01) is True


async def test_the_follow_ask_brings_a_following_job_up_to_date_and_leaves_any_other_alone(js, actions):
    following = await _pending("test.follow", state="running")
    other = await _pending("test.say", state="running")
    for ask_id, job_id, followed in (("f1", following, True), ("f2", other, False)):
        await engine_asks.answer(OWNER, ask_id, {"name": asked.ASK_FOLLOW, "args": {"job": str(job_id)}})
        assert json.loads(await _fact(f"answer:{ask_id}")) == {"followed": followed}
    assert (await _row(following)).progress == "3 of 4" and (await _row(other)).progress == ""


OWNER_TOKEN = "js_owner_token_1234567890"


def _dispatcher() -> loop.Dispatcher:
    """The engine's dispatcher with only the handler of the asks, on the pinned clock, with no pauses."""
    table = {topics.ENGINE_ASK: handlers.HANDLERS[topics.ENGINE_ASK]}
    return loop.Dispatcher(OWNER, clock=lambda: NOW, table=table, gaps=False)


@pytest.fixture
def engine(monkeypatch):
    """The engine of this test process: every ring of the bell (a nudge) makes the dispatcher look at the asks once, as
    the real engine's `look` does. `looks` are the looks made, so a test can wait for them."""
    dispatcher = _dispatcher()
    looks: list[asyncio.Task] = []
    monkeypatch.setattr(engine_calls, "nudge", lambda: looks.append(asyncio.get_running_loop().create_task(
        dispatcher.drain())))
    return SimpleNamespace(dispatcher=dispatcher, looks=looks)


async def _quiet(engine) -> None:
    """Every look is over and every job's task has ended."""
    await asyncio.gather(*engine.looks)
    await runner.settle()


async def _page_do(name: str, args: dict | None = None, dry_run: bool = False) -> dict:
    async with db.session_factory()() as session:
        return await asked.do(session, OWNER, name, args, dry_run)


async def _rows() -> int:
    async with db.read_session_factory()() as reader:
        return await reader.scalar(select(func.count()).select_from(ActionRun)) or 0


async def test_the_page_asks_and_the_engine_answers_with_the_finished_result(js, engine, actions):
    got = await asyncio.wait_for(_page_do("test.say", {}), 10)
    assert got["ok"] is True and got["state"] == "done" and got["said"] == "Said something."
    assert got["dry_run"] is False and got["result"] == {"done": 1, "changed": True}
    await _quiet(engine)
    assert await _rows() == 1


async def test_with_no_engine_the_open_job_is_answered_after_the_wait_and_the_ask_is_kept(js, actions, monkeypatch):
    monkeypatch.setattr(program, "PROGRAM", "page")  # a page asks; in the whole app the ask runs on the spot
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)  # nobody looks
    monkeypatch.setattr(asked, "ASK_SECONDS", 0.01)
    got = await _page_do("test.say", {})
    assert got["ok"] is True and got["state"] == "pending" and "not started yet" in got["said"]
    assert await _state(uuid.UUID(got["job"])) == "pending"
    async with db.read_session_factory()() as reader:
        asks = await facts.get_many(reader, OWNER, "ask:")
        todos = list((await reader.scalars(select(Todo.topic))).all())
    assert await _rows() == 1 and todos == ["ask"]
    assert len(asks) == 1 and json.loads(next(iter(asks.values())))["name"] == asked.ASK_DO


async def test_a_call_left_pending_is_begun_once_by_the_engines_look_at_its_start(js, actions, monkeypatch):
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    monkeypatch.setattr(asked, "ASK_SECONDS", 0.01)
    job_id = uuid.UUID((await _page_do("test.say", {}))["job"])
    starting = _dispatcher()  # the engine starts: it posts `*` for the asks, then looks
    await starting.start_up()
    assert await starting.drain() >= 1
    await runner.settle()
    assert await _state(job_id) == "done"
    assert await starting.drain() == 0, "the ask has its answer: a second look begins nothing"


async def test_a_notice_lost_but_the_row_finished_is_answered_at_the_re_read(js, engine, actions, monkeypatch):
    monkeypatch.setattr(runner, "_tell_ended", lambda owner, job_id: None)  # the job ends and tells no one
    real_wait = notice.Waiter.wait

    async def late(self, timeout):
        await asyncio.gather(*engine.looks)  # the engine looked and answered the ask
        await runner.settle()  # and the job ended
        return await real_wait(self, 0)  # no notice for the job came: the time is out

    monkeypatch.setattr(notice.Waiter, "wait", late)
    got = await asyncio.wait_for(_page_do("test.say", {}), 10)
    assert got["state"] == "done" and got["said"] == "Said something."


async def test_two_at_once_the_second_is_refused_naming_the_holder_and_leaves_no_row(js, engine, actions):
    first = asyncio.create_task(_page_do("test.once", {}))
    await asyncio.wait_for(actions.entered.wait(), 10)
    with pytest.raises(asked.Refused) as refused:
        await asyncio.wait_for(_page_do("test.once", {}), 10)
    assert refused.value.status == 409 and str(refused.value).startswith("test.once is running already (job ")
    actions.gate.set()
    done = await asyncio.wait_for(first, 10)
    assert done["state"] == "done" and str(refused.value).count(done["job"]) == 1
    await _quiet(engine)
    assert await _rows() == 1


@pytest.mark.parametrize(("name", "args", "status", "words"), [
    ("test.nowhere", {}, 404, "there is no action test.nowhere"),
    ("test.say", {"extra": 1}, 422, "does not take"),
])
async def test_a_call_the_engine_refuses_raises_its_words_and_leaves_no_row(js, engine, actions, name, args, status,
                                                                            words):
    with pytest.raises(asked.Refused) as refused:
        await asyncio.wait_for(_page_do(name, args), 10)
    assert refused.value.status == status and words in str(refused.value)
    await _quiet(engine)
    assert await _rows() == 0


async def test_a_dry_run_asked_through_the_engine_changes_nothing_and_says_so_first(js, engine, actions):
    got = await asyncio.wait_for(_page_do("test.write", {}, True), 10)
    assert got["ok"] is True and got["dry_run"] is True and list(got)[1] == "said"
    assert got["said"].startswith("Nothing was changed")
    await _quiet(engine)
    assert await _fact("made-up") is None and await _state(uuid.UUID(got["job"])) == "done"


async def test_a_real_write_asked_through_the_engine_is_kept(js, engine, actions):
    got = await asyncio.wait_for(_page_do("test.write", {}), 10)
    assert got["state"] == "done" and got["dry_run"] is False
    await _quiet(engine)
    assert await _fact("made-up") == "x"


async def test_the_page_and_the_engine_hold_no_turn_while_the_action_runs(js, engine, actions):
    calling = asyncio.create_task(_page_do("test.once", {}))
    await asyncio.wait_for(actions.entered.wait(), 10)

    async def another_writer() -> None:
        async with db.session_factory()() as other:
            await facts.put(other, OWNER, "night_left", "x", now=NOW)
            await other.commit()

    await asyncio.wait_for(another_writer(), 5)  # with a turn held by the wait this would end in Busy
    actions.gate.set()
    assert (await asyncio.wait_for(calling, 10))["state"] == "done"
    await _quiet(engine)
    assert await _fact("night_left") == "x"


async def test_act_job_asks_the_engine_to_bring_a_following_job_up_to_date(js, engine, actions):
    started = await asyncio.wait_for(_page_do("test.follow", {}), 10)
    assert started["state"] == "running" and "not done yet" in started["said"]
    async with db.session_factory()() as session:
        shown = await asyncio.wait_for(asked.job(session, OWNER, started["job"]), 10)
    assert shown["state"] == "running" and shown["progress"] == "3 of 4"
    await _quiet(engine)


async def test_act_job_reads_a_finished_job_and_the_list_and_refuses_what_is_no_job_without_asking(
        js, engine, actions, monkeypatch):
    done = await asyncio.wait_for(_page_do("test.say", {}), 10)
    await _quiet(engine)

    async def no_ask(*args, **kwargs):
        raise AssertionError("a finished job, the list and a refusal need no ask")

    monkeypatch.setattr(engine_calls, "ask", no_ask)
    async with db.session_factory()() as session:
        one = await asked.job(session, OWNER, done["job"])
        many = await asked.job(session, OWNER, None)
        with pytest.raises(NotFound):
            await asked.job(session, OWNER, str(uuid.UUID(int=9)))
        with pytest.raises(asked.Refused) as refused:
            await asked.job(session, OWNER, "not-an-id")
    assert one["state"] == "done" and [j["job"] for j in many["jobs"]] == [done["job"]]
    assert refused.value.status == 422


async def test_the_local_backend_runs_the_ones_own_do_and_job(monkeypatch):
    seen: list = []

    async def do(owner, name, raw, dry_run):
        seen.append(("do", owner, name, raw, dry_run))
        return {"ok": True, "via": "do"}

    async def job(session, owner, job_id):
        seen.append(("job", session, owner, job_id))
        return {"ok": True, "via": "job"}

    monkeypatch.setattr(runner, "do", do)
    monkeypatch.setattr(runner, "job", job)
    backend = engine_calls.Local()
    assert await backend.owner_do("S", OWNER, "x", {"a": 1}, True) == {"ok": True, "via": "do"}
    assert await backend.owner_job("S", OWNER, "j") == {"ok": True, "via": "job"}
    assert seen == [("do", OWNER, "x", {"a": 1}, True), ("job", "S", OWNER, "j")]


async def test_the_remote_backend_writes_a_row_and_asks_the_engine(monkeypatch):
    seen: list = []

    async def do(session, owner, name, raw, dry_run):
        seen.append(("do", session, owner, name, raw, dry_run))
        return {"ok": True, "via": "do"}

    async def job(session, owner, job_id):
        seen.append(("job", session, owner, job_id))
        return {"ok": True, "via": "job"}

    monkeypatch.setattr(asked, "do", do)
    monkeypatch.setattr(asked, "job", job)
    backend = engine_calls.Remote()
    assert await backend.owner_do("S", OWNER, "x", {"a": 1}, True) == {"ok": True, "via": "do"}
    assert await backend.owner_job("S", OWNER, "j") == {"ok": True, "via": "job"}
    assert seen == [("do", "S", OWNER, "x", {"a": 1}, True), ("job", "S", OWNER, "j")]


@pytest.fixture
def owner_door(tmp_path, monkeypatch):
    """A context carrying the owner's token, as the `door` fixture of test_owner_door_page.py makes it. The owner the
    door works for is `settings.owner`, which the autouse fixture at the top of this file sets to OWNER."""
    (tmp_path / "owner-token").write_text(OWNER_TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr(settings, "owner_token_file", str(tmp_path / "owner-token"))
    return SimpleNamespace(headers={"authorization": f"Bearer {OWNER_TOKEN}"})


async def test_the_door_tools_act_do_and_act_job_go_through_the_installed_engine_door(js, owner_door):
    class Recorder:
        def nudge(self) -> None:
            return None

        async def owner_do(self, session, owner, name, raw, dry_run):
            return {"ok": True, "via": "do", "got": [owner, name, raw, dry_run]}

        async def owner_job(self, session, owner, job_id):
            return {"ok": True, "via": "job", "got": [owner, job_id]}

    engine_calls.install(Recorder())
    try:
        did = await owner_mcp.act_do(owner_door, "requeue", {"scope": "stale"}, True)
        job = await owner_mcp.act_job(owner_door, "abc")
    finally:
        engine_calls.install(None)
    assert did == {"ok": True, "via": "do", "got": [OWNER, "requeue", {"scope": "stale"}, True]}
    assert job == {"ok": True, "via": "job", "got": [OWNER, "abc"]}


async def test_an_answer_of_not_started_is_not_waited_for(js, monkeypatch):
    async def ask(session, owner, name, args, wait_s):
        await db.release(session)  # saves the row, as the real ask does
        return {"started": False}

    async def no_wait(self, timeout):
        raise AssertionError("an answer of started false has no job to wait for")

    monkeypatch.setattr(engine_calls, "ask", ask)
    monkeypatch.setattr(notice.Waiter, "wait", no_wait)
    got = await _page_do("test.say", {})
    assert got["state"] == "pending" and "not started yet" in got["said"]
