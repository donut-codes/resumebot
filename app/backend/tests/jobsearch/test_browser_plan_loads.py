"""The browser queue's plan loads only what it reads.

A plan is made at most every thirty seconds and read about 5,600 whole roles and every item the queue has ever made
(23,000, most of them done or dropped) to build a dict from a dozen columns of each role and to compare seven facts of
each item. The roles' descriptions, analyses and gate facts, and the items' leases, tries and notes, were decoded for
nothing. The roles are loaded with the columns the lists read (`reader.PLAN_ROLE_COLUMNS`), the items with the columns
the plan compares, and the postings' dates the age of a role needs are read once for the lists of a plan
(`chase.HeldOnce`) and not once for each. Every name here is made up, and no test reaches the network."""

import asyncio
import contextlib
import re

import pytest
from sqlalchemy import event, inspect, select

from app import db
from app.jobsearch.browser import go_checks
from app.jobsearch.browser import queue as bq
from app.jobsearch.browser import reader as br
from app.jobsearch.fit import freshness
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import chase
from app.jobsearch.postings import sourced as sourcing
from app.timeutil import owner_today
from tests.builders import browser_world
from tests.jobsearch.test_browser_work import (
    FRESH_PASS,
    NOW,
    _hard_pass,
    _posting_of,
    _role,
    _sourced,
)
from tests.support import DEFAULT_OWNER as OWNER

#: What a role's row holds that no list of the plan reads, and what an item's row holds that no plan compares.
ROLE_BULK = ("description", "analysis", "gate_facts")
ITEM_BULK = ("last_note", "leased_by", "lease_until", "run_id", "tries", "due_by", "last_result", "code_at", "next_at",
             "created_at")


@contextlib.contextmanager
def statements():
    seen: list[str] = []

    def sent(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        seen.append(" ".join(statement.split()))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", sent)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", sent)


def _listed(statement: str) -> str:
    """The columns a select names (before its first FROM: a condition may name any column)."""
    return statement.split(" FROM ", 1)[0]


async def _world() -> None:
    """A role of each kind the plan lists: to source (on deck, starred, in Followup needed), to read at its source, to
    read in full, one the owner applied to with no text (`unbuilt`), and one with no link."""
    await browser_world()
    await _hard_pass(FRESH_PASS, read_robots_exempt=True)  # the lists for the job board's pages are asked too
    await _role(1)
    await _role(2, status="followup_needed", posted=None, found=NOW.date())
    await _role(3, status="followup_needed", favorited_at=NOW)
    await _sourced(4)
    await _role(5, status="applied", description=None)
    await _role(6, status="cleansed_data", url="https://www.listings.example.test/jobs/view/1006", description="Short.")
    await _role(7, status="followup_needed", url=None, board_url=None)


async def test_the_plan_reads_no_role_description_analysis_or_gate_facts_and_no_item_lease_or_note(js, monkeypatch):
    """`source_held` (it sources a role in code: it reads whole roles to write them, and is another lane's) and Go's
    check of a posting (a few roles) are left out of the plan here: the rest of it is this lane's."""

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(sourcing, "source_held", nothing)
    monkeypatch.setattr(go_checks, "browser_checks", nothing)
    await _world()
    with statements() as seen:
        async with db.session_factory()() as session:
            plan = await bq._plan(session, OWNER, NOW)
            await session.rollback()
    assert plan is not None and plan.adds, "the plan made its items"
    roles = [q for q in seen if q.startswith("SELECT js_opportunities.")]
    assert len(roles) >= 5, "the lists were read"
    for query in roles:
        for column in ROLE_BULK:
            assert not re.search(rf"js_opportunities\.{column}\b", _listed(query)), f"{column} is loaded: {query[:200]}"
    items = [q for q in seen if q.startswith("SELECT js_browser_items.") and _listed(q).count("js_browser_items.") > 3]
    assert items, "the items were read"
    for query in items:
        for column in ITEM_BULK:
            assert f"js_browser_items.{column}" not in _listed(query), f"{column} is loaded: {query[:200]}"


