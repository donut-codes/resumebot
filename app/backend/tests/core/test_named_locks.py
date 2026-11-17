"""Named locks: one process holds the database, so what the
advisory locks did across connections is an ordinary lock here, `db.named_lock`. These tests use a SQLite file of
their own and no schema: a lock is not a row.

Two kinds, as the advisory locks were two: `named_lock` is held for a block (the re-gate's, which spans the commits of a
run), and `transaction_lock` is held until the session's transaction ends (the others). Each site that took an advisory
lock is tested for the behaviour it kept."""

import asyncio
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app import db
from app.errors import Busy
from app.jobsearch.browser import reader
from app.jobsearch.gating import regate
from app.jobsearch.pipeline import quiet
from app.jobsearch.work import hand_outs


@pytest.fixture
async def engine(tmp_path):
    """The app's engine on an empty SQLite file of its own."""
    made = db.configure(db.sqlite_url(tmp_path / "locks.db"))
    yield made
    await made.dispose()
    if db._lifeline_engine is not None:
        await db._lifeline_engine.dispose()
    db._engine = db._sessions = db._lifeline_engine = db._lifeline_sessions = None


@pytest.fixture
def key():
    """A name no other test uses: the locks live in the process."""
    return f"test:{uuid.uuid4()}"


@pytest.fixture
def short_wait(monkeypatch):
    """A lock is waited for 0.15 seconds (the app's is 5, and 20 in the background) before the caller is told busy."""
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 150)
    monkeypatch.setattr(db, "BACKGROUND_LOCK_TIMEOUT_MS", 150)


def a_session():
    return db.session_factory()()


def waited(wait: float) -> float:
    """The least a wait of `wait` seconds reads between two reads of the loop's clock. The wait is `asyncio.timeout`,
    which runs on that clock (`time.monotonic`), and the loop fires a timer up to one step of the clock early. On
    Windows the step is 15.6 ms and a reading is in whole milliseconds, so a wait of 150 ms reads 140 or 141 ms when it
    fires nine steps on, and 140 ms as a difference of two floats is a hair under 0.14. A millisecond more is let go
    for that rounding. A wait that was not made reads next to nothing, far under this."""
    return wait - time.get_clock_info("monotonic").resolution - 0.001


# --- named_lock: held for a block --------------------------------------------------------------------------------


async def test_one_holder_at_a_time_and_the_second_goes_in_when_the_first_leaves(key):
    order: list[str] = []
    first_in, leave = asyncio.Event(), asyncio.Event()

    async def first():
        async with db.named_lock(key):
            order.append("first in")
            first_in.set()
            await leave.wait()
            order.append("first out")

    async def second():
        await asyncio.wait_for(first_in.wait(), 5)
        async with db.named_lock(key):
            order.append("second in")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await asyncio.wait_for(first_in.wait(), 5)
    await asyncio.sleep(0.05)
    assert order == ["first in"], "the second waits"
    leave.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert order == ["first in", "first out", "second in"]


async def test_a_second_caller_that_will_not_wait_is_told_busy_and_the_holder_is_not_disturbed(key):
    async with db.named_lock(key):
        with pytest.raises(Busy, match="a run of this is going"):
            async with db.named_lock(key, wait=0, why="a run of this is going"):
                pytest.fail("the lock was not free")
        async with db.named_lock(key + "-another", wait=0):
            pass  # another name is free
    async with db.named_lock(key, wait=0):
        pass  # and this one is, now its holder has left


async def test_a_caller_waits_as_long_as_it_is_told_and_then_is_told_busy(key):
    async with db.named_lock(key):
        began = asyncio.get_running_loop().time()
        with pytest.raises(Busy):
            async with db.named_lock(key, wait=0.1):
                pytest.fail("the lock was not free")
        assert 0.09 <= asyncio.get_running_loop().time() - began < 1.0


async def test_a_caller_that_waits_gets_the_lock_when_the_holder_leaves_in_time(key):
    entered = asyncio.Event()

    async def holder():
        async with db.named_lock(key):
            entered.set()
            await asyncio.sleep(0.1)

    running = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), 5)
    async with db.named_lock(key, wait=5):
        pass
    await running


async def test_the_lock_is_let_go_when_the_block_raises(key):
    with pytest.raises(RuntimeError, match="boom"):
        async with db.named_lock(key):
            raise RuntimeError("boom")
    async with db.named_lock(key, wait=0):
        pass


