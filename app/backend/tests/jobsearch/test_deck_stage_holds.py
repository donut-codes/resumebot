"""The deck's take-whole and make-roles stages and the stated place hold the writer's one turn for their
writes alone, not for the reads that decide what to write. Each reads in a reader's session where its
caller holds nothing, writes a batch of roles to a transaction, and writes the same as a pass inside one
transaction. Made-up data."""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app import db
from app.jobsearch import off_turn, stages
from app.jobsearch.gating import stated_place
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import deck
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.postings import raw_copies
from app.jobsearch.postings import text as posting_text
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
WORDS = "A whole posting with real duties, asks and a place. " * 12


async def _second_writer_gets_through() -> bool:
    """Whether another writer is given its turn now (a statement in a session of its own, rolled back)."""
    async with db.session_factory()() as other:
        try:
            await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.slug == "no-such").values(
                slug="no-such")), 1)
            return True
        except TimeoutError:  # queued behind the holder of the writer's turn
            return False
        finally:
            await other.rollback()


def _mark_keep_whole_text(monkeypatch) -> dict:
    """`raw_copies.keep_whole_text` finds its candidates with `off_turn.reading` too, inside the transaction
    `take_whole` has just written and flushed in, where it must read in that session (it sees the batch's uncommitted
    descriptions). The returned dict says whether that call is running, so a spy can tell `take_whole`'s own reading
    from it."""
    inside = {"now": False}
    real = raw_copies.keep_whole_text

    async def marked(*args, **more):
        inside["now"] = True
        try:
            return await real(*args, **more)
        finally:
            inside["now"] = False

    monkeypatch.setattr(raw_copies, "keep_whole_text", marked)
    return inside


class _Reads(list):
    """The readings of the stages (`reads`), and apart from them what `keep_whole_text` saw: (the session it read in
    was the writer's own, with the flushed batch in it)."""

    def __init__(self):
        super().__init__()
        self.in_writer: list[tuple[bool, bool]] = []


@pytest.fixture
def reads(monkeypatch):
    """What each `off_turn.reading` of the stages gave: (a reader's session, a second writer got through while it was
    open). The reading `keep_whole_text` makes inside a writer's transaction is kept in `.in_writer` instead."""
    seen = _Reads()
    inside = _mark_keep_whole_text(monkeypatch)
    real = off_turn.reading

    @contextlib.asynccontextmanager
    async def watched(session, **more):
        async with real(session, **more) as reader:
            if inside["now"]:
                seen.in_writer.append((reader is session, db.is_reader(reader)))
                yield reader
                return
            apart = db.is_reader(reader)
            seen.append((apart, await _second_writer_gets_through() if apart else False))
            yield reader

    monkeypatch.setattr(off_turn, "reading", watched)
    return seen


def _writing(transactions, table: str) -> int:
    """How many transactions that ended wrote to `table` (not counting the probe of `_second_writer_gets_through`)."""
    return sum(1 for each in transactions.ended if any(
        x.lstrip().upper().startswith(("INSERT INTO " + table.upper(), "UPDATE " + table.upper()))
        and "SET slug=?" not in x for x in each))


async def _in_a_transaction_that_writes(run):
    """`run(session)` in a session that already holds a write, so nothing is read apart or released."""
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).where(Opportunity.slug == "no-such").values(slug="no-such"))
        out = await run(session)
        await session.commit()
    return out


# --- take_whole ---------------------------------------------------------------------------------------------------


async def _snippet_roles(n: int) -> None:
    source, then = uuid.uuid4(), datetime.now(UTC) - timedelta(hours=2)
    async with db.session_factory()() as session:
        for i in range(n):
            role = Opportunity(owner=OWNER, slug=f"snip-{i}", company=f"Firm {i}", title=f"Lead {i}",
                               origin="scout", match_key=f"firm {i}|lead {i}", status=stages.FIRST_IMPRESSION,
                               description=f"Lead {i}", description_at=then)
            session.add(role)
            await session.flush()
            session.add(JobPosting(owner=OWNER, employer_id=source, ats=EXAMPLE_SYSTEM, ext_id=str(i),
                                   company=role.company, title=role.title, match_key=role.match_key,
                                   first_seen=then, last_seen=then, links=[], opportunity_id=role.id, text=WORDS,
                                   detail_at=then))
        await session.commit()