async def test_the_lists_the_plan_makes_are_what_the_whole_roles_gave(js):
    """The same lists from roles loaded whole: the narrow load changes nothing a plan says."""
    await _world()
    async with db.session_factory()() as session:
        sites = await br.browser_sites(session, OWNER)
        narrow = [await br.board_roles(session, OWNER, sites), await br.listing_roles(session, OWNER),
                  await br.to_read_in_full(session, OWNER, sites, -1),
                  await br.to_read_at_source(session, OWNER, -1)]
        session.expunge_all()
        every = tuple(getattr(Opportunity, c.key) for c in inspect(Opportunity).column_attrs)
        with pytest.MonkeyPatch.context() as patch:  # every column, as before
            patch.setattr(br, "PLAN_ROLE_COLUMNS", every)
            whole = [await br.board_roles(session, OWNER, sites), await br.listing_roles(session, OWNER),
                     await br.to_read_in_full(session, OWNER, sites, -1),
                     await br.to_read_at_source(session, OWNER, -1)]
    assert narrow == whole and any(narrow)


# --- the postings' dates, once for a plan -----------------------------------------------------------------------------


async def _followup_roles(n: int) -> list[Opportunity]:
    await browser_world()
    await _hard_pass(FRESH_PASS)
    for i in range(1, n + 1):
        role = await _role(i, status="followup_needed", posted=None, found=owner_today())
        await _posting_of(role, i, first_seen_days_ago=i, posted=None)
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER)
                                           .order_by(Opportunity.slug))).all())


def _counting_reads(monkeypatch) -> list[object]:
    reads: list[object] = []
    real = freshness.held_postings

    async def counted(session, owner, ids=None, boards=None):
        reads.append(None if ids is None else sorted(ids))
        return await real(session, owner, ids, boards)

    monkeypatch.setattr(freshness, "held_postings", counted)
    return reads


async def test_lists_of_a_great_many_roles_share_one_read_of_the_owners_postings(js, monkeypatch):
    roles = await _followup_roles(3)
    monkeypatch.setattr(chase, "WHOLE_AT", 1)  # a list of more than one role is most of the owner's roles
    reads = _counting_reads(monkeypatch)
    async with db.session_factory()() as session:
        held = chase.HeldOnce()
        first = await chase.wanted(session, OWNER, Opportunity.slug.in_(["role-1", "role-2"]), held=held)
        second = await chase.wanted(session, OWNER, Opportunity.slug.in_(["role-2", "role-3"]), held=held)
        alone = await chase.wanted(session, OWNER, Opportunity.slug.in_(["role-2", "role-3"]))
    assert len(reads) == 2 and reads[0] is None and len(reads[1]) == 2, (
        "the two lists that share the read read the owner's postings once; the one that does not reads its two roles")
    assert {r.id for r in second} == {r.id for r in alone} and len(first) == 2 and len(roles) == 3


async def test_short_lists_read_the_postings_of_their_roles_by_id_and_each_role_only_once(js, monkeypatch):
    roles = await _followup_roles(3)
    reads = _counting_reads(monkeypatch)
    ids = {r.slug: r.id for r in roles}
    async with db.session_factory()() as session:
        held = chase.HeldOnce()
        await chase.wanted(session, OWNER, Opportunity.slug.in_(["role-1", "role-2"]), held=held)
        await chase.wanted(session, OWNER, Opportunity.slug.in_(["role-2", "role-3"]), held=held)
        await chase.wanted(session, OWNER, Opportunity.slug.in_(["role-1", "role-3"]), held=held)
    assert reads == [sorted([ids["role-1"], ids["role-2"]]), [ids["role-3"]]]


async def test_a_list_with_many_roles_is_read_by_their_ids_a_statement_at_a_time(js, monkeypatch):
    roles = await _followup_roles(3)
    reads = _counting_reads(monkeypatch)
    monkeypatch.setattr(chase, "IDS_AT_ONCE", 2)
    async with db.session_factory()() as session:
        await chase.wanted(session, OWNER, held=chase.HeldOnce())
    assert [len(r) for r in reads] == [2, 1] and sorted(i for r in reads for i in r) == sorted(r.id for r in roles)


