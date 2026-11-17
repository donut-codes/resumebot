"""The profile in force is built once for as long as nothing is saved (`profile_store.current`, docs/PRD.md 2.2): each
call asks the database which row is the newest of each of the four documents (one statement, by the rows' ids) and
builds the profile only when they are not the rows it was built from. The key is rows, never version numbers, so a
database emptied and filled again, a second process or a save gives another key. Everything here is made up."""

import asyncio
import contextlib

import pytest
from sqlalchemy import delete, event

from app import db
from app.jobsearch.models import ProfileVersion
from app.jobsearch.profile import store as profile_store
from tests.support import FIVE, OWNER, load_profile

GATES = {"location": {"remote": {"on": True}}, "compensation": {"amount": 100_000, "against": "maximum"}}
MORE = {"location": {"remote": {"on": True}}, "compensation": {"amount": 140_000, "against": "maximum"}}


class Sent:
    def __init__(self) -> None:
        self.statements: list[str] = []


@pytest.fixture
def sent(js):
    seen = Sent()

    def noted(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        seen.statements.append(statement)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", noted)


def when_all_have_asked_to_build(monkeypatch, cache: profile_store.OwnerCache, n: int) -> asyncio.Event:
    """An Event that is set when `n` tasks have asked `cache` to build (`building`: the first holds the lock and the
    others wait at it). A build that waits on it takes as long as the tasks need to arrive: no sleep to guess it."""
    arrived: list[str] = []
    everyone = asyncio.Event()
    real = cache.building

    @contextlib.asynccontextmanager
    async def building(owner):
        arrived.append(owner)
        if len(arrived) == n:
            everyone.set()
        async with real(owner):
            yield

    monkeypatch.setattr(cache, "building", building)
    return everyone


async def test_the_profile_in_force_is_one_statement_once_it_has_been_built(js, sent):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        await session.commit()
    async with db.session_factory()() as session:
        first = await profile_store.current(session, OWNER)
        sent.statements.clear()
        again = await profile_store.current(session, OWNER)
    assert again == first
    assert len(sent.statements) == 1 and "profile_versions" in sent.statements[0], "which row is newest, and no more"


async def test_a_save_of_each_document_shows_at_the_next_call(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        await session.commit()
    async with db.session_factory()() as session:
        before = await profile_store.current(session, OWNER)
        assert before.gates.compensation.amount == 100_000
        await profile_store.save(session, OWNER, "gates", MORE, by="test")
        await session.commit()
    async with db.session_factory()() as session:
        after = await profile_store.current(session, OWNER)
        assert after.gates.compensation.amount == 140_000 and after.scoring == before.scoring
        await profile_store.save(session, OWNER, "settings", {"classifier_min_examples": 33}, by="test")
        await session.commit()
        assert (await profile_store.current(session, OWNER)).settings.classifier_min_examples == 33
        assert (await profile_store.current(session, OWNER)).gates.compensation.amount == 140_000
        await profile_store.save(session, OWNER, "criteria", {"company": "A small agency."}, by="test")
        await session.commit()
        assert (await profile_store.current(session, OWNER)).criteria.company == "A small agency."


async def test_a_save_not_yet_committed_shows_to_the_session_that_made_it_and_to_no_other(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        await session.commit()
    async with db.session_factory()() as mine, db.read_session_factory()() as other:  # a reader: no turn
        assert (await profile_store.current(mine, OWNER)).gates.compensation.amount == 100_000
        await profile_store.save(mine, OWNER, "gates", MORE, by="test")
        assert (await profile_store.current(mine, OWNER)).gates.compensation.amount == 140_000
        assert (await profile_store.current(other, OWNER)).gates.compensation.amount == 100_000, "not committed yet"
        await mine.rollback()
        assert (await profile_store.current(mine, OWNER)).gates.compensation.amount == 100_000, "and never was"


async def test_what_a_caller_changes_in_the_profile_it_was_given_is_its_own(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        await session.commit()
    async with db.session_factory()() as session:
        mine = await profile_store.current(session, OWNER)
        mine.criteria.company = "Rewritten by a caller"
        mine.gates.compensation.amount = 1
        again = await profile_store.current(session, OWNER)
    assert again.criteria.company != "Rewritten by a caller" and again.gates.compensation.amount == 100_000
    assert again is not mine


async def test_with_nothing_saved_the_profile_is_empty_and_the_first_save_shows(js):
    async with db.session_factory()() as session:
        assert (await profile_store.current(session, OWNER)).gates.compensation.amount in (None, 0)
        assert not (await profile_store.in_force(session, OWNER)).ids[0]
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        assert (await profile_store.current(session, OWNER)).gates.compensation.amount == 100_000


async def test_a_database_emptied_and_filled_again_with_the_same_owner_and_version_numbers_gives_the_new_profile(js):
    """What the test suite does between tests (it deletes every row, and the next test saves version 1 again), and
    what a restored backup is: the version numbers come back, the rows do not."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        await session.commit()
    async with db.session_factory()() as session:
        old = await profile_store.current(session, OWNER)
        assert (await profile_store.in_force(session, OWNER)).version_of("gates") == 1
        await session.execute(delete(ProfileVersion))
        await session.commit()
        await load_profile(session, OWNER, scoring=FIVE, gates=MORE)
        await session.commit()
    async with db.session_factory()() as session:
        assert (await profile_store.in_force(session, OWNER)).version_of("gates") == 1, "the same number"
        new = await profile_store.current(session, OWNER)
    assert old.gates.compensation.amount == 100_000 and new.gates.compensation.amount == 140_000


async def test_a_second_task_asking_while_the_first_builds_waits_and_builds_nothing(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        await session.commit()
    built = []
    real = profile_store._profile_of_rows
    everyone = when_all_have_asked_to_build(monkeypatch, profile_store._PROFILES, 3)

    async def counting(session, held):
        built.append(1)
        await asyncio.wait_for(everyone.wait(), 10)  # the other two are at the lock by now
        return await real(session, held)

    monkeypatch.setattr(profile_store, "_profile_of_rows", counting)

    async def ask():
        async with db.read_session_factory()() as session:
            return await profile_store.current(session, OWNER)

    first, second, third = await asyncio.gather(ask(), ask(), ask())
    assert first == second == third and len(built) == 1
