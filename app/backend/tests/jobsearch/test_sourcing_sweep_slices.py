"""`sourced.sweep`, called where the caller can end its transaction, reads the roles and what their choice rests on
apart (in a reader's session, in a handful of statements for any number of roles), runs its two gate passes a batch of
roles to a transaction, and writes the sourced roles `SLICE` at a time, each role in a savepoint, each slice on the
rows as they are then. Inside a savepoint nothing can be released and the sweep is the one transaction it was. Every
name here is made up, and no test reaches the network."""

import math
import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event, inspect, select, text, update

from app import db
from app.jobsearch import stages
from app.jobsearch.employers.core import key_of
from app.jobsearch.gating import gate_facts
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.postings import sourced as sourcing
from app.models.work import TrailEvent
from tests.conftest import Transactions
from tests.jobsearch.made_up_sites_b import example_system_address
from tests.jobsearch.test_gate_facts import NOTHING, PAY_OK, REMOTE, world
from tests.support import EXAMPLE_SYSTEM, OWNER

NOW = datetime.now(UTC)
LINK = example_system_address("initech{n}", "40{n}")
BOARD_ROOT = example_system_address("fabrikam", "0").rsplit("/jobs/", 1)[0]


async def _unsourced(count: int, *, employers: int = 0, **more) -> list[uuid.UUID]:
    """`count` roles in Followup needed, not Sourced, each holding a link that is one posting (oldest first), over
    `employers` employer records (none: found by company name, as the roles of an older import are)."""
    async with db.session_factory()() as session:
        made = [Employer(owner=OWNER, key=key_of(f"Initech {e}"), name=f"Initech {e}") for e in range(employers)]
        session.add_all(made)
        await session.flush()
        roles = []
        for n in range(1, count + 1):
            roles.append(Opportunity(**{
                "owner": OWNER, "slug": f"role-{n}", "company": f"Initech {n % employers if employers else n}",
                "title": f"Programs Director {n}", "match_key": f"initech {n}|programs director {n}",
                "origin": "scout", "status": stages.FOLLOWUP_NEEDED, "found": date(2026, 10, 1),
                "url": LINK.format(n=n), "apply_url": LINK.format(n=n), "sourced_at": None,
                "created_at": NOW - timedelta(minutes=count - n),
                "employer_id": made[n % employers].id if employers else None, **more}))
        session.add_all(roles)
        await session.commit()
        return [r.id for r in roles]


async def _found(count: int) -> None:
    """The profile, and `count` Sourced roles in Found that no gate has read."""
    kinds = (PAY_OK, NOTHING)
    await world(*((f"Company{n}", kinds[n % 2], {**REMOTE, "created_at": NOW - timedelta(hours=n)})
                  for n in range(count)))


async def _rows(ids) -> list:
    async with db.session_factory()() as session:
        out = []
        for i in ids:
            row = await session.get(Opportunity, i, populate_existing=True)
            out.append((row.slug, row.source_url, row.source_kind, row.sourced_at is not None, row.url, row.board_url,
                        row.status, {k: v for k, v in row.gate_facts.items() if k != "at"}))
        return out


def _writes(transactions) -> int:
    return len([t for t in transactions.ended if Transactions.writes(t)])


async def test_a_sweep_takes_a_turn_for_each_slice_of_roles_and_each_batch_of_gates(js, transactions):
    await _found(40)
    ids = await _unsourced(50)
    async with db.session_factory()() as session:
        transactions.clear()
        assert await sourcing.sweep(session, OWNER) == {"looked": 50, "sourced": 50}
        await session.commit()
    assert _writes(transactions) >= math.ceil(50 / sourcing.SLICE) + math.ceil(40 / gate_facts.BATCH), \
        "50 roles in slices and 40 in batches are not one turn"
    assert all(done and source == LINK.format(n=n) for n, (_s, source, _k, done, *_r) in enumerate(await _rows(ids), 1))


