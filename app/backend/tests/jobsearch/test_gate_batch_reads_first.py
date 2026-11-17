"""The gate-facts batch reads first and locks after (docs/PRD.md 39): everything a
role's read needs from the database or from code alone is read for the whole batch with nothing locked, then the roles
that are due are locked in one statement and each is written only if it is still as it was read. `stale` reads the
roles in slices and stops at its limit. Everything here is made up."""

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text, update

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import gate_facts
from app.jobsearch.models import JobPosting, Opportunity
from app.trail import service as trail
from tests.jobsearch.test_gate_copy import list_posting
from tests.jobsearch.test_gate_facts import NOTHING, PAY_LOW, PAY_OK, REMOTE, UNCLEAR, world
from tests.support import EXAMPLE_SYSTEM, OWNER

NOW = datetime.now(UTC)


class Statements:
    """Every statement the app sends while a block runs, in order."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def lock_at(self) -> int:
        """Where the batch's lock is (SQLite has no row lock: `as_read.lock` is the first read of the roles by id), or
        -1."""
        return next((i for i, s in enumerate(self.sent)
                     if "FROM js_opportunities" in s and "ORDER BY js_opportunities.id" in s), -1)

    def before_lock(self) -> list[str]:
        return self.sent[:self.lock_at()]

    def after_lock(self) -> list[str]:
        return self.sent[self.lock_at() + 1:]


@pytest.fixture
def statements(js):
    seen = Statements()

    def noted(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        seen.sent.append(re.sub(r"\s+", " ", statement).strip())

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", noted)


def reads(statement: str) -> bool:
    return statement.upper().startswith(("SELECT", "WITH"))


async def twenty_five(**per_role) -> dict:
    """Twenty-five roles in Found, unread, of the made-up kinds a night holds: pay that stops, pay that passes, a
    posting that states nothing."""
    kinds = (PAY_LOW, PAY_OK, NOTHING, PAY_OK, PAY_LOW)
    return await world(*((f"Company{n}", kinds[n % 5], {**REMOTE, "created_at": NOW - timedelta(hours=n), **per_role})
                         for n in range(25)))


# --- what is read when ------------------------------------------------------------------------------------------------


async def test_a_batch_of_twenty_five_roles_reads_nothing_per_role_under_the_lock_but_its_writes(js, statements):
    """Under the lock: the one statement that locks, the three that check what was read first is still so (the copies,
    the job sites that say which postings are a board's, and the postings' dates: one each, for every role of the
    batch), and for each role its savepoint pair and its writes. No read of where the owner put the role, of its copy,
    of its postings or of its posting date is made for a role while the lock is held (265 statements for the 25 became
    about a hundred, 50 of them savepoints).

    Edited with PRD 39.31 (a role is written only when its copy and its posted day are those that were read): old, one
    read under the lock, of the copies; new, three, of the copies, the sites and the postings' dates."""
    ids = await twenty_five()
    async with db.session_factory()() as session:
        statements.sent.clear()
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert out["looked"] == 25 and out["stopped"] == 10
    after = statements.after_lock()
    selects = [s for s in after if reads(s)]
    assert len(selects) == 3, f"the copies, the sites and the postings' dates, and no more: {selects}"
    assert sum("js_cleansed_postings" in s for s in selects) == 1, "the copies are read again once"
    assert sum("FROM js_postings" in s and "js_cleansed_postings" not in s for s in selects) == 1, "the dates, once"
    savepoints = [s for s in after if "SAVEPOINT" in s]
    assert len(savepoints) == 2 * 25, "one pair for each role read"
    writes = [s for s in after if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert len(after) == len(savepoints) + len(writes) + 3, "nothing else is sent under the lock"
    assert len(statements.before_lock()) <= 30, "what is read first does not grow with the roles"


async def test_the_writer_turn_is_free_while_the_batch_works_out_what_each_role_states(js, monkeypatch):
    """`_prepare` reads the copies and the days in a savepoint, then ends the transaction (`db.release_if_free`), so its
    loop of CPU work (the facts each text states, 15 ms a role) holds no writer turn: another writer's transaction
    can begin in the middle of it (with the turn held, the guard raises `NestedWriter`) and the batch's own session has
    no transaction open. Statement order cannot show this: it is the same with or without the release."""
    ids = await twenty_five()
    real, probes = gate_facts._extract, []

    def extract(role, text_):
        probes.append(role.id)
        return real(role, text_)

    monkeypatch.setattr(gate_facts, "_extract", extract)
    open_in_loop: list[bool] = []

    async def probing_tick(self) -> None:
        if not probes:  # the earlier loops of the batch (reading the texts) are not the one under test
            return
        open_in_loop.append(self.session.in_transaction())
        async with db.session_factory()() as other:  # a writer: it needs the turn, and raises if this task holds it
            await other.execute(text("UPDATE js_opportunities SET id = id WHERE 0"))
            await other.commit()

    class Pace(gate_facts._Pace):
        session = None

        async def tick(self) -> None:
            await probing_tick(self)

    monkeypatch.setattr(gate_facts, "_Pace", Pace)
    async with db.session_factory()() as session:
        Pace.session = session
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert out["looked"] == 25 and len(probes) == 25
    assert open_in_loop and not any(open_in_loop), "no transaction of the batch's is open while the loop runs"


async def test_the_writer_turn_is_free_while_the_batch_works_out_the_text_each_role_is_judged_on(js, monkeypatch):
    """`_judged_texts` reads the postings, then ends the transaction (`db.release_if_free`), so its loop (`judged`, one
    call for each role) holds no writer turn: another writer can begin in the middle of it (with the turn held, the
    guard raises `NestedWriter`) and the batch's own session has no transaction open. Statement order cannot show it."""
    ids = await twenty_five()
    real, open_in_loop = gate_facts.judged, []

    class Pace(gate_facts._Pace):
        session = None
        loop_on = False

        async def tick(self) -> None:
            if not self.loop_on:
                return
            open_in_loop.append(self.session.in_transaction())
            async with db.session_factory()() as other:  # a writer: it raises if this task holds the turn
                await other.execute(text("UPDATE js_opportunities SET id = id WHERE 0"))
                await other.commit()

    def judged(role, postings):
        Pace.loop_on = True
        return real(role, postings)

    monkeypatch.setattr(gate_facts, "_Pace", Pace)
    monkeypatch.setattr(gate_facts, "judged", judged)
    async with db.session_factory()() as session:
        Pace.session = session
        roles = list((await session.scalars(select(Opportunity).where(Opportunity.id.in_(list(ids.values()))))).all())
        assert session.in_transaction(), "the roles were read in a transaction"
        texts = await gate_facts._judged_texts(session, OWNER, roles)
    assert len(texts) == 25 and len(open_in_loop) == 25
    assert not any(open_in_loop), "no transaction of the batch's is open while the texts are judged"


async def test_the_roles_not_due_are_not_locked_and_a_batch_with_none_due_locks_nothing(js, statements):
    ids = await twenty_five()
    async with db.session_factory()() as session:
        await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
        statements.sent.clear()
        again = await gate_facts.read_batch(session, OWNER, list(ids.values()))
    assert again == {"looked": 0, "stopped": 0, "waits": 0}
    assert statements.lock_at() == -1, "every role was read in its present state: no lock is taken"
    assert not [s for s in statements.sent if s.startswith(("INSERT", "UPDATE", "DELETE"))]


async def test_the_batch_gives_the_event_loop_back_between_roles(js, monkeypatch):
    """Its code between statements (the facts a posting's text states: 15 ms a role) is no longer inside a statement's
    wait, so it yields (`YIELD_AFTER`): here forced to every role."""
    ids = await twenty_five()
    real, yields = asyncio.sleep, []

    async def sleep(delay, *args):
        yields.append(delay)
        return await real(delay, *args)

    monkeypatch.setattr(gate_facts, "YIELD_AFTER", 0.0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    async with db.session_factory()() as session:
        await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert yields.count(0) >= 25, "at least once for each role read"


# --- a role that changed between the read and the lock is left as it is ---------------------------------------------


def change_before_the_lock(monkeypatch, role_id, **fields) -> None:
    """Another transaction changes the role (and commits) after the batch has read it and before it locks it."""
    real = gate_facts.as_read.lock

    async def lock(session, model, rows, **kw):
        async with db.session_factory()() as other:
            role = await other.get(Opportunity, role_id)
            for name, value in fields.items():
                setattr(role, name, value)
            await other.commit()
        return await real(session, model, rows, **kw)

    monkeypatch.setattr(gate_facts.as_read, "lock", lock)


@pytest.mark.parametrize("change", [
    {"location": "Dallas, TX"}, {"comp": "$90,000"}, {"title": "Programs Director"}, {"company": "Hooli"},
    {"move_type": "hybrid"}, {"description": PAY_OK + " It is also an edited posting."},
    {"source_url": "https://jobs.example.test/elsewhere"}, {"posted": NOW.date() - timedelta(days=40)},
], ids=lambda c: next(iter(c)))
async def test_a_role_whose_column_changed_between_the_read_and_the_lock_is_left_unwritten(js, monkeypatch, change):
    ids = await world(("Fabrikam", PAY_LOW, REMOTE), ("Initech", PAY_LOW, REMOTE))
    change_before_the_lock(monkeypatch, ids["Fabrikam"], **change)
    async with db.session_factory()() as session:
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert out == {"looked": 1, "stopped": 1, "waits": 0}, "only Initech was read"
    async with db.session_factory()() as session:
        left = await session.get(Opportunity, ids["Fabrikam"])
        assert not left.gate_facts and left.status == stages.SECOND_IMPRESSION, "left for the next turn"
        assert (await session.get(Opportunity, ids["Initech"])).status == stages.GATED_OUT
        assert ids["Fabrikam"] in await gate_facts.stale(session, OWNER, limit=10), "and the next turn finds it"


async def test_a_role_whose_copy_changed_between_the_read_and_the_lock_is_left_unwritten(js, monkeypatch):
    """The posting has a newer copy (a detail read, a cleaning again) by the time the roles are locked: the facts and
    the pay ranges the gates read were made from the old one."""
    ids = await world(("Fabrikam", PAY_LOW, REMOTE), ("Initech", PAY_LOW, REMOTE))
    async with db.session_factory()() as session:
        for company in ("Fabrikam", "Initech"):
            role = await session.get(Opportunity, ids[company])
            await list_posting(session, role, "old", system=EXAMPLE_SYSTEM, seen=9, text=PAY_LOW)
        await session.commit()
    real = gate_facts.as_read.lock

    async def lock(session, model, rows, **kw):
        async with db.session_factory()() as other:
            role = await other.get(Opportunity, ids["Fabrikam"])
            await list_posting(other, role, "new", system=EXAMPLE_SYSTEM, seen=1, text=PAY_OK)  # the employer's newest
            await other.commit()
        return await real(session, model, rows, **kw)

    monkeypatch.setattr(gate_facts.as_read, "lock", lock)
    async with db.session_factory()() as session:
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert out["looked"] == 1
    async with db.session_factory()() as session:
        assert not (await session.get(Opportunity, ids["Fabrikam"])).gate_facts
        assert (await session.get(Opportunity, ids["Initech"])).status == stages.GATED_OUT


async def test_a_role_whose_posting_was_dated_again_between_the_read_and_the_lock_is_left_unwritten(js, monkeypatch):
    """The employer's posting of the role is dated again (a read of its page, a board's new listing) by the time the
    roles are locked: the age the gates judge was counted from the old date. Found 2026-11-06 in review: the posted day
    was no stand, so the role was written on the older day."""
    ids = await world(("Fabrikam", PAY_LOW, REMOTE), ("Initech", PAY_LOW, REMOTE))
    async with db.session_factory()() as session:
        for company in ("Fabrikam", "Initech"):
            role = await session.get(Opportunity, ids[company])
            await list_posting(session, role, "emp", system=EXAMPLE_SYSTEM, seen=3, text=PAY_LOW, posted=20)
        await session.commit()
    real = gate_facts.as_read.lock

    async def lock(session, model, rows, **kw):
        async with db.session_factory()() as other:
            await other.execute(update(JobPosting).where(JobPosting.opportunity_id == ids["Fabrikam"]).values(
                posted=(NOW - timedelta(days=8)).date()))
            await other.commit()
        return await real(session, model, rows, **kw)

    monkeypatch.setattr(gate_facts.as_read, "lock", lock)
    async with db.session_factory()() as session:
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert out == {"looked": 1, "stopped": 1, "waits": 0}, "only Initech was read"
    async with db.session_factory()() as session:
        assert not (await session.get(Opportunity, ids["Fabrikam"])).gate_facts, "left for the next turn"
        assert (await session.get(Opportunity, ids["Initech"])).status == stages.GATED_OUT
        assert ids["Fabrikam"] in await gate_facts.stale(session, OWNER, limit=10), "which finds it"


async def test_a_role_a_session_took_between_the_read_and_the_lock_is_left_to_the_session(js, monkeypatch):
    ids = await world(("Fabrikam", PAY_OK, REMOTE), ("Initech", PAY_OK, REMOTE))
    change_before_the_lock(monkeypatch, ids["Fabrikam"], analysis_state="waiting", analysis_claimed_at=NOW)
    async with db.session_factory()() as session:
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert out["looked"] == 1
    async with db.session_factory()() as session:
        assert not (await session.get(Opportunity, ids["Fabrikam"])).gate_facts


async def test_a_role_the_owner_moved_after_the_read_is_not_read(js, monkeypatch):
    """The owner moves it (a line of their own in its history, and its stage) after the batch has read where the owner
    put each role and before it locks them: the stage it was read at is no longer the one it holds."""
    ids = await world(("Fabrikam", PAY_LOW, REMOTE), ("Initech", PAY_LOW, REMOTE))
    real = gate_facts.as_read.lock

    async def lock(session, model, rows, **kw):
        async with db.session_factory()() as other:
            (await other.get(Opportunity, ids["Fabrikam"])).status = stages.READY_TO_LAUNCH
            await trail.record(other, OWNER, event="role_moved", actor="person", who="Pat", sentence="You moved it.",
                               link=("opportunity", ids["Fabrikam"]))
            await other.commit()
        return await real(session, model, rows, **kw)

    monkeypatch.setattr(gate_facts.as_read, "lock", lock)
    async with db.session_factory()() as session:
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert out["looked"] == 1
    async with db.session_factory()() as session:
        moved = await session.get(Opportunity, ids["Fabrikam"])
        assert moved.status == stages.READY_TO_LAUNCH and not moved.gate_facts


# --- one role that cannot be read ahead does not stop the others -----------------------------------------------


async def test_a_role_whose_facts_cannot_be_read_ahead_is_left_and_the_others_in_its_batch_are_read(js, monkeypatch):
    """Found 2026-11-06 in review: the reading ahead is outside each role's savepoint, so a role it fails on must not
    raise out of the batch, where the turn would stop and every turn after it meet the same role first."""
    ids = await world(("Fabrikam", PAY_LOW, REMOTE), ("Initech", PAY_LOW, REMOTE), ("Hooli", PAY_OK, REMOTE))
    real = gate_facts._extract

    def extract(role, text):
        if role.company == "Initech":
            raise ValueError("a text the reader cannot read")
        return real(role, text)

    monkeypatch.setattr(gate_facts, "_extract", extract)
    async with db.session_factory()() as session:
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert out == {"looked": 2, "stopped": 1, "waits": 0}
    async with db.session_factory()() as session:
        assert not (await session.get(Opportunity, ids["Initech"])).gate_facts, "as when its own read failed"
        assert (await session.get(Opportunity, ids["Fabrikam"])).status == stages.GATED_OUT


async def test_a_failure_in_the_reads_ahead_falls_back_to_each_role_reading_what_it_needs_as_it_is_locked(
        js, monkeypatch):
    ids = await world(("Fabrikam", PAY_LOW, REMOTE), ("Initech", PAY_LOW, REMOTE), ("Hooli", PAY_OK, REMOTE))
    real = gate_facts.freshness.posted_by_role

    async def posted(session, owner, roles, held=None):
        if len(list(roles)) > 1:
            raise ValueError("the posted day of a batch could not be worked out")
        return await real(session, owner, roles, held)

    monkeypatch.setattr(gate_facts.freshness, "posted_by_role", posted)
    async with db.session_factory()() as session:
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert out == {"looked": 3, "stopped": 2, "waits": 0}, "every role read, one by one, as before"


async def test_a_database_error_in_the_reads_ahead_does_not_stop_the_batch_and_each_role_is_read_as_it_is_locked(
        js, monkeypatch):
    """Found 2026-11-06 in review: in Postgres a statement that fails aborts the transaction, so a failed read ahead
    outside a savepoint made the lock after it fail (`InFailedSQLTransaction`), and the batch and the turn's other
    batches were lost, where the log said each role is read as it is locked."""
    ids = await world(("Fabrikam", PAY_LOW, REMOTE), ("Initech", PAY_LOW, REMOTE), ("Hooli", PAY_OK, REMOTE))
    real = gate_facts.cleansed.gate_copy_ids_for
    calls = []

    async def failing(session, owner, role_ids):
        calls.append(list(role_ids))
        if len(calls) == 1:
            await session.execute(text("SELECT 1/0"))  # a statement the database refuses, as a timeout would be
        return await real(session, owner, role_ids)

    monkeypatch.setattr(gate_facts.cleansed, "gate_copy_ids_for", failing)
    async with db.session_factory()() as session:
        out = await gate_facts.read_batch(session, OWNER, list(ids.values()))
        await session.commit()
    assert calls, "the read ahead did fail"
    assert out == {"looked": 3, "stopped": 2, "waits": 0}, "every role read, one by one, as before"
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, ids["Fabrikam"])).status == stages.GATED_OUT
        assert (await session.get(Opportunity, ids["Initech"])).status == stages.GATED_OUT