async def test_a_new_event_loop_begins_with_the_lock_free(key):
    """Held for good on a loop that is gone (a test's), the lock does not stand in the way of the next loop."""
    held = threading.Event()

    async def hold_and_never_leave():
        await db.named_lock(key).__aenter__()
        held.set()

    await asyncio.to_thread(lambda: asyncio.run(hold_and_never_leave()))
    assert held.is_set()
    async with db.named_lock(key, wait=0):
        pass


# --- transaction_lock: held until the session's transaction ends -------------------------------------------------


async def test_a_transaction_lock_is_held_until_the_transaction_commits(engine, key):
    async with a_session() as one, a_session() as two:
        assert await db.transaction_lock(one, key) is True
        assert one.in_transaction(), "the lock lasts to the end of a transaction, so taking it begins one"
        assert await db.transaction_lock(two, key, wait=0) is False
        await one.commit()
        assert await db.transaction_lock(two, key, wait=0) is True


async def test_a_transaction_lock_is_let_go_by_a_rollback_and_by_closing_the_session(engine, key):
    async with a_session() as one:
        await db.transaction_lock(one, key)
        await one.rollback()
        async with a_session() as two:
            assert await db.transaction_lock(two, key, wait=0) is True
    async with a_session() as three:  # `two` left without a commit or a rollback: closing the session ended it
        assert await db.transaction_lock(three, key, wait=0) is True


async def test_a_session_that_holds_the_lock_takes_it_again_without_waiting(engine, key):
    async with a_session() as one:
        assert await db.transaction_lock(one, key, wait=0) is True
        assert await db.transaction_lock(one, key, wait=0) is True
        async with a_session() as two:
            assert await db.transaction_lock(two, key, wait=0) is False
        await one.commit()
        async with a_session() as two:
            assert await db.transaction_lock(two, key, wait=0) is True


async def test_the_end_of_a_savepoint_does_not_let_the_lock_go(engine, key):
    async with a_session() as one, a_session() as two:
        async with one.begin_nested():
            await db.transaction_lock(one, key)
        assert await db.transaction_lock(two, key, wait=0) is False
        await one.commit()
        assert await db.transaction_lock(two, key, wait=0) is True


async def test_each_name_is_a_lock_of_its_own(engine, key):
    async with a_session() as one, a_session() as two:
        await db.read_only(one)  # a reader takes no turn: a writer's would keep `two` from beginning
        await db.transaction_lock(one, key)
        assert await db.transaction_lock(two, key + "-another", wait=0) is True


async def test_a_transaction_lock_waits_the_lock_wait_and_then_says_no(engine, key, short_wait):
    async with a_session() as one, a_session() as two:
        await db.transaction_lock(one, key)
        began = asyncio.get_running_loop().time()
        assert await db.transaction_lock(two, key) is False
        assert waited(0.15) <= asyncio.get_running_loop().time() - began < 2.0, "it waited the lock wait, not for ever"


async def test_background_work_waits_the_background_lock_wait(engine, key, monkeypatch):
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 10)
    monkeypatch.setattr(db, "BACKGROUND_LOCK_TIMEOUT_MS", 400)
    async with a_session() as one, a_session() as two:
        await db.transaction_lock(one, key)
        began = asyncio.get_running_loop().time()
        with db.background():
            assert await db.transaction_lock(two, key) is False
        assert asyncio.get_running_loop().time() - began >= 0.35, "it waited the background wait"


async def test_a_transaction_lock_is_got_when_the_holder_ends_within_the_wait(engine, key):
    async with a_session() as one, a_session() as two:
        await db.transaction_lock(one, key)
        asyncio.get_running_loop().call_later(0.1, lambda: asyncio.ensure_future(one.commit()))
        assert await db.transaction_lock(two, key, wait=5) is True


async def test_transaction_lock_or_busy_raises_busy_and_the_failed_caller_holds_nothing(engine, key, short_wait):
    async with a_session() as one, a_session() as two, a_session() as three:
        await db.transaction_lock(one, key)
        with pytest.raises(Busy, match="a run is going"):
            await db.transaction_lock_or_busy(two, key, "a run is going")
        await one.commit()
        assert await db.transaction_lock(three, key, wait=0) is True, "the refused caller took nothing"


