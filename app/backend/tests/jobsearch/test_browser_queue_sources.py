"""The browser queue's two writing steps before its reads, `sourced.source_held` and `reader.note_no_link`, read in a
reader's session and take the writer's turn only for the roles they change (the first used to hold the turn at every
start of the queue). Every name here is made up, and no test reaches the network."""

import asyncio
import contextlib

import pytest
from sqlalchemy import select, update

from app import db
from app.jobsearch import as_read, off_turn
from app.jobsearch.browser import reader as br
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.postings import sourced as sourcing
from tests.builders import browser_world
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.jobsearch.test_browser_work import FEEDSYS, NOW, _get, _role
from tests.support import DEFAULT_OWNER as OWNER

pytestmark = pytest.mark.usefixtures("example_world")

WORKER_NOTE = "a worker's own note"


class Reads:
    """What `off_turn.reading` was asked for while a test ran: whether each read was apart from the writer's turn and,
    if so, whether a second writer got through meanwhile. `between` is another writer's change to a row the step read,
    made once the read is open."""

    def __init__(self) -> None:
        self.apart: list[bool] = []
        self.second_writer: list[bool] = []
        self.between = None


@contextlib.contextmanager
def watching(monkeypatch):
    seen = Reads()
    real = off_turn.reading

    @contextlib.asynccontextmanager
    async def reading(session, **kwargs):
        async with real(session, **kwargs) as reader:
            seen.apart.append(db.is_reader(reader))
            if db.is_reader(reader):
                async with db.session_factory()() as other:
                    try:
                        await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.slug == "bystander")
                                                             .values(slug="bystander")), 1)
                        seen.second_writer.append(True)
                    except TimeoutError:  # queued behind the step: it still holds the writer's turn
                        seen.second_writer.append(False)
                    await other.rollback()
                if seen.between is not None:
                    async with db.session_factory()() as other:
                        await seen.between(other)
                        await other.commit()
            yield reader

    monkeypatch.setattr(off_turn, "reading", reading)
    yield seen


async def _held_world() -> dict:
    """Two roles in Followup needed that hold their employer's link (to source), one that holds only its board's, and a
    bystander a second writer may touch."""
    await browser_world()
    return {
        "bystander": await _role(9, slug="bystander"),
        "one": await _role(1, status="followup_needed", url=FEEDSYS.format(n=1), apply_url=FEEDSYS.format(n=1),
                           board_url=None),
        "two": await _role(2, status="followup_needed", url=FEEDSYS.format(n=2), apply_url=FEEDSYS.format(n=2),
                           board_url=None),
        "open": await _role(3, status="followup_needed"),
    }


async def _sources(ids) -> dict:
    out = {}
    for key, role in ids.items():
        row = await _get(Opportunity, role)
        out[key] = (row.source_url, row.source_kind, row.sourced_at is not None)
    return out


async def test_source_held_reads_without_the_writers_turn_and_sources_what_the_same_run_in_one_transaction_sources(
        js, monkeypatch):
    """Old: the wanted roles were read in the writer's transaction, which held the turn for the read. New: they are read
    in a reader's session (a second writer gets through meanwhile) and the turn is taken for the roles to source alone.
    Run inside a savepoint, where a transaction cannot be ended, it reads in the session and sources the same roles."""
    ids = await _held_world()
    with watching(monkeypatch) as seen:
        async with db.session_factory()() as session:
            assert await sourcing.source_held(session, OWNER) == 2
            await session.commit()
    assert seen.apart == [True] and seen.second_writer == [True]
    apart = await _sources(ids)
    assert apart["one"] == (FEEDSYS.format(n=1), "employer", True)
    assert apart["two"] == (FEEDSYS.format(n=2), "employer", True)
    assert apart["open"] == (None, None, False)
    async with db.session_factory()() as session:  # the same world again, sourced inside one transaction
        await session.execute(update(Opportunity).where(Opportunity.owner == OWNER).values(
            source_url=None, source_kind=None, sourced_at=None))
        await session.commit()
    with watching(monkeypatch) as seen:
        async with db.session_factory()() as session:
            async with session.begin_nested():
                assert await sourcing.source_held(session, OWNER) == 2
            await session.commit()
    assert seen.apart == [False], "inside a savepoint it reads in the session"
    assert await _sources(ids) == apart


async def test_source_held_leaves_a_role_another_writer_sourced_between_its_read_and_its_turn(js, monkeypatch):
    ids = await _held_world()
    elsewhere = "https://careers.elsewhere.test/jobs/1"

    async def sourced_meanwhile(other) -> None:
        await other.execute(update(Opportunity).where(Opportunity.id == ids["one"]).values(
            source_url=elsewhere, source_kind="recruiter", sourced_at=NOW))

    with watching(monkeypatch) as seen:
        seen.between = sourced_meanwhile
        async with db.session_factory()() as session:
            assert await sourcing.source_held(session, OWNER) == 1
            await session.commit()
    sources = await _sources(ids)
    assert sources["one"] == (elsewhere, "recruiter", True), "its other source stands"
    assert sources["two"] == (FEEDSYS.format(n=2), "employer", True)


async def test_source_held_takes_no_turn_when_no_role_is_to_be_sourced(js):
    await browser_world()
    await _role(1, status="followup_needed")
    async with db.session_factory()() as session:
        assert await sourcing.source_held(session, OWNER) == 0
        assert not session.in_transaction(), "nothing was written, so no turn was taken"


