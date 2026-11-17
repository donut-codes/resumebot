"""A role listed in several places is judged on one of its postings, chosen one way (the owner, 2026-11-04): the
employer's main career site is the source of truth, and when that does not work, whichever posting is newest.

The work location, way of working and pay gates
read the cleansed copy of the role's posting with the most text, ties broken by the id of the copy, which is drawn again
at every nightly re-read. A role with a posting on the employer's job system and others on job boards (in other
countries, or at other pay) was stopped one night and released the next. Now the posting is the employer's own (the
role's Sourced one, or one read from the employer's own job system or site; the newest of them), else the newest
posting, and every reader of those gates reads that one. Everything here is made up."""

import uuid
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import ats, stages
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts, posting_facts, regate, stated_place
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting
from app.jobsearch.pipeline import deck
from app.jobsearch.postings import cleansed, gate_copy, raw_copies
from app.jobsearch.postings.gate_copy import Choice
from app.jobsearch.postings.record import CODE, LISTING
from app.jobsearch.research import settle
from tests.jobsearch.example_world import EXAMPLE_SYSTEM, OTHER_SYSTEM, THIRD_SYSTEM
from tests.jobsearch.test_gate_paths_agree import GATES, make_role
from tests.support import FEW_ARGUMENTS, FIVE, OWNER, POSTING_WORDS, bound_arguments, load_profile

NOW = datetime.now(UTC)
SOURCE = "https://jobs.example.test/Fabrikam"


def days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


def choice(n: int, *, system: str | None = ats.JOB_SITE, seen: int = 5, posted: int | None = None, size: int = 500,
           links: tuple[str, ...] = (), copy: int | None = None) -> Choice:
    """One posting with a copy; `n` makes its ids, which a tie is broken by (the copy's id is `1000 + n`, or `copy`)."""
    return Choice(posting_id=uuid.UUID(int=n), cleansed_id=uuid.UUID(int=1000 + n if copy is None else copy),
                  system=system, links=links,
                  posted=(NOW - timedelta(days=posted)).date() if posted is not None else None,
                  first_seen=days_ago(seen), size=size)


def picked(*choices: Choice, source: str | None = None) -> int | None:
    found = gate_copy.pick(list(choices), source)
    return found.posting_id.int if found is not None else None


def read_picked(*choices: Choice, source: str | None = None) -> int | None:
    """The posting the gates read when a posting not read yet (a copy under `cleansed.MIN_TEXT`) is no choice while
    another has been read: how `cleansed` asks for the choice."""
    found = gate_copy.pick(list(choices), source, cleansed.MIN_TEXT)
    return found.posting_id.int if found is not None else None


# --- the rule ------------------------------------------------------------------------------------------------------


def test_the_employers_own_job_system_beats_a_newer_board_posting_with_more_text():
    own = choice(1, system=EXAMPLE_SYSTEM, seen=30, size=300)
    assert picked(own, choice(2, system=ats.JOB_SITE, seen=1, size=9000)) == 1


def test_a_posting_read_from_the_employers_own_site_is_the_employers_too():
    assert picked(choice(1, system=ats.BROWSER, seen=1), choice(2, system=ats.OWN_SITE, seen=40)) == 2


def test_of_several_employer_postings_the_newest_is_chosen():
    assert picked(choice(1, system=EXAMPLE_SYSTEM, seen=60), choice(2, system=OTHER_SYSTEM, seen=9),
                  choice(3, system=THIRD_SYSTEM, seen=30)) == 2


def test_the_newest_is_counted_as_a_roles_posted_day_is_over_each_posting():
    """The employer's date, held to the day the app first saw the posting: a board that dates it again on every read
    does not make it new, and a posting the employer dated is as old as its date."""
    redated = choice(1, system=EXAMPLE_SYSTEM, seen=40, posted=2)  # dated two days ago, first seen 40 days ago: 40 days
    older = choice(2, system=OTHER_SYSTEM, seen=40, posted=50)  # dated 50 days ago: 50 days
    assert picked(redated, older) == 1
    assert picked(older, redated) == 1, "the order they are given in decides nothing"


def test_with_no_employer_posting_the_newest_posting_is_chosen_whatever_its_size():
    assert picked(choice(1, seen=20, size=9000), choice(2, seen=3, size=300), choice(3, seen=10, size=500)) == 2


def test_the_roles_sourced_employer_posting_is_the_employers_on_whatever_system_it_was_read():
    sourced = choice(1, system=ats.BROWSER, seen=50, links=(SOURCE,))
    assert picked(sourced, choice(2, system=ats.JOB_SITE, seen=1), source=SOURCE) == 1
    assert picked(sourced, choice(2, system=ats.JOB_SITE, seen=1), source=None) == 2, "not Sourced to the employer"


