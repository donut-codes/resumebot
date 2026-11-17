"""The browser's hand-out reads what it will hand out before it locks the mission's row.

`next_item` and `code_take` held the mission's row for the read of every waiting item, and the offer's reads, with the
whole queue's hand-outs waiting behind. Now a call that is a plain ask reads the items, the domains the workers are on
and the claims with no lock held (`items._look`), keeps the first few as candidates, and under the lock locks those
rows in one statement and leases the first that is still free (`items._pick`). The read can only ever name candidates:
a wait, a stop or an empty answer is decided under the lock, by the code that always decided it, from a read made
there. These tests pin the order of the statements, the answers of the calls that must not take the new way, and that a
candidate gone stale under the lock costs the old path and never a different answer. Every name here is made up, and no
test reaches the network."""

import asyncio
import contextlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event, inspect, select

from app import db
from app.jobsearch.browser import items as bd
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import takeable
from app.jobsearch.browser.takeable import Taker
from app.jobsearch.models import BrowserDomain, BrowserItem, MissionRun
from tests.builders import PATHFINDER, WORKER, WORKER_B, browser_world, items, lease, next_item
from tests.jobsearch.test_browser_work import NOW, _refresh, _role, _sourced
from tests.support import DEFAULT_OWNER as OWNER

COMMIT = "COMMIT"
LOCKED = "LOCKED"  # where the mission's turn was taken (the database has one writer: no row to name in a statement)