async def _reset(before: dict, trail_before: set) -> None:
    async with db.session_factory()() as session:
        for i, values in before.items():
            await session.execute(update(Opportunity).where(Opportunity.id == i).values(**values))
        for row in (await session.scalars(select(TrailEvent))).all():
            if row.id not in trail_before:
                await session.delete(row)
        await session.commit()


async def _everything(ids) -> tuple:
    async with db.session_factory()() as session:
        events = (await session.scalars(select(TrailEvent).order_by(text("rowid")))).all()
    rows = await _rows(ids)
    slugs = {i: row[0] for i, row in zip(ids, rows, strict=True)}
    return rows, [(slugs.get(e.link_id), e.event, e.sentence, e.detail) for e in events]


async def test_the_sliced_sweep_writes_the_rows_of_the_sweep_in_one_transaction_in_the_same_order(js):
    await _found(30)
    ids = await _unsourced(45)
    columns = [c.key for c in inspect(Opportunity).columns if c.key != "id"]
    async with db.session_factory()() as session:
        before = {r.id: {k: getattr(r, k) for k in columns} for r in (await session.scalars(select(Opportunity))).all()}
        trail_before = {e.id for e in (await session.scalars(select(TrailEvent))).all()}
    async with db.session_factory()() as session:
        async with session.begin_nested():  # nothing can be released here: the one-transaction sweep
            assert await sourcing.sweep(session, OWNER) == {"looked": 45, "sourced": 45}
        await session.commit()
    one = await _everything(ids)
    await _reset(before, trail_before)
    async with db.session_factory()() as session:
        assert await sourcing.sweep(session, OWNER) == {"looked": 45, "sourced": 45}
        await session.commit()
    assert await _everything(ids) == one
    assert len(one[1]) >= 45, "the sourcing events are there"


async def test_a_savepoint_takes_the_one_transaction_path(js, transactions):
    await _found(30)
    await _unsourced(45)
    async with db.session_factory()() as session:
        transactions.clear()
        async with session.begin_nested():
            await sourcing.sweep(session, OWNER)
        await session.commit()
    assert _writes(transactions) == 1


async def test_a_role_that_raises_in_its_savepoint_costs_that_role_and_not_its_slice(js, monkeypatch):
    await _found(0)
    ids = await _unsourced(20)
    real = sourcing._keep_source

    async def keep(session, role, *args, **kw):
        await real(session, role, *args, **kw)
        if role.slug == "role-7":
            raise RuntimeError("one bad role")

    monkeypatch.setattr(sourcing, "_keep_source", keep)
    async with db.session_factory()() as session:
        assert await sourcing.sweep(session, OWNER) == {"looked": 20, "sourced": 19}
        await session.commit()
    done = {slug: sourced for slug, _u, _k, sourced, *_r in await _rows(ids)}
    assert not done["role-7"] and sum(done.values()) == 19