def test_the_sourced_posting_is_found_under_another_writing_of_its_address():
    other = choice(1, system=ats.BROWSER, seen=50,
                   links=("https://www.jobs.example.test/job/482137-programs-lead?utm=x",))
    board = choice(2, system=ats.JOB_SITE, seen=1)
    assert picked(other, board, source="https://jobs.example.test/job/482137") == 1
    assert picked(other, board, source="https://jobs.example.test/job/999999") == 2


def test_a_posting_held_by_its_apply_address_or_a_link_it_appeared_under_is_found_by_it():
    assert gate_copy.at_source(("https://a.example.test/x", SOURCE.upper() + "/"), SOURCE)
    assert not gate_copy.at_source((), SOURCE) and not gate_copy.at_source((SOURCE,), None)
    assert not gate_copy.at_source((SOURCE,), "  ")


@pytest.mark.parametrize("order", [(1, 2, 3), (3, 2, 1), (2, 3, 1)])
def test_a_tie_is_broken_by_the_posting_dated_by_the_employer_then_the_most_text_then_the_lowest_posting_id(order):
    """Nothing in the choice is drawn again between nights: the copy's id is (it is made again by every cleaning), the
    posting's is not."""
    same_day = {
        1: choice(1, system=EXAMPLE_SYSTEM, seen=9, size=500),
        2: choice(2, system=EXAMPLE_SYSTEM, seen=9, posted=9, size=500),  # the employer dated it: counted from its date
        3: choice(3, system=EXAMPLE_SYSTEM, seen=9, posted=9, size=700)}  # the same day, more text
    assert picked(*[same_day[n] for n in order]) == 3
    twins = [choice(7, system=EXAMPLE_SYSTEM, seen=9, size=500), choice(5, system=EXAMPLE_SYSTEM, seen=9, size=500)]
    assert picked(*twins) == 5 == picked(*reversed(twins))


@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
def test_on_the_same_day_the_posting_the_employer_dated_beats_one_counted_from_the_day_it_was_seen(order):
    """The one counted from the day it was seen has the most text and the lowest posting id, so only the employer's date
    can make the other the choice."""
    seen_only = choice(1, system=EXAMPLE_SYSTEM, seen=9, size=900)
    dated = choice(2, system=EXAMPLE_SYSTEM, seen=9, posted=9, size=300)
    assert picked(*[{1: seen_only, 2: dated}[n] for n in order]) == 2


@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
def test_of_two_postings_as_new_and_as_full_the_one_with_the_lowest_posting_id_wins_whatever_its_copys_id(order):
    """The copy's id is drawn again by every cleaning: the posting with the lower id has the higher copy id here, and
    the choice is its copy."""
    low = choice(5, system=EXAMPLE_SYSTEM, seen=9, size=500, copy=900)
    high = choice(7, system=EXAMPLE_SYSTEM, seen=9, size=500, copy=100)
    found = gate_copy.pick([{1: low, 2: high}[n] for n in order])
    assert found is not None and found.posting_id.int == 5 and found.cleansed_id.int == 900


def test_no_posting_with_a_copy_is_no_choice():
    assert gate_copy.pick([]) is None


@pytest.mark.parametrize("order", [(1, 2, 3), (3, 2, 1), (2, 3, 1)])
def test_a_posting_not_read_yet_does_not_decide_while_another_posting_of_the_role_has_been_read(order):
    """The newest posting is often one whose text has not been fetched: a title and a few header lines. It states no
    place and no pay, so taking it would release a role the posting before it stops, to stop it again the day its text
    is read (the owner, 2026-11-04, docs/PRD.md 54.4)."""
    unread = choice(1, system=EXAMPLE_SYSTEM, seen=1, size=20)  # the employer's own, and the newest
    board = choice(2, system=ats.JOB_SITE, seen=20, size=900)
    older_employer = choice(3, system=OTHER_SYSTEM, seen=40, size=600)
    held = {1: unread, 2: board, 3: older_employer}
    assert read_picked(*[held[n] for n in order]) == 3, "the employer's own posting that has been read"
    assert read_picked(unread, board) == 2 == read_picked(board, unread), "the newest that has been read"
    assert picked(unread, board) == 1, "with no minimum asked for, the posting is a choice like any other"


@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
def test_a_posting_not_read_yet_decides_when_no_other_posting_of_the_role_has_been_read(order):
    """Every posting of the role is a choice again, by the rule: the employer's own, else the newest."""
    newer_board = choice(1, system=ats.JOB_SITE, seen=1, size=10)
    older_employer = choice(2, system=EXAMPLE_SYSTEM, seen=30, size=20)
    assert read_picked(*[{1: newer_board, 2: older_employer}[n] for n in order]) == 2, "the employer's own"
    assert read_picked(*[{1: choice(3, seen=9, size=299), 2: choice(4, seen=3, size=0)}[n] for n in order]) == 4
    assert read_picked(choice(5, size=20)) == 5, "a role's one posting is read whatever its text"