@contextlib.contextmanager
def trail():
    """Every statement the app sends while the block runs, in order, whitespace folded, with a marker where a
    transaction commits and one where the mission's turn is taken."""
    seen: list[str] = []
    real_lock = bp.lock_mission

    async def lock_and_note(session, owner):
        await real_lock(session, owner)
        seen.append(LOCKED)

    def sent(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        seen.append(" ".join(statement.split()))

    def committed(_conn) -> None:
        seen.append(COMMIT)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", sent)
    event.listen(engine, "commit", committed)
    patch = pytest.MonkeyPatch()
    patch.setattr(bp, "lock_mission", lock_and_note)
    try:
        yield seen
    finally:
        patch.undo()
        event.remove(engine, "before_cursor_execute", sent)
        event.remove(engine, "commit", committed)


def _lock_at(seen: list[str]) -> int:
    """Where the mission's turn is taken."""
    return seen.index(LOCKED)


def _reads_the_queue(statement: str) -> bool:
    """A read of the waiting items made before the turn is taken (not `as_read.lock`'s read by id): `takeable.ask`'s."""
    return ("FROM js_browser_items" in statement and "next_at <=" in statement
            and "js_browser_items.id IN" not in statement)


async def _world(n: int, **fields) -> list[uuid.UUID]:
    """`n` roles to source, each on a domain of its own (one worker at a time on a domain), the queue made."""
    await browser_world(extra_sites=False, learned=False)  # no recipe: no scan beside the roles' source items
    roles = [await _role(i, url=f"https://careers.company{i}-like.test/jobs/{i}", board_url=None, **fields)
             for i in range(1, n + 1)]
    await _refresh()
    return roles


async def _item_of(role: uuid.UUID, kind: str = "source") -> BrowserItem:
    (row,) = await items(kind=kind, target=str(role))
    return row


# --- the order of the statements --------------------------------------------------------------------------------------


async def test_the_hand_out_reads_the_waiting_items_and_the_domains_before_it_locks_the_missions_row(js):
    await _world(2)
    with trail() as seen:
        got = await next_item(WORKER, NOW)
    assert got["item"]["kind"] == "source"
    lock = _lock_at(seen)
    assert any(_reads_the_queue(q) for q in seen[:lock]), "the items are read before the lock"
    assert any("FROM js_browser_domains" in q for q in seen[:lock]), "and the domains' pace"
    assert not [q for q in seen[lock:] if _reads_the_queue(q)], "and the queue is not read again under it"


async def test_the_hand_out_locks_the_items_it_may_take_in_one_statement_and_commits_before_it_offers(js):
    await _world(3)
    offered_in_a_transaction: list[bool] = []
    real = bd._offer

    async def offer(session, *args, **kwargs):
        offered_in_a_transaction.append(session.in_transaction())
        return await real(session, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch, trail() as seen:
        patch.setattr(bd, "_offer", offer)
        await next_item(WORKER, NOW)
    lock = _lock_at(seen)
    until_commit = seen[lock:seen.index(COMMIT, lock)]
    locks_items = [q for q in until_commit if "FROM js_browser_items" in q and "js_browser_items.id IN" in q]
    assert len(locks_items) == 1 and "ORDER BY js_browser_items.id" in locks_items[0], until_commit
    assert offered_in_a_transaction == [False], "the lease is committed before the offer is read"
    assert len(until_commit) <= 28, f"{len(until_commit)} statements under the lock:\n" + "\n".join(until_commit)


async def test_an_offer_that_fails_leaves_the_item_leased_and_the_sessions_next_call_is_sent_it_again(js, monkeypatch):
    """The lease is committed before the offer is read, so an offer that fails is no lost item: the session asks again,
    holds the item, and is sent it as an item it holds."""
    roles = await _world(2)
    real = bd._offer

    async def failing(*args, **kwargs):
        raise RuntimeError("the offer's read failed")

    monkeypatch.setattr(bd, "_offer", failing)
    with pytest.raises(RuntimeError):
        await next_item(WORKER, NOW)
    held = [i for i in await items(kind="source") if i.leased_by == WORKER]
    assert len(held) == 1 and held[0].target in {str(r) for r in roles}, "the lease was committed"
    monkeypatch.setattr(bd, "_offer", real)
    again = await next_item(WORKER, NOW)
    assert again["item"]["item_id"] == str(held[0].id) and "hold this item already" in again["note"]


async def test_the_code_first_worker_reads_before_the_lock_too_and_is_given_a_whole_item(js):
    await browser_world(extra_sites=False)
    role = await _sourced(1)  # its text is a read: the app's code tries it first
    await _refresh()
    async with db.session_factory()() as session:
        with trail() as seen:
            row = await bd.code_take(session, OWNER, "code-test0001", NOW)
            await session.commit()
        assert row is not None and row.target == str(role)
        assert not inspect(row).unloaded, "every column is loaded: the worker reads the whole item"
    lock = _lock_at(seen)
    assert any(_reads_the_queue(q) for q in seen[:lock]) and not [q for q in seen[lock:] if _reads_the_queue(q)]


# --- the calls that do not take the new way ---------------------------------------------------------------------------


async def test_a_call_that_names_a_claim_reads_nothing_of_the_queue_before_the_lock(js):
    """`hand_outs.take` writes, so a call with a claim is made whole under the lock."""
    await _world(2)
    with trail() as seen:
        got = await next_item(WORKER, NOW, claim=str(uuid.uuid4()))
    assert got["ok"] and got["item"]["kind"] == "source"
    assert not [q for q in seen[:_lock_at(seen)] if _reads_the_queue(q)]


async def test_a_session_that_must_stop_is_stopped_with_no_read_of_the_queue_before_the_lock(js):
    await browser_world(extra_sites=False, resumed=False)  # the owner has paused reading in the browser
    await _role(1, url="https://careers.company1-like.test/jobs/1", board_url=None)
    with trail() as seen:
        got = await next_item(WORKER, NOW)
    assert got["stop"] and "paused" in got["why"]
    assert not [q for q in seen if _reads_the_queue(q)]


async def test_a_session_that_holds_an_item_is_sent_it_again_with_no_read_of_the_queue_before_the_lock(js):
    await _world(2)
    first = await next_item(WORKER, NOW)
    with trail() as seen:
        again = await next_item(WORKER, NOW)
    assert again["item"]["item_id"] == first["item"]["item_id"] and "hold this item already" in again["note"]
    assert not [q for q in seen if _reads_the_queue(q)]


async def test_a_session_that_has_taken_its_share_is_stopped_with_no_read_of_the_queue_before_the_lock(js, monkeypatch):
    await _world(3)
    monkeypatch.setattr(bd, "MAX_ITEMS_PER_SESSION", 1)
    first = await next_item(WORKER, NOW)
    async with db.session_factory()() as session:  # it handed the first back: it holds nothing, and has taken one
        row = await session.get(BrowserItem, uuid.UUID(first["item"]["item_id"]))
        row.state, row.leased_by, row.lease_until = bp.WAITING, None, None
        await session.commit()
    with trail() as seen:
        got = await next_item(WORKER, NOW)
    assert got["stop"] and "a session's share" in got["why"]
    assert not [q for q in seen if _reads_the_queue(q)]


# --- two sessions, a candidate gone stale -----------------------------------------------------------------------------


async def test_two_sessions_asking_together_each_take_a_different_item_and_each_opens_its_run(js, monkeypatch):
    """Both read the same first item before either has the lock; the one that locks second finds it leased and takes the
    next one, and is never told to wait for want of it."""
    await _world(2)
    arrived = []
    both_in = asyncio.Event()
    real_look = bd._look

    async def meet_after_the_look(*args, **kwargs):
        seen = await real_look(*args, **kwargs)
        arrived.append(1)
        if len(arrived) >= 2:
            both_in.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both_in.wait(), 5)
        return seen

    monkeypatch.setattr(bd, "_look", meet_after_the_look)
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 300_000)
    got = await asyncio.wait_for(asyncio.gather(next_item("browser_reader-1", NOW), next_item("browser_reader-2", NOW)),
                                 60)
    assert all(g["ok"] and "item" in g for g in got), got
    assert got[0]["item"]["item_id"] != got[1]["item"]["item_id"]
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(MissionRun).where(MissionRun.owner == OWNER))).all()) == 2