async def test_the_texts_of_the_roles_are_judged_giving_the_event_loop_back(js, monkeypatch):
    ids = await twenty_five()
    real, yields = asyncio.sleep, []

    async def sleep(delay, *args):
        yields.append(delay)
        return await real(delay, *args)

    monkeypatch.setattr(gate_facts, "YIELD_AFTER", 0.0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity).where(Opportunity.id.in_(list(ids.values()))))).all())
        yields.clear()
        texts = await gate_facts._judged_texts(session, OWNER, roles)
    assert len(texts) == 25 and yields.count(0) >= 25, "at least once for each role judged"


# --- the same answer with the reads made first ------------------------------------------------------------------


COMPANIES = (("A", PAY_LOW), ("B", PAY_OK), ("C", NOTHING), ("D", UNCLEAR), ("E", PAY_LOW.replace("Pay", "Salary")))


def settled(role: Opportunity) -> dict:
    """What a read left on a role, without the moments and the hash of the company (each has a company of its own)."""
    facts = {k: v for k, v in (role.gate_facts or {}).items() if k not in ("at", "fp")}
    return {"status": role.status, "facts": facts, "comp": role.comp, "location": role.location,
            "move_type": role.move_type, "gate": role.screened_out_gate_id, "state": role.analysis_state}