def test_a_copy_of_the_least_text_has_been_read_and_one_character_less_has_not():
    edge = cleansed.MIN_TEXT
    assert read_picked(choice(1, seen=20, size=edge), choice(2, seen=1, size=edge - 1)) == 1
    assert read_picked(choice(1, seen=20, size=edge - 1), choice(2, seen=1, size=edge - 1)) == 2


def test_the_roles_sourced_posting_is_left_out_too_while_it_has_not_been_read():
    sourced = choice(1, system=ats.BROWSER, seen=50, size=20, links=(SOURCE,))
    board = choice(2, system=ats.JOB_SITE, seen=1, size=900)
    assert read_picked(sourced, board, source=SOURCE) == 2
    assert read_picked(sourced, choice(3, system=ats.JOB_SITE, seen=1, size=20), source=SOURCE) == 1


# --- the postings of a role, read from the database -----------------------------------------------------------------


async def list_posting(session, role: Opportunity, name: str, *, system: str, seen: int, size: int = 500,
                       places: tuple[str, ...] = (), mode: str | None = None, pay: tuple[int, int] | None = None,
                       text: str | None = None, url: str | None = None, posted: int | None = None,
                       copy: bool = True, owner: str = OWNER, posting_id: uuid.UUID | None = None,
                       copy_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """One posting of the role on `system`, first seen `seen` days ago, with its current copy of `size` characters (or
    `text`) stating `places`, a way of working and a pay band; the copy's id, or None when it has no copy."""
    source = uuid.uuid4()
    posting = JobPosting(
        owner=owner, employer_id=source, ats=system, ext_id=f"{role.slug}-{name}", company=role.company,
        title=role.title, match_key=role.match_key, first_seen=days_ago(seen), last_seen=days_ago(seen), links=[],
        location="x", url=url or f"https://{name}.example.test/{role.slug}", opportunity_id=role.id,
        posted=(NOW - timedelta(days=posted)).date() if posted is not None else None)
    if posting_id:
        posting.id = posting_id
    session.add(posting)
    if not copy:
        await session.flush()
        return None
    raw = RawPosting(owner=owner, source_id=source, reader="legacy", kind="legacy", ext_id=posting.ext_id,
                     fetched_at=days_ago(seen), body=b"x", body_sha256=posting.ext_id.ljust(64, "0"), body_bytes=1,
                     no_raw=True)
    session.add(raw)
    await session.flush()
    made = CleansedPosting(owner=owner, raw_id=raw.id, cleaner_version=1, locations=list(places), work_mode=mode,
                           pay_min=pay[0] if pay else None, pay_max=pay[1] if pay else None,
                           text_clean=text if text is not None else "x" * size)
    if copy_id:
        made.id = copy_id
    session.add(made)
    await session.flush()
    await raw_copies.repoint(session, OWNER)  # the pointer the code that writes copies keeps (`raw_copies._point`)
    return made.id


async def bare_role(session, name: str, **fields) -> Opportunity:
    role = Opportunity(**{
        "owner": OWNER, "slug": name, "company": name, "title": "Programs Lead", "origin": "scout",
        "match_key": f"{name}|programs lead", "status": stages.FIRST_IMPRESSION, "found": date.today(),
        "url": f"https://jobs.example.test/{name}", **fields})
    session.add(role)
    await session.flush()
    return role


async def test_the_copy_the_gates_read_follows_the_rule_for_every_role_and_the_one_role_read_agrees(js):
    async with db.session_factory()() as session:
        employers_own = await bare_role(session, "own")
        employer_copy = await list_posting(session, employers_own, "a", system=EXAMPLE_SYSTEM, seen=30, size=300)
        await list_posting(session, employers_own, "b", system=ats.JOB_SITE, seen=1, size=9000)
        neither = await bare_role(session, "neither")
        await list_posting(session, neither, "a", system=ats.JOB_SITE, seen=20, size=9000)
        newest_copy = await list_posting(session, neither, "b", system=ats.BROWSER, seen=2, size=400)
        sourced = await bare_role(session, "sourced", source_url="https://sourced.example.test/job/771234",
                                  source_kind="employer")
        sourced_copy = await list_posting(session, sourced, "a", system=ats.BROWSER, seen=40,
                                          url="https://www.sourced.example.test/job/771234-programs-lead")
        await list_posting(session, sourced, "b", system=ats.JOB_SITE, seen=1)
        lone = await bare_role(session, "lone")
        lone_copy = await list_posting(session, lone, "a", system=ats.JOB_SITE, seen=3)
        unread = await bare_role(session, "unread")  # the employer's posting has no copy: it is no choice
        await list_posting(session, unread, "a", system=EXAMPLE_SYSTEM, seen=30, copy=False)
        unread_copy = await list_posting(session, unread, "b", system=ats.JOB_SITE, seen=9)
        thin = await bare_role(session, "thin")  # the newest posting has a few header lines: not read yet, no choice
        thin_copy = await list_posting(session, thin, "a", system=ats.JOB_SITE, seen=20, size=900)
        await list_posting(session, thin, "b", system=EXAMPLE_SYSTEM, seen=1, size=40)
        unfetched = await bare_role(session, "unfetched")  # no posting has been read: the rule is over all of them
        await list_posting(session, unfetched, "a", system=ats.JOB_SITE, seen=9, size=20)
        unfetched_copy = await list_posting(session, unfetched, "b", system=EXAMPLE_SYSTEM, seen=30, size=10)
        stub = await bare_role(session, "stub")  # one posting, not read yet: the role's only posting is its choice
        stub_copy = await list_posting(session, stub, "a", system=EXAMPLE_SYSTEM, seen=2, size=15)
        await list_posting(session, await bare_role(session, "theirs"), "a", system=EXAMPLE_SYSTEM, seen=3,
                           owner="another")
        await session.commit()

    want = {employers_own.id: employer_copy, neither.id: newest_copy, sourced.id: sourced_copy, lone.id: lone_copy,
            unread.id: unread_copy, thin.id: thin_copy, unfetched.id: unfetched_copy, stub.id: stub_copy}
    async with db.session_factory()() as session:
        assert await cleansed.gate_current_for_roles(session, OWNER) == want
        held = await cleansed.gate_copies_by_role(session, OWNER)
        assert {role_id: c.id for role_id, c in held.items()} == want
        some = list(want)[:3]
        part = await cleansed.gate_copies_of(session, OWNER, some)
        assert part == {role_id: held[role_id] for role_id in some}, "a few roles are read as all of them are"
        for role_id, copy_id in want.items():
            one = await cleansed.gate_copy_of(session, OWNER, role_id)
            assert one is not None and one.id == copy_id, "reading one role chooses as reading them all does"
        assert await cleansed.gate_copy_of(session, OWNER, uuid.uuid4()) is None


async def test_the_copies_of_more_roles_than_a_few_are_read_with_a_few_arguments_whatever_their_number(js):
    """The re-gate reads the copy of every role that has one and asyncpg refuses
    a statement of more than 32,767 arguments, which an `IN` list of the copies' ids carries one of for each. More roles
    than a few have a copy here, and the statement that loads the copies (`gate_copies_by_role`) carries the ids as one
    argument, with the same copy found for each role."""
    async with db.session_factory()() as session:
        want = {}
        for number in range(FEW_ARGUMENTS + 1):
            role = await bare_role(session, f"r{number}")
            copy_id = await list_posting(session, role, "a", system=EXAMPLE_SYSTEM, seen=3, places=("Oakfield, OH",))
            assert copy_id is not None
            want[role.id] = copy_id
        await session.commit()
    async with db.session_factory()() as session:
        with bound_arguments() as bound:
            held = await cleansed.gate_copies_by_role(session, OWNER)
    assert {role_id: copy.id for role_id, copy in held.items()} == want
    assert bound and max(bound) <= FEW_ARGUMENTS


async def test_the_copy_chosen_for_each_of_a_list_of_roles_is_read_with_a_few_arguments_whatever_their_number(js):
    """The gate at Cleansed Data asks which copy to read for the roles there (`gate_copy_ids_for`), as many as the stage
    holds: an `IN` list of their ids carried one argument for each, and asyncpg refuses more than 32,767. The ids go
    as one argument, and the same copy is found for each role."""
    async with db.session_factory()() as session:
        want = {}
        for number in range(FEW_ARGUMENTS + 1):
            role = await bare_role(session, f"r{number}")
            copy_id = await list_posting(session, role, "a", system=EXAMPLE_SYSTEM, seen=3, places=("Oakfield, OH",))
            assert copy_id is not None
            want[role.id] = copy_id
        await session.commit()
    async with db.session_factory()() as session:
        with bound_arguments() as bound:
            chosen = await cleansed.gate_copy_ids_for(session, OWNER, list(want))
    assert chosen == want
    assert bound and max(bound) <= FEW_ARGUMENTS


class Touched:
    """A copy that notes the name of each attribute read from it (`role_subject` reads it as an object with fields)."""

    def __init__(self, row) -> None:
        self._row = row
        self.names: set[str] = set()

    def __getattr__(self, name: str):
        self.names.add(name)
        return getattr(self._row, name)


async def test_the_re_gate_reads_a_copy_with_only_the_columns_the_gates_read_and_they_judge_it_as_they_do_the_row(js):
    """`gate_copies_by_role` loads nine columns as plain values where `gate_copy_of` loads the row. The subject
    the gates read of a role is the same either way, for each shape of copy the subject tells apart: a copy that states
    its places, a pay band and a way of working, one that is partial, one whose way is only a listing's tag over
    words the role's own read gave, and one that says one in its words (`silent_copy` reads the text)."""
    settled = {"state": gate_facts.SETTLED, "way": True}
    words = {"work_mode": {"value": "hybrid", "by": CODE, "quote": "hybrid two days"}}
    calm = "Lead the programs team and keep the books in order. " * 8
    shapes = {
        "states-all": dict(places=("Oakfield, OH", "3 Locations"), mode="remote", pay=(80_000, 120_000), text=calm),
        "partial": dict(places=("Oakfield, OH",), mode=None, pay=None, text=calm, partial=True),
        "tag-over-words": dict(places=("Oakfield, OH",), mode="remote", pay=(0, 90_000), text=calm, tag=True),
        "says-remote": dict(places=(), mode=None, pay=None, text=calm + "This is a fully remote position."),
    }
    async with db.session_factory()() as session:
        for name, shape in shapes.items():
            role = await bare_role(session, name, gate_facts={**settled, "facts": words}, move_type="hybrid")
            copy_id = await list_posting(session, role, "a", system=EXAMPLE_SYSTEM, seen=3, places=shape["places"],
                                         mode=shape["mode"], pay=shape["pay"], text=shape["text"])
            copy = await session.get(CleansedPosting, copy_id)
            assert copy is not None
            copy.partial = shape.get("partial", False)
            copy.marks = {"work_mode": {"source": LISTING if shape.get("tag") else CODE}} if shape["mode"] else {}
        await session.commit()
    touched: set[str] = set()
    async with db.session_factory()() as session:
        light = await cleansed.gate_copies_by_role(session, OWNER)
        roles = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all()
        assert {r.slug for r in roles} == set(shapes) and set(light) == {r.id for r in roles}
        for role in roles:
            whole = await cleansed.gate_copy_of(session, OWNER, role.id)
            assert whole is not None
            spy = Touched(whole)
            gating.role_subject(role, None, spy)
            touched |= spy.names
            held = light[role.id]
            assert {f.name: getattr(held, f.name) for f in fields(cleansed.GateCopy)} == {
                f.name: getattr(whole, f.name) for f in fields(cleansed.GateCopy)}, "the columns hold the row's values"
            assert gating.role_subject(role, None, held) == gating.role_subject(role, None, whole), role.slug
        # The shapes differ in what the gates read, so the equality above is a test of each: no way assumed from a
        # copy that speaks of one or is partial; the tag does not lift the way the role's own words state.
        by_name = {r.slug: gating.role_subject(r, None, light[r.id]) for r in roles}
        assert [by_name[n].way_read for n in shapes] == [True, False, True, False]
        assert by_name["tag-over-words"].work_mode == "hybrid" and by_name["states-all"].work_mode == "remote"
        assert by_name["states-all"].location == "Oakfield, OH", "a count such as 3 Locations is no place stated"
    assert touched and touched <= {f.name for f in fields(cleansed.GateCopy)}, (
        "the gates read a field of the copy that the re-gate's copy does not carry: " + str(touched))
    row_columns = {column.key for column in CleansedPosting.__table__.columns}
    assert {f.name for f in fields(cleansed.GateCopy)} <= row_columns - {"removed", "from_raw", "title"}


async def test_postings_that_tie_are_told_apart_by_the_postings_id_not_the_id_of_the_copy(js):
    """The same system, day and size: the posting with the lower id is chosen, though its copy has the higher id (a
    copy is made again by every cleaning, so its id says nothing)."""
    low, high = uuid.UUID(int=5), uuid.UUID(int=7)
    low_copy, high_copy = uuid.UUID(int=900), uuid.UUID(int=100)
    async with db.session_factory()() as session:
        role = await bare_role(session, "Fabrikam")
        await list_posting(session, role, "a", system=EXAMPLE_SYSTEM, seen=9, posting_id=high, copy_id=high_copy)
        await list_posting(session, role, "b", system=EXAMPLE_SYSTEM, seen=9, posting_id=low, copy_id=low_copy)
        await session.commit()
    async with db.session_factory()() as session:
        assert await cleansed.gate_current_for_roles(session, OWNER) == {role.id: low_copy}
        assert (await cleansed.gate_copy_of(session, OWNER, role.id)).id == low_copy


async def test_a_role_with_one_posting_has_the_copy_every_other_reader_reads(js):
    async with db.session_factory()() as session:
        for name, system in (("a", EXAMPLE_SYSTEM), ("b", ats.JOB_SITE), ("c", ats.SCOUT)):
            await list_posting(session, await bare_role(session, name), "only", system=system, seen=4)
        await session.commit()
    async with db.session_factory()() as session:
        assert await cleansed.gate_current_for_roles(session, OWNER) == await cleansed.current_for_roles(session, OWNER)


async def test_the_text_a_reader_reads_is_still_the_posting_with_the_most_text(js):
    """The lane changes the place and pay gates only: the ranker and the favorites' check read the fullest copy."""
    async with db.session_factory()() as session:
        role = await bare_role(session, "Fabrikam")
        await list_posting(session, role, "own", system=EXAMPLE_SYSTEM, seen=30, size=300)
        fullest = await list_posting(session, role, "board", system=ats.JOB_SITE, seen=1, size=9000)
        await session.commit()
        assert (await cleansed.copy_of(session, OWNER, role.id)).id == fullest
        assert (await cleansed.current_for_roles(session, OWNER))[role.id] == fullest


async def test_the_choice_does_not_flip_when_a_posting_is_read_again(js):
    """Cleaning a posting again draws its copy a new id, and a read may make it the longest: neither changes the posting
    the gates read, which was the cause of the flips."""
    async with db.session_factory()() as session:
        role = await bare_role(session, "Fabrikam")
        await list_posting(session, role, "own", system=EXAMPLE_SYSTEM, seen=30, size=300)
        board = await list_posting(session, role, "board", system=ats.JOB_SITE, seen=1, size=900)
        first = (await cleansed.gate_copy_of(session, OWNER, role.id)).id
        grown = await session.get(CleansedPosting, board)
        grown.text_clean = "y" * 5000
        await session.flush()
        again = (await cleansed.gate_copy_of(session, OWNER, role.id)).id
        assert first == again != board


# --- every reader of the place and pay gates reads that posting ------------------------------------------------------

#: The owner works remote, or hybrid and in person in Oakfield only.
RIVERTON, OAKFIELD = "US, OH, Riverton", "Oakfield"


async def listed_in_two_places(session, name: str, *, employer_in_area: bool, **fields) -> Opportunity:
    """A role whose employer's own posting (older, with the least text) puts the work in one place and a job board's
    (newer, with the most text, the copy the gates read before) in the other. Its own columns say remote."""
    role = await make_role(session, name, POSTING_WORDS, **fields)
    own, board = (OAKFIELD, RIVERTON) if employer_in_area else (RIVERTON, OAKFIELD)
    await list_posting(session, role, "own", system=EXAMPLE_SYSTEM, seen=30, size=300, places=(own,), mode="hybrid")
    await list_posting(session, role, "board", system=ats.JOB_SITE, seen=1, size=9000, places=(board,), mode="hybrid")
    return role


async def test_the_re_gate_and_the_settle_judge_the_place_of_the_employers_posting(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        out = await listed_in_two_places(session, "Fabrikam", employer_in_area=False)
        kept = await listed_in_two_places(session, "Initech", employer_in_area=True)
        settled = await listed_in_two_places(session, "Hooli", employer_in_area=False)

        await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
        await settle.roles(session, [settled], cause="The analyst researched it")

        for role in (out, kept, settled):
            await session.refresh(role)
        assert out.status == stages.GATED_OUT and out.screened_out_gate_id == "location", "the employer's: Riverton"
        assert settled.status == stages.GATED_OUT and settled.screened_out_gate_id == "location"
        assert kept.status == stages.FIRST_IMPRESSION, "the employer's is Oakfield, whatever the board says"


async def listed_twice_the_newer_posting_not_read(session, name: str, *, older_size: int) -> Opportunity:
    """A role whose employer's older posting puts the work in Riverton, outside the owner's area, and whose employer's
    newer posting states no place (a title and a few header lines, `older_size` characters on the older one)."""
    role = await make_role(session, name, POSTING_WORDS)
    await list_posting(session, role, "old", system=EXAMPLE_SYSTEM, seen=30, size=older_size, places=(RIVERTON,),
                       mode="hybrid")
    await list_posting(session, role, "new", system=EXAMPLE_SYSTEM, seen=1, size=40)
    return role


async def test_a_role_the_older_posting_stops_is_not_released_by_a_newer_posting_not_read_yet(js):
    """The newest posting of a role is often one whose text is not fetched
    yet; it states no place, so the gate released the role for a day, to stop it again when the text was read."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        held = await listed_twice_the_newer_posting_not_read(session, "Fabrikam", older_size=cleansed.MIN_TEXT)
        await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
        await session.refresh(held)
        assert held.status == stages.GATED_OUT and held.screened_out_gate_id == "location", "the read posting: Riverton"


async def test_a_posting_not_read_yet_decides_the_gates_when_no_other_posting_of_the_role_has_been_read(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        unread = await listed_twice_the_newer_posting_not_read(session, "Initech", older_size=cleansed.MIN_TEXT - 1)
        lone = await make_role(session, "Hooli", POSTING_WORDS)
        await list_posting(session, lone, "only", system=EXAMPLE_SYSTEM, seen=3, size=40, places=(RIVERTON,),
                           mode="hybrid")
        await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
        await session.refresh(unread)
        await session.refresh(lone)
        assert unread.status == stages.FIRST_IMPRESSION, "neither has been read: the newest states no place"
        assert lone.status == stages.GATED_OUT and lone.screened_out_gate_id == "location", "its only posting decides"


async def test_a_role_gated_on_the_employers_place_is_not_released_when_a_board_posting_grows_or_is_cleaned_again(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await listed_in_two_places(session, "Fabrikam", employer_in_area=False)
        await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
        await session.refresh(role)
        assert role.status == stages.GATED_OUT
        was = role.screened_out_reason

        grown = await session.scalar(select(CleansedPosting).where(func.length(CleansedPosting.text_clean) == 9000))
        grown.text_clean = "z" * 12_000  # the night's re-read: the board's copy is longer still
        await session.flush()
        report = await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
        await session.refresh(role)

        assert role.status == stages.GATED_OUT and role.screened_out_reason == was
        assert report.roles_returned == 0


async def test_the_check_before_a_session_takes_the_role_judges_the_place_of_the_employers_posting(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        out = await listed_in_two_places(session, "Fabrikam", employer_in_area=False)
        kept = await listed_in_two_places(session, "Initech", employer_in_area=True)

        assert await gate_facts.stop_if_failed(session, out, "before a session takes it") is True
        assert await gate_facts.stop_if_failed(session, kept, "before a session takes it") is False
        assert out.screened_out_gate_id == "location"


async def test_the_cleansed_data_gate_judges_the_place_of_the_employers_posting(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        out = await listed_in_two_places(session, "Fabrikam", employer_in_area=False, status=stages.CLEANSED_DATA)
        kept = await listed_in_two_places(session, "Initech", employer_in_area=True, status=stages.CLEANSED_DATA)

        assert await deck.gate(session, OWNER) == 1

        assert out.status == stages.GATED_OUT and out.screened_out_gate_id == "location"
        assert kept.status == stages.CLEANSED_DATA


async def test_the_known_town_check_judges_the_place_of_the_employers_posting(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        out = await listed_in_two_places(session, "Fabrikam", employer_in_area=False, status=stages.READY_TO_LAUNCH,
                                         location="Oakfield", move_type="hybrid")
        kept = await listed_in_two_places(session, "Initech", employer_in_area=True, status=stages.READY_TO_LAUNCH,
                                          location="US, OH, Riverton", move_type="hybrid")

        assert await stated_place.decide(session, OWNER, out, cause="The role's place changed") is True
        assert await stated_place.decide(session, OWNER, kept, cause="The role's place changed") is False
        assert out.status == stages.GATED_OUT and kept.status == stages.READY_TO_LAUNCH


async def test_the_recheck_before_the_writer_and_the_gate_facts_step_ask_about_the_place_the_employers_posting_states(
        js, monkeypatch):
    """The role holds only a country and a way of working. The employer's posting says where; the board's newer, longer
    one says nothing. Decided on the employer's, so the analyst is not sent back for a place and Haiku is not asked."""
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        done = {"status": stages.READY_TO_LAUNCH, "analysis_state": "done", "location": "United States",
                "move_type": "onsite", "analysis": {"required": [{"item": "Five years", "class": "P"}]}}
        stated = await make_role(session, "Fabrikam", POSTING_WORDS, **done)
        await list_posting(session, stated, "own", system=EXAMPLE_SYSTEM, seen=30, size=300,
                           places=(OAKFIELD,), mode="onsite")
        await list_posting(session, stated, "board", system=ats.JOB_SITE, seen=1, size=9000)
        found = await make_role(session, "Initech", POSTING_WORDS, location="United States", move_type="onsite")
        await list_posting(session, found, "own", system=EXAMPLE_SYSTEM, seen=30, size=300,
                           places=(OAKFIELD,), mode="onsite")
        await list_posting(session, found, "board", system=ats.JOB_SITE, seen=1, size=9000)

        assert await stated_place.recheck(session, OWNER, stated, cause="Before the writer") == stated_place.PASSED
        assert (await gate_facts.process(session, found)).state == gate_facts.CLEAR
        assert found.gate_facts["haiku"]["asks"] == []


async def test_the_role_page_lists_the_gate_the_re_gate_decides(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await listed_in_two_places(session, "Fabrikam", employer_in_area=False)
        role_id = role.id
        await session.commit()

    body = (await client.get(f"/api/v1/roles/{role_id}")).json()
    line = {g["id"]: g for g in body["gates"]}["location"]
    assert line["result"] == "fail" and RIVERTON in line["fact"]


# --- the pay -------------------------------------------------------------------------------------------------------

#: A posting that states two bands, one for each place (the most generous of them is what a gate may stop on).
TWO_BANDS = POSTING_WORDS.replace("Programs Lead\n", (
    "Programs Lead\nPay range: $80,000 - $90,000 in Riverton\n"
    "Pay range: $125,000 - $135,000 in Oakfield\n"), 1)
ONE_BAND = POSTING_WORDS.replace("Programs Lead\n", "Programs Lead\nPay range: $80,000 - $90,000 per year\n", 1)


async def test_the_pay_is_judged_on_the_band_of_the_employers_posting_not_the_most_generous_of_another_postings(js):
    """The role holds a board's text, which states two bands; the employer's own posting states the one band. The pay
    floor is $105,000: the employer's band fails it, the board's most generous band passes."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", TWO_BANDS)
        await list_posting(session, role, "own", system=EXAMPLE_SYSTEM, seen=30, text=ONE_BAND, pay=(80_000, 90_000))
        await list_posting(session, role, "board", system=ats.JOB_SITE, seen=1, text=TWO_BANDS, pay=(80_000, 90_000))

        outcome = await gate_facts.process(session, role)

        assert outcome.state == gate_facts.STOPPED and role.screened_out_gate_id == "compensation", (
            "the employer's posting states $80,000 to $90,000, under the floor")
        await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
        await session.refresh(role)
        assert role.status == stages.GATED_OUT, "the re-gate reads the same posting"


async def test_the_most_generous_band_still_stands_across_the_bands_of_the_one_posting_that_is_chosen(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", TWO_BANDS)
        await list_posting(session, role, "own", system=EXAMPLE_SYSTEM, seen=30, text=TWO_BANDS, pay=(80_000, 90_000))
        await list_posting(session, role, "board", system=ats.JOB_SITE, seen=1, text=ONE_BAND, pay=(80_000, 90_000))

        outcome = await gate_facts.process(session, role)

        assert outcome.state != gate_facts.STOPPED and role.status != stages.GATED_OUT


async def test_with_no_copy_the_pay_is_judged_on_the_text_the_step_read_as_before(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        rules = await gating.rules(session, OWNER)
        role = await make_role(session, "Fabrikam", TWO_BANDS)
        found = posting_facts.extract(TWO_BANDS, role.company)
        assert len(found.bands) == 2
        assert gate_facts.decide(role, rules, found) is None, "the most generous of the two bands passes"


async def test_a_chosen_posting_that_states_no_pay_in_its_text_leaves_the_pay_to_the_text_the_step_read(js):
    """A fact the chosen posting does not state is the role's (the text the step read), as for the place."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", TWO_BANDS)
        await list_posting(session, role, "own", system=EXAMPLE_SYSTEM, seen=30, text=POSTING_WORDS)
        await list_posting(session, role, "board", system=ats.JOB_SITE, seen=1, text=ONE_BAND, pay=(80_000, 90_000))

        outcome = await gate_facts.process(session, role)

        assert outcome.state != gate_facts.STOPPED and role.status != stages.GATED_OUT


# --- the rest of the readers: the deck's turn for a Ready role, a changed pay floor, a Haiku answer -------------------


async def test_the_decks_turn_judges_a_ready_role_on_the_place_of_the_employers_posting(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        out = await listed_in_two_places(session, "Fabrikam", employer_in_area=False, status=stages.READY_TO_LAUNCH,
                                         location="Oakfield", move_type="hybrid")
        kept = await listed_in_two_places(session, "Initech", employer_in_area=True, status=stages.READY_TO_LAUNCH,
                                          location="US, OH, Riverton", move_type="hybrid")

        assert await deck.gate(session, OWNER) == 1

        assert out.status == stages.GATED_OUT and out.screened_out_gate_id == "location"
        assert kept.status == stages.READY_TO_LAUNCH


async def test_a_changed_pay_floor_is_judged_again_on_the_pay_of_the_employers_posting(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates={**GATES, "compensation": {"amount": 50_000,
                                                                                         "against": "ask"}})
        role = await make_role(session, "Fabrikam", POSTING_WORDS)
        await list_posting(session, role, "own", system=EXAMPLE_SYSTEM, seen=30, size=300, pay=(80_000, 90_000))
        await list_posting(session, role, "board", system=ats.JOB_SITE, seen=1, size=9000, pay=(120_000, 140_000))
        assert (await gate_facts.process(session, role)).state != gate_facts.STOPPED, "$80,000 clears $50,000"

        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)  # the floor is now $105,000
        outcome = await gate_facts.process(session, role)

        assert outcome.state == gate_facts.STOPPED and role.screened_out_gate_id == "compensation"


async def test_a_haiku_answer_is_judged_on_the_place_of_the_employers_posting(js, monkeypatch):
    """The employer's posting names Riverton and no way of working, and the board's names Oakfield: the way of working
    is asked for. The answer, on site, fails Riverton and would not fail Oakfield."""
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    said = "Team members are expected on site every day."
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", f"{POSTING_WORDS}\n{said}", location="United States",
                               move_type=None)
        await list_posting(session, role, "own", system=EXAMPLE_SYSTEM, seen=30, size=300, places=(RIVERTON,))
        await list_posting(session, role, "board", system=ats.JOB_SITE, seen=1, size=9000, places=(OAKFIELD,))
        assert (await gate_facts.process(session, role)).state == gate_facts.WAITS
        waiting = await gate_facts.waiting(session, OWNER, skip_leased=True)
        asked = [await gate_facts.item(session, r) for r in waiting]
        gate_facts.hand_out(waiting, "titles-1")
        await session.flush()
        assert [i["id"] for i in asked] == [str(role.id)[:8]]

        taken = await gate_facts.take_answers(session, OWNER, [{
            "id": str(role.id)[:8], "kind": "facts", "facts": {"work_mode": {"value": "on-site", "quote": said}}}])

        assert taken["problems"] == [] and taken["stopped"] == 1
        assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "location"


# --- a job board page's chip is a tag, as a job system's is -----------------------------------------------------------