async def _descriptions() -> dict[str, str | None]:
    async with db.session_factory()() as session:
        return {r.slug: r.description for r in await session.scalars(
            select(Opportunity).execution_options(populate_existing=True))}


async def test_the_roles_that_wait_for_a_whole_text_are_found_without_the_writers_turn_and_given_it_a_batch_at_a_time(
        js, monkeypatch, reads, transactions):
    """The scan of every role's description held the file's one turn for 0.4 to 1.2 s a turn. It is read in a reader's
    session (a second writer gets through), and the roles are written `WHOLE_BATCH` to a transaction."""
    monkeypatch.setattr(posting_text, "WHOLE_BATCH", 2)
    await _snippet_roles(3)
    reads.clear()
    transactions.clear()
    async with db.session_factory()() as session:
        given = await posting_text.take_whole(session, OWNER)
        await session.commit()
    assert len(given) == 3 and reads == [(True, True)], "found apart, and a second writer got through"
    assert reads.in_writer == [(True, False)] * 2, "each batch's raw copies are found in the writer's own session"
    assert _writing(transactions, "js_opportunities") == 2, "two batches (2 and 1), each its own transaction"
    apart = await _descriptions()
    assert set(apart.values()) == {WORDS.strip()}


async def test_a_pass_inside_one_transaction_gives_take_whole_the_same_roles_and_texts(js, monkeypatch, reads):
    monkeypatch.setattr(posting_text, "WHOLE_BATCH", 2)
    await _snippet_roles(3)
    reads.clear()
    given = await _in_a_transaction_that_writes(lambda s: posting_text.take_whole(s, OWNER))
    assert len(given) == 3 and reads == [(False, False)], "a transaction with a write in it is read in itself"
    assert reads.in_writer == [(True, False)] * 2, "and so are the raw copies of each batch"
    assert set((await _descriptions()).values()) == {WORDS.strip()}


async def test_a_role_that_took_a_whole_text_since_it_was_found_waiting_is_left_as_it_is(js, monkeypatch):
    """The roles are found apart and read again in the transaction that writes them: one whose description became a
    whole text meanwhile (the owner's edit, a read) is not given another, and its date is not moved."""
    await _snippet_roles(3)
    real = off_turn.reading
    inside = _mark_keep_whole_text(monkeypatch)
    mine = "The owner's own words about the role, written out in full. " * 10

    @contextlib.asynccontextmanager
    async def then_changed(session, **more):
        async with real(session, **more) as reader:
            yield reader
        if inside["now"]:  # keep_whole_text's read, in the writer's transaction: a second writer would be nested
            return
        async with db.session_factory()() as other:
            await other.execute(update(Opportunity).where(Opportunity.slug == "snip-0").values(description=mine))
            await other.commit()

    monkeypatch.setattr(off_turn, "reading", then_changed)
    async with db.session_factory()() as session:
        assert len(await posting_text.take_whole(session, OWNER)) == 2
        await session.commit()
    assert await _descriptions() == {"snip-0": mine, "snip-1": WORDS.strip(), "snip-2": WORDS.strip()}


# --- make_roles ---------------------------------------------------------------------------------------------------










# --- make_roles ---------------------------------------------------------------------------------------------------


async def _waiting(n: int) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        for i in range(n):
            title = f"Programs Director {i:02d}"
            session.add(JobPosting(
                owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=f"p{i:02d}", company="Fabrikam",
                title=title, match_key=match_key("Fabrikam", title), location="Remote", work_mode="remote",
                url=f"https://jobs.example.test/{i}", first_seen=NOW + timedelta(minutes=i), last_seen=NOW,
                filtered_at=NOW, route="roles", links=[]))
        await session.commit()


async def test_the_postings_and_rules_that_decide_the_roles_are_read_without_the_writers_turn(
        js, monkeypatch, reads, transactions):
    """The waiting postings, the profile, the gates and the job boards' hosts were read in the transaction that made the
    first slice, a long hold of the writer's turn before the first role. They are read in a reader's session, and the
    roles are made in slices as before."""
    monkeypatch.setattr(role_from_posting, "SLICE", 2)
    await _waiting(5)
    reads.clear()
    transactions.clear()
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER) == 5
        await session.commit()
    # the stage's read, then the watch plan's read apart (`where.settle_watch`) once the slices are saved
    assert reads == [(True, True), (True, True)], "read apart, and a second writer got through"
    assert _writing(transactions, "js_opportunities") == 3, "slices of 2, 2 and 1, each its own transaction"
    async with db.session_factory()() as session:
        made = [r.title for r in await session.scalars(select(Opportunity).order_by(Opportunity.created_at))]
    assert made == [f"Programs Director {i:02d}" for i in (4, 3, 2, 1, 0)], "newest first, as before"


