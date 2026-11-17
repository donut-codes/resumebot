"""follow_roles reads a few columns, looks hosts up in sets and folds each title once. Every name and address
here is made up; nothing reaches the network."""

import random
import re

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import ats
from app.jobsearch.address import is_board_link
from app.jobsearch.employers import follow as ep
from app.jobsearch.models import Opportunity
from app.sites import avoid
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.jobsearch.test_board_loads import statements
from tests.jobsearch.test_employer_posting import BOARD, OWN, _employer, _listing, _role
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("example_world")


def _old_is_board_link(url, hosts=frozenset()):
    host = avoid.host_of(url)
    return (bool(host) and any(avoid.covers(b, host) for b in (*ats.BOARDS, *hosts))
            and not ats.own_careers_posting(url))


def test_is_board_link_is_what_it_was_over_a_thousand_five_hundred_hosts():
    rng = random.Random(7)
    owned = frozenset({"jobs.sitebox.example.test", "assoc.example.test", "a.b.c.example.test", "example.org"})
    names = [*ats.BOARDS, *owned]
    hosts: list[str] = ["notlistings.test", "listings.test.example.test", "xboard.test", "board.test.", "",
                        "example.org", "www.example.org", "deep.sub.example.org", "c.example.test", "b.c.example.test"]
    for _ in range(1490):
        base = rng.choice(names + ["plain.example.test", "other.test"])
        kind = rng.randrange(5)
        hosts.append({0: base, 1: f"sub{rng.randrange(9)}.{base}", 2: f"not{base}",
                      3: f"{base}.x{rng.randrange(9)}.test", 4: f"a.b.{base}"}[kind])
    assert len(hosts) >= 1500
    for host in hosts:
        for url in (f"https://{host}/jobs/view/1", f"{host}/x", f"https://www.{host}/"):
            assert is_board_link(url, owned) == _old_is_board_link(url, owned), url
            assert is_board_link(url) == _old_is_board_link(url), url
    assert is_board_link(None) is False


def test_a_title_is_folded_once_however_often_it_is_asked(monkeypatch):
    ep.title_tokens.cache_clear()
    calls = []
    real = ep._words

    def counting(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(ep, "_words", counting)
    titles = ["Sr. Program Mgr (Remote)", "Analyst II", "Director of Programs"]
    first = [(ep.title_tokens(t), ep.title_tokens(t, loose=True)) for t in titles]
    made = len(calls)
    assert made
    for _ in range(5):
        assert [(ep.title_tokens(t), ep.title_tokens(t, loose=True)) for t in titles] == first
    assert len(calls) == made, "asked again, a title is not folded again"
    assert first[0] == (("senior", "program", "manager", "remote"), ("senior", "program", "manager"))
    assert first[1][0] == ("analyst", "2")
    assert all(isinstance(a, tuple) and isinstance(b, tuple) for a, b in first)


async def _board_only_roles(n):
    """n roles on a board, none with an employer id (found by the company's name), each with an own listing."""
    async with db.session_factory()() as session:
        for i in range(n):
            employer = await _employer(session, name=f"Fabrikam {i:02d}", board={"slug": f"fabrikam{i}"},
                                       board_url=f"https://job-boards.feedsys.test/fabrikam{i}")
            await _listing(session, employer, title=f"Program Manager {i}", url=f"{OWN}{i}", ext_id=f"x{i}")
            await _role(session, employer, title=f"Program Manager {i}", url=f"{BOARD}{i}", board_url=f"{BOARD}{i}",
                        employer_id=None, slug=f"pm-{i}", match_key=f"k{i}")
        await session.commit()


def _followed(n):
    return [(f"pm-{i}", f"{OWN}{i}", f"{BOARD}{i}") for i in sorted(range(n), key=lambda i: f"pm-{i}")]


async def _state():
    async with db.session_factory()() as session:
        roles = (await session.scalars(select(Opportunity).order_by(Opportunity.slug))).all()
        return [(r.slug, r.url, r.board_url) for r in roles]


async def _trail_count():
    from app.models.work import TrailEvent
    async with db.session_factory()() as session:
        return len((await session.scalars(
            select(TrailEvent).where(TrailEvent.event == "employer_posting_found"))).all())


def _employer_statements(seen):
    return [q for q in seen if re.search(r"FROM js_employers", q)]


async def test_outside_a_savepoint_the_roles_are_followed_with_no_employer_statement_per_role(js, monkeypatch):
    n = 45
    await _board_only_roles(n)
    released = []
    real = db.release_if_free

    async def release(session):
        released.append(await real(session))
        return released[-1]

    monkeypatch.setattr(ep.db, "release_if_free", release)
    async with db.session_factory()() as session:
        with statements() as seen:
            result = await ep.follow_roles(session, OWNER)
        await session.commit()
    assert result == {"looked": n, "followed": n}
    assert await _state() == _followed(n)
    assert await _trail_count() == n
    assert released == [True, True], "the turn is let go after every twenty follows"
    assert len(_employer_statements(seen)) <= 3 + 2 * 3, "a few for the lot, one pair per twenty roles"


async def test_inside_a_savepoint_the_reads_fall_back_to_the_session_and_the_result_is_the_same(js):
    n = 25
    await _board_only_roles(n)
    async with db.session_factory()() as session:
        async with session.begin_nested():
            result = await ep.follow_roles(session, OWNER)
        await session.commit()
    assert result == {"looked": n, "followed": n}
    assert await _state() == _followed(n)
    assert await _trail_count() == n


async def test_a_role_changed_since_the_read_is_left_for_the_next_night(js, monkeypatch):
    await _board_only_roles(3)
    real = ep._own_listings_of

    async def listings(reader, owner, ids, hosts):
        out = await real(reader, owner, ids, hosts)
        async with db.session_factory()() as other:  # a writer moves one role's link after the read
            role = await other.scalar(select(Opportunity).where(Opportunity.slug == "pm-1"))
            role.url = role.board_url = "https://www.board.test/jobs/view/changed"
            await other.commit()
        return out

    monkeypatch.setattr(ep, "_own_listings_of", listings)
    async with db.session_factory()() as session:
        result = await ep.follow_roles(session, OWNER)
        await session.commit()
    assert result == {"looked": 3, "followed": 2}
    state = {slug: url for slug, url, _ in await _state()}
    assert state["pm-1"] == "https://www.board.test/jobs/view/changed"
    assert state["pm-0"] == f"{OWN}0" and state["pm-2"] == f"{OWN}2"