@pytest.fixture
def statements(js):
    sent: list[str] = []

    def noted(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        sent.append(re.sub(r"\s+", " ", statement).strip())

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        yield sent
    finally:
        event.remove(engine, "before_cursor_execute", noted)


async def test_a_hundred_roles_over_thirty_employers_are_read_in_a_constant_number_of_statements(statements):
    await _found(0)
    await _unsourced(100, employers=30)
    async with db.session_factory()() as session:
        statements.clear()
        assert (await sourcing.sweep(session, OWNER))["sourced"] == 100
        await session.commit()
    # The read: one statement for the employers by id and one for the postings, whatever the number of roles; then
    # one more read of the postings for each slice, under its turn.
    assert len([s for s in statements if "FROM js_employers" in s and "js_employers.id IN" in s]) == 1
    postings = [s for s in statements if s.startswith(
        "SELECT js_postings.id, js_postings.ats, js_postings.url, js_postings.apply_url, "
        "js_postings.opportunity_id FROM")]
    assert len(postings) == 1 + math.ceil(100 / sourcing.SLICE)


async def test_a_role_whose_postings_changed_between_the_read_and_the_lock_is_left_unsourced(js, monkeypatch):
    """The role's own link is a board's, so only its job system's posting makes its choice; that posting goes before
    the lock is taken, and nothing the role itself holds shows it."""
    await _found(0)
    (kept, changed) = await _unsourced(2, url="https://www.example-board.test/jobs/view/1", apply_url=None)
    async with db.session_factory()() as session:
        session.add_all(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=str(n), company="Fabrikam",
            title=f"Programs Lead {n}", match_key=f"fabrikam|programs lead {n}", first_seen=NOW, last_seen=NOW,
            links=[], opportunity_id=i, url=f"{BOARD_ROOT}/jobs/{n}")
            for n, i in enumerate((kept, changed), 1))
        await session.commit()
    real = sourcing.as_read.lock

    async def lock(session, model, rows, **kw):
        async with db.session_factory()() as other:  # a writer, after the read and before the lock
            await other.execute(update(JobPosting).where(JobPosting.opportunity_id == changed).values(ats="scout"))
            await other.commit()
        return await real(session, model, rows, **kw)

    monkeypatch.setattr(sourcing.as_read, "lock", lock)
    async with db.session_factory()() as session:
        assert await sourcing.sweep(session, OWNER) == {"looked": 2, "sourced": 1}
        await session.commit()
    done = [sourced for _s, _u, _k, sourced, *_r in await _rows([kept, changed])]
    assert done == [True, False]


async def test_a_role_whose_employer_changed_between_the_read_and_the_lock_is_left_unsourced(js, monkeypatch):
    await _found(0)
    link = f"{BOARD_ROOT}/jobs/4012345"
    (role,) = await _unsourced(1, employers=1, url=link, apply_url=link)
    async with db.session_factory()() as session:
        employer = (await session.scalars(select(Employer))).one()
        employer.ats, employer.board = EXAMPLE_SYSTEM, {"slug": "fabrikam"}
        await session.commit()
    real = sourcing.as_read.lock

    async def lock(session, model, rows, **kw):
        async with db.session_factory()() as other:
            moved = (await other.scalars(select(Employer))).one()
            moved.board = {"slug": "another"}
            await other.commit()
        return await real(session, model, rows, **kw)

    monkeypatch.setattr(sourcing.as_read, "lock", lock)
    async with db.session_factory()() as session:
        assert (await sourcing.sweep(session, OWNER))["sourced"] == 0
        await session.commit()
    assert not (await _rows([role]))[0][3]


async def test_the_board_sweep_starts_once_and_after_the_last_slice(js, monkeypatch):
    await _found(0)
    await _unsourced(45)  # three slices
    async with db.session_factory()() as session:  # and a role whose board has to be read
        session.add(Opportunity(owner=OWNER, slug="on-board", company="Fabrikam", title="Programs Lead",
                                match_key="fabrikam|programs lead", origin="scout", status=stages.FOLLOWUP_NEEDED,
                                found=date(2026, 10, 1), url=BOARD_ROOT, apply_url=BOARD_ROOT, sourced_at=None))
        await session.commit()
    seen: list[str] = []
    started: list[int] = []
    monkeypatch.setattr(sourcing, "start_board_sweep", lambda owner: started.append(len(seen)) or True)
    real = sourcing.as_read.lock

    async def lock(session, model, rows, **kw):
        if kw.get("stands") == sourcing._SOURCE_STANDS:  # the gate passes lock too
            seen.append("slice")
        return await real(session, model, rows, **kw)

    monkeypatch.setattr(sourcing.as_read, "lock", lock)
    async with db.session_factory()() as session:
        assert (await sourcing.sweep(session, OWNER))["sourced"] == 45
        assert started == []
        await session.commit()
        await session.commit()
    assert started == [3], "once, with all three slices taken"