async def test_a_batch_leaves_every_role_as_reading_it_alone_does(js, monkeypatch):
    """Two copies of each role, one read by `process` on its own (it reads what it needs as it goes), the other by
    the batch (it was handed what was read first): the same status, facts, place, pay and stop."""
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    alone = await world(*((f"Alone{n}", text, {"location": "Riverton, OH"}) for n, text in COMPANIES))
    batch = await world(*((f"Batch{n}", text, {"location": "Riverton, OH"}) for n, text in COMPANIES))
    async with db.session_factory()() as session:
        for role_id in alone.values():
            await gate_facts.process(session, await session.get(Opportunity, role_id))
        await session.commit()
        assert (await gate_facts.read_batch(session, OWNER, list(batch.values())))["looked"] == len(COMPANIES)
        await session.commit()
    async with db.session_factory()() as session:
        one = [settled(await session.get(Opportunity, role_id)) for role_id in alone.values()]
        many = [settled(await session.get(Opportunity, role_id)) for role_id in batch.values()]
    assert one == many
    assert {row["status"] for row in one} == {stages.GATED_OUT, stages.SECOND_IMPRESSION}, "roles of kinds that differ"
    assert any(row["facts"].get("state") == gate_facts.PENDING for row in one), "and one waits for a Haiku read"