async def test_note_no_link_reads_without_the_writers_turn_and_writes_the_notes_the_one_transaction_run_writes(
        js, monkeypatch):
    """Old: the roles were streamed in the writer's transaction. New: in a reader's session; only the roles whose note
    changes are locked and written. Run inside a savepoint it reads in the session and writes the same notes."""
    await browser_world()
    await _role(9, slug="bystander", status="followup_needed")
    await _role(1, status="followup_needed", url=None, board_url=None)
    await _role(2, status="followup_needed", url=None, board_url=None)
    await _role(3, status="followup_needed", browser_note=br.NO_LINK)  # it has a link again: it loses the note
    await _role(4, status="followup_needed", url=None, board_url=None, browser_note=WORKER_NOTE)

    async def notes() -> dict:
        async with db.session_factory()() as session:
            return {r.slug: r.browser_note for r in (await session.scalars(select(Opportunity).where(
                Opportunity.owner == OWNER))).all()}

    with watching(monkeypatch) as seen:
        async with db.session_factory()() as session:
            await br.note_no_link(session, OWNER)
            await session.commit()
    assert seen.apart == [True] and seen.second_writer == [True]
    apart = await notes()
    assert apart == {"bystander": None, "role-1": br.NO_LINK, "role-2": br.NO_LINK, "role-3": None,
                     "role-4": WORKER_NOTE}
    async with db.session_factory()() as session:  # the same world again, noted inside one transaction
        await session.execute(update(Opportunity).where(Opportunity.owner == OWNER).values(browser_note=None))
        await session.execute(update(Opportunity).where(Opportunity.slug == "role-3").values(browser_note=br.NO_LINK))
        await session.execute(update(Opportunity).where(Opportunity.slug == "role-4").values(
            browser_note=WORKER_NOTE))
        await session.commit()
    with watching(monkeypatch) as seen:
        async with db.session_factory()() as session:
            async with session.begin_nested():
                await br.note_no_link(session, OWNER)
            await session.commit()
    assert seen.apart == [False]
    assert await notes() == apart


async def test_note_no_link_leaves_a_role_whose_note_another_writer_changed_between_its_read_and_its_turn(
        js, monkeypatch):
    await browser_world()
    ids = [await _role(1, status="followup_needed", url=None, board_url=None),
           await _role(2, status="followup_needed", url=None, board_url=None)]

    async def worker_noted(other) -> None:
        await other.execute(update(Opportunity).where(Opportunity.id == ids[0]).values(browser_note=WORKER_NOTE))

    with watching(monkeypatch) as seen:
        seen.between = worker_noted
        async with db.session_factory()() as session:
            await br.note_no_link(session, OWNER)
            await session.commit()
    assert [(await _get(Opportunity, i)).browser_note for i in ids] == [WORKER_NOTE, br.NO_LINK]


async def test_note_no_link_takes_no_turn_when_no_note_changes(js):
    await browser_world()
    await _role(1, status="followup_needed")
    async with db.session_factory()() as session:
        await br.note_no_link(session, OWNER)
        assert not session.in_transaction(), "nothing was written, so no turn was taken"


async def test_source_held_leaves_a_role_whose_employer_link_changed_between_its_read_and_its_turn(js, monkeypatch):
    """Old: the role's own columns were checked under the turn, not its employer's: a role chosen on its employer's job
    system board was sourced although the employer's board had changed to another since the read. New: the employer is
    read again under the turn and the choice made again on it."""
    ids = await _held_world()
    async with db.session_factory()() as session:
        employer = Employer(owner=OWNER, key="initech-1", name="Initech 1", aliases=[], offices=[], sectors=[],
                            events=[], sources={}, owner_fields=[], ats="feedsys", board={"slug": "initech1"})
        session.add(employer)
        await session.flush()
        await session.execute(update(Opportunity).where(Opportunity.id == ids["one"]).values(employer_id=employer.id))
        await session.commit()
        employer_id = employer.id

    async def board_changed(other) -> None:
        await other.execute(update(Employer).where(Employer.id == employer_id).values(board={"slug": "elsewhere"}))

    real = as_read.lock

    async def lock_after_the_change(*args, **kwargs):  # the employer changes after the read, before the turn
        async with db.session_factory()() as other:
            await board_changed(other)
            await other.commit()
        return await real(*args, **kwargs)

    monkeypatch.setattr(as_read, "lock", lock_after_the_change)
    async with db.session_factory()() as session:
        assert await sourcing.source_held(session, OWNER) == 1
        await session.commit()
    sources = await _sources(ids)
    assert sources["one"] == (None, None, False), "its employer's board is another now: left for the next turn"
    assert sources["two"] == (FEEDSYS.format(n=2), "employer", True)


async def test_the_plan_reads_the_mission_and_the_sites_without_the_writers_turn(js, monkeypatch):
    """Old: the plan read the mission and the browser's sites in the writer's session, which began the turn there and
    held it until `source_held`'s release. New: they are read in a reader's session. Inside a savepoint, where the
    transaction cannot be ended, they are read in the session and the plan is the same."""
    from app.jobsearch.browser import queue as bq
    from app.timeutil import utc_now

    await browser_world()
    await _role(1, status="followup_needed")
    asked: list[bool] = []
    real = br.browser_sites

    async def spy(session, owner):
        asked.append(db.is_reader(session))
        return await real(session, owner)

    monkeypatch.setattr(br, "browser_sites", spy)
    now = utc_now()
    async with db.session_factory()() as session:
        apart = await bq._plan(session, OWNER, now)
        await session.rollback()
    assert asked[0] is True
    asked.clear()
    async with db.session_factory()() as session:
        async with session.begin_nested():
            inside = await bq._plan(session, OWNER, now)
        await session.rollback()
    assert asked[0] is False
    assert apart is not None and inside is not None
    assert [(a["kind"], a["target"]) for a in apart.adds] == [(a["kind"], a["target"]) for a in inside.adds]