async def test_a_shared_read_gives_each_role_the_posted_day_it_would_get_alone(js, monkeypatch):
    roles = await _followup_roles(3)
    async with db.session_factory()() as session:
        alone = await freshness.posted_by_role(session, OWNER, roles)
        for whole_at, at_once in ((1, 5), (chase.WHOLE_AT, 5), (chase.WHOLE_AT, 2)):
            monkeypatch.setattr(chase, "WHOLE_AT", whole_at)
            monkeypatch.setattr(chase, "IDS_AT_ONCE", at_once)
            shared = await chase.HeldOnce().posted_by_role(session, OWNER, roles)
            assert shared == alone, f"with more than {whole_at} roles read whole, {at_once} ids to a statement"


async def test_the_loop_is_given_back_every_few_rows_while_a_plan_is_made(js, monkeypatch):
    """A plan works through thousands of roles and items; it is cut into stretches of a few milliseconds."""
    real_sleep, gave = asyncio.sleep, []

    async def sleep(delay, *args, **kwargs):
        if delay == 0:
            gave.append(delay)
        return await real_sleep(delay, *args, **kwargs)

    await _world()
    monkeypatch.setattr(chase, "YIELD_EVERY", 2)
    monkeypatch.setattr(bq, "YIELD_EVERY", 2)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    async with db.session_factory()() as session:
        plan = await bq._plan(session, OWNER, NOW)
        await session.rollback()
    assert plan is not None and len(gave) >= 10, len(gave)


# --- the roles and postings a list reads are built a few at a time ----------------------------------------------------


async def _most_built_in_one_stretch(work, *models) -> int:
    """The most instances of these models built with no other task given a turn between them while `work` runs: a ticker
    takes a turn each time the loop is given back (to the database or by `sleep(0)`), and what was built since its last
    turn is one stretch. Counting the turns would not tell: a read that builds every row on its first fetch and then
    gives the loop back as often as it likes is one long stretch."""
    built, most, done = 0, 0, False

    def made(*_args) -> None:
        nonlocal built
        built += 1

    async def tick() -> None:
        nonlocal built, most
        while not done:
            most, built = max(most, built), 0
            await asyncio.sleep(0)

    for model in models:
        event.listen(model, "load", made)
    ticker = asyncio.ensure_future(tick())
    try:
        await work()
    finally:
        done = True
        await ticker
        for model in models:
            event.remove(model, "load", made)
    return max(most, built)


async def test_the_roles_note_no_link_reads_are_built_a_few_at_a_time_with_the_loop_given_back_between(js, monkeypatch):
    """Old: the roles were read with `session.scalars`, which builds every instance on its first fetch (a few thousand
    roles in one stretch of 35 to 43 ms) and only then cut them into partitions. New: they are streamed, `YIELD_EVERY`
    at a time (a load in partitions of at most 250 rows). The notes are what they were."""
    for i in range(1, 9):
        await _role(i, status="followup_needed", url=None, board_url=None)
    await _role(9, status="followup_needed", browser_note=br.NO_LINK)  # it has a link again: it loses the note
    monkeypatch.setattr(chase, "YIELD_EVERY", 3)

    async def note() -> None:
        async with db.session_factory()() as session:
            await br.note_no_link(session, OWNER)
            await session.commit()

    most = await _most_built_in_one_stretch(note, Opportunity)
    assert 0 < most <= 3, f"{most} roles were built with the loop not given back between them"
    async with db.session_factory()() as session:
        notes = {r.slug: r.browser_note for r in (await session.scalars(select(Opportunity).where(
            Opportunity.owner == OWNER))).all()}
    assert notes == {**{f"role-{i}": br.NO_LINK for i in range(1, 9)}, "role-9": None}


