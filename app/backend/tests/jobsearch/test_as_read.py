"""`as_read.lock`, the one step behind every decision made from a read that held no lock: it reads rows again in id
order and names the rows still as they were read. The database is SQLite with one writer: there is no row lock to
take, skip or wait for, so what is tested is what the step still does (the order, the stands, `where`, what is read
again) and what the writer's turn gives it (a second session that writes waits for the first, and reads what the
first saved). Every test goes through that one function. Every name is made up."""

import asyncio
import re
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import as_read, stages
from app.jobsearch.models import Employer, JobPosting, Opportunity
from tests.support import OWNER

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FIRST = stages.FIRST_IMPRESSION


def _role(n: int, **more) -> Opportunity:
    """A role of a made-up firm; the ids rise with `n`, so the id order is the order of the numbers."""
    return Opportunity(**{
        "id": uuid.UUID(int=n), "owner": OWNER, "slug": f"role-{n}", "company": "Firm a", "title": f"Lead {n}",
        "match_key": f"firm a|lead {n}", "origin": "scout", "status": FIRST, "found": date(2026, 11, 3),
        "analysis": {"read": n}, "description": f"The text {n}.", **more})


async def _roles(*numbers: int, **more) -> list[uuid.UUID]:
    async with db.session_factory()() as session:
        session.add_all(_role(n, **more) for n in numbers)
        await session.commit()
    return [uuid.UUID(int=n) for n in numbers]


async def _change(role_id: uuid.UUID, **fields) -> None:
    """Another transaction changes the row and commits."""
    async with db.session_factory()() as other:
        row = await other.get(Opportunity, role_id)
        for key, value in fields.items():
            setattr(row, key, value)
        await other.commit()


async def _posting(n: int, **more) -> uuid.UUID:
    async with db.session_factory()() as session:
        session.add(JobPosting(**{
            "id": uuid.UUID(int=n), "owner": OWNER, "employer_id": uuid.UUID(int=900), "ats": "scout",
            "ext_id": f"ext-{n}", "company": "Firm a", "title": f"Lead {n}", "first_seen": NOW, "last_seen": NOW,
            **more}))
        await session.commit()
    return uuid.UUID(int=n)