# --- stale: slices, yields and the limit -----------------------------------------------------------------------


async def aged(n: int, **fields) -> dict:
    """`n` roles in Found, unread, the first the oldest."""
    return await world(*((f"Company{k}", PAY_OK, {**REMOTE, "created_at": NOW - timedelta(days=n - k), **fields})
                         for k in range(n)))


async def test_stale_reads_the_roles_in_slices_and_stops_at_its_limit(js, monkeypatch, statements):
    monkeypatch.setattr(gate_facts, "STALE_SLICE", 2)
    ids = await aged(7)
    async with db.session_factory()() as session:
        statements.sent.clear()
        found = await gate_facts.stale(session, OWNER, limit=3)
    oldest = [ids[f"Company{k}"] for k in range(7)]
    assert found == oldest[:3], "the oldest three, as reading every role gives"
    loads = [s for s in statements.sent if s.startswith("SELECT js_opportunities.id, js_opportunities.owner")]
    assert len(loads) == 2, "two slices of two roles are enough for three: the other two slices are never read"
    async with db.session_factory()() as session:
        assert await gate_facts.stale(session, OWNER, limit=100) == oldest, "and with room, every role"


async def test_the_roles_the_owner_moved_do_not_count_toward_the_limit_in_any_slice(js, monkeypatch):
    monkeypatch.setattr(gate_facts, "STALE_SLICE", 1)
    ids = await aged(5)
    async with db.session_factory()() as session:
        for k in (0, 2):
            await trail.record(session, OWNER, event="role_moved", actor="person", who="Pat", sentence="You moved it.",
                               link=("opportunity", ids[f"Company{k}"]))
        await session.commit()
        found = await gate_facts.stale(session, OWNER, limit=2)
    assert found == [ids["Company1"], ids["Company3"]], "the oldest two the step may read"