async def _take_before_the_lock(monkeypatch, *targets: tuple[uuid.UUID, str]) -> None:
    """Another session leases these roles' items (one each: `(role, session)`) after this call has read the queue and
    before it locks the mission's row."""
    real = bp.lock_mission

    async def lock_after_they_were_taken(*args, **kwargs):
        for role, sid in targets:
            await lease("source", str(role), sid, NOW)
        return await real(*args, **kwargs)

    monkeypatch.setattr(bp, "lock_mission", lock_after_they_were_taken)


async def _in_hand_out_order(roles: list[uuid.UUID]) -> list[uuid.UUID]:
    """These roles, in the order their items would be handed out."""
    by_item = {str((await _item_of(r)).id): r for r in roles}
    async with db.session_factory()() as session:
        ready = await takeable.ask(session, Taker.session(OWNER, bp.WORKER, owners_only=False), NOW)
    return [by_item[str(row.id)] for row, _ in ready if str(row.id) in by_item]


async def test_an_item_taken_between_the_read_and_the_lock_gives_the_call_the_next_free_one(js, monkeypatch):
    first, second = await _in_hand_out_order(await _world(2))
    await _take_before_the_lock(monkeypatch, (first, WORKER_B))
    got = await next_item(WORKER, NOW)
    assert got["ok"] and got["item"]["item_id"] == str((await _item_of(second)).id)
    assert (await _item_of(first)).leased_by == WORKER_B


async def test_a_call_whose_only_candidate_was_taken_is_told_what_the_locked_path_always_told_it(js, monkeypatch):
    (role,) = await _world(1)
    await _take_before_the_lock(monkeypatch, (role, WORKER_B))
    got = await next_item(WORKER, NOW)
    assert got["stop"] and "nothing more you can take now" in got["why"], got
    async with db.session_factory()() as session:
        runs = (await session.scalars(select(MissionRun).where(MissionRun.owner == OWNER))).all()
    assert {r.outcome for r in runs if (r.started or {}).get("session_id") == WORKER} == {"done"}, "its run is closed"


async def test_when_none_of_the_candidates_is_free_any_longer_the_call_reads_the_queue_under_the_lock(js, monkeypatch):
    """Only the first `CANDIDATES` are tried under the lock; a call whose candidates were all taken goes on as it always
    did and is handed the next item, which the read before the lock never named."""
    first, second, third = await _in_hand_out_order(await _world(3))
    monkeypatch.setattr(bd, "CANDIDATES", 2)
    await _take_before_the_lock(monkeypatch, (first, WORKER_B), (second, "browser_reader-cccc4444"))
    with trail() as seen:
        got = await next_item(WORKER, NOW)
    assert got["ok"] and got["item"]["item_id"] == str((await _item_of(third)).id)
    assert any(_reads_the_queue(q) for q in seen[_lock_at(seen):]), "the locked path read the queue as it always did"


async def test_an_item_whose_lease_ran_out_is_handed_again_by_the_call_that_finds_it_under_the_lock(js):
    """An unlocked read cannot see what `expire` frees under the lock, and must not tell a session there is nothing."""
    (role,) = await _world(1)
    await lease("source", str(role), WORKER_B, NOW - timedelta(minutes=30))  # its session went quiet long ago
    got = await next_item(WORKER, NOW)
    assert got["ok"] and got["item"]["item_id"] == str((await _item_of(role)).id)


async def test_the_first_item_in_line_whose_lease_ran_out_is_handed_before_the_items_the_read_before_the_lock_named(js):
    """With one item the test above cannot tell: the read before the lock names nothing, so the locked path answers.
    With three, the read names the second and third, which it saw free; the call that finds the first freed under the
    lock hands the first, as it always did (the guard on `expired` in `next_item`: without it, the second)."""
    first, second, _third = await _in_hand_out_order(await _world(3))
    await lease("source", str(first), WORKER_B, NOW - timedelta(minutes=30))  # its session went quiet long ago
    got = await next_item(WORKER, NOW)
    assert got["ok"] and got["item"]["item_id"] == str((await _item_of(first)).id) != str((await _item_of(second)).id)