# --- the turn first, the lock second: no writer holds a transaction lock without the writer's turn ----------------


async def test_a_session_refused_its_turn_inside_a_savepoint_is_told_busy_and_holds_no_lock(engine, key, monkeypatch):
    """Seen live: a start report's first savepoint was refused the turn, which left its session in a transaction with
    no connection; the next savepoint asked for `browser_mode:<owner>` and was handed it without the turn, then waited
    for the turn while the report that held the turn waited for that lock."""
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 0)  # a turn or a lock that is not free now is refused at once

    async def refused_then_asks(refused) -> None:  # a task of its own, as another request is
        with pytest.raises(Busy, match="turn did not come"):
            async with refused.begin_nested():
                await refused.execute(text("SELECT 1"))
        assert refused.in_transaction(), "the savepoint began the session's transaction, which has no turn"
        with pytest.raises(Busy, match="turn did not come"):
            async with refused.begin_nested():
                await db.transaction_lock(refused, key)

    async with a_session() as holder, a_session() as refused:
        await holder.execute(text("SELECT 1"))  # the writer's turn
        await asyncio.create_task(refused_then_asks(refused))
        assert not refused.info.get(db._HELD), "refused its turn, it holds no lock"
        assert await db.transaction_lock(holder, key, wait=0) is True, "so the turn's holder takes it at once"


async def test_a_session_waiting_for_its_turn_holds_no_lock_and_takes_it_when_its_turn_comes(engine, key, monkeypatch):
    async with a_session() as holder, a_session() as waiter:
        asked = asyncio.Event()
        connect = waiter.connection

        async def connection(*args, **kwargs):  # the session's connection is its turn (`BEGIN IMMEDIATE`)
            asked.set()
            return await connect(*args, **kwargs)

        monkeypatch.setattr(waiter, "connection", connection)
        await holder.execute(text("SELECT 1"))  # the writer's turn
        asking = asyncio.create_task(db.transaction_lock(waiter, key))
        try:
            await asyncio.wait_for(asked.wait(), 5)
            assert await db.transaction_lock(holder, key, wait=0) is True, "the waiter holds no lock while it waits"
            await holder.commit()
            assert await asyncio.wait_for(asking, 5) is True, "its turn came, and the lock with it"
            assert await db.transaction_lock(holder, key, wait=0) is False
        finally:
            asking.cancel()
            await asyncio.gather(asking, return_exceptions=True)


# --- the sites ---------------------------------------------------------------------------------------------------


async def test_a_hand_out_lock_is_tried_without_waiting_and_held_to_the_end_of_the_transaction(engine):
    owner = f"owner-{uuid.uuid4()}"
    async with a_session() as one, a_session() as two:
        await db.read_only(one)  # a reader takes no turn: a writer's would keep `two` from beginning
        assert await hand_outs.lock(one, owner, "parser") is True
        began = asyncio.get_running_loop().time()
        assert await hand_outs.lock(two, owner, "parser") is False, "another is deciding this kind: hand out nothing"
        assert asyncio.get_running_loop().time() - began < 0.1, "and the ask does not wait"
        assert await hand_outs.lock(two, owner, "search") is True, "another kind is free"
        assert await hand_outs.lock(two, f"{owner}-other", "parser") is True, "another owner's is free"
        await one.commit()
        assert await hand_outs.lock(two, owner, "parser") is True


async def test_a_sessions_own_take_next_waits_for_a_claim_in_progress_and_gives_up_after_the_lock_wait(
        engine, short_wait):
    owner = f"owner-{uuid.uuid4()}"
    async with a_session() as ask, a_session() as take:
        assert await hand_outs.lock(ask, owner, "parser") is True
        began = asyncio.get_running_loop().time()
        assert await hand_outs.decide(take, owner, "parser") is False, "it takes nothing this turn and asks again"
        assert asyncio.get_running_loop().time() - began >= waited(0.15), "it waited the lock wait for the claim"
        asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(ask.commit()))
        assert await hand_outs.decide(take, owner, "parser") is True, "the claim committed within the wait"