class _Reads:
    """The statements on the roles the block sent, each as `(statement, ids)`: the ids a statement's parameters hold,
    in the order they were bound."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, list[uuid.UUID]]] = []

    def __call__(self, _conn, _cursor, statement, parameters, _context, _many) -> None:
        if "FROM js_opportunities" not in statement:
            return
        ids = []
        for value in parameters if isinstance(parameters, tuple | list) else []:
            try:
                ids.append(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
            except ValueError:
                pass
        self.seen.append((statement, ids))


class _watching:  # a context manager used like a function
    def __enter__(self) -> list[tuple[str, list[uuid.UUID]]]:
        self.reads = _Reads()
        self.engine = db.engine().sync_engine
        event.listen(self.engine, "before_cursor_execute", self.reads)
        return self.reads.seen

    def __exit__(self, *_exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self.reads)


# --- one order, no row lock --------------------------------------------------------------------------------------


async def test_rows_asked_for_in_any_order_are_read_in_id_order_across_the_statements(js, monkeypatch):
    ids = await _roles(1, 2, 3, 4, 5)
    monkeypatch.setattr(as_read, "STATEMENT_ROWS", 2)
    async with db.session_factory()() as session:
        roles = {r.id: r for r in await session.scalars(select(Opportunity))}
        with _watching() as reads:
            held = await as_read.lock(session, Opportunity, [roles[i] for i in reversed(ids)])
        await session.rollback()
    assert len(reads) == 3 and all("ORDER BY js_opportunities.id" in s for s, _ in reads), reads
    assert not [s for s, _ in reads if "FOR UPDATE" in s or "SKIP LOCKED" in s], "no row lock is asked for"
    assert [i for _, asked in reads for i in asked] == ids, "the rows are read in id order, whatever the asking"
    assert held.ids == set(ids) and list(held.rows) == ids and held.skipped == ()
    assert all(held.rows[i] is roles[i] for i in ids), "the instances passed come back"


async def test_no_rows_send_no_statement_and_a_row_given_twice_is_read_once(js, transactions):
    (one,) = await _roles(1)
    async with db.session_factory()() as session:
        transactions.clear()
        held = await as_read.lock(session, Opportunity, [])
        assert (held.ids, held.rows, held.skipped) == (frozenset(), {}, ()) and not transactions.timeline
        role = await session.get(Opportunity, one)
        with _watching() as reads:
            held = await as_read.lock(session, Opportunity, [one, role, one], stands=(Opportunity.status,))
        await session.rollback()
    ((_, asked),) = reads
    assert asked == [one] and held.rows == {one: role}, "the instance wins over its id: its stands are the ones read"


async def test_a_row_that_is_gone_is_skipped_and_the_rest_are_read(js):
    one, two, three = await _roles(1, 2, 3)
    gone = uuid.UUID(int=99)
    async with db.session_factory()() as session:
        held = await as_read.lock(session, Opportunity, [one, two, three, gone])
        assert held.ids == {one, two, three} and held.skipped == (gone,)
        assert one in held and (await session.get(Opportunity, three)) in held and gone not in held
        await session.rollback()


async def test_a_session_that_writes_waits_for_the_turn_and_reads_what_the_first_saved(js):
    """What a row held by another transaction was: a second session that asks while the first holds the writer's turn
    does not skip the row, it waits for the turn (it is the first's to give back) and then reads the row as the first
    left it, so a decision made from an older read finds the row moved."""
    one, two = await _roles(1, 2)
    first_has_the_turn, order = asyncio.Event(), []

    async def first() -> None:
        async with db.session_factory()() as session:
            held = await as_read.lock(session, Opportunity, [one, two])
            first_has_the_turn.set()
            await asyncio.sleep(0.05)
            for role in held.rows.values():
                role.status = stages.SECOND_IMPRESSION
            order.append("the first wrote")
            await session.commit()

    async def second() -> as_read.Held[Opportunity]:
        await first_has_the_turn.wait()
        async with db.session_factory()() as session:
            held = await as_read.lock(session, Opportunity, [one, two], where=(Opportunity.status == FIRST,))
            order.append("the second read")
            await session.rollback()
            return held

    _, got = await asyncio.wait_for(asyncio.gather(first(), second()), 5)
    assert order == ["the first wrote", "the second read"]
    assert got.ids == frozenset() and got.skipped == (one, two), "both rows moved on: neither is as the decision read"


@pytest.mark.parametrize("first_asks", [(1, 2), (2, 1)])
async def test_two_sessions_asking_for_the_same_rows_in_opposite_orders_take_turns_and_both_finish(
        js, monkeypatch, first_asks):
    """A statement for each row, so the two could interleave. They cannot: the second's transaction begins when the
    first's ends, so the order of asking is nothing to either and neither is told busy or deadlocked."""
    low, high = await _roles(1, 2)
    monkeypatch.setattr(as_read, "STATEMENT_ROWS", 1)
    by_number = {1: low, 2: high}
    first_has_one_row, order = asyncio.Event(), []

    async def asks(who: str, numbers: tuple[int, ...]) -> as_read.Held[Opportunity]:
        async with db.session_factory()() as session:
            real = session.scalars
            statements = 0

            async def scalars(*args, **kwargs):
                nonlocal statements
                got = await real(*args, **kwargs)
                statements += 1
                if who == "first" and statements == 1:  # it has read one row: the second may ask
                    first_has_one_row.set()
                    await asyncio.sleep(0.05)
                return got

            session.scalars = scalars  # pyright: ignore[reportAttributeAccessIssue]
            held = await as_read.lock(session, Opportunity, [by_number[n] for n in numbers])
            order.append(f"{who} done")
            await session.commit()
            return held

    async def second() -> as_read.Held[Opportunity]:
        await first_has_one_row.wait()
        return await asks("second", first_asks[::-1])

    got_first, got_second = await asyncio.wait_for(asyncio.gather(asks("first", first_asks), second()), 15)
    assert order == ["first done", "second done"], "one after the other"
    assert got_first.ids == got_second.ids == {low, high} and got_second.skipped == ()


# --- as read ------------------------------------------------------------------------------------------------------


async def test_a_row_changed_in_a_stand_since_the_read_is_left_and_one_changed_elsewhere_is_taken(js):
    one, two, three = await _roles(1, 2, 3)
    async with db.session_factory()() as session:
        roles = {r.id: r for r in await session.scalars(select(Opportunity))}
        await db.release(session)  # a pass reads, then ends its transaction (the writer's turn is one for the file)
        await _change(one, status=stages.SECOND_IMPRESSION)  # a stand
        await _change(two, title="Lead 2, changed")  # not a stand
        held = await as_read.lock(session, Opportunity, roles.values(), stands=(Opportunity.status,))
        assert held.ids == {two, three} and held.skipped == (), "the moved row is neither taken nor skipped"
        assert roles[one].status == stages.SECOND_IMPRESSION, "its instance holds what the row is now"
        assert roles[two].title == "Lead 2, changed" and held.rows[two] is roles[two], "a row taken is read again"


async def test_the_stands_are_the_values_the_instances_held_when_the_lock_was_asked(js):
    (one,) = await _roles(1)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, one)
        await db.release(session)
        await _change(one, status=stages.SECOND_IMPRESSION, analysis_state="waiting")
        held = await as_read.lock(session, Opportunity, [role], stands=(Opportunity.status, Opportunity.analysis_state))
        assert held.ids == frozenset() and (role.status, role.analysis_state) == (stages.SECOND_IMPRESSION, "waiting")
        again = await as_read.lock(session, Opportunity, [role], stands=(Opportunity.status,))
        assert again.ids == {one}, "a row read again is as read from there on: the stands are what the instance held"