async def test_the_first_item_the_code_has_to_try_whose_lease_ran_out_is_taken_before_the_items_the_read_named(js):
    """The same for `code_take` (without the guard on `expired` there: the second)."""
    await browser_world(extra_sites=False)
    roles = [await _sourced(i) for i in range(1, 4)]  # read items the code tries first, each on a domain of its own
    await _refresh()
    by_item = {str((await _item_of(r, "read")).id): r for r in roles}
    async with db.session_factory()() as session:
        ready = await takeable.ask(
            session, Taker(OWNER, bp.WORKER, takeable.CODE_KINDS, code_first=True, owners_only=False), NOW)
    first, second, _third = [by_item[str(row.id)] for row, _ in ready]
    await lease("read", str(first), WORKER_B, NOW - timedelta(minutes=30))
    async with db.session_factory()() as session:
        row = await bd.code_take(session, OWNER, "code-test0001", NOW)
        await session.commit()
    assert row is not None and row.target == str(first) != str(second)


# --- every item comes whole -------------------------------------------------------------------------------------------


async def test_every_kind_of_item_a_session_is_handed_comes_whole_with_what_the_offer_reads(js):
    await browser_world()
    await _role(1, url="https://careers.company1-like.test/jobs/1", board_url=None)
    await _sourced(2)
    await _refresh()
    handed = []
    for n in range(1, 6):
        got = await next_item(f"browser_reader-{n}", NOW)
        if "item" not in got:
            break
        handed.append(got["item"])
    assert {i["kind"] for i in handed} >= {"source", "read", "scan"}, handed
    assert all(i["link"] and i["domain"] and i["results"] for i in handed)
    assert any("role" in i for i in handed if i["kind"] in ("source", "read")), "the role the item is about"


async def test_the_pathfinder_is_handed_its_map_item_the_same_way(js):
    await browser_world(learned=False, extra_sites=False)
    await _refresh()
    got = await next_item(PATHFINDER, NOW)
    assert got["ok"] and got["item"]["kind"] == "map"


# --- what `takeable.ask` loads -------------------------------------------------------------------------------------


async def test_ready_reads_only_the_columns_it_orders_and_judges_by_and_makes_no_instance_of_each_item(js):
    await _world(2)
    with trail() as seen:
        async with db.session_factory()() as session:
            ready = await takeable.ask(session, Taker.session(OWNER, bp.WORKER, owners_only=False), NOW)
            await session.rollback()
    (reads,) = [q for q in seen if _reads_the_queue(q)]
    listed = reads.split(" FROM ", 1)[0]
    for column in ("last_note", "leased_by", "lease_until", "run_id", "tries", "due_by", "done_at", "last_result"):
        assert f"js_browser_items.{column}" not in listed, f"{column} is not read to order the items"
    assert ready and not isinstance(ready[0][0], BrowserItem), "a row of the columns, not an item of the model"
    assert set(ready[0][0]._fields) == {c.key for c in takeable.READY_COLUMNS} >= {"id", "kind", "target", "domain"}


async def test_a_row_ready_returned_is_taken_whole_before_it_is_leased(js):
    await _world(1)
    async with db.session_factory()() as session:
        (row, _), *_ = await takeable.ask(session, Taker.session(OWNER, bp.WORKER, owners_only=False), NOW)
        item = await bp.whole(session, row)
        assert item.id == row.id and not inspect(item).unloaded and item.state == bp.WAITING


async def test_an_item_on_a_host_a_refusal_stopped_is_left_out_by_the_host_of_its_link_until_the_stop_ends(js):
    """`takeable.recheck` works out an item's host only when some host is stopped, and then leaves out what is on it."""
    roles = await _world(2)
    async with db.session_factory()() as session:
        session.add(BrowserDomain(owner=OWNER, domain="careers.company1-like.test", blocks=0, stopped_at=NOW,
                                  stopped_for="refused", stopped_until=None))
        await session.commit()
    stopped = str((await _item_of(roles[0])).id)
    assert stopped not in await _ready_ids()
    async with db.session_factory()() as session:
        pace = await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == "careers.company1-like.test"))
        pace.stopped_until = NOW - timedelta(minutes=1)  # its end has come: no stop
        await session.commit()
    assert stopped in await _ready_ids()


async def _ready_ids() -> list[str]:
    async with db.session_factory()() as session:
        taker = Taker.session(OWNER, bp.WORKER, owners_only=False)
        return [str(row.id) for row, _ in await takeable.ask(session, taker, NOW)]


async def test_the_loop_is_given_back_every_few_rows_while_the_waiting_items_are_judged(js, monkeypatch):
    """Judging thousands of items is one stretch of the loop's one core; it is cut into stretches of a few milliseconds,
    so nothing else the app does waits behind it for long."""
    await _world(5)
    real_sleep, gave = asyncio.sleep, []

    async def sleep(delay, *args, **kwargs):
        if delay == 0:
            gave.append(delay)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(takeable, "YIELD_EVERY", 2)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    async with db.session_factory()() as session:
        ready = await takeable.ask(session, Taker.session(OWNER, bp.WORKER, owners_only=False), NOW)
    assert len(ready) == 5 and len(gave) >= 5, gave