async def test_the_watch_list_is_settled_after_the_last_slice_is_saved_unless_the_owners_link_is_the_batch(
        js, monkeypatch):
    """The employers' watch list is a read of its own; it was settled in the transaction of the last slice."""
    from app.sites import where

    monkeypatch.setattr(role_from_posting, "SLICE", 2)
    open_when_settled: list[bool] = []

    async def settled(session, owner):
        open_when_settled.append(session.in_transaction())

    monkeypatch.setattr(where, "settle_watch", settled)
    await _waiting(5)
    async with db.session_factory()() as session:
        posting_id = (await session.scalars(select(JobPosting.id).order_by(*role_from_posting.newest_first()))).first()
        assert await role_from_posting.make_roles(session, OWNER, only=[posting_id]) == 1
        assert open_when_settled == [True], "a link the owner added is saved by its caller, not here"
        await session.commit()
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER, limit=1) == 1
        assert open_when_settled == [True, False], "one slice, no only: saved before the watch list"
        await session.commit()
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER) == 3
        await session.commit()
    assert open_when_settled == [True, False, False], "three postings in slices of two: saved before the watch list"


async def test_a_pass_inside_one_transaction_makes_the_same_roles(js, monkeypatch, reads):
    monkeypatch.setattr(role_from_posting, "SLICE", 2)
    await _waiting(5)
    reads.clear()
    assert await _in_a_transaction_that_writes(lambda s: role_from_posting.make_roles(s, OWNER)) == 5
    # the stage's read in the writer's own session, then the watch plan's read apart once the slices are saved
    assert reads == [(False, False), (True, True)]
    async with db.session_factory()() as session:
        assert len(list(await session.scalars(select(Opportunity)))) == 5


# --- stated_place -------------------------------------------------------------------------------------------------


async def _roles_without_a_place(*slugs: str, naming: tuple[str, ...] = ()) -> None:
    """Roles in First Impression with no place; the postings of those in `naming` name one."""
    source = uuid.uuid4()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 5})
        for slug in slugs:
            role = Opportunity(owner=OWNER, slug=slug, company=f"Firm {slug}", title=f"Lead {slug}", origin="scout",
                               match_key=f"firm {slug}|lead {slug}", status=stages.FIRST_IMPRESSION, location=None)
            session.add(role)
            await session.flush()
            session.add(JobPosting(owner=OWNER, employer_id=source, ats=EXAMPLE_SYSTEM, ext_id=slug,
                                   company=role.company, title=role.title, match_key=role.match_key,
                                   first_seen=NOW, last_seen=NOW, links=[], opportunity_id=role.id,
                                   location="Anytown, ST" if slug in naming else None))
        await session.commit()


async def _places() -> dict[str, str | None]:
    async with db.session_factory()() as session:
        return {r.slug: r.location for r in await session.scalars(
            select(Opportunity).execution_options(populate_existing=True))}


async def test_the_question_the_deck_asks_before_writing_places_is_the_one_settle_places_answers(js):
    """`deck._naming_places` had a copy of the query in `settle_places`; both ask `stated_place.naming_places`."""
    await _roles_without_a_place("a", "b", "c", naming=("a", "c"))
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity).order_by(Opportunity.slug))).all())
        ids = {r.slug: r.id for r in roles}
        named = await stated_place.naming_places(session, OWNER, roles)
        assert named == {ids["a"]: "Anytown, ST", ids["c"]: "Anytown, ST"}
        assert await deck._naming_places(session, OWNER, roles) == list(named), "the same roles, in the same order"
        assert [r.slug for r in await stated_place.settle_places(session, OWNER, roles)] == ["a", "c"]
        assert await stated_place.naming_places(session, OWNER, roles) == {}, "once written, nothing is left to write"