async def test_stale_gives_the_event_loop_back_between_slices_and_as_it_judges(js, monkeypatch):
    monkeypatch.setattr(gate_facts, "STALE_SLICE", 2)
    monkeypatch.setattr(gate_facts, "YIELD_AFTER", 0.0)
    await aged(5)
    real, yields = asyncio.sleep, []

    async def sleep(delay, *args):
        yields.append(delay)
        return await real(delay, *args)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    async with db.session_factory()() as session:
        await gate_facts.stale(session, OWNER, limit=100)
    assert yields.count(0) >= 3 + 5, "after each of the three slices and each of the five roles judged"


async def test_stale_sees_a_role_in_found_only_once_and_in_its_own_order_when_ids_share_a_day(js, monkeypatch):
    """Ties in the day are broken by id, as before: the same list whatever the slice."""
    monkeypatch.setattr(gate_facts, "STALE_SLICE", 2)
    same = NOW - timedelta(days=3)
    ids = await world(*((f"Company{k}", PAY_OK, {**REMOTE, "created_at": same, "id": uuid.UUID(int=9 - k)})
                        for k in range(5)))
    async with db.session_factory()() as session:
        found = await gate_facts.stale(session, OWNER, limit=100)
        want = (await session.scalars(select(Opportunity.id).order_by(Opportunity.created_at, Opportunity.id))).all()
    assert found == list(want) and sorted(found) == sorted(ids.values())


async def test_sweep_gives_read_batch_the_stale_roles_a_slice_at_a_time_and_adds_up_what_it_reads(js, monkeypatch):
    """`sweep` named up to 400 roles and gave them all to one `read_batch`, which reads everything of every role first
    (the texts, the copies, the postings' dates of all of them at once): a slice at a time keeps those loads to what
    `stale` loads."""
    monkeypatch.setattr(gate_facts, "STALE_SLICE", 2)
    ids = await aged(5)
    oldest = [ids[f"Company{k}"] for k in range(5)]
    real, given = gate_facts.read_batch, []

    async def spy(session, owner, batch, rules=None):
        given.append(list(batch))
        return await real(session, owner, batch, rules)

    monkeypatch.setattr(gate_facts, "read_batch", spy)
    async with db.session_factory()() as session:
        out = await gate_facts.sweep(session, OWNER)
        await session.commit()
    assert given == [oldest[:2], oldest[2:4], oldest[4:]], "the oldest first, two at a time"
    assert out == {"looked": 5, "stopped": 0, "waits": 0}, "what every slice read, added up"
    async with db.session_factory()() as session:
        assert await gate_facts.stale(session, OWNER, limit=10) == [], "every role was read"