async def test_where_is_judged_by_the_database_on_the_locked_row(js):
    one, two = await _roles(1, 2)
    async with db.session_factory()() as session:
        roles = list(await session.scalars(select(Opportunity)))
        await db.release(session)
        await _change(two, status=stages.SECOND_IMPRESSION)
        held = await as_read.lock(session, Opportunity, roles, where=(Opportunity.status == FIRST,))
    assert held.ids == {one} and held.skipped == (two,), "a row that fails it is not locked, and is not as read"
    live, closed = await _posting(1), await _posting(2, closed_at=NOW)
    async with db.session_factory()() as session:  # a hybrid such as `is_open` is fine
        held = await as_read.lock(session, JobPosting, [live, closed], where=(JobPosting.is_open,), refresh=False)
    assert held.ids == {live} and held.skipped == (closed,)


async def test_a_row_that_stops_matching_before_the_lock_is_dropped(js):
    one, two = await _roles(1, 2)
    async with db.session_factory()() as session:
        roles = list(await session.scalars(select(Opportunity)))
        await db.release(session)
        await _change(one, status=stages.SECOND_IMPRESSION)  # the other session moves it and commits
        held = await as_read.lock(session, Opportunity, roles, where=(Opportunity.status == FIRST,))
    assert held.ids == {two} and held.skipped == (one,)


# --- what is read again -------------------------------------------------------------------------------------------


async def test_defer_keeps_what_the_callers_read_loaded_and_what_it_left_out_stays_out(js):
    one, two = await _roles(1, 2)
    light = (Opportunity.analysis, Opportunity.description)
    async with db.session_factory()() as session:
        read_light = (await session.scalars(select(Opportunity).where(Opportunity.id == one).options(
            *(defer(c) for c in light)))).one()
        read_whole = await session.get(Opportunity, two)
        await db.release(session)
        for role_id in (one, two):
            await _change(role_id, title="Changed", analysis={"read": "since"})
        held = await as_read.lock(session, Opportunity, [read_light, read_whole], defer=light)
        assert held.ids == {one, two}
        assert {"analysis", "description"} <= inspect(read_light).unloaded, "a column the read left out is not loaded"
        assert read_light.title == read_whole.title == "Changed", "every other column is read again"
        assert read_whole.analysis == {"read": 2}, "a column the read loaded stays as it was"


