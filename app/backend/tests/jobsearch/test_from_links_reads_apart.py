"""`resolve.from_links` reads the postings its probe-guessed employers held in a reader's session (no writer's turn), in
slices, and gives the event loop back by the clock while it parses (docs/PRD.md path 28). Synthetic data only."""

import asyncio
import time

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.employers import resolve
from app.jobsearch.models import Employer
from app.jobsearch.postings.coop import EVERY
from app.timeutil import utc_now
from tests.employer_builders import make_employer, make_posting, make_role
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("example_world")

TENANT_LINK = "https://{}.wd5.tenantsys.test/Careers/job/x"


async def _guessed(name: str, system: str, *, postings: int = 0, link: str | None = None, read: bool = True) -> None:
    async with db.session_factory()() as session:
        e = await make_employer(session, name)
        resolve._set(e, system, {"slug": name.lower()}, "probe")
        if read:
            e.fetched_at, e.fetch_ok, e.open_roles = utc_now(), True, postings
        session.add_all([make_posting(e, f"p{n}", ats=system) for n in range(postings)])
        if link:
            role = make_role(e, "Lead")
            role.url = link
            session.add(role)
        await session.commit()


async def _state() -> dict:
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(Employer).order_by(Employer.name))).all()
        return {e.name: (e.ats, e.ats_found_by, e.board) for e in rows}


async def _from_links() -> dict:
    async with db.session_factory()() as session:
        result = await resolve.from_links(session, OWNER)
        await session.commit()
    return result


async def test_probe_guesses_are_kept_or_replaced_as_before(js):
    await _guessed("Aa Empty", "applysys", link=TENANT_LINK.format("aa"), read=False)  # never held a posting: gives way
    await _guessed("Bb Held", "applysys", postings=2, link=TENANT_LINK.format("bb"))  # held a posting: stands
    await _guessed("Cc Real", "slugsys", link=TENANT_LINK.format("cc"), read=False)  # no empty accounts: stands
    got = await _from_links()
    assert (got["found"], got["cleared"]) == (1, 0)
    state = await _state()
    assert state["Aa Empty"][:2] == ("tenantsys", "link") and state["Aa Empty"][2]["tenant"] == "aa"
    assert state["Bb Held"] == ("applysys", "probe", {"slug": "bb held"})
    assert state["Cc Real"] == ("slugsys", "probe", {"slug": "cc real"})


async def test_a_late_probe_guess_that_stands_is_kept(js, monkeypatch):
    async with db.session_factory()() as session:
        e = await make_employer(session, "Late Guess")
        resolve._set(e, "slugsys", {"slug": "lateguess"}, "link")  # not a guess when the reader looks
        role = make_role(e, "Lead")
        role.url = TENANT_LINK.format("late")
        session.add(role)
        await session.commit()
    real = resolve._held_by_guesses

    async def looked(session, owner):
        held = await real(session, owner)
        async with db.session_factory()() as writer:  # a writer changes the row in the gap
            late = await writer.scalar(select(Employer).where(Employer.name == "Late Guess"))
            late.ats_found_by = "probe"
            await writer.commit()
        return held

    monkeypatch.setattr(resolve, "_held_by_guesses", looked)
    got = await _from_links()
    assert got["found"] == 0
    assert (await _state())["Late Guess"] == ("slugsys", "probe", {"slug": "lateguess"}), "its guess stands: kept"


async def test_the_held_postings_join_runs_in_no_writer_turn(js, transactions):
    await _guessed("Aa Empty", "applysys", link=TENANT_LINK.format("aa"), read=False)
    transactions.clear()
    await _from_links()
    joins = [t for t in transactions.ended if any("js_postings" in s and "JOIN js_employers" in s.replace("\n", " ")
                                                  for s in t)]
    assert joins, "the join ran"
    for turn in joins:
        assert not [s for s in turn if s.lstrip().upper().startswith("SELECT") and "FROM js_employers" in s
                    and "js_postings" not in s], "the turn that held the join also loaded the employers"


async def test_from_links_over_many_links_keeps_the_loop_going(js, monkeypatch):
    async with db.session_factory()() as session:
        for n in range(200):
            e = await make_employer(session, f"Company {n:03d}")
            # each already holds the board its links name, so the pass changes no row (the write is not under test)
            resolve._set(e, "slugsys", {"slug": f"acme{n}"}, "link")
            for i, link in enumerate(["https://jobs.slugsys.test/acme{n}/{i}", "https://jobs.slugsys.test/acme{n}/{i}x",
                                      "https://job-boards.feedsys.test/acme{n}/jobs/{i}"]):
                role = make_role(e, f"Role {i}", url=link.format(n=n, i=i))
                role.slug = f"{role.slug}-{n}-{i}"
                session.add(role)
        await session.commit()
    real = resolve._board_of

    parsed: list[tuple[float, int]] = []  # when each link was parsed, and the turns the loop had had by then

    def slow(link, known=None):
        if known is None or link not in known:
            parsed.append((time.perf_counter(), ticks))
            end = time.perf_counter() + 0.001  # a parse that costs a millisecond
            while time.perf_counter() < end:
                pass
        return real(link, known)

    monkeypatch.setattr(resolve, "_board_of", slow)
    ticks, stop = 0, False

    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await _from_links()
    stop = True
    await task
    # The stretch in which the links are parsed (the reads before it and the write after are other work). The pass
    # gives the loop back by the clock (every EVERY seconds) and every 250 links; the ticker gets one turn per
    # give-back. Counting turns, not timing a gap, holds on any timer. A pass that never gave the loop back leaves 0
    # turns; the bound allows ten times EVERY between give-backs.
    assert len(parsed) >= 100
    (first, first_ticks), (last, last_ticks) = parsed[0], parsed[-1]
    turns = last_ticks - first_ticks
    assert turns >= 1, "the loop got no turn while the links were parsed"
    assert turns >= int((last - first) / (10 * EVERY))