async def test_the_places_are_written_a_batch_of_roles_to_a_transaction_and_equal_those_of_one_transaction(
        js, monkeypatch, transactions):
    """`settle_places` writes a batch (`PLACES_BATCH`) to a transaction itself, whoever calls it, and a batch after the
    first is read again as it is then. A caller inside a transaction that cannot be released has them all in it."""
    monkeypatch.setattr(stated_place, "PLACES_BATCH", 2)
    await _roles_without_a_place("a", "b", "c", naming=("a", "b", "c"))
    transactions.clear()
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity))).all())
        assert len(await stated_place.settle_places(session, OWNER, roles)) == 3
        await session.commit()
    assert _writing(transactions, "js_opportunities") == 2, "two batches (2 and 1), each its own transaction"
    apart = await _places()
    assert set(apart.values()) == {"Anytown, ST"}

    async with db.session_factory()() as session:  # put back, and written inside a savepoint: one transaction
        await session.execute(update(Opportunity).values(location=None))
        await session.commit()
    transactions.clear()
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity))).all())
        async with session.begin_nested():
            assert len(await stated_place.settle_places(session, OWNER, roles)) == 3
        await session.commit()
    assert _writing(transactions, "js_opportunities") == 1
    assert await _places() == apart


async def test_a_role_that_took_a_place_meanwhile_is_not_given_one_by_a_later_batch(js, monkeypatch):
    """A batch after the first is read again: a role the owner gave a place between two batches keeps it."""
    monkeypatch.setattr(stated_place, "PLACES_BATCH", 1)
    await _roles_without_a_place("a", "b", naming=("a", "b"))
    real = db.release_if_free
    done = []

    async def interfering(session):
        saved = await real(session)
        if saved and not done:
            done.append(True)
            async with db.session_factory()() as other:
                await other.execute(update(Opportunity).values(location="Their Town, ST").where(
                    Opportunity.location.is_(None)))
                await other.commit()
        return saved

    monkeypatch.setattr(db, "release_if_free", interfering)
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity).order_by(Opportunity.slug))).all())
        assert [r.slug for r in await stated_place.settle_places(session, OWNER, roles)] == ["a"]
        await session.commit()
    assert sorted((await _places()).values()) == ["Anytown, ST", "Their Town, ST"]


async def test_a_role_whose_postings_no_longer_name_a_place_is_not_given_the_one_asked_before(js, monkeypatch):
    """A batch after the first asks again which place the postings name and takes that answer for its roles: a role
    whose postings were moved off it between two batches is left as it is, not given the place first asked."""
    monkeypatch.setattr(stated_place, "PLACES_BATCH", 1)
    await _roles_without_a_place("a", "b", naming=("a", "b"))
    real = db.release_if_free
    done = []

    async def interfering(session):
        saved = await real(session)
        if saved and not done:
            done.append(True)
            async with db.session_factory()() as other:
                await other.execute(update(JobPosting).values(location=None).where(JobPosting.ext_id == "b"))
                await other.commit()
        return saved

    monkeypatch.setattr(db, "release_if_free", interfering)
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity).order_by(Opportunity.slug))).all())
        assert [r.slug for r in await stated_place.settle_places(session, OWNER, roles)] == ["a"]
        await session.commit()
    assert await _places() == {"a": "Anytown, ST", "b": None}


async def test_the_posted_days_and_the_standing_of_a_batch_of_roles_are_read_once_not_for_each_role(js, monkeypatch):
    """Each role's gates read its own posted day (every job board's host again) and where it stands (a few reads), about
    30 ms a role. `settle.roles` reads both for the batch and hands each role its own."""
    from app.jobsearch.fit import freshness
    from app.jobsearch.pipeline import placement

    asked = {"posted_of": 0, "one": 0}
    posted_of, one = freshness.posted_of, placement.one

    async def counting_posted_of(*args, **kwargs):
        asked["posted_of"] += 1
        return await posted_of(*args, **kwargs)

    async def counting_one(*args, **kwargs):
        asked["one"] += 1
        return await one(*args, **kwargs)

    monkeypatch.setattr(freshness, "posted_of", counting_posted_of)
    monkeypatch.setattr(placement, "one", counting_one)
    await _roles_without_a_place("a", "b", "c", naming=("a", "b", "c"))
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity))).all())
        assert len(await stated_place.settle_places(session, OWNER, roles)) == 3
        await session.commit()
    assert asked == {"posted_of": 0, "one": 0}