async def test_without_defer_a_row_loaded_whole_is_read_again_and_bare_ids_come_back_whole(js):
    one, two = await _roles(1, 2)
    async with db.session_factory()() as session:
        read = await session.get(Opportunity, one)
        await db.release(session)
        await _change(one, analysis={"read": "since"})
        held = await as_read.lock(session, Opportunity, [read, two])
        assert read.analysis == {"read": "since"} and held.rows[one] is read
        assert held.rows[two].analysis == {"read": 2} and held.rows[two].title == "Lead 2"
    async with db.session_factory()() as session:
        held = await as_read.lock(session, Opportunity, [one], defer=(Opportunity.analysis,))
    assert "analysis" in inspect(held.rows[one]).unloaded and held.rows[one].title == "Lead 1", "whole but `defer`"


async def test_without_refresh_the_lock_reads_nothing_back_and_touches_no_instance(js):
    one, two = await _roles(1, 2)
    async with db.session_factory()() as session:
        read = await session.get(Opportunity, one)
        await db.release(session)
        await _change(one, title="Changed")
        await _change(two, status=stages.SECOND_IMPRESSION)
        with _watching() as reads:
            held = await as_read.lock(session, Opportunity, [read, two], refresh=False)
        assert held.ids == {one, two} and held.rows == {}
        assert read.title == "Lead 1", "the instance is as it was: nothing was read back"
        ((statement, _),) = reads
        assert "js_opportunities.title" not in statement, statement
    async with db.session_factory()() as session:  # its stands are still compared, from the id and the stands alone
        roles = list(await session.scalars(select(Opportunity).where(Opportunity.id == two)))
        await db.release(session)
        await _change(two, status=FIRST)
        held = await as_read.lock(session, Opportunity, roles, stands=(Opportunity.status,), refresh=False)
        assert held.ids == frozenset() and held.skipped == () and roles[0].status == stages.SECOND_IMPRESSION


# --- misuse -------------------------------------------------------------------------------------------------------


async def test_a_lock_that_cannot_say_what_was_read_raises_and_sends_nothing(js, transactions):
    one, two = await _roles(1, 2)
    unflushed = Opportunity(owner=OWNER, slug="new", company="Firm a", title="New", match_key="firm a|new",
                            origin="scout", status=FIRST, found=date(2026, 11, 3))  # no id yet
    async with db.session_factory()() as session:
        not_loaded = (await session.scalars(select(Opportunity).where(Opportunity.id == one).options(
            defer(Opportunity.status)))).one()
        changed = await session.get(Opportunity, two)
        changed.status = stages.SECOND_IMPRESSION  # not flushed
        transactions.clear()
        status = (Opportunity.status,)
        for rows, more in (
                ([not_loaded], {"stands": status}),  # a stand not loaded
                ([changed], {"stands": status}),  # a stand changed and not flushed
                ([two], {"stands": status}),  # a stand with a bare id
                ([unflushed], {}),  # a role with no id yet
                ([Employer(owner=OWNER, key="a", name="Fabrikam")], {}),  # not a role
                ([changed], {"stands": (Opportunity.title,), "defer": (Opportunity.title,)})):  # never read again
            with pytest.raises(ValueError):
                await as_read.lock(session, Opportunity, rows, **more)
        assert not transactions.timeline, "no statement was sent"
        await session.rollback()


async def test_the_lock_sends_nothing_but_selects(js, transactions):
    """No write, and no row lock (`FOR UPDATE` is not asked for)."""
    ids = await _roles(1, 2, 3)
    async with db.session_factory()() as session:
        roles = list(await session.scalars(select(Opportunity)))
        transactions.clear()
        await as_read.lock(session, Opportunity, roles, stands=(Opportunity.status,))
        await as_read.lock(session, Opportunity, ids, refresh=False)
        statements = list(transactions.open)
        await session.rollback()
    assert statements and all(s.lstrip().startswith("SELECT") for s in statements)
    assert not [s for s in statements if re.search(r"\b(INSERT|DELETE)\b|(?<!FOR )\bUPDATE\b", s)], statements