async def test_two_regates_for_one_owner_never_interleave_and_another_owners_is_not_held_back(engine):
    owner = f"owner-{uuid.uuid4()}"
    events: list[tuple[str, str]] = []
    other_in = asyncio.Event()

    async def regate_for(name: str, who: str, hold: float):
        async with a_session() as session:
            async with regate._one_at_a_time(session, who):
                events.append((name, "in"))
                if name == "other":
                    other_in.set()
                await asyncio.sleep(hold)
                events.append((name, "out"))

    first = asyncio.create_task(regate_for("first", owner, 0.15))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(regate_for("second", owner, 0))
    other = asyncio.create_task(regate_for("other", f"{owner}-other", 0))
    await asyncio.wait_for(other_in.wait(), 1)
    assert ("first", "out") not in events, "another owner's run went in while the first was running"
    await asyncio.gather(first, second, other)
    mine = [e for e in events if e[0] != "other"]
    assert mine == [("first", "in"), ("first", "out"), ("second", "in"), ("second", "out")]


async def test_a_regate_that_waits_for_another_holds_no_transaction_while_it_waits(engine):
    """The second is a save that has already written: its transaction is committed before it waits, so a wait of a
    minute behind the first run holds neither a pooled connection nor the database (a queue that freezes the app)."""
    owner = f"owner-{uuid.uuid4()}"
    first_in, leave = asyncio.Event(), asyncio.Event()

    async def first():
        async with a_session() as session, regate._one_at_a_time(session, owner):
            first_in.set()
            await leave.wait()

    running = asyncio.create_task(first())
    await asyncio.wait_for(first_in.wait(), 5)
    async with a_session() as second:
        await second.execute(text("SELECT 1"))
        assert second.in_transaction()
        waiting = asyncio.create_task(_enter(regate._one_at_a_time(second, owner)))
        await asyncio.sleep(0.1)
        assert not waiting.done(), "it waits"
        assert not second.in_transaction(), "and holds no transaction while it does"
        leave.set()
        await waiting
    await running


async def _enter(manager) -> None:
    async with manager:
        pass


async def test_a_regate_inside_a_savepoint_is_held_to_the_end_of_the_outer_transaction_and_a_second_is_told_busy(
        engine, short_wait):
    owner = f"owner-{uuid.uuid4()}"
    async with a_session() as one, a_session() as two:
        async with one.begin_nested(), regate._one_at_a_time(one, owner):
            pass
        async with two.begin_nested():
            with pytest.raises(Busy, match="another save"):
                await _enter(regate._one_at_a_time(two, owner))
        await one.commit()
        await two.rollback()
        async with two.begin_nested():
            await _enter(regate._one_at_a_time(two, owner))


async def test_a_quiet_run_is_one_at_a_time_for_an_owner(engine, short_wait):
    owner = f"owner-{uuid.uuid4()}"
    async with a_session() as one, a_session() as two:
        await db.read_only(one)  # a reader takes no turn: a writer's would keep `two` from beginning
        await quiet._lock(one, owner)
        await quiet._lock(one, owner)  # a session that holds it takes it again
        with pytest.raises(Busy, match="quiet"):
            await quiet._lock(two, owner)
        await quiet._lock(two, f"{owner}-other")
        await one.commit()
        await quiet._lock(two, owner)


async def test_the_browser_mode_check_is_made_under_a_lock_of_its_own_for_the_owner(monkeypatch):
    """Five runners start at once and one does it: the check reads the last Activity line under `browser_mode:<owner>`,
    and a second is told busy rather than reading the same line."""
    taken: list[tuple[str, str]] = []

    async def lock(session, name, why, **_):
        taken.append((name, why))

    monkeypatch.setattr(db, "transaction_lock_or_busy", lock)

    class Session:
        async def scalar(self, *_):
            taken.append(("read", ""))

    await reader.browser_mode_seen(Session(), "the-owner", "profile")  # type: ignore[arg-type]
    assert [name for name, _ in taken] == ["browser_mode:the-owner", "read"], "the lock comes before the read"


# --- the guard the sites share -----------------------------------------------------------------------------------


def test_no_advisory_lock_is_left_in_the_app():
    """Postgres's advisory locks are gone from the app's code: the named locks above are what they became."""
    app = Path(db.__file__).parent
    found = [f"{path.relative_to(app).as_posix()}:{n}" for path in sorted(app.rglob("*.py"))
             for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
             if "advisory" in line and "pg_" in line]
    assert found == [], f"an advisory lock is taken at {found}"
